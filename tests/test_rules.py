"""规则解析器单元测试。"""

import pytest

from app.rules import (
    Condition,
    Linear,
    RuleParseError,
    parse_rules_text,
)


def test_parse_basic_rule_with_aliases():
    rules = parse_rules_text("R1 | 毛重 - 托盘重 > 申报净重 + 100")
    assert len(rules) == 1
    r = rules[0]
    assert r.rule_id == "R1"
    assert len(r.conditions) == 1
    c = r.conditions[0]
    assert c.op == ">"
    assert c.left.coeffs == {"gross": 1, "tare": -1}
    assert c.left.const == 0
    assert c.right.coeffs == {"declared": 1}
    assert c.right.const == 100


def test_parse_english_aliases_and_all_comparators():
    text = "\n".join(
        [
            "A | G = T",
            "B | G == T",
            "C | G != T",
            "D | G <> T",
            "E | G >= T",
            "F | G <= T",
            "G | G > 1",
            "H | G < 1",
        ]
    )
    rules = parse_rules_text(text)
    assert [r.rule_id for r in rules] == list("ABCDEFGH")
    assert {r.conditions[0].op for r in rules} == {"=", "==", "!=", "<>", ">=", "<=", ">", "<"}


def test_parse_multiple_conditions_and_separators():
    for sep in ("；", ";", ",", "且", "AND", "and"):
        rules = parse_rules_text(f"R9 | 毛重 > 0 {sep} 托盘重 >= 0")
        assert len(rules[0].conditions) == 2, sep


def test_parse_constant_only_expressions():
    rules = parse_rules_text("R1 | 100 - 50 > 毛重 - 毛重")
    c = rules[0].conditions[0]
    assert c.left.const == 50
    assert c.left.coeffs == {}
    assert c.right.coeffs == {"gross": 0}  # 抵消后系数为 0（键保留）
    assert c.right.const == 0
    assert rules[0].fires({"gross": 0, "tare": 0, "declared": 0})


def test_linear_eval_and_rule_fire():
    rules = parse_rules_text("R1 | 毛重 - 托盘重 - 申报净重 > 500")
    r = rules[0]
    assert r.fires({"gross": 2000, "tare": 300, "declared": 1000})  # 差 700
    assert not r.fires({"gross": 1500, "tare": 300, "declared": 1000})  # 差 200


def test_unary_minus_constant_supported():
    # 允许带前导符号的整数：毛重 > -100 在 0 下界下恒真，但语法合法
    rules = parse_rules_text("R1 | 毛重 > -100")
    assert rules[0].conditions[0].right.const == -100
    assert rules[0].fires({"gross": 0, "tare": 0, "declared": 0})


def test_double_sign_rejected():
    with pytest.raises(RuleParseError):
        parse_rules_text("R1 | 毛重 > --100")


def test_trailing_operator_rejected():
    with pytest.raises(RuleParseError):
        parse_rules_text("R1 | 毛重 > 托盘重 +")


def test_unknown_field_rejected():
    with pytest.raises(RuleParseError, match="未知字段"):
        parse_rules_text("R1 | 净重差 > 10")


def test_missing_comparator_rejected():
    with pytest.raises(RuleParseError, match="比较符"):
        parse_rules_text("R1 | 毛重 + 托盘重")


def test_duplicate_id_rejected():
    with pytest.raises(RuleParseError, match="重复"):
        parse_rules_text("R1 | 毛重 > 0\nR1 | 托盘重 > 0")


def test_missing_pipe_rejected():
    with pytest.raises(RuleParseError, match="分隔符"):
        parse_rules_text("R1 毛重 > 0")


def test_comment_and_blank_lines_ignored():
    rules = parse_rules_text("# 注释\n\nR1 | 毛重 > 0\n")
    assert len(rules) == 1


def test_to_text_roundtrip_readable():
    rules = parse_rules_text("R1 | 毛重 - 托盘重 - 申报净重 > 500")
    assert rules[0].to_text() == "毛重 - 托盘重 - 申报净重 > 500"
    c = rules[0].conditions[0]
    assert c.left.to_text() == "毛重 - 托盘重 - 申报净重"
    assert c.right.to_text() == "500"


def test_combined_coefficients():
    rules = parse_rules_text("R1 | 毛重 + 毛重 - 托盘重 >= 0")
    c = rules[0].conditions[0]
    assert c.left.coeffs == {"gross": 2, "tare": -1}


def test_validate_bounds():
    from app.rules import validate_bounds

    validate_bounds(0, 1_000_000, "毛重")
    with pytest.raises(RuleParseError):
        validate_bounds(-1, 10, "毛重")
    with pytest.raises(RuleParseError):
        validate_bounds(0, 1_000_001, "毛重")
    with pytest.raises(RuleParseError):
        validate_bounds(10, 5, "毛重")
