"""Tests for ledger_parser — FR-9."""
from unittest.mock import MagicMock

import pytest

from ledger_parser import LedgerParser, parse_amount


# ── parse_amount ──────────────────────────────────────────────────────────────


def test_parse_plain_integer():
    assert parse_amount("50000") == 50000


def test_parse_dot_thousand_separator():
    assert parse_amount("50.000") == 50000


def test_parse_comma_thousand_separator():
    assert parse_amount("50,000") == 50000


def test_parse_k_suffix():
    assert parse_amount("50k") == 50000


def test_parse_tr_suffix():
    assert parse_amount("2tr") == 2_000_000


def test_parse_decimal_tr_suffix():
    assert parse_amount("1.5tr") == 1_500_000


def test_parse_m_suffix():
    assert parse_amount("2m") == 2_000_000


def test_parse_amount_strips_whitespace():
    assert parse_amount("  100k  ") == 100_000


def test_parse_amount_rejects_non_numeric():
    with pytest.raises(ValueError):
        parse_amount("abc")


def test_parse_amount_rejects_zero():
    with pytest.raises(ValueError):
        parse_amount("0")


# ── classify_category — fast-path ─────────────────────────────────────────────

CATEGORIES = [
    {"id": 1, "name": "Ăn uống", "kind": "expense"},
    {"id": 2, "name": "Di chuyển", "kind": "expense"},
    {"id": 3, "name": "Lương", "kind": "income"},
]

CATEGORIES_WITH_NHAN = [
    {"id": 1, "name": "Ăn uống", "kind": "expense"},
    {"id": 2, "name": "Di chuyển", "kind": "expense"},
    {"id": 9, "name": "Mua sắm cá nhân", "kind": "expense"},
    {"id": 3, "name": "Lương", "kind": "income"},
]


@pytest.fixture()
def parser_no_llm():
    """Parser with no LLM client — LLM must not be called in fast-path tests."""
    return LedgerParser(client=None)


def test_fast_path_exact_match_returns_category_id(parser_no_llm):
    result = parser_no_llm.classify_category("ăn trưa với bạn bè", CATEGORIES)
    assert result == 1  # "Ăn uống"


def test_fast_path_no_match_returns_none(parser_no_llm):
    result = parser_no_llm.classify_category("mua đồ xyz không rõ", CATEGORIES)
    assert result is None


def test_fast_path_word_match_not_substring(parser_no_llm):
    # "an" from "ăn trưa" must NOT match "nhân" in "Mua sắm cá nhân" (substring false positive)
    # With "Mua sắm cá nhân" present, "ăn trưa" should still resolve to "Ăn uống" (1 match)
    result = parser_no_llm.classify_category("ăn trưa", CATEGORIES_WITH_NHAN)
    assert result == 1  # "Ăn uống" — not None due to false multi-match


def test_fast_path_an_sang_word_match(parser_no_llm):
    result = parser_no_llm.classify_category("ăn sáng", CATEGORIES_WITH_NHAN)
    assert result == 1  # "Ăn uống"


# ── classify_category — LLM fallback ─────────────────────────────────────────


def _make_llm_response(category_id: int, confidence: float) -> MagicMock:
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.name = "classify_category"
    tool_use.input = {"category_id": category_id, "confidence": confidence}

    msg = MagicMock()
    msg.stop_reason = "tool_use"
    msg.content = [tool_use]
    return msg


def test_llm_fallback_high_confidence_returns_id():
    client = MagicMock()
    client.messages.create.return_value = _make_llm_response(2, 0.85)
    parser = LedgerParser(client=client)

    ambiguous_cats = [
        {"id": 1, "name": "Ăn uống", "kind": "expense"},
        {"id": 2, "name": "Cà phê", "kind": "expense"},
    ]
    # "rang sang" matches neither category → LLM fallback triggered
    result = parser.classify_category("rang sang hoa qua gi do", ambiguous_cats)
    assert result == 2
    client.messages.create.assert_called_once()


def test_llm_fallback_low_confidence_returns_none():
    client = MagicMock()
    client.messages.create.return_value = _make_llm_response(1, 0.4)
    parser = LedgerParser(client=client)

    result = parser.classify_category("chi tiêu linh tinh", CATEGORIES)
    assert result is None


def test_llm_fallback_called_when_multiple_matches():
    client = MagicMock()
    client.messages.create.return_value = _make_llm_response(1, 0.9)
    parser = LedgerParser(client=client)

    overlapping = [
        {"id": 1, "name": "ăn", "kind": "expense"},
        {"id": 2, "name": "ăn uống", "kind": "expense"},
    ]
    parser.classify_category("ăn uống buổi trưa", overlapping)
    client.messages.create.assert_called_once()


def test_llm_returns_invalid_id_falls_back_to_none():
    # LLM hallucinates a category_id not in the provided list → must return None
    # Use "cafe" — no fast-path match → forces LLM fallback
    client = MagicMock()
    client.messages.create.return_value = _make_llm_response(999, 0.95)
    parser = LedgerParser(client=client)

    result = parser.classify_category("cafe", CATEGORIES)
    assert result is None  # id=999 not in CATEGORIES → rejected


def test_llm_skipped_when_no_categories():
    # Empty category list → skip LLM entirely, return None immediately
    client = MagicMock()
    parser = LedgerParser(client=client)

    result = parser.classify_category("ăn sáng", [])
    assert result is None
    client.messages.create.assert_not_called()


# ── parse_command ─────────────────────────────────────────────────────────────


def test_parse_command_expense(parser_no_llm):
    result = parser_no_llm.parse_command("chi: 50k ăn trưa", CATEGORIES)
    assert result["kind"] == "expense"
    assert result["amount"] == 50_000
    assert result["description"] == "ăn trưa"


def test_parse_command_income(parser_no_llm):
    result = parser_no_llm.parse_command("thu: 5tr lương", CATEGORIES)
    assert result["kind"] == "income"
    assert result["amount"] == 5_000_000
    assert result["description"] == "lương"


def test_parse_command_no_description(parser_no_llm):
    result = parser_no_llm.parse_command("chi: 100k", [])
    assert result["kind"] == "expense"
    assert result["amount"] == 100_000
    assert result["description"] == ""


def test_parse_command_rejects_missing_amount(parser_no_llm):
    with pytest.raises(ValueError):
        parser_no_llm.parse_command("chi: ăn trưa", [])


def test_parse_command_rejects_unknown_prefix(parser_no_llm):
    with pytest.raises(ValueError):
        parser_no_llm.parse_command("mua: 50k gì đó", [])
