"""通过真实 HTTP 演示主码迁移核对页：上传、冲突暂停、更正、恢复下载。

先启动服务（Docker Compose 或本地 gunicorn），再运行：
    python scripts/demo_web_migration.py [http://127.0.0.1:8000]

演示内容：
  A. 正常批：主码确定 / 通过 / 待核 三种判定，下载排除待核行；
  B. 成环批：指出造成冲突的历史记录，下载返回 409；
  C. 更正成未命中的主码：保留更正与原因，下载仍暂停；
  D. 更正为相容主码：冲突排除，原下载地址恢复 200。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")

RULES = """\
A | 毛重 >= 1000
B | 毛重 >= 900
C | 托盘重 >= 100
D | 申报净重 >= 100
"""

HISTORY_OK = """\
记录号,毛重,托盘重,申报净重,主码
H001,1000,0,0,A
H002,1200,0,0,A
H003,500,0,0,
H004,900,0,0,B
H005,1000,200,100,A
H006,1200,200,100,A
"""

PENDING_OK = """\
记录号,毛重,托盘重,申报净重
P001,1500,0,0
P002,500,0,0
P003,900,0,100
P004,1000,200,100
"""

CYCLE_RULES = """\
A | 毛重 >= 1000
B | 毛重 >= 900
C | 托盘重 >= 100
"""

CYCLE_HISTORY = """\
记录号,毛重,托盘重,申报净重,主码
C1,1000,0,0,A
C2,1200,0,0,B
"""

CYCLE_PENDING = """\
记录号,毛重,托盘重,申报净重
X1,1500,0,0
X2,500,0,0
"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def get(path: str) -> tuple[int, str]:
    try:
        with _opener.open(BASE + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]):
    boundary = "----migrationdemo" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    for name, (filename, content) in files.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


def post_upload(rules: str, history: str, pending: str) -> str:
    boundary, body = _multipart(
        {"rules_text": rules},
        {
            "history_file": ("history.csv", history.encode()),
            "pending_file": ("pending.csv", pending.encode()),
        },
    )
    req = urllib.request.Request(
        BASE + "/migration",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with _opener.open(req, timeout=10) as resp:
            raise AssertionError(f"上传应重定向，实际 {resp.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 302, exc.code
        location = exc.headers["Location"]
    return location.rstrip("/").split("/")[-1]


def post_correct(job_id: str, record_no: str, code: str) -> tuple[int, str]:
    data = urllib.parse.urlencode(
        {"record_no": record_no, "primary_code": code}
    ).encode()
    req = urllib.request.Request(
        BASE + f"/migration/{job_id}/correct",
        data=data,
        method="POST",
    )
    try:
        with _opener.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 302:  # 成功会更正后重定向回结果页
            return get(exc.headers["Location"])
        return exc.code, exc.read().decode("utf-8", "replace")


def must_contain(body: str, *needles: str) -> None:
    for needle in needles:
        assert needle in body, f"页面缺少 “{needle}”"


def show_download(job_id: str) -> tuple[int, str]:
    status, body = get(f"/migration/{job_id}/download")
    if status == 200:
        print("  下载内容（UTF-8 BOM 已去除）：")
        for line in body.lstrip("﻿").splitlines():
            print("   ", line)
    return status, body


print("A. 正常批：核对三种判定并下载")
job = post_upload(RULES, HISTORY_OK, PENDING_OK)
status, page = get(f"/migration/{job}")
assert status == 200
must_contain(page, "未发现冲突", "主码确定", "通过", "待核", "P001", "P003")
status, csv_text = show_download(job)
assert status == 200
assert "P001" in csv_text and "P002" in csv_text
assert "P003" not in csv_text, "待核行不应进入下载"
print(f"  任务号 {job}：待核 P003 已排除，下载 200 ✓\n")

print("B. 成环批：指出历史记录并暂停下载")
job = post_upload(CYCLE_RULES, CYCLE_HISTORY, CYCLE_PENDING)
status, page = get(f"/migration/{job}")
must_contain(page, "先后关系成环", "C1、C2", "下载已暂停")
status, _ = get(f"/migration/{job}/download")
assert status == 409, status
print(f"  任务号 {job}：冲突记录 C1、C2，下载 409 ✓\n")

print("C. 更正为未命中的主码：保留原因，仍暂停")
status, page = post_correct(job, "C2", "C")
must_contain(page, "历史主码未命中", "引入冲突", "下载已暂停")
status, _ = get(f"/migration/{job}/download")
assert status == 409, status
print("  更正记录已保留，下载仍 409 ✓\n")

print("D. 更正为相容主码：恢复原下载地址")
status, page = post_correct(job, "C2", "A")
must_contain(page, "未发现冲突", "下载迁移结果 CSV")
status, csv_text = show_download(job)
assert status == 200 and "X1" in csv_text
print("  冲突排除，原下载地址恢复 200 ✓")
print("\n网页端到端演示通过 ✓")
