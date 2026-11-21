"""主码迁移核对网页端到端测试：上传 -> 冲突暂停 -> 更正 -> 恢复下载。"""

import io

import pytest

from app.main import app

RULES_TEXT = "\n".join(
    [
        "A | 毛重 >= 1000",
        "B | 毛重 >= 900",
    ]
)
GOOD_RULES = "\n".join(
    [
        "A | 毛重 >= 1000",
        "B | 托盘重 >= 100",
        "C | 申报净重 >= 100",
        "D | 毛重 - 托盘重 - 申报净重 > 500",
    ]
)
HISTORY_HEADER = "记录号,毛重,托盘重,申报净重,主码\n"
PENDING_HEADER = "记录号,毛重,托盘重,申报净重\n"


@pytest.fixture()
def client():
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def _post(client, rules_text, history, pending):
    return client.post(
        "/migration",
        data={
            "rules_text": rules_text,
            "history_file": (io.BytesIO(history.encode()), "history.csv"),
            "pending_file": (io.BytesIO(pending.encode()), "pending.csv"),
        },
        content_type="multipart/form-data",
    )


def _job_id(resp):
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def test_index_renders(client):
    resp = client.get("/migration")
    assert resp.status_code == 200
    assert "主码迁移核对".encode() in resp.data
    assert 'name="history_file"'.encode() in resp.data
    # 默认不预填演示数据，避免误把示例当成真实批次提交
    assert "H001,1000,0,0,A".encode() not in resp.data
    assert "填入一套演示数据".encode() in resp.data


def test_demo_link_prefills_sample_data(client):
    resp = client.get("/migration?demo=1")
    body = resp.data.decode()
    assert "H001,1000,0,0,A" in body
    assert "P001,1500,0,0" in body
    assert "A | 毛重 &gt;= 1000" in body  # Jinja 自动转义


def test_good_batch_downloads_only_pass_and_determined(client):
    history = HISTORY_HEADER + (
        "H1,1000,0,0,A\nH2,1000,200,100,A\nH3,500,0,0,\n"
    )
    pending = PENDING_HEADER + (
        "P1,1000,0,0\nP2,500,0,0\nP3,500,200,200\n"
    )
    resp = _post(client, GOOD_RULES, history, pending)
    assert resp.status_code == 302
    job_id = _job_id(resp)

    page = client.get(f"/migration/{job_id}")
    body = page.data.decode()
    assert "未发现冲突" in body
    assert "主码确定" in body
    assert "待核" in body
    assert f"/migration/{job_id}/download" in body

    csv_resp = client.get(f"/migration/{job_id}/download")
    assert csv_resp.status_code == 200
    assert csv_resp.mimetype == "text/csv"
    text = csv_resp.data
    assert text.startswith(b"\xef\xbb\xbf")  # BOM
    decoded = text.decode("utf-8-sig")
    assert "P1" in decoded and "P2" in decoded
    assert "P3" not in decoded  # 待核不进下载
    assert "record_no,gross_g,tare_g,declared_net_g,primary_code,status" in decoded


def test_cycle_pauses_download_and_correction_restores_it(client):
    history = HISTORY_HEADER + "C1,1000,0,0,A\nC2,1200,0,0,B\n"
    pending = PENDING_HEADER + "P1,1500,0,0\nP2,500,0,0\n"
    job_id = _job_id(_post(client, RULES_TEXT, history, pending))

    page = client.get(f"/migration/{job_id}")
    body = page.data.decode()
    assert "先后关系成环" in body
    assert "C1" in body and "C2" in body
    assert "下载已暂停" in body
    assert f"/migration/{job_id}/download" not in body

    blocked = client.get(f"/migration/{job_id}/download")
    assert blocked.status_code == 409
    assert "暂停".encode() in blocked.data

    # 更正 C2 的主码为 A，环解除，下载恢复
    corrected = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "C2", "primary_code": "A"},
        follow_redirects=True,
    )
    assert corrected.status_code == 200
    body = corrected.data.decode()
    assert "未发现冲突" in body
    assert "下载迁移结果 CSV" in body
    assert "更正记录" in body
    assert "C2" in body

    csv_resp = client.get(f"/migration/{job_id}/download")
    assert csv_resp.status_code == 200
    decoded = csv_resp.data.decode("utf-8-sig")
    assert "P1" in decoded and "P2" in decoded


def test_correction_introducing_conflict_keeps_reason_and_blocks_original_download(client):
    history = HISTORY_HEADER + "H1,1000,0,0,A\nH2,500,0,0,\n"
    pending = PENDING_HEADER + "P1,1000,0,0\nP2,500,0,0\n"
    job_id = _job_id(_post(client, GOOD_RULES, history, pending))
    assert client.get(f"/migration/{job_id}/download").status_code == 200

    # 把“通过”的 H2 改成一个实际未命中的主码 B -> 引入冲突
    resp = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "H2", "primary_code": "B"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "历史主码未命中" in body
    assert "引入冲突" in body
    assert "下载已暂停" in body

    blocked = client.get(f"/migration/{job_id}/download")
    assert blocked.status_code == 409

    # 再改回空码，冲突排除，原地址恢复下载
    restored = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "H2", "primary_code": ""},
        follow_redirects=True,
    )
    assert "未发现冲突" in restored.data.decode()
    assert client.get(f"/migration/{job_id}/download").status_code == 200


def test_correction_can_introduce_and_then_remove_cycle(client):
    rules = "A | 毛重 >= 1000\nB | 毛重 >= 900\nC | 托盘重 >= 100\n"
    # 初始：A 先于 B/C；B、C 只在各自命中时出现，未形成环
    history = HISTORY_HEADER + "C1,1200,0,0,A\nC2,1000,200,0,A\n"
    pending = PENDING_HEADER + "P1,1500,0,0\nP2,500,0,0\n"
    job_id = _job_id(_post(client, rules, history, pending))
    assert client.get(f"/migration/{job_id}/download").status_code == 200

    # 把 C2 的主码改成 B：新增 B 先于 A，与已有的 A 先于 B 成环
    resp = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "C2", "primary_code": "B"},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert "先后关系成环" in body
    assert "引入冲突" in body
    assert "C1" in body and "C2" in body
    assert "下载已暂停" in body
    assert client.get(f"/migration/{job_id}/download").status_code == 409

    # 改回 A，环解除
    resp = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "C2", "primary_code": "A"},
        follow_redirects=True,
    )
    assert "未发现冲突" in resp.data.decode()
    download = client.get(f"/migration/{job_id}/download")
    assert download.status_code == 200
    assert "P1" in download.data.decode("utf-8-sig")


def test_correction_with_unknown_rule_id_is_recorded_as_conflict(client):
    history = HISTORY_HEADER + "H1,1000,0,0,A\nH2,500,0,0,\n"
    pending = PENDING_HEADER + "P1,1000,0,0\nP2,500,0,0\n"
    job_id = _job_id(_post(client, GOOD_RULES, history, pending))
    assert client.get(f"/migration/{job_id}/download").status_code == 200

    resp = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "H1", "primary_code": "ZZZ"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    # 未知主码不会静默吞掉：保留更正、记为历史主码未命中、暂停下载
    assert "ZZZ" in body
    assert "不是已录入的规则编号" in body
    assert "引入冲突" in body
    assert "下载已暂停" in body
    assert client.get(f"/migration/{job_id}/download").status_code == 409

    # 改回原值即撤销更正，下载恢复
    restored = client.post(
        f"/migration/{job_id}/correct",
        data={"record_no": "H1", "primary_code": "A"},
        follow_redirects=True,
    )
    assert "未发现冲突" in restored.data.decode()
    assert client.get(f"/migration/{job_id}/download").status_code == 200


def test_can_submit_pasted_csv_text_without_files(client):
    history = HISTORY_HEADER + "H1,1000,0,0,A\nH2,500,0,0,\n"
    pending = PENDING_HEADER + "P1,1000,0,0\nP2,500,0,0\n"
    resp = client.post(
        "/migration",
        data={
            "rules_text": GOOD_RULES,
            "history_text": history,
            "pending_text": pending,
        },
    )
    assert resp.status_code == 302
    job_id = _job_id(resp)
    page = client.get(f"/migration/{job_id}")
    body = page.data.decode()
    assert "未发现冲突" in body
    assert "主码确定" in body
    assert "通过" in body


def test_missing_file_reports_error(client):
    resp = client.post(
        "/migration",
        data={"rules_text": GOOD_RULES},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "历史称重 CSV".encode() in resp.data


def test_history_missing_primary_column_reports_error(client):
    resp = _post(
        client,
        GOOD_RULES,
        PENDING_HEADER + "H1,1000,0,0\n",
        PENDING_HEADER + "P1,1000,0,0\n",
    )
    assert resp.status_code == 400
    assert "主码".encode() in resp.data


def test_bad_rules_reported(client):
    resp = _post(
        client,
        "A | 毛重 + 托盘重",
        HISTORY_HEADER + "H1,1000,0,0,A\n",
        PENDING_HEADER + "P1,1000,0,0\n",
    )
    assert resp.status_code == 400
    assert "比较符".encode() in resp.data


def test_gbk_encoded_upload_accepted(client):
    history = (HISTORY_HEADER + "H1,1000,0,0,A\n").encode("gbk")
    pending = (PENDING_HEADER + "P1,1000,0,0\n").encode("gbk")
    resp = client.post(
        "/migration",
        data={
            "rules_text": GOOD_RULES,
            "history_file": (io.BytesIO(history), "h.csv"),
            "pending_file": (io.BytesIO(pending), "p.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
