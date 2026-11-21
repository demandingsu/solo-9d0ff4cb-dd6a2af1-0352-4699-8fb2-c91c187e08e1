"""旧数仓称重明细的主码迁移核对。

旧系统只保存“首条命中的拒收码”（主码），没有保存规则顺序。本模块：

1. 读取历史称重 CSV（三列重量 + 记录号 + 历史主码）与待迁移 CSV（三列重量 + 记录号）；
2. 用历史行的主码与“该行所有命中规则”推断先后关系：
   主码对应的规则必须先于该行其他所有命中规则；
3. 校验历史主码是否命中、空主码是否真的没有命中、先后关系是否成环；
4. 无冲突时，按所有相容顺序判断待迁移行的首条命中：
   无命中记“通过”，首条命中在所有相容顺序中唯一则“主码确定”，否则“待核”；
5. 只允许下载“通过”和“主码确定”的行。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .replay import HEADER_ALIASES, parse_int_cell
from .rules import FIELDS, Rule

STATUS_PASS = "pass"
STATUS_DETERMINED = "determined"
STATUS_REVIEW = "review"

STATUS_LABELS: Dict[str, str] = {
    STATUS_PASS: "通过",
    STATUS_DETERMINED: "主码确定",
    STATUS_REVIEW: "待核",
}

CONFLICT_PRIMARY_NOT_FIRED = "primary_not_fired"
CONFLICT_EMPTY_CODE_FIRED = "empty_code_fired"
CONFLICT_ORDER_CYCLE = "order_cycle"

CONFLICT_LABELS: Dict[str, str] = {
    CONFLICT_PRIMARY_NOT_FIRED: "历史主码未命中",
    CONFLICT_EMPTY_CODE_FIRED: "空码实际有命中",
    CONFLICT_ORDER_CYCLE: "先后关系成环",
}

MIGRATION_HEADER = [
    "record_no",
    "gross_g",
    "tare_g",
    "declared_net_g",
    "primary_code",
    "status",
]


class MigrationInputError(ValueError):
    """上传文件或表头无法处理。"""


# ------------------------------------------------------------------ 读取 CSV


@dataclass(frozen=True)
class WeighRow:
    record_no: str
    line_no: int
    values: Dict[str, int]
    primary_code: str = ""


def _normalize_header(header: Sequence[str]) -> List[str]:
    normalized: List[str] = []
    for cell in header:
        key = HEADER_ALIASES.get(cell.strip().lower(), cell.strip())
        if key == "row_id":  # 回放用的行号别名，在这里统一叫记录号
            key = "record_no"
        normalized.append(key)
    return normalized


def _cell(record: Sequence[str], index: int, lineno: int, label: str) -> str:
    if index >= len(record):
        raise MigrationInputError(f"第 {lineno} 行缺少 {label} 列的值")
    return record[index]


def parse_weighing_csv(
    text: str,
    *,
    with_primary: bool,
    label: str = "称重 CSV",
) -> List[WeighRow]:
    """解析称重 CSV；任一表头/单元格问题都抛 MigrationInputError。

    with_primary=True 用于历史文件，要求“主码”列，并校验记录号唯一
    （网页更正按记录号定位）。待迁移文件只需记录号与三列重量。
    """
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise MigrationInputError(f"{label} 为空") from None

    normalized = _normalize_header(header)
    index: Dict[str, int] = {}
    for i, name in enumerate(normalized):
        index.setdefault(name, i)

    required = ["record_no", *FIELDS]
    if with_primary:
        required.append("primary_code")
    missing = [name for name in required if name not in index]
    if missing:
        labels = {
            "record_no": "记录号",
            "gross": "毛重",
            "tare": "托盘重",
            "declared": "申报净重",
            "primary_code": "主码",
        }
        raise MigrationInputError(
            f"{label} 表头缺少必需列：{'、'.join(labels[m] for m in missing)}"
            f"（实际表头：{', '.join(header)}）"
        )

    rows: List[WeighRow] = []
    first_line_by_record: Dict[str, int] = {}
    for lineno, record in enumerate(reader, start=2):
        if not any(cell.strip() for cell in record):
            continue
        record_no = _cell(record, index["record_no"], lineno, "记录号").strip()
        if not record_no:
            raise MigrationInputError(f"{label} 第 {lineno} 行记录号为空")
        if record_no in first_line_by_record:
            raise MigrationInputError(
                f"{label} 第 {lineno} 行记录号 “{record_no}” 重复"
                f"（首次出现在第 {first_line_by_record[record_no]} 行）；"
                "记录号必须唯一"
            )
        first_line_by_record[record_no] = lineno
        try:
            values = {
                name: parse_int_cell(
                    _cell(record, index[name], lineno, name), lineno, name
                )
                for name in FIELDS
            }
        except ValueError as exc:
            raise MigrationInputError(f"{label}：{exc}") from None
        primary = ""
        if with_primary:
            primary = _cell(
                record, index["primary_code"], lineno, "主码"
            ).strip()
        rows.append(
            WeighRow(
                record_no=record_no,
                line_no=lineno,
                values=values,
                primary_code=primary,
            )
        )
    return rows


# ------------------------------------------------------------------ 推断与判定


@dataclass
class Conflict:
    kind: str
    record_nos: List[str]
    rule_ids: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def label(self) -> str:
        return CONFLICT_LABELS.get(self.kind, self.kind)

    @property
    def record_text(self) -> str:
        return "、".join(self.record_nos)


@dataclass
class HistoryView:
    row: WeighRow
    effective_primary: str
    fired_ids: List[str]
    conflicts: List[Conflict] = field(default_factory=list)

    @property
    def original_primary(self) -> str:
        return self.row.primary_code

    @property
    def corrected(self) -> bool:
        return self.effective_primary != self.row.primary_code

    @property
    def conflict_labels(self) -> List[str]:
        labels: List[str] = []
        for conflict in self.conflicts:
            if conflict.label not in labels:
                labels.append(conflict.label)
        return labels

    @property
    def conflict_reasons(self) -> List[str]:
        return [f"[{c.label}] {c.detail}" for c in self.conflicts]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


@dataclass
class PendingDecision:
    row: WeighRow
    fired_ids: List[str]
    status: str
    primary_code: str = ""
    candidates: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass
class MigrationReport:
    history: List[HistoryView]
    pending: List[PendingDecision]
    conflicts: List[Conflict]
    order_edges: List[Tuple[str, str, List[str]]]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    @property
    def download_allowed(self) -> bool:
        return not self.conflicts

    @property
    def download_decisions(self) -> List[PendingDecision]:
        return [
            d for d in self.pending if d.status in (STATUS_PASS, STATUS_DETERMINED)
        ]

    @property
    def download_rows(self) -> List[Dict[str, str]]:
        return [
            {
                "record_no": d.row.record_no,
                "gross_g": str(d.row.values["gross"]),
                "tare_g": str(d.row.values["tare"]),
                "declared_net_g": str(d.row.values["declared"]),
                "primary_code": d.primary_code,
                "status": d.status_label,
            }
            for d in self.download_decisions
        ]

    def counts(self) -> Dict[str, int]:
        result = {
            STATUS_PASS: 0,
            STATUS_DETERMINED: 0,
            STATUS_REVIEW: 0,
        }
        for decision in self.pending:
            result[decision.status] += 1
        return result


def _tarjan_scc(
    nodes: Sequence[str], edges: Dict[str, Set[str]]
) -> List[List[str]]:
    """Tarjan 强连通分量；迭代实现避免深递归。"""
    index_of: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    result: List[List[str]] = []
    counter = 0

    for start in nodes:
        if start in index_of:
            continue
        # 显式栈模拟递归，每层保存 (节点, 未遍历的后继迭代器)
        work: List[Tuple[str, Iterable[str]]] = [(start, iter(sorted(edges.get(start, ()))))]
        index_of[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            node, successors = work[-1]
            advanced = False
            for succ in successors:
                if succ not in index_of:
                    index_of[succ] = lowlink[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(sorted(edges.get(succ, ())))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[succ])
            if advanced:
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component: List[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(component)
    return result


def _forward_reach(edges: Dict[str, Set[str]], nodes: Iterable[str]) -> Dict[str, Set[str]]:
    reach: Dict[str, Set[str]] = {}
    for start in nodes:
        seen: Set[str] = set()
        stack = list(edges.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, ()))
        reach[start] = seen
    return reach


def analyze(
    rules: Sequence[Rule],
    history_rows: Sequence[WeighRow],
    pending_rows: Sequence[WeighRow],
    corrections: Optional[Dict[str, str]] = None,
) -> MigrationReport:
    """用历史主码推断规则先后关系，并判定待迁移行。

    corrections: 记录号 -> 网页更正后的主码（空字符串表示通过）。
    """
    corrections = corrections or {}
    rule_ids = [r.rule_id for r in rules]
    rule_id_set = set(rule_ids)
    history_order = {row.record_no: i for i, row in enumerate(history_rows)}

    edges: Dict[str, Set[str]] = {}
    edge_records: Dict[Tuple[str, str], List[str]] = {}
    conflicts: List[Conflict] = []
    history_views: List[HistoryView] = []

    for row in history_rows:
        code = corrections.get(row.record_no, row.primary_code)
        fired = [r.rule_id for r in rules if r.fires(row.values)]
        row_conflicts: List[Conflict] = []

        if code == "":
            if fired:
                row_conflicts.append(
                    Conflict(
                        kind=CONFLICT_EMPTY_CODE_FIRED,
                        record_nos=[row.record_no],
                        rule_ids=fired,
                        detail=(
                            "主码为空（应表示通过），"
                            f"但实际命中 {'、'.join(fired)}"
                        ),
                    )
                )
        elif code not in fired:
            if code not in rule_id_set:
                reason = f"主码 “{code}” 不是已录入的规则编号"
            else:
                reason = (
                    f"主码 {code} 未在该行命中"
                    f"（实际命中：{'、'.join(fired) if fired else '无'}）"
                )
            row_conflicts.append(
                Conflict(
                    kind=CONFLICT_PRIMARY_NOT_FIRED,
                    record_nos=[row.record_no],
                    rule_ids=fired,
                    detail=reason,
                )
            )
        else:
            for other in fired:
                if other == code:
                    continue
                edges.setdefault(code, set()).add(other)
                edge_records.setdefault((code, other), []).append(row.record_no)

        conflicts.extend(row_conflicts)
        history_views.append(
            HistoryView(
                row=row,
                effective_primary=code,
                fired_ids=fired,
                conflicts=row_conflicts,
            )
        )

    # 成环检测：只报告强连通分量内的边，并带上给出这些边的历史记录
    all_nodes = set(edges)
    for targets in edges.values():
        all_nodes.update(targets)
    components = _tarjan_scc(sorted(all_nodes), edges)
    for component in components:
        if len(component) < 2:
            continue
        member_set = set(component)
        intra: List[Tuple[str, str, List[str]]] = []
        cycle_records: Set[str] = set()
        for src in component:
            for dst in edges.get(src, set()):
                if dst not in member_set:
                    continue
                records = edge_records.get((src, dst), [])
                intra.append((src, dst, records))
                cycle_records.update(records)
        if not intra:
            continue
        intra.sort()
        ordered_records = sorted(cycle_records, key=lambda r: history_order.get(r, 10**9))
        rule_order = {rid: i for i, rid in enumerate(rule_ids)}
        ordered_rules = sorted(component, key=lambda r: rule_order.get(r, 10**9))
        detail = "；".join(
            f"{src} 先于 {dst}（记录 {'、'.join(records)}）"
            for src, dst, records in intra
        )
        cycle_conflict = Conflict(
            kind=CONFLICT_ORDER_CYCLE,
            record_nos=ordered_records,
            rule_ids=ordered_rules,
            detail=f"这些记录的先后关系无法同时成立：{detail}",
        )
        conflicts.append(cycle_conflict)
        record_set = set(ordered_records)
        for view in history_views:
            if view.row.record_no in record_set:
                view.conflicts.append(cycle_conflict)

    order_edges: List[Tuple[str, str, List[str]]] = []
    rule_order = {rid: i for i, rid in enumerate(rule_ids)}
    for (src, dst), records in sorted(
        edge_records.items(),
        key=lambda item: (
            rule_order.get(item[0][0], 10**9),
            rule_order.get(item[0][1], 10**9),
        ),
    ):
        order_edges.append((src, dst, records))

    reach = _forward_reach(edges, all_nodes)
    has_conflict = bool(conflicts)
    pending_decisions: List[PendingDecision] = []
    for row in pending_rows:
        fired = [r.rule_id for r in rules if r.fires(row.values)]
        if has_conflict:
            pending_decisions.append(
                PendingDecision(
                    row=row,
                    fired_ids=fired,
                    status=STATUS_REVIEW,
                    note="历史存在冲突，本批已暂停；冲突排除后再判定",
                )
            )
            continue
        if not fired:
            pending_decisions.append(
                PendingDecision(
                    row=row,
                    fired_ids=fired,
                    status=STATUS_PASS,
                    primary_code="",
                    note="未命中任何规则，记为通过",
                )
            )
            continue

        # 只有没有任何“已命中规则必须排在其前”的命中规则，才可能排在首位
        possible = [
            rid
            for rid in fired
            if not any(
                rid in reach.get(other, set()) for other in fired if other != rid
            )
        ]
        if len(possible) == 1:
            pending_decisions.append(
                PendingDecision(
                    row=row,
                    fired_ids=fired,
                    status=STATUS_DETERMINED,
                    primary_code=possible[0],
                    candidates=possible,
                    note=f"全部相容顺序中，首条命中均为 {possible[0]}",
                )
            )
        else:
            if possible:
                note = f"存在多种相容顺序，首条命中可能为 {'、'.join(possible)}"
            else:
                note = "先后关系互相矛盾，无法确定首条命中（请先排除冲突）"
            pending_decisions.append(
                PendingDecision(
                    row=row,
                    fired_ids=fired,
                    status=STATUS_REVIEW,
                    candidates=possible,
                    note=note,
                )
            )

    return MigrationReport(
        history=history_views,
        pending=pending_decisions,
        conflicts=conflicts,
        order_edges=order_edges,
    )


def correction_reason(report: MigrationReport, record_no: str) -> str:
    """某条更正当前涉及的冲突原因（无冲突时返回“未引入冲突”）。"""
    hits = [c for c in report.conflicts if record_no in c.record_nos]
    if not hits:
        return "未引入冲突"
    return "；".join(f"[{c.label}] {c.detail}" for c in hits)


def render_migration_csv(rows: Sequence[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=MIGRATION_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
