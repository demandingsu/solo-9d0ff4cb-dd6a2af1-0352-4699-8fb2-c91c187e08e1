"""CSV 生成与回放测试。"""

import io

import pytest

from app.replay import build_rows, render_csv, replay_csv
from app.rules import parse_rules_text
from app.solver import solve_rules

FULL_RANGE = {f: (0, 1_000_000) for f in ("gross", "tare", "declared")}


@pytest.fixture()
def rules_and_results():
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 < 托盘重",
                "R2 | 毛重 - 托盘重 - 申报净重 > 500",
            ]
        )
    )
    results = solve_rules(rules, FULL_RANGE)
    return rules, results


def test_rendered_csv_roundtrips(rules_and_results):
    rules, results = rules_and_results
    csv_text = "\ufeff" + render_csv(build_rows(results))
    report = replay_csv(csv_text, rules)
    assert report.errors == []
    assert len(report.rows) == 2
    assert report.mismatch_count == 0
    assert report.matched_count == 2
    fired_sets = [set(r.fired_ids) for r in report.rows]
    assert fired_sets == [{"R1"}, {"R2"}]


def test_replay_recomputes_fired_ids_independently():
    # 即使文件里的 fired/expected 列被改错，回放以规则引擎重新计算为准
    rules = parse_rules_text("R1 | 毛重 > 10")
    tampered = (
        "row_id,gross_g,tare_g,declared_net_g,expected_rule_ids,fired_rule_ids\n"
        "R001,11,0,0,R9,R9\n"
    )
    report = replay_csv(tampered, rules)
    assert report.rows[0].fired_ids == ["R1"]
    assert report.mismatch_count == 1  # 预期 R9 与实际 R1 不一致


def test_replay_chinese_header_alias():
    rules = parse_rules_text("R1 | 托盘重 = 7")
    text = "编号,毛重,托盘重,申报净重\nX1,3,7,0\n"
    report = replay_csv(text, rules)
    assert report.errors == []
    assert report.rows[0].fired_ids == ["R1"]
    assert report.rows[0].row_id == "X1"


def test_replay_out_of_range_value_reported():
    rules = parse_rules_text("R1 | 毛重 > 0")
    text = "row_id,gross_g,tare_g,declared_net_g\nX1,1000001,0,0\n"
    report = replay_csv(text, rules)
    assert len(report.rows) == 0
    assert any("超出允许范围" in e for e in report.errors)


def test_replay_missing_column_reported():
    rules = parse_rules_text("R1 | 毛重 > 0")
    report = replay_csv("gross_g,tare_g\n1,2\n", rules)
    assert report.rows == []
    assert any("缺少必需列" in e for e in report.errors)


def test_replay_unknown_expected_id_reported_but_still_replays():
    rules = parse_rules_text("R1 | 毛重 > 10")
    text = "row_id,gross_g,tare_g,declared_net_g,expected_rule_ids\nX1,11,0,0,R77\n"
    report = replay_csv(text, rules)
    assert len(report.rows) == 1
    assert any("R77" in e for e in report.errors)
    assert report.rows[0].match is False


def test_replay_no_fire_row():
    rules = parse_rules_text("R1 | 毛重 > 10")
    text = "row_id,gross_g,tare_g,declared_net_g\nX1,5,0,0\n"
    report = replay_csv(text, rules)
    assert report.rows[0].fired_ids == []
    assert report.rows[0].match is None  # 无预期列时不做比对


def test_never_rules_produce_no_csv_rows():
    rules = parse_rules_text("R1 | 毛重 > 毛重\nR2 | 毛重 = 0")
    results = solve_rules(rules, FULL_RANGE)
    rows = build_rows(results)
    assert len(rows) == 1  # 只有 R2 产生一行
    assert rows[0]["gross_g"] == "0"
