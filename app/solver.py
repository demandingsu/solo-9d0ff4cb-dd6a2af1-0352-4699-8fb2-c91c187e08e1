"""用 z3 为每条拒收规则求解“仅触发该规则”的称重数据。

三种结论：
  * solo        —— 存在一行只触发该规则（理想情况，生成该行 CSV）；
  * joint       —— 该规则在给定范围内能触发，但任何触发它的行都会连带触发
                    其他规则；返回连带触发规则数最少的一行及其编号；
  * never       —— 该规则在给定取值范围内从不触发（规则自身不可满足）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import z3

from .rules import FIELDS, Condition, Linear, Rule

STATUS_SOLO = "solo"
STATUS_JOINT = "joint"
STATUS_NEVER = "never"


@dataclass
class RuleResult:
    rule: Rule
    status: str
    row: Optional[Dict[str, int]] = None
    fired_ids: List[str] = field(default_factory=list)
    extra_ids: List[str] = field(default_factory=list)
    reason: str = ""

    @property
    def is_solo(self) -> bool:
        return self.status == STATUS_SOLO


def _z3_linear(expr: Linear, zvars: Dict[str, z3.ArithRef]) -> z3.ArithRef:
    terms = [z3.IntVal(expr.const)]
    for name, coeff in expr.coeffs.items():
        terms.append(coeff * zvars[name])
    return z3.Sum(terms) if len(terms) > 1 else terms[0]


def _z3_condition(cond: Condition, zvars: Dict[str, z3.ArithRef]) -> z3.BoolRef:
    a, b = _z3_linear(cond.left, zvars), _z3_linear(cond.right, zvars)
    op = cond.op
    if op in ("=", "=="):
        return a == b
    if op in ("!=", "<>"):
        return a != b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    raise ValueError(f"不支持的比较符 {op}")


def _rule_constraints(rule: Rule, zvars: Dict[str, z3.ArithRef]) -> List[z3.BoolRef]:
    return [_z3_condition(c, zvars) for c in rule.conditions]


def _model_values(model: z3.ModelRef, zvars: Dict[str, z3.ArithRef]) -> Dict[str, int]:
    values: Dict[str, int] = {}
    for name in FIELDS:
        v = model.eval(zvars[name], model_completion=True)
        values[name] = v.as_long()
    return values


def solve_rules(
    rules: List[Rule],
    bounds: Dict[str, Tuple[int, int]],
) -> List[RuleResult]:
    """对每条规则求解。bounds: 字段名 -> (下限, 上限)，均为闭区间整数。"""
    zvars = {name: z3.Int(name) for name in FIELDS}
    range_cons = [zvars[n] >= lo for n, (lo, hi) in bounds.items()]
    range_cons += [zvars[n] <= hi for n, (lo, hi) in bounds.items()]

    # 每个规则是否触发的布尔量，供“连带触发最少”优化使用
    fire_bools: Dict[str, z3.BoolRef] = {}
    for r in rules:
        fire_bools[r.rule_id] = z3.And(*_rule_constraints(r, zvars))

    results: List[RuleResult] = []
    for idx, target in enumerate(rules):
        target_cons = _rule_constraints(target, zvars)

        # 第一步：规则自身（在取值范围内）是否可触发
        feasibility = z3.Solver()
        feasibility.add(*range_cons)
        feasibility.add(*target_cons)
        if feasibility.check() == z3.unsat:
            results.append(
                RuleResult(
                    rule=target,
                    status=STATUS_NEVER,
                    reason="该规则的全部比较在给定取值范围内不可能同时成立",
                )
            )
            continue

        # 第二步：在“触发目标规则”的前提下，最小化同时触发的其他规则数量
        opt = z3.Optimize()
        opt.add(*range_cons)
        opt.add(*target_cons)
        others = [fire_bools[r.rule_id] for j, r in enumerate(rules) if j != idx]
        if others:
            penalty = z3.Sum([z3.If(b, 1, 0) for b in others])
            opt.minimize(penalty)
        check = opt.check()
        assert check == z3.sat, f"优化器意外失败：{check}"

        values = _model_values(opt.model(), zvars)
        fired = [r.rule_id for r in rules if r.fires(values)]
        extras = [rid for rid in fired if rid != target.rule_id]

        if not extras:
            results.append(
                RuleResult(
                    rule=target,
                    status=STATUS_SOLO,
                    row=values,
                    fired_ids=[target.rule_id],
                )
            )
        else:
            results.append(
                RuleResult(
                    rule=target,
                    status=STATUS_JOINT,
                    row=values,
                    fired_ids=fired,
                    extra_ids=extras,
                    reason=(
                        f"任何触发规则 {target.rule_id} 的数据行都会连带触发其他规则；"
                        f"已给出连带规则数最少（{len(extras)} 条）的一行"
                    ),
                )
            )
    return results
