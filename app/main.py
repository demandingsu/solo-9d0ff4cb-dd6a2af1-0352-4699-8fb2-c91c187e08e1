"""Flask 网页：录入规则与取值范围、试算、下载/回放称重 CSV、主码迁移核对。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
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

from .migration import (
    MigrationInputError,
    WeighRow,
    analyze,
    correction_reason,
    parse_weighing_csv,
    render_migration_csv,
)
from .replay import (
    ReplayReport,
    build_rows,
    decode_csv_bytes,
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

# 主码迁移任务同样保存在进程内存中
_MIGRATION_JOBS: Dict[str, "MigrationJob"] = {}
_MIGRATION_JOBS_LOCK = threading.Lock()
_MIGRATION_JOB_LIMIT = 200

_DEFAULT_RULES = """\
# 每行一条：规则编号 | 比较式（多个比较用 ；分隔，需同时成立）
# 字段：毛重 / 托盘重 / 申报净重；比较符：= != > >= < <=；支持整数加减
R001 | 毛重 - 托盘重 - 申报净重 > 500
R002 | 毛重 < 托盘重
R003 | 毛重 - 托盘重 = 申报净重
"""

_MIGRATION_DEFAULT_RULES = """\
# 主码迁移核对：规则顺序未知，由历史主码与逐行命中反推
A | 毛重 >= 1000
B | 毛重 >= 900
C | 托盘重 >= 100
D | 申报净重 >= 100
"""

_MIGRATION_DEFAULT_HISTORY = """\
记录号,毛重,托盘重,申报净重,主码
H001,1000,0,0,A
H002,1200,0,0,A
H003,500,0,0,
H004,900,0,0,B
H005,1000,200,100,A
H006,1200,200,100,A
"""

_MIGRATION_DEFAULT_PENDING = """\
记录号,毛重,托盘重,申报净重
P001,1500,0,0
P002,500,0,0
P003,900,0,100
P004,1000,200,100
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


@dataclass
class CorrectionEntry:
    record_no: str
    original: str
    new_code: str
    reason: str
    at: str
    introduced: bool
    conflict_reasons: List[str] = field(default_factory=list)


@dataclass
class MigrationJob:
    job_id: str
    rules: List[Rule]
    rules_text: str
    history_rows: List[WeighRow]
    pending_rows: List[WeighRow]
    history_text: str = ""
    pending_text: str = ""
    history_filename: str = ""
    pending_filename: str = ""
    corrections: Dict[str, str] = field(default_factory=dict)
    correction_log: List[CorrectionEntry] = field(default_factory=list)
    _lock: "threading.RLock" = field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    _report_cache: Optional[object] = None
    _cache_key: Optional[Tuple[Tuple[str, str], ...]] = None

    def report(self):
        with self._lock:
            key = tuple(sorted(self.corrections.items()))
            if self._report_cache is None or self._cache_key != key:
                self._report_cache = analyze(
                    self.rules,
                    self.history_rows,
                    self.pending_rows,
                    self.corrections,
                )
                self._cache_key = key
            return self._report_cache

    @property
    def csv_text(self) -> str:
        return "\ufeff" + render_migration_csv(self.report().download_rows)


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


def _save_migration_job(job: MigrationJob) -> None:
    with _MIGRATION_JOBS_LOCK:
        _MIGRATION_JOBS[job.job_id] = job
        if len(_MIGRATION_JOBS) > _MIGRATION_JOB_LIMIT:
            for old_id in list(_MIGRATION_JOBS)[: len(_MIGRATION_JOBS) - _MIGRATION_JOB_LIMIT]:
                _MIGRATION_JOBS.pop(old_id, None)


def _get_migration_job(job_id: str) -> MigrationJob:
    job = _MIGRATION_JOBS.get(job_id)
    if job is None:
        abort(404, description="迁移任务不存在或已过期，请重新上传")
    return job


def _read_upload(store, field_name: str, label: str) -> str:
    upload = store.get(field_name)
    if upload is None or not upload.filename:
        raise MigrationInputError(f"请选择{label}")
    try:
        return decode_csv_bytes(upload.read())
    except ValueError as exc:
        raise MigrationInputError(f"{label}：{exc}") from None


def _migration_input(
    files, form, field_name: str, text_name: str, label: str
) -> str:
    """优先读上传文件；未上传文件时使用页面上的 CSV 文本（可直接用示例内容演示）。"""
    upload = files.get(field_name)
    if upload is not None and upload.filename:
        return _read_upload(files, field_name, label)
    text = (form.get(text_name) or "").strip()
    if not text:
        raise MigrationInputError(f"请选择{label}，或粘贴 CSV 内容")
    return text


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
        text = decode_csv_bytes(raw)
    except ValueError as exc:
        return render_template(
            "replay.html",
            report=None,
            rules_text=rules_text,
            error=str(exc),
        ), 400

    report = replay_csv(text, rules)
    return render_template(
        "replay.html", report=report, rules_text=rules_text, error=error
    )


# ------------------------------------------------------------ 主码迁移核对


def _migration_context(job: MigrationJob, report=None, error: Optional[str] = None):
    report = report or job.report()
    return {
        "job": job,
        "report": report,
        "error": error,
        "default_rules": job.rules_text,
        "default_history": job.history_text,
        "default_pending": job.pending_text,
        "demo_loaded": False,
    }


@app.route("/migration")
def migration_index() -> str:
    demo = request.args.get("demo") == "1"
    return render_template(
        "migration.html",
        job=None,
        report=None,
        error=None,
        default_rules=_MIGRATION_DEFAULT_RULES if demo else "",
        default_history=_MIGRATION_DEFAULT_HISTORY if demo else "",
        default_pending=_MIGRATION_DEFAULT_PENDING if demo else "",
        demo_loaded=demo,
    )


@app.route("/migration", methods=["POST"])
def migration_start():
    rules_text = request.form.get("rules_text", "")
    history_text: Optional[str] = None
    pending_text: Optional[str] = None
    try:
        rules = parse_rules_text(rules_text)
        history_text = _migration_input(
            request.files, request.form, "history_file", "history_text", "历史称重 CSV"
        )
        pending_text = _migration_input(
            request.files, request.form, "pending_file", "pending_text", "待迁移称重 CSV"
        )
        history_rows = parse_weighing_csv(
            history_text, with_primary=True, label="历史称重 CSV"
        )
        pending_rows = parse_weighing_csv(
            pending_text, with_primary=False, label="待迁移称重 CSV"
        )
    except (RuleParseError, MigrationInputError) as exc:
        return (
            render_template(
                "migration.html",
                job=None,
                report=None,
                error=str(exc),
                default_rules=rules_text,
                default_history=history_text or "",
                default_pending=pending_text or "",
                demo_loaded=False,
            ),
            400,
        )

    job = MigrationJob(
        job_id=uuid.uuid4().hex[:12],
        rules=rules,
        rules_text=rules_text,
        history_rows=history_rows,
        pending_rows=pending_rows,
        history_text=history_text,
        pending_text=pending_text,
        history_filename=(
            request.files["history_file"].filename
            if "history_file" in request.files and request.files["history_file"].filename
            else "（页面粘贴）"
        ),
        pending_filename=(
            request.files["pending_file"].filename
            if "pending_file" in request.files and request.files["pending_file"].filename
            else "（页面粘贴）"
        ),
    )
    _save_migration_job(job)
    return redirect(url_for("migration_result", job_id=job.job_id))


@app.route("/migration/<job_id>")
def migration_result(job_id: str) -> str:
    job = _get_migration_job(job_id)
    return render_template("migration.html", **_migration_context(job))


@app.route("/migration/<job_id>/correct", methods=["POST"])
def migration_correct(job_id: str):
    job = _get_migration_job(job_id)
    record_no = request.form.get("record_no", "").strip()
    new_code = request.form.get("primary_code", "").strip()
    known = {row.record_no: row for row in job.history_rows}
    if record_no not in known:
        return (
            render_template(
                "migration.html",
                **_migration_context(job, error=f"历史记录 {record_no} 不存在"),
            ),
            400,
        )

    row = known[record_no]
    current_effective = job.corrections.get(record_no, row.primary_code)
    if new_code == current_effective:
        # 未改变任何取值：不写更正记录，直接回到结果页
        return redirect(url_for("migration_result", job_id=job.job_id))
    with job._lock:
        before = job.report()
        before_view = next(
            v for v in before.history if v.row.record_no == record_no
        )

        if new_code == row.primary_code:
            job.corrections.pop(record_no, None)
        else:
            job.corrections[record_no] = new_code

        after = job.report()
        after_view = next(
            v for v in after.history if v.row.record_no == record_no
        )
        reason = correction_reason(after, record_no)
        introduced = (not before_view.has_conflict) and after_view.has_conflict
        entry = CorrectionEntry(
            record_no=record_no,
            original=row.primary_code,
            new_code=new_code,
            reason=reason,
            at=time.strftime("%Y-%m-%d %H:%M:%S"),
            introduced=introduced,
            conflict_reasons=after_view.conflict_reasons,
        )
        job.correction_log.insert(0, entry)
    return redirect(url_for("migration_result", job_id=job.job_id))


@app.route("/migration/<job_id>/download")
def migration_download(job_id: str):
    job = _get_migration_job(job_id)
    report = job.report()
    if not report.download_allowed:
        return (
            render_template(
                "migration.html",
                **_migration_context(job, report),
            ),
            409,
        )
    resp = Response(job.csv_text, mimetype="text/csv")
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="migration_{job_id}.csv"'
    )
    return resp


@app.errorhandler(404)
def not_found(err):  # pragma: no cover - 简单错误页
    return render_template("error.html", message=getattr(err, "description", "页面不存在")), 404


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=8000)
