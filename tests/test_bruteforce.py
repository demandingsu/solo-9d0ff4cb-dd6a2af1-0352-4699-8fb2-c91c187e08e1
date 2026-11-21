"""暴力枚举对照：在极小值域上穷举所有称重组合，独立验证求解器结论。"""

import itertools
import random

from app.rules import FIELD_LABELS, parse_rules_text
from app.solver import STATUS_JOINT, STATUS_NEVER, STATUS_SOLO, solve_rules

_TERMS = ["毛重", "托盘重", "申报净重", "1", "2", "3"]
_OPS = ["=", "!=", ">", ">=", "<", "<="]


def _random_expr(rng: random.Random) -> str:
    n = rng.randint(1, 3)
    parts = [rng.choice(_TERMS)]
    for _ in range(n - 1):
        parts.append(rng.choice(["+", "-"]))
        parts.append(rng.choice(_TERMS))
    return " ".join(parts)


def _random_rule_text(rng: random.Random, rid: str) -> str:
    cond_count = rng.randint(1, 2)
    conds = [
        f"{_random_expr(rng)} {rng.choice(_OPS)} {_random_expr(rng)}"
        for _ in range(cond_count)
    ]
    return f"{rid} | " + "；".join(conds)


def _bruteforce(rules, domain):
    """返回 {规则编号: (status, min_extra_count)}，与求解器口径一致。"""
    points = list(itertools.product(domain, repeat=3))
    expect = {}
    for target in rules:
        best = None
        for g, t, d in points:
            values = {"gross": g, "tare": t, "declared": d}
            if not target.fires(values):
                continue
            fired = {r.rule_id for r in rules if r.fires(values)}
            extra = len(fired - {target.rule_id})
            best = extra if best is None else min(best, extra)
        if best is None:
            expect[target.rule_id] = (STATUS_NEVER, None)
        elif best == 0:
            expect[target.rule_id] = (STATUS_SOLO, 0)
        else:
            expect[target.rule_id] = (STATUS_JOINT, best)
    return expect


def test_solver_matches_bruteforce_on_random_rulesets():
    rng = random.Random(20261121)
    domain = list(range(5))  # 0..4，每规则集 125 个组合
    bounds = {f: (0, 4) for f in ("gross", "tare", "declared")}

    for set_no in range(12):
        rule_count = rng.randint(1, 5)
        text = "\n".join(_random_rule_text(rng, f"R{i}") for i in range(rule_count))
        rules = parse_rules_text(text)
        results = solve_rules(rules, bounds)
        expect = _bruteforce(rules, domain)

        assert len(results) == rule_count
        for res in results:
            exp_status, exp_extra = expect[res.rule.rule_id]
            assert res.status == exp_status, (set_no, res.rule.rule_id, text)
            if exp_status == STATUS_NEVER:
                assert res.row is None
                continue
            # 求解器给的行必须真的触发目标规则且落在值域内
            assert res.rule.fires(res.row)
            for name, v in res.row.items():
                assert 0 <= v <= 4
            # 用规则引擎复核该行的实际触发集合与连带条数
            fired = {r.rule_id for r in rules if r.fires(res.row)}
            assert res.rule.rule_id in fired
            assert set(res.fired_ids) == fired
            assert len(res.extra_ids) == exp_extra
