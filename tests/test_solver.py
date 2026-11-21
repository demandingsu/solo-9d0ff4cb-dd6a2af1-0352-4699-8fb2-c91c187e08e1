"""求解器测试：solo / joint / never 三种结论与连带最小化。"""

import pytest

from app.rules import parse_rules_text
from app.solver import (
    STATUS_JOINT,
    STATUS_NEVER,
    STATUS_SOLO,
    solve_rules,
)

FULL_RANGE = {f: (0, 1_000_000) for f in ("gross", "tare", "declared")}
SMALL_RANGE = {f: (0, 20) for f in ("gross", "tare", "declared")}


def _by_id(results):
    return {r.rule.rule_id: r for r in results}


def test_solo_rules_each_get_unique_trigger_row():
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 < 托盘重",
                "R2 | 毛重 > 托盘重 + 申报净重",
                "R3 | 毛重 - 托盘重 = 申报净重",
            ]
        )
    )
    results = solve_rules(rules, FULL_RANGE)
    got = _by_id(results)
    for rid in ("R1", "R2", "R3"):
        res = got[rid]
        assert res.status == STATUS_SOLO, rid
        assert res.fired_ids == [rid]
        # 行内取值落在声明范围内
        for name, v in res.row.items():
            lo, hi = FULL_RANGE[name]
            assert lo <= v <= hi
    # 纯 Python 规则引擎复核：每行确实只触发对应规则
    for rid, res in got.items():
        fired = [r.rule_id for r in rules if r.fires(res.row)]
        assert fired == [rid]


def test_never_when_rule_contradicts_itself():
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 > 托盘重；毛重 < 托盘重",  # 自相矛盾
                "R2 | 毛重 >= 0",
            ]
        )
    )
    got = _by_id(solve_rules(rules, FULL_RANGE))
    assert got["R1"].status == STATUS_NEVER
    assert got["R1"].row is None
    assert got["R2"].status == STATUS_SOLO


def test_never_due_to_bounds():
    # 要求毛重超过范围上界
    rules = parse_rules_text("R1 | 毛重 > 20")
    got = _by_id(solve_rules(rules, SMALL_RANGE))
    assert got["R1"].status == STATUS_NEVER


def test_joint_when_feasible_region_covered_by_others():
    # R1 = 毛重 >= 10（可行域 [10,20]）
    # R2 = 毛重 >= 15（覆盖 R1 可行域的一半）
    # R3 = 毛重 <= 14（覆盖另一半）
    # 任何触发 R1 的行必连带触发 R2 或 R3；不存在 solo 行
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 >= 10",
                "R2 | 毛重 >= 15",
                "R3 | 毛重 <= 14",
            ]
        )
    )
    got = _by_id(solve_rules(rules, SMALL_RANGE))
    assert got["R1"].status == STATUS_JOINT
    # 连带数最少 = 1（只带 R2 或只带 R3，不会两个都带）
    assert len(got["R1"].extra_ids) == 1
    assert set(got["R1"].fired_ids) in ({"R1", "R2"}, {"R1", "R3"})
    # R2 的可行域是 R1 的子集：触发 R2 必连带 R1；R3 可取小值单独触发
    assert got["R2"].status == STATUS_JOINT
    assert got["R2"].extra_ids == ["R1"]
    assert got["R3"].status == STATUS_SOLO


def test_joint_minimizes_number_of_extra_rules():
    # 可行域被三层规则嵌套覆盖：触发 R1 时最多连带 3 条，最优只连带 1 条
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 >= 5",
                "R2 | 毛重 >= 6",
                "R3 | 毛重 >= 7",
                "R4 | 毛重 <= 5",  # 只在毛重=5 与 R1 同时触发，提供连带 1 条的出口
            ]
        )
    )
    got = _by_id(solve_rules(rules, SMALL_RANGE))
    assert got["R1"].status == STATUS_JOINT
    # 最优解连带 1 条：毛重=5（带 R4）或毛重=6（带 R2）；毛重>=7 会连带 2 条
    assert len(got["R1"].extra_ids) == 1
    assert got["R1"].row["gross"] in (5, 6)
    if got["R1"].row["gross"] == 5:
        assert got["R1"].extra_ids == ["R4"]
    else:
        assert got["R1"].extra_ids == ["R2"]


def test_identical_rules_always_fire_together():
    # 两条完全相同的规则：彼此必然连带，且连带数为 1（最小）
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 申报净重 = 0",
                "R2 | 申报净重 = 0",
            ]
        )
    )
    got = _by_id(solve_rules(rules, FULL_RANGE))
    assert got["R1"].status == STATUS_JOINT
    assert got["R1"].extra_ids == ["R2"]
    assert got["R2"].status == STATUS_JOINT
    assert got["R2"].extra_ids == ["R1"]


def test_equality_with_additive_expressions_solvable():
    rules = parse_rules_text("R1 | 毛重 - 托盘重 = 申报净重 + 100")
    got = _by_id(solve_rules(rules, FULL_RANGE))
    assert got["R1"].status == STATUS_SOLO
    row = got["R1"].row
    assert row["gross"] - row["tare"] == row["declared"] + 100


def test_results_count_and_bound_respect():
    rules = parse_rules_text("R1 | 毛重 > 100\nR2 | 托盘重 = 0")
    wide_range = {f: (0, 200) for f in ("gross", "tare", "declared")}
    results = solve_rules(rules, wide_range)
    assert len(results) == 2
    for res in results:
        for name, v in res.row.items():
            assert 0 <= v <= 200


def test_physical_weight_net_difference_example():
    # 模拟业务：净重差 = 毛重 - 托盘重 - 申报净重
    rules = parse_rules_text(
        "\n".join(
            [
                "R1 | 毛重 - 托盘重 - 申报净重 > 500",       # 超重拒收
                "R2 | 毛重 - 托盘重 - 申报净重 < -500",      # 欠重拒收
                "R3 | 毛重 - 托盘重 < 申报净重 - 1000",      # 被 R2 完全包含
            ]
        )
    )
    got = _by_id(solve_rules(rules, FULL_RANGE))
    # R3 的可行域是 R2 的子集：触发 R3 必连带 R2
    assert got["R3"].status == STATUS_JOINT
    assert got["R3"].extra_ids == ["R2"]
    # R1、R2 可以单独触发
    assert got["R1"].status == STATUS_SOLO
    assert got["R2"].status == STATUS_SOLO
