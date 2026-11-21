"""主码迁移核对端到端演示（无需启动网页）。

覆盖三条主线：
  1. 无冲突批：待迁移行的“通过 / 主码确定 / 待核”判定与下载内容；
  2. 历史主码互相矛盾（成环）：指出记录、暂停下载；
  3. 网页更正主码：先改出环（暂停），再改成相容值（恢复下载）。

运行：python scripts/demo_migration.py
"""

from app.migration import (
    CONFLICT_LABELS,
    STATUS_LABELS,
    analyze,
    parse_weighing_csv,
    render_migration_csv,
)
from app.rules import parse_rules_text

RULES_TEXT = """\
A | 毛重 >= 1000
B | 毛重 >= 900
C | 托盘重 >= 100
D | 申报净重 >= 100
"""

HISTORY_OK = """\
记录号,毛重,托盘重,申报净重,主码
H001,1000,0,0,A
H002,1200,0,0,A
H003,500,0,0,
H004,900,0,0,B
H005,1000,200,100,A
H006,1200,200,100,A
"""

PENDING = """\
记录号,毛重,托盘重,申报净重
P001,1500,0,0
P002,500,0,0
P003,900,0,100
P004,1000,200,100
"""

# A、B 完全重叠（毛重 >= 900 时两者都命中）：
# C1 说 A 先于 B，C2 说 B 先于 A —— 无任何规则顺序能同时满足
CYCLE_RULES = "A | 毛重 >= 1000\nB | 毛重 >= 900\nC | 托盘重 >= 100\n"
CYCLE_HISTORY = """\
记录号,毛重,托盘重,申报净重,主码
C1,1000,0,0,A
C2,1200,0,0,B
"""

rules = parse_rules_text(RULES_TEXT)
history = parse_weighing_csv(HISTORY_OK, with_primary=True)
pending = parse_weighing_csv(PENDING, with_primary=False)


def show(report, title):
    print(f"\n=== {title} ===")
    print("先后关系：")
    for src, dst, records in report.order_edges:
        print(f"  {src} 先于 {dst}  ← 记录 {'、'.join(records)}")
    if not report.order_edges:
        print("  （无可用的先后关系）")
    print("冲突：")
    if not report.conflicts:
        print("  无")
    for conflict in report.conflicts:
        print(
            f"  [{CONFLICT_LABELS[conflict.kind]}] 记录 {conflict.record_text}："
            f"{conflict.detail}"
        )
    print("待迁移判定：")
    for d in report.pending:
        primary = d.primary_code or ("-" if d.status != "review" else "？")
        print(
            f"  {d.row.record_no} 命中={d.fired_ids or '无'} "
            f"结论={STATUS_LABELS[d.status]} 主码={primary}"
        )
    print(f"下载：{'允许' if report.download_allowed else '暂停'}，"
          f"{len(report.download_rows)} 行")
    return report


report = show(analyze(rules, history, pending), "1. 无冲突批")

assert not report.has_conflict and report.download_allowed
by_id = {d.row.record_no: d for d in report.pending}
assert by_id["P001"].primary_code == "A"
assert by_id["P002"].status == "pass"
assert by_id["P003"].status == "review"
assert [r["record_no"] for r in report.download_rows] == ["P001", "P002", "P004"]

print("\n=== 下载内容（通过 + 主码确定）===")
print(render_migration_csv(report.download_rows).strip())

cycle = analyze(
    parse_rules_text(CYCLE_RULES),
    parse_weighing_csv(CYCLE_HISTORY, with_primary=True),
    parse_weighing_csv("记录号,毛重,托盘重,申报净重\nX1,1500,0,0\n", with_primary=False),
)
show(cycle, "2. 历史主码互相矛盾：成环，暂停下载")
assert cycle.has_conflict and not cycle.download_allowed
assert cycle.conflicts[0].record_nos == ["C1", "C2"]

# 网页上的“更正主码”等价于把 corrections 传给 analyze。
# 先改出一个新的冲突：C2 改成 C（C 未命中，历史主码未命中）
broken = analyze(
    parse_rules_text(CYCLE_RULES),
    parse_weighing_csv(CYCLE_HISTORY, with_primary=True),
    parse_weighing_csv("记录号,毛重,托盘重,申报净重\nX1,1500,0,0\n", with_primary=False),
    {"C2": "C"},
)
show(broken, "3a. 误改成 C：历史主码未命中，仍暂停下载")
assert broken.has_conflict and not broken.download_allowed

# 再改成相容值：C2 也说 A 先（A、B 同时命中且 A 在首位）
fixed = analyze(
    parse_rules_text(CYCLE_RULES),
    parse_weighing_csv(CYCLE_HISTORY, with_primary=True),
    parse_weighing_csv("记录号,毛重,托盘重,申报净重\nX1,1500,0,0\n", with_primary=False),
    {"C2": "A"},
)
show(fixed, "3b. 更正 C2=A：环解除，恢复下载")
assert not fixed.has_conflict and fixed.download_allowed
assert fixed.pending[0].primary_code == "A"

print("\n全部不变量断言通过 ✓")
