"""称重 CSV 的生成与回放。

CSV 列（带 BOM，Excel 友好）：
  row_id, gross_g, tare_g, declared_net_g, expected_rule_ids, fired_rule_ids
回放时只要求前三列，表头支持中英文别名；expected_rule_ids 若存在则用于核对。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .rules import FIELDS, MAX_VALUE, Rule
from .solver import RuleResult, STATUS_JOINT, STATUS_SOLO

CSV_HEADER = [
    "row_id",
    "gross_g",
    "tare_g",
    "declared_net_g",
    "expected_rule_ids",
    "fired_rule_ids",
]

_HEADER_ALIASES = {
    "row_id": "row_id",
    "编号": "row_id",
    "行号": "row_id",
    "gross_g": "gross",
    "gross": "gross",
    "毛重": "gross",
    "毛重(g)": "gross",
    "毛重（g）": "gross",
    "毛重_g": "gross",
    "tare_g": "tare",
    "tare": "tare",
    "托盘重": "tare",
    "托盘重(g)": "tare",
    "托盘重（g）": "tare",
    "托盘重_g": "tare",
    "皮重": "tare",
    "declared_net_g": "declared",
    "declared": "declared",
    "申报净重": "declared",
    "申报净重(g)": "declared",
    "申报净重（g）": "declared",
    "净重": "declared",
    "净重_g": "declared",
    "expected_rule_ids": "expected_rule_ids",
    "预期触发规则": "expected_rule_ids",
    "应触发规则": "expected_rule_ids",
    "fired_rule_ids": "fired_rule_ids",
    "实际触发规则": "fired_rule_ids",
}


@dataclass
class ReplayRow:
    row_id: str
    values: Dict[str, int]
    fired_ids: List[str]
    expected_ids: List[str]
    match: Optional[bool]  # None 表示 CSV 未提供预期列


@dataclass
class ReplayReport:
    rows: List[ReplayRow]
    errors: List[str]

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.match is True)

    @property
    def mismatch_count(self) -> int:
        return sum(1 for r in self.rows if r.match is False)


def build_rows(results: Sequence[RuleResult]) -> List[Dict[str, str]]:
    """把求解结果转成 CSV 行字典；never 规则不产生数据行。

    row_id 直接采用目标规则编号，保证数据行与规则一一对应、不错位。
    """
    rows: List[Dict[str, str]] = []
    for res in results:
        if res.row is None:
            continue
        r = res.row
        rows.append(
            {
                "row_id": res.rule.rule_id,
                "gross_g": str(r["gross"]),
                "tare_g": str(r["tare"]),
                "declared_net_g": str(r["declared"]),
                "expected_rule_ids": "|".join(res.fired_ids),
                "fired_rule_ids": "|".join(res.fired_ids),
            }
        )
    return rows


def render_csv(rows: Sequence[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _parse_int(raw: str, lineno: int, col: str) -> int:
    raw = raw.strip()
    if not raw:
        raise ValueError(f"第 {lineno} 行 {col} 为空")
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"第 {lineno} 行 {col} 不是整数：{raw}") from None
    if not 0 <= v <= MAX_VALUE:
        raise ValueError(f"第 {lineno} 行 {col} 超出允许范围 0~{MAX_VALUE:,}：{v}")
    return v


def _parse_id_list(raw: str) -> List[str]:
    return [part.strip() for part in raw.replace(",", "|").replace(";", "|").replace("，", "|").split("|") if part.strip()]


def replay_csv(text: str, rules: List[Rule]) -> ReplayReport:
    """回放称重 CSV，按给定规则重新计算实际触发的规则编号。"""
    errors: List[str] = []
    report_rows: List[ReplayRow] = []

    # 兼容带 BOM 的文件
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return ReplayReport(rows=[], errors=["CSV 文件为空"])

    normalized = [_HEADER_ALIASES.get(h.strip().lower(), h.strip()) for h in header]
    missing = [f for f in FIELDS if f not in normalized]
    if missing:
        return ReplayReport(
            rows=[],
            errors=[f"CSV 表头缺少必需列：{', '.join(missing)}（实际表头：{', '.join(header)}）"],
        )

    index = {name: i for i, name in enumerate(normalized)}
    id_index = normalized.index("row_id") if "row_id" in normalized else None
    exp_index = normalized.index("expected_rule_ids") if "expected_rule_ids" in normalized else None

    rule_by_id = {r.rule_id: r for r in rules}

    for offset, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        try:
            values = {
                f: _parse_int(record[index[f]], offset, f)
                for f in FIELDS
            }
        except (ValueError, IndexError) as exc:
            errors.append(str(exc) if isinstance(exc, ValueError) else f"第 {offset} 行列数不足")
            continue

        fired = [r.rule_id for r in rules if r.fires(values)]
        expected: List[str] = []
        match: Optional[bool] = None
        if exp_index is not None and exp_index < len(record):
            expected = _parse_id_list(record[exp_index])
            unknown = [rid for rid in expected if rid not in rule_by_id]
            if unknown:
                errors.append(f"第 {offset} 行预期规则编号不存在：{', '.join(unknown)}")
            match = set(expected) == set(fired)

        row_id = record[id_index].strip() if id_index is not None and id_index < len(record) else f"第{offset}行"
        if not row_id:
            row_id = f"第{offset}行"
        report_rows.append(
            ReplayRow(
                row_id=row_id,
                values=values,
                fired_ids=fired,
                expected_ids=expected,
                match=match,
            )
        )

    return ReplayReport(rows=report_rows, errors=errors)


def summarize_for_template(results: Sequence[RuleResult]) -> Tuple[int, int, int]:
    solo = sum(1 for r in results if r.status == STATUS_SOLO)
    joint = sum(1 for r in results if r.status == STATUS_JOINT)
    never = sum(1 for r in results if r.status not in (STATUS_SOLO, STATUS_JOINT))
    return solo, joint, never
