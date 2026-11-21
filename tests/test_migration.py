"""主码迁移核对：解析、冲突检测、首条命中判定与下载内容。"""

from app.migration import (
    CONFLICT_EMPTY_CODE_FIRED,
    CONFLICT_ORDER_CYCLE,
    CONFLICT_PRIMARY_NOT_FIRED,
    STATUS_DETERMINED,
    STATUS_PASS,
    STATUS_REVIEW,
    MigrationInputError,
    analyze,
    parse_weighing_csv,
    render_migration_csv,
)
from app.rules import parse_rules_text

RULES = parse_rules_text(
    "\n".join(
        [
            "A | 毛重 >= 1000",
            "B | 托盘重 >= 100",
            "C | 申报净重 >= 100",
            "D | 毛重 - 托盘重 - 申报净重 > 500",
        ]
    )
)

HISTORY_HEADER = "记录号,毛重,托盘重,申报净重,主码\n"
PENDING_HEADER = "记录号,毛重,托盘重,申报净重\n"


def _history(*rows):
    return parse_weighing_csv(HISTORY_HEADER + "".join(rows), with_primary=True)


def _pending(*rows):
    return parse_weighing_csv(PENDING_HEADER + "".join(rows), with_primary=False)


# A >= 1000, B >= 100, C >= 100, D = A - B - C > 500
# H1: A=1000 B=0 C=0 -> A,D 命中，主码 A  => A 先于 D
# H2: A=1000 B=200 C=100 -> A,B,C,D 命中，主码 A => A 先于 B/C/D
# H3: A=1000 B=200 C=0 -> A,B,D 命中，主码 A  => A 先于 B/D
# H4: A=500 B=0 C=0 -> 无命中，空主码
HISTORY = [
    "H1,1000,0,0,A\n",
    "H2,1000,200,100,A\n",
    "H3,1000,200,0,A\n",
    "H4,500,0,0,\n",
]


def test_no_conflict_determines_primary_and_passes_empty():
    report = analyze(
        RULES,
        _history(*HISTORY),
        _pending(
            "P1,1000,0,0\n",        # A,D 命中，A 必在其前 -> A
            "P2,500,0,0\n",         # 无命中 -> 通过
            "P3,500,200,200\n",     # B,C 命中，先后未知 -> 待核
            "P4,3000,0,0\n",        # A,D 命中 -> A
        ),
    )
    assert not report.has_conflict
    by_id = {d.row.record_no: d for d in report.pending}
    assert by_id["P1"].status == STATUS_DETERMINED
    assert by_id["P1"].primary_code == "A"
    assert by_id["P2"].status == STATUS_PASS
    assert by_id["P2"].primary_code == ""
    assert by_id["P3"].status == STATUS_REVIEW
    assert by_id["P3"].primary_code == ""
    assert set(by_id["P3"].candidates) == {"B", "C"}
    assert by_id["P4"].status == STATUS_DETERMINED
    assert by_id["P4"].primary_code == "A"


def test_download_only_includes_pass_and_determined():
    report = analyze(
        RULES,
        _history(*HISTORY),
        _pending("P1,1000,0,0\n", "P2,500,0,0\n", "P3,500,200,200\n"),
    )
    assert report.download_allowed
    rows = report.download_rows
    assert [r["record_no"] for r in rows] == ["P1", "P2"]
    assert rows[0]["primary_code"] == "A"
    assert rows[1]["primary_code"] == ""
    assert rows[1]["status"] == "通过"
    csv_text = render_migration_csv(rows)
    assert csv_text.splitlines()[0] == (
        "record_no,gross_g,tare_g,declared_net_g,primary_code,status"
    )
    assert "P3" not in csv_text


def test_primary_not_fired_conflicts_and_names_record():
    report = analyze(
        RULES,
        _history("H9,500,0,0,A\n"),  # A 未命中，主码却是 A
        _pending("P1,500,0,0\n"),
    )
    assert report.has_conflict
    assert not report.download_allowed
    kinds = {c.kind for c in report.conflicts}
    assert kinds == {CONFLICT_PRIMARY_NOT_FIRED}
    conflict = report.conflicts[0]
    assert conflict.record_nos == ["H9"]
    assert conflict.record_text == "H9"
    assert "未在该行命中" in conflict.detail


def test_unknown_primary_code_conflicts():
    report = analyze(
        RULES,
        _history("H9,1000,0,0,NOPE\n"),
        _pending("P1,1000,0,0\n"),
    )
    assert [c.kind for c in report.conflicts] == [CONFLICT_PRIMARY_NOT_FIRED]
    assert "不是已录入的规则编号" in report.conflicts[0].detail


def test_empty_primary_with_fire_conflicts():
    report = analyze(
        RULES,
        _history("H8,1000,0,0,\n"),
        _pending("P1,1000,0,0\n"),
    )
    assert [c.kind for c in report.conflicts] == [CONFLICT_EMPTY_CODE_FIRED]
    assert report.conflicts[0].record_nos == ["H8"]


def test_cycle_conflict_names_all_records_and_blocks_download():
    # 两条规则互相覆盖：任何命中行都同时命中 A、B，H1 说 A 先，H2 说 B 先
    rules = parse_rules_text("A | 毛重 >= 1000\nB | 毛重 >= 900")
    report = analyze(
        rules,
        _history("C1,1000,0,0,A\n", "C2,1200,0,0,B\n"),
        _pending("P1,1500,0,0\n", "P2,500,0,0\n"),
    )
    assert [c.kind for c in report.conflicts] == [CONFLICT_ORDER_CYCLE]
    cycle = report.conflicts[0]
    assert cycle.record_nos == ["C1", "C2"]
    assert set(cycle.rule_ids) == {"A", "B"}
    assert "A 先于 B" in cycle.detail and "B 先于 A" in cycle.detail
    # 冲突时下载暂停，待迁移行全部不进入下载
    assert not report.download_allowed
    assert report.download_rows == []
    assert all(d.status == STATUS_REVIEW for d in report.pending)


def test_correction_removes_cycle_and_restores_download():
    rules = parse_rules_text("A | 毛重 >= 1000\nB | 毛重 >= 900")
    history = _history("C1,1000,0,0,A\n", "C2,1200,0,0,B\n")
    pending = _pending("P1,1500,0,0\n", "P2,500,0,0\n")
    assert not analyze(rules, history, pending).download_allowed
    corrected = analyze(rules, history, pending, {"C2": "A"})
    assert not corrected.has_conflict
    assert corrected.download_allowed
    by_id = {d.row.record_no: d for d in corrected.pending}
    assert by_id["P1"].status == STATUS_DETERMINED
    assert by_id["P1"].primary_code == "A"
    assert by_id["P2"].status == STATUS_PASS


def test_correction_introducing_conflict_is_reflected():
    history = _history(*HISTORY)
    pending = _pending("P1,1000,0,0\n")
    benign = analyze(RULES, history, pending, {"H4": ""})
    assert not benign.has_conflict
    conflicting = analyze(RULES, history, pending, {"H4": "B"})
    assert conflicting.has_conflict
    assert conflicting.conflicts[0].kind == CONFLICT_PRIMARY_NOT_FIRED
    assert conflicting.conflicts[0].record_nos == ["H4"]
    assert conflicting.download_rows == []


def test_prefix_chain_gives_other_order():
    # 只观察到 A 先于 B；B、C 之间无约束时按传递性仍需比较
    rules = parse_rules_text("A | 毛重 >= 1000\nB | 毛重 >= 900\nC | 申报净重 >= 100")
    report = analyze(
        rules,
        _history(
            "H1,1000,0,0,A\n",     # A、B 命中，A 先
            "H2,900,0,100,B\n",    # B、? 命中：B, C（毛重 900 不触发 A）
        ),
        _pending("P1,1000,0,100\n"),  # A,B,C 全命中，A 最先
    )
    assert not report.has_conflict
    decision = report.pending[0]
    assert decision.status == STATUS_DETERMINED
    assert decision.primary_code == "A"


def test_parse_requires_primary_column_for_history():
    try:
        parse_weighing_csv(
            "记录号,毛重,托盘重,申报净重\nH1,1,2,3\n", with_primary=True
        )
    except MigrationInputError as exc:
        assert "主码" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少主码列应报错")


def test_parse_rejects_duplicate_record_numbers():
    try:
        parse_weighing_csv(
            "记录号,毛重,托盘重,申报净重,主码\nH1,1,2,3,A\nH1,4,5,6,\n",
            with_primary=True,
        )
    except MigrationInputError as exc:
        assert "重复" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复记录号应报错")


def test_parse_rejects_bad_number():
    try:
        parse_weighing_csv(
            "记录号,毛重,托盘重,申报净重,主码\nH1,abc,2,3,A\n",
            with_primary=True,
        )
    except MigrationInputError as exc:
        assert "不是整数" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("非整数应报错")


def test_parse_accepts_utf8_bom_and_gbk_headers():
    text = "﻿记录号,毛重,托盘重,申报净重,主码\nH1,1000,0,0,A\n"
    rows = parse_weighing_csv(text, with_primary=True)
    assert rows[0].record_no == "H1"
    assert rows[0].values == {"gross": 1000, "tare": 0, "declared": 0}
    assert rows[0].primary_code == "A"


def test_english_headers_accepted():
    rows = parse_weighing_csv(
        "record_no,gross_g,tare_g,declared_net_g,primary_code\nX,1,2,3,\n",
        with_primary=True,
    )
    assert rows[0].record_no == "X"
