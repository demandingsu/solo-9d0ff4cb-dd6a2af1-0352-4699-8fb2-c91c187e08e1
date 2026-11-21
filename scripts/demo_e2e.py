"""端到端手工演示：规则互相覆盖场景，直接打印求解结论与 CSV。"""

from app.replay import build_rows, render_csv, replay_csv
from app.rules import parse_rules_text
from app.solver import STATUS_JOINT, STATUS_NEVER, STATUS_SOLO, solve_rules

RULES_TEXT = """\
# 演示规则集（单位：克），每行一条，规则内比较同时成立
R001 | 毛重 - 托盘重 - 申报净重 > 500
R002 | 毛重 - 托盘重 - 申报净重 < -500
R003 | 毛重 - 托盘重 - 申报净重 < -1000
R004 | 毛重 < 托盘重
R005 | 毛重 > 1000000
R006 | 毛重 >= 0；毛重 <= 0
"""

BOUNDS = {f: (0, 1_000_000) for f in ("gross", "tare", "declared")}

rules = parse_rules_text(RULES_TEXT)
results = solve_rules(rules, BOUNDS)

label = {STATUS_SOLO: "可单独触发", STATUS_JOINT: "只能连带触发", STATUS_NEVER: "从不触发"}
print("=== 逐条求解结论 ===")
for r in results:
    print(f"[{r.rule.rule_id}] {label[r.status]}  {r.rule.to_text()}")
    if r.row is not None:
        print(f"      示例行: 毛重={r.row['gross']} 托盘重={r.row['tare']} "
              f"申报净重={r.row['declared']} 实际触发={r.fired_ids}")
        if r.status == STATUS_JOINT:
            print(f"      连带编号: {r.extra_ids}（{r.reason}）")
    else:
        print(f"      原因: {r.reason}")

csv_text = "\ufeff" + render_csv(build_rows(results))
print("\n=== 生成的称重 CSV ===")
print(csv_text)

print("=== 回放核对（独立重算触发编号）===")
report = replay_csv(csv_text, rules)
for row in report.rows:
    print(f"{row.row_id}: 实际触发={row.fired_ids} 预期={row.expected_ids} 一致={row.match}")
print("解析错误:", report.errors or "无")

# 不变量自检
by_id = {r.rule.rule_id: r for r in results}
assert by_id["R001"].status == STATUS_SOLO
assert by_id["R002"].status == STATUS_SOLO
assert by_id["R003"].status == STATUS_JOINT and by_id["R003"].extra_ids == ["R002"]
assert by_id["R004"].status == STATUS_SOLO
assert by_id["R005"].status == STATUS_NEVER
assert by_id["R006"].status == STATUS_SOLO
assert report.mismatch_count == 0 and not report.errors
print("\n全部不变量断言通过 ✓")
