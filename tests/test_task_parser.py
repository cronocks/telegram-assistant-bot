"""Tests for task_parser.TaskParser.

All tests use an injected mock Anthropic client — no real API calls.

Covers:
  - Happy path: tool_use response → ParsedTask with correct fields.
  - Category detection: study, reminder, task default.
  - Recurring rule: weekly / daily / null (one-shot).
  - Past deadline → ParseError('past_deadline').
  - No tool_use block (text-only response) → ParseError('parse_failed').
  - Empty title / empty deadline in tool_use → ParseError('parse_failed').
  - Invalid deadline ISO format → ParseError('parse_failed').
  - LLM API exception → ParseError('llm_error').
  - morning_default is embedded in the system prompt.
  - now parameter controls both prompt content and validation reference.
  - Deadline exactly at now → rejected (must be strictly future).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from task_parser import ParseError, ParsedTask, TaskParser

VN_TZ = timezone(timedelta(hours=7))
NOW = datetime(2026, 5, 25, 10, 0, 0, tzinfo=VN_TZ)   # fixed reference time


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_client(tool_input: dict | None = None, *, raise_exc: Exception | None = None):
    """Return a mock Anthropic client whose messages.create behaves as specified.

    Args:
        tool_input: if not None, the mock returns a tool_use response with this input.
                    if None, the mock returns a text-only response.
        raise_exc:  if provided, messages.create raises this exception instead.
    """
    client = MagicMock()
    if raise_exc is not None:
        client.messages.create.side_effect = raise_exc
        return client

    if tool_input is not None:
        block = MagicMock()
        block.type = "tool_use"
        block.input = tool_input
    else:
        block = MagicMock()
        block.type = "text"
        block.text = "Vui lòng nói rõ deadline hơn."

    response = MagicMock()
    response.content = [block]
    client.messages.create.return_value = response
    return client


def _parser(tool_input: dict | None = None, *, raise_exc: Exception | None = None) -> TaskParser:
    return TaskParser(client=_make_client(tool_input, raise_exc=raise_exc))


def _future(hours: int = 5) -> str:
    """ISO 8601 string N hours in the future from NOW."""
    dt = NOW + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+07:00")


def _past(hours: int = 1) -> str:
    """ISO 8601 string N hours in the past from NOW."""
    dt = NOW - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+07:00")


# ═════════════════════════════════════════════════════════════════════════════
# Happy path
# ═════════════════════════════════════════════════════════════════════════════


class TestHappyPath:

    def test_returns_parsed_task(self):
        parser = _parser({"title": "Mua sữa", "deadline_iso": _future(), "category": "task"})
        result = parser.parse("mua sua 5h chieu mai", now=NOW)
        assert isinstance(result, ParsedTask)

    def test_title_preserved(self):
        parser = _parser({"title": "Mua sữa", "deadline_iso": _future(), "category": "task"})
        result = parser.parse("mua sua 5h chieu mai", now=NOW)
        assert result.title == "Mua sữa"

    def test_deadline_iso_preserved(self):
        dl = _future(3)
        parser = _parser({"title": "Task", "deadline_iso": dl, "category": "task"})
        result = parser.parse("any input", now=NOW)
        assert result.deadline_iso == dl

    def test_category_task(self):
        parser = _parser({"title": "Làm báo cáo", "deadline_iso": _future(), "category": "task"})
        result = parser.parse("lam bao cao ngay mai", now=NOW)
        assert result.category == "task"

    def test_category_study(self):
        parser = _parser({"title": "Học tiếng Anh", "deadline_iso": _future(), "category": "study"})
        result = parser.parse("hoc tieng anh 7h sang mai", now=NOW)
        assert result.category == "study"

    def test_category_reminder(self):
        parser = _parser({"title": "Uống thuốc", "deadline_iso": _future(), "category": "reminder"})
        result = parser.parse("nhac uong thuoc 8h toi", now=NOW)
        assert result.category == "reminder"

    def test_one_shot_has_no_recurring_rule(self):
        parser = _parser({"title": "Mua sữa", "deadline_iso": _future(), "category": "task"})
        result = parser.parse("mua sua chieu mai", now=NOW)
        assert result.recurring_rule is None

    def test_recurring_rule_weekly(self):
        parser = _parser({
            "title": "Học tiếng Anh",
            "deadline_iso": _future(),
            "category": "study",
            "recurring_rule": "weekly:MON,WED,FRI@07:00",
        })
        result = parser.parse("hoc tieng anh thu 2-6 luc 7h", now=NOW)
        assert result.recurring_rule == "weekly:MON,WED,FRI@07:00"

    def test_recurring_rule_daily(self):
        parser = _parser({
            "title": "Tập thể dục",
            "deadline_iso": _future(),
            "category": "task",
            "recurring_rule": "daily@06:30",
        })
        result = parser.parse("tap the duc moi sang 6h30", now=NOW)
        assert result.recurring_rule == "daily@06:30"

    def test_empty_string_recurring_rule_becomes_none(self):
        # LLM may return "" instead of null — should be treated as None.
        parser = _parser({
            "title": "Task",
            "deadline_iso": _future(),
            "category": "task",
            "recurring_rule": "",
        })
        result = parser.parse("do something tomorrow", now=NOW)
        assert result.recurring_rule is None

    def test_title_is_stripped(self):
        parser = _parser({"title": "  Mua sữa  ", "deadline_iso": _future(), "category": "task"})
        result = parser.parse("mua sua", now=NOW)
        assert result.title == "Mua sữa"


# ═════════════════════════════════════════════════════════════════════════════
# Past deadline
# ═════════════════════════════════════════════════════════════════════════════


class TestPastDeadline:

    def test_past_deadline_raises(self):
        parser = _parser({"title": "Task", "deadline_iso": _past(2), "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something yesterday", now=NOW)
        assert exc_info.value.code == "past_deadline"

    def test_deadline_exactly_now_raises(self):
        # Deadline == now is not strictly future → rejected.
        dl = NOW.strftime("%Y-%m-%dT%H:%M:%S+07:00")
        parser = _parser({"title": "Task", "deadline_iso": dl, "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something now", now=NOW)
        assert exc_info.value.code == "past_deadline"

    def test_past_deadline_error_message_mentions_deadline(self):
        dl = _past(1)
        parser = _parser({"title": "Task", "deadline_iso": dl, "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("do it", now=NOW)
        assert dl in str(exc_info.value)

    def test_future_deadline_does_not_raise(self):
        parser = _parser({"title": "Task", "deadline_iso": _future(1), "category": "task"})
        result = parser.parse("do it in 1 hour", now=NOW)
        assert result is not None


# ═════════════════════════════════════════════════════════════════════════════
# LLM returns no tool_use block
# ═════════════════════════════════════════════════════════════════════════════


class TestNoToolUse:

    def test_text_only_response_raises_parse_failed(self):
        parser = _parser(tool_input=None)  # text-only response
        with pytest.raises(ParseError) as exc_info:
            parser.parse("tuan sau lam bai tap", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_parse_failed_message_guides_user(self):
        parser = _parser(tool_input=None)
        with pytest.raises(ParseError) as exc_info:
            parser.parse("ambiguous input", now=NOW)
        # Error message should hint at providing a deadline.
        assert "deadline" in str(exc_info.value).lower() or "thời gian" in str(exc_info.value)


# ═════════════════════════════════════════════════════════════════════════════
# Malformed tool_use input
# ═════════════════════════════════════════════════════════════════════════════


class TestMalformedToolInput:

    def test_empty_title_raises_parse_failed(self):
        parser = _parser({"title": "", "deadline_iso": _future(), "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_missing_title_raises_parse_failed(self):
        parser = _parser({"deadline_iso": _future(), "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_empty_deadline_raises_parse_failed(self):
        parser = _parser({"title": "Task", "deadline_iso": "", "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_missing_deadline_raises_parse_failed(self):
        parser = _parser({"title": "Task", "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_invalid_deadline_format_raises_parse_failed(self):
        parser = _parser({"title": "Task", "deadline_iso": "not-a-date", "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=NOW)
        assert exc_info.value.code == "parse_failed"

    def test_missing_category_defaults_to_task(self):
        # If LLM omits category, parser falls back to 'task'.
        parser = _parser({"title": "Task", "deadline_iso": _future()})
        result = parser.parse("something", now=NOW)
        assert result.category == "task"


# ═════════════════════════════════════════════════════════════════════════════
# LLM API exception
# ═════════════════════════════════════════════════════════════════════════════


class TestLLMError:

    def test_api_exception_raises_llm_error(self):
        parser = _parser(raise_exc=ConnectionError("timeout"))
        with pytest.raises(ParseError) as exc_info:
            parser.parse("any input", now=NOW)
        assert exc_info.value.code == "llm_error"

    def test_llm_error_wraps_original(self):
        exc = RuntimeError("server_error")
        parser = _parser(raise_exc=exc)
        with pytest.raises(ParseError) as exc_info:
            parser.parse("any input", now=NOW)
        assert exc_info.value.__cause__ is exc


# ═════════════════════════════════════════════════════════════════════════════
# Prompt construction (morning_default, now_iso)
# ═════════════════════════════════════════════════════════════════════════════


class TestPromptConstruction:

    def test_morning_default_in_system_prompt(self):
        client = _make_client({"title": "Task", "deadline_iso": _future(), "category": "task"})
        parser = TaskParser(client=client)
        parser.parse("do something tomorrow", morning_default="08:30", now=NOW)

        call_kwargs = client.messages.create.call_args.kwargs
        assert "08:30" in call_kwargs["system"]

    def test_now_iso_in_system_prompt(self):
        custom_now = datetime(2026, 6, 15, 14, 30, 0, tzinfo=VN_TZ)
        # Deadline must be after custom_now, so use a far-future absolute date.
        far_future_dl = "2099-01-01T09:00:00+07:00"
        client = _make_client({"title": "Task", "deadline_iso": far_future_dl, "category": "task"})
        parser = TaskParser(client=client)
        parser.parse("do something tomorrow", now=custom_now)

        call_kwargs = client.messages.create.call_args.kwargs
        assert "2026-06-15T14:30:00+07:00" in call_kwargs["system"]

    def test_tool_passed_to_api(self):
        client = _make_client({"title": "Task", "deadline_iso": _future(), "category": "task"})
        parser = TaskParser(client=client)
        parser.parse("something", now=NOW)

        call_kwargs = client.messages.create.call_args.kwargs
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["tools"][0]["name"] == "create_task"

    def test_free_form_sent_as_user_message(self):
        client = _make_client({"title": "Task", "deadline_iso": _future(), "category": "task"})
        parser = TaskParser(client=client)
        parser.parse("nhac mua sua 5h chieu mai", now=NOW)

        call_kwargs = client.messages.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0]["role"] == "user"
        assert "nhac mua sua 5h chieu mai" in messages[0]["content"]

    def test_now_controls_deadline_validation(self):
        # Deadline that is 1 hour from NOW should pass; 1 hour from an earlier
        # reference time might be in the past relative to a different now.
        far_future_now = datetime(2050, 1, 1, 0, 0, 0, tzinfo=VN_TZ)
        dl = _future(hours=1)   # only 1h from test's NOW (2026) — past relative to 2050
        parser = _parser({"title": "Task", "deadline_iso": dl, "category": "task"})
        with pytest.raises(ParseError) as exc_info:
            parser.parse("something", now=far_future_now)
        assert exc_info.value.code == "past_deadline"
