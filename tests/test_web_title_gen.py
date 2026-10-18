"""tests/test_web_title_gen.py — Unit tests for AnthropicLLM.generate_chat_title (FR-5.5)."""
from unittest.mock import MagicMock

import pytest

import importlib

import config
import ledger_parser
import task_parser
from claude_client import AnthropicLLM
from config import HAIKU_MODEL as _HAIKU_MODEL


def _make_llm(response_text: str, in_tok: int = 10, out_tok: int = 5) -> AnthropicLLM:
    """Build an AnthropicLLM with a mocked Anthropic client."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=response_text)]
    mock_resp.usage.input_tokens = in_tok
    mock_resp.usage.output_tokens = out_tok

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm._client = mock_client
    llm._model = "claude-sonnet-4-6"
    return llm


class TestHaikuConfig:
    """Centralised Haiku model config (single source of truth in config.py)."""

    @pytest.fixture(autouse=True)
    def _restore_config(self):
        yield
        importlib.reload(config)

    def test_default_model_is_haiku_5_5(self, monkeypatch):
        monkeypatch.delenv("HAIKU_MODEL", raising=False)
        importlib.reload(config)
        assert config.HAIKU_MODEL == "claude-haiku-5-5"

    def test_env_overrides_model(self, monkeypatch):
        monkeypatch.setenv("HAIKU_MODEL", "claude-haiku-custom")
        importlib.reload(config)
        assert config.HAIKU_MODEL == "claude-haiku-custom"

    def test_default_prices_match_haiku_5_5(self, monkeypatch):
        monkeypatch.delenv("HAIKU_PRICE_INPUT_PER_M", raising=False)
        monkeypatch.delenv("HAIKU_PRICE_OUTPUT_PER_M", raising=False)
        importlib.reload(config)
        assert config.HAIKU_PRICE_INPUT_PER_M == 0.10
        assert config.HAIKU_PRICE_OUTPUT_PER_M == 0.50

    def test_env_overrides_prices(self, monkeypatch):
        monkeypatch.setenv("HAIKU_PRICE_INPUT_PER_M", "0.25")
        monkeypatch.setenv("HAIKU_PRICE_OUTPUT_PER_M", "1.5")
        importlib.reload(config)
        assert config.HAIKU_PRICE_INPUT_PER_M == 0.25
        assert config.HAIKU_PRICE_OUTPUT_PER_M == 1.5

    def test_parsers_use_central_model(self):
        assert ledger_parser.HAIKU_MODEL == config.HAIKU_MODEL
        assert task_parser.HAIKU_MODEL == config.HAIKU_MODEL

    def test_cost_monitor_uses_central_prices(self):
        import cost_monitor
        assert cost_monitor.PRICE_INPUT_PER_M == config.HAIKU_PRICE_INPUT_PER_M
        assert cost_monitor.PRICE_OUTPUT_PER_M == config.HAIKU_PRICE_OUTPUT_PER_M


class TestGenerateChatTitle:
    def test_returns_tuple_str_int(self):
        llm = _make_llm("Học Python cơ bản")
        result = llm.generate_chat_title("Python là gì?", "Python là ngôn ngữ lập trình.")
        assert isinstance(result, tuple)
        assert isinstance(result[0], str)
        assert isinstance(result[1], int)

    def test_title_value_matches_response(self):
        llm = _make_llm("Học Python cơ bản")
        title, _ = llm.generate_chat_title("Python là gì?", "Python là ngôn ngữ lập trình.")
        assert title == "Học Python cơ bản"

    def test_strips_double_quotes_from_title(self):
        llm = _make_llm('"Học Python cơ bản"')
        title, _ = llm.generate_chat_title("Python?", "Đây là ngôn ngữ.")
        assert title == "Học Python cơ bản"

    def test_strips_single_quotes_from_title(self):
        llm = _make_llm("'Tìm hiểu AI'")
        title, _ = llm.generate_chat_title("AI là gì?", "AI là trí tuệ nhân tạo.")
        assert title == "Tìm hiểu AI"

    def test_token_count_is_sum_of_usage(self):
        llm = _make_llm("Some Title", in_tok=20, out_tok=8)
        _, tokens = llm.generate_chat_title("msg", "reply")
        assert tokens == 28

    def test_uses_haiku_model_not_configured_model(self):
        llm = _make_llm("Title")
        llm.generate_chat_title("msg", "reply")
        call_kwargs = llm._client.messages.create.call_args
        assert call_kwargs.kwargs.get("model") == _HAIKU_MODEL or call_kwargs.args[0] == _HAIKU_MODEL or _HAIKU_MODEL in str(call_kwargs)
