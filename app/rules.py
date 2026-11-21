"""拒收规则的文本解析与纯 Python 求值。

规则文本格式（每行一条规则）::

    <规则编号> | <比较式> [；/ ;/ ,/ 且/ AND] <比较式> ...

比较式两边是“线性表达式”：字段名、非负整数常量，或它们的加减式；
比较符支持 ``= == != <> > >= < <=``。

字段（克）：
  毛重   G  gross
  托盘重 T  tare
  申报净重 D  declared
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

# 字段别名 -> 规范名
FIELD_ALIASES: Dict[str, str] = {
    "毛重": "gross",
    "gross": "gross",
    "g": "gross",
    "托盘重": "tare",
    "皮重": "tare",
    "tare": "tare",
    "t": "tare",
    "申报净重": "declared",
    "净重": "declared",
    "declared": "declared",
    "d": "declared",
}

FIELDS: Tuple[str, ...] = ("gross", "tare", "declared")
FIELD_LABELS: Dict[str, str] = {
    "gross": "毛重",
    "tare": "托盘重",
    "declared": "申报净重",
}

MAX_VALUE = 1_000_000  # 各列取值上界（克）

_COMPARATORS = ("==", "!=", "<>", ">=", "<=", "=", ">", "<")
# 条件分隔：中文分号/逗号、英文分号/逗号、换行、且、AND（大小写不敏感）
_COND_SPLIT_RE = re.compile(r"\s*(?:;|；|,|，|且|and)\s*", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"(?P<num>\d+)"
    r"|(?P<name>[^\s=!<>+\-]+)"
    r"|(?P<op>==|!=|<>|>=|<=|=|>|<|\+|-)"
)


class RuleParseError(ValueError):
    """规则文本解析失败，消息中带行号与出错位置。"""


@dataclass(frozen=True)
class Linear:
    """整数系数的线性表达式：coeffs·字段 + const。"""

    coeffs: Dict[str, int]
    const: int = 0

    def eval(self, values: Dict[str, int]) -> int:
        return sum(c * values[name] for name, c in self.coeffs.items()) + self.const

    def to_text(self) -> str:
        parts: List[str] = []
        for name in FIELDS:
            c = self.coeffs.get(name, 0)
            if c == 0:
                continue
            label = FIELD_LABELS[name]
            if not parts:
                parts.append(label if c == 1 else f"{c}{label}" if c > 0 else f"-{label}"
                           if c == -1 else f"-{abs(c)}{label}")
            else:
                parts.append(f"+ {label}" if c == 1 else f"- {label}" if c == -1
                             else f"+ {c}{label}" if c > 0 else f"- {abs(c)}{label}")
        if self.const or not parts:
            sign = "+" if self.const >= 0 else "-"
            if not parts:
                parts.append(str(self.const))
            else:
                parts.append(f"{sign} {abs(self.const)}")
        return " ".join(parts)


@dataclass(frozen=True)
class Condition:
    """一条比较：left op right。"""

    left: Linear
    op: str
    right: Linear

    def eval(self, values: Dict[str, int]) -> bool:
        a, b = self.left.eval(values), self.right.eval(values)
        return _COMPARE_FUNCS[self.op](a, b)

    def to_text(self) -> str:
        op = "=" if self.op == "=" else self.op
        return f"{self.left.to_text()} {op} {self.right.to_text()}"


_COMPARE_FUNCS = {
    "=": lambda a, b: a == b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<>": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


@dataclass(frozen=True)
class Rule:
    """一条带编号的拒收规则，内部所有比较同时成立（AND）才触发。"""

    rule_id: str
    conditions: Tuple[Condition, ...]
    source: str = ""

    def fires(self, values: Dict[str, int]) -> bool:
        return all(cond.eval(values) for cond in self.conditions)

    def to_text(self) -> str:
        return "；".join(c.to_text() for c in self.conditions)


# ---------------------------------------------------------------- 解析器

class _Tokenizer:
    def __init__(self, text: str):
        self.tokens: List[Tuple[str, str]] = []
        pos = 0
        n = len(text)
        while pos < n:
            ch = text[pos]
            if ch.isspace():
                pos += 1
                continue
            m = _TOKEN_RE.match(text, pos)
            if not m:
                raise RuleParseError(f"无法识别的字符 “{ch}”（第 {pos + 1} 列）")
            kind = m.lastgroup
            assert kind is not None
            self.tokens.append((kind, m.group()))
            pos = m.end()
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok


def _parse_linear(tk: _Tokenizer) -> Linear:
    """linear := sign? term (('+'|'-') term)*"""
    coeffs: Dict[str, int] = {}
    const = 0
    sign = 1
    kind, val = tk.peek()
    if kind == "op" and val in ("+", "-"):
        sign = -1 if val == "-" else 1
        tk.next()

    def _require_term():
        k, v = tk.next()
        if k == "num":
            return ("num", int(v))
        if k == "name":
            key = v.lower()
            if key not in FIELD_ALIASES:
                raise RuleParseError(f"未知字段 “{v}”，可用字段：毛重 / 托盘重 / 申报净重")
            return ("field", FIELD_ALIASES[key])
        raise RuleParseError("表达式应以字段名或整数开头")

    kind, val = _require_term()
    if kind == "num":
        const += sign * val
    else:
        coeffs[val] = coeffs.get(val, 0) + sign

    while True:
        k, v = tk.peek()
        if k != "op" or v not in ("+", "-"):
            break
        tk.next()
        term_sign = -1 if v == "-" else 1
        kind, val = _require_term()
        if kind == "num":
            const += term_sign * val
        else:
            coeffs[val] = coeffs.get(val, 0) + term_sign

    return Linear(coeffs=coeffs, const=const)


def _parse_condition(text: str) -> Condition:
    tk = _Tokenizer(text)
    left = _parse_linear(tk)
    kind, op = tk.next()
    if kind != "op" or op not in _COMPARATORS:
        raise RuleParseError(f"缺少比较符（= != > >= < <=），已解析到：{left.to_text()}")
    right = _parse_linear(tk)
    extra, _ = tk.next()
    if extra is not None:
        raise RuleParseError(f"比较式尾部有多余内容：{text.strip()}")
    return Condition(left=left, op=op, right=right)


def _parse_rule_body(body: str) -> Tuple[Condition, ...]:
    pieces = [p.strip() for p in _COND_SPLIT_RE.split(body) if p.strip()]
    if not pieces:
        raise RuleParseError("规则没有任何比较式")
    return tuple(_parse_condition(p) for p in pieces)


def parse_rules_text(text: str) -> List[Rule]:
    """解析整段规则文本，返回规则列表；重复编号 / 空规则 / 语法错都抛错。"""
    rules: List[Rule] = []
    seen: Dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise RuleParseError(f"第 {lineno} 行缺少编号分隔符 “|”：{line}")
        rid, body = line.split("|", 1)
        rid = rid.strip()
        if not rid:
            raise RuleParseError(f"第 {lineno} 行规则编号为空")
        if rid in seen:
            raise RuleParseError(f"规则编号 “{rid}” 重复（第 {seen[rid]} 行与第 {lineno} 行）")
        try:
            conds = _parse_rule_body(body)
        except RuleParseError as exc:
            raise RuleParseError(f"第 {lineno} 行（规则 {rid}）：{exc}") from None
        seen[rid] = lineno
        rules.append(Rule(rule_id=rid, conditions=conds, source=line))
    if not rules:
        raise RuleParseError("至少需要录入一条规则")
    return rules


def validate_bounds(low: int, high: int, label: str) -> None:
    if not (0 <= low <= MAX_VALUE and 0 <= high <= MAX_VALUE):
        raise RuleParseError(f"{label} 必须是 0 到 {MAX_VALUE:,} 之间的整数")
    if low > high:
        raise RuleParseError(f"{label} 的下限不能大于上限")
