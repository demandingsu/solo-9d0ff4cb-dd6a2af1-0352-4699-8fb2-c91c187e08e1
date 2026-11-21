"""Flask 网页：录入规则与取值范围、试算、下载/回放称重 CSV。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)

from .replay import (
    ReplayReport,
    build_rows,
    render_csv,
    replay_csv,
    summarize_for_template,
)
from .rules import (
    FIELD_LABELS,
    FIELDS,
    MAX_VALUE,
    Rule,
    RuleParseError,
    parse_rules_text,
    validate_bounds,
)
from .solver import RuleResult, STATUS_JOINT, STATUS_NEVER, STATUS_SOLO, solve_rules

app = Flask(__name__)

# 简单内存任务表（gunicorn 单 worker 部署）；最多保留 200 个任务
_JOBS: Dict[str, "Job"] = {}
_JOBS_LOCK = threading.Lock()
_JOB_LIMIT = 200

_DEFAULT_RULES = """\
# 每行一条：规则编号 | 比较式（多个比较用 ；分隔，需同时成立）
# 字段：毛重 / 托盘重 / 申报净重；比较符：= != > >= < <=；支持整数加减
R001 | 毛重 - 托盘重 - 申报净重 > 500
R002 | 毛重 < 托盘重
R003 | 毛重 - 托盘重 = 申报净重
"""


@dataclass
class Job:
    job_id: str
    rules: List[Rule]
    bounds: Dict[str, Tuple[int, int]]
    results: List[RuleResult]
    csv_rows: List[Dict[str, str]]
    rules_text: str = ""

    @property
    def csv_text(self) -> str:
        return "\ufeff" + render_csv(self.csv_rows)


def _parse_bounds(form) -> Dict[str, Tuple[int, int]]:
    bounds: Dict[str, Tuple[int, int]] = {}
    for name in FIELDS:
        label = FIELD_LABELS[name]
        try:
            lo = int(form.get(f"{name}_low", "").strip())
            hi = int(form.get(f"{name}_high", "").strip())
        except ValueError:
            raise RuleParseError(f"{label} 的上下限必须是整数") from None
        validate_bounds(lo, hi, label)
        bounds[name] = (lo, hi)
    return bounds


def _save_job(job: Job) -> None:
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job
        if len(_JOBS) > _JOB_LIMIT:
            for old_id in list(_JOBS)[: len(_JOBS) - _JOB_LIMIT]:
                _JOBS.pop(old_id, None)


def _get_job(job_id: str) -> Job:
    job = _JOBS.get(job_id)
    if job is None:
        abort(404, description="任务不存在或已过期，请重新试算")
    return job


@app.route("/")
def index() -> str:
    return render_template("index.html", default_rules=_DEFAULT_RULES, max_value=MAX_VALUE)


@app.route("/solve", methods=["POST"])
def solve():
    rules_text = request.form.get("rules_text", "")
    try:
        rules = parse_rules_text(rules_text)
        bounds = _parse_bounds(request.form)
    except RuleParseError as exc:
        return render_template("error.html", message=str(exc)), 400

    results = solve_rules(rules, bounds)
    csv_rows = build_rows(results)
    job = Job(
        job_id=uuid.uuid4().hex[:12],
        rules=rules,
        bounds=bounds,
        results=results,
        csv_rows=csv_rows,
        rules_text=rules_text,
    )
    _save_job(job)
    return redirect(url_for("result", job_id=job.job_id))


@app.route("/jobs/<job_id>")
def result(job_id: str) -> str:
    job = _get_job(job_id)
    solo, joint, never = summarize_for_template(job.results)
    return render_template(
        "result.html",
        job=job,
        solo=solo,
        joint=joint,
        never=never,
        status_labels={
            STATUS_SOLO: "可单独触发",
            STATUS_JOINT: "只能连带触发",
            STATUS_NEVER: "范围内从不触发",
        },
        field_labels=FIELD_LABELS,
    )


@app.route("/jobs/<job_id>/csv")
def download_csv(job_id: str):
    job = _get_job(job_id)
    resp = Response(job.csv_text, mimetype="text/csv")
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="weighing_{job_id}.csv"'
    return resp


@app.route("/replay", methods=["GET", "POST"])
def replay():
    report: Optional[ReplayReport] = None
    rules_text = ""
    error: Optional[str] = None
    prefill_job_id = request.args.get("job_id", "")

    if request.method == "GET":
        if prefill_job_id:
            job = _JOBS.get(prefill_job_id)
            if job is not None:
                rules_text = job.rules_text
        return render_template(
            "replay.html", report=None, rules_text=rules_text, error=None
        )

    rules_text = request.form.get("rules_text", "")
    upload = request.files.get("csv_file")
    try:
        rules = parse_rules_text(rules_text)
    except RuleParseError as exc:
        return render_template(
            "replay.html", report=None, rules_text=rules_text, error=str(exc)
        ), 400

    if upload is None or not upload.filename:
        return render_template(
            "replay.html",
            report=None,
            rules_text=rules_text,
            error="请选择要回放的称重 CSV 文件",
        ), 400

    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            return render_template(
                "replay.html",
                report=None,
                rules_text=rules_text,
                error="CSV 编码无法识别，请保存为 UTF-8 或 GBK 后重试",
            ), 400

    report = replay_csv(text, rules)
    return render_template(
        "replay.html", report=report, rules_text=rules_text, error=error
    )


@app.errorhandler(404)
def not_found(err):  # pragma: no cover - 简单错误页
    return render_template("error.html", message=getattr(err, "description", "页面不存在")), 404


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=8000)
