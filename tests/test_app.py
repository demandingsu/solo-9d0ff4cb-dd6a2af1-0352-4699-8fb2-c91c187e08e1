"""Flask 端到端测试：录入 -> 试算 -> 下载 -> 回放全链路。"""

import io

import pytest

from app.main import app


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _bounds(prefix_low="0", prefix_high="1000000"):
    return {
        "gross_low": "0",
        "gross_high": "20",
        "tare_low": "0",
        "tare_high": "20",
        "declared_low": "0",
        "declared_high": "20",
    }


def test_index_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "拒收规则".encode() in resp.data


def test_full_flow_solo_joint_never(client):
    rules_text = "\n".join(
        [
            "R1 | 毛重 >= 10",                 # 被 R2/R3 联合覆盖 -> joint
            "R2 | 毛重 >= 15",                 # 必连带 R1 -> joint
            "R3 | 毛重 <= 14",                 # 可 solo
            "R4 | 毛重 > 20",                  # 超出范围 -> never
        ]
    )
    resp = client.post(
        "/solve",
        data={"rules_text": rules_text, **_bounds()},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "只能连带触发" in body
    assert "范围内从不触发" in body
    assert "可单独触发" in body

    # 任务页里能拿到下载链接
    assert "/csv" in body

    # 从重定向地址取任务号
    job_id = resp.request.path.rstrip("/").split("/")[-1]
    csv_resp = client.get(f"/jobs/{job_id}/csv")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    csv_bytes = csv_resp.data
    assert csv_bytes.startswith(b"\xef\xbb\xbf")  # BOM
    assert b"gross_g" in csv_bytes

    # 回放下载的 CSV：预期与实际完全一致
    replay_resp = client.post(
        "/replay",
        data={
            "rules_text": rules_text,
            "csv_file": (io.BytesIO(csv_bytes), "weighing.csv"),
        },
        content_type="multipart/form-data",
    )
    assert replay_resp.status_code == 200
    replay_body = replay_resp.data.decode()
    assert "✗ 不一致" not in replay_body
    assert "R1" in replay_body and "R2" in replay_body and "R3" in replay_body
    # never 规则没有行
    assert "R4" not in replay_body.split("回放结果")[1]


def test_solve_rejects_bad_rules(client):
    resp = client.post(
        "/solve",
        data={"rules_text": "R1 | 毛重 + 托盘重", **_bounds()},
    )
    assert resp.status_code == 400
    assert "比较符".encode() in resp.data


def test_solve_rejects_garbled_rule(client):
    resp = client.post(
        "/solve",
        data={"rules_text": "R1 | 毛重 >> 0", **_bounds()},
    )
    assert resp.status_code == 400


def test_solve_rejects_bad_bounds(client):
    data = _bounds()
    data["gross_high"] = "5"
    data["gross_low"] = "10"
    resp = client.post("/solve", data={"rules_text": "R1 | 毛重 > 0", **data})
    assert resp.status_code == 400
    assert "下限不能大于上限".encode() in resp.data


def test_replay_missing_file(client):
    resp = client.post(
        "/replay",
        data={"rules_text": "R1 | 毛重 > 0"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "CSV".encode() in resp.data


def test_expired_job_404(client):
    resp = client.get("/jobs/nonexistent123")
    assert resp.status_code == 404


def test_replay_prefill_rules_from_job(client):
    rules_text = "R1 | 毛重 > 0\n"
    resp = client.post("/solve", data={"rules_text": rules_text, **_bounds()})
    # /solve 重定向到 /jobs/<job_id>，从重定向地址取任务号
    job_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    page = client.get(f"/replay?job_id={job_id}")
    assert page.status_code == 200
    # Jinja 自动转义：> 渲染为 &gt;
    assert "毛重 &gt; 0".encode() in page.data
