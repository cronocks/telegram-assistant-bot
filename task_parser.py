"""task_parser.py — LLM-backed task parser for FR-7.

Decisions implemented:
  D3  — Hybrid input: prefix dispatched by core_handler; free-form parsed here.
  D15 — Uses Haiku 4.5 (cheap model) via Anthropic tool-use API.
  D16 — morning_default_time applied when user omits a specific time.
  D17 — Past-deadline input is rejected with ParseError('past_deadline').
  D19 — LLM parse failure returns ParseError('parse_failed'); no pending state.

Public API::

    parser = TaskParser()                   # uses real Anthropic client
    parser = TaskParser(client=mock_client) # injectable for tests

    try:
        parsed = parser.parse("mua sua 5h chieu mai")
    except ParseError as e:
        # e.code: 'parse_failed' | 'past_deadline' | 'llm_error'
        ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from timeutils import VIETNAM_TZ

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ── Tool definition ───────────────────────────────────────────────────────────

_CREATE_TASK_TOOL: dict = {
    "name": "create_task",
    "description": (
        "Trích xuất thông tin task từ mô tả của user. "
        "Chỉ gọi tool này khi đủ thông tin để xác định title và deadline."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Tên task ngắn gọn, 3-8 từ tiếng Việt.",
            },
            "deadline_iso": {
                "type": "string",
                "description": (
                    "Deadline theo ISO 8601 với timezone +07:00. "
                    "Ví dụ: '2026-05-25T17:00:00+07:00'."
                ),
            },
            "recurring_rule": {
                "type": "string",
                "description": (
                    "Quy tắc lặp lại. Format: 'weekly:MON,WED@07:00' hoặc 'daily@21:00'. "
                    "Để trống (null/bỏ qua) nếu task chỉ xảy ra một lần."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["task", "study", "reminder"],
                "description": (
                    "'study' nếu liên quan học tập / lịch học của trẻ. "
                    "'reminder' nếu chỉ nhắc nhở, không cần hành động cụ thể. "
                    "'task' cho mọi trường hợp còn lại."
                ),
            },
        },
        "required": ["title", "deadline_iso", "category"],
    },
}

# System prompt (Vietnamese — tunes LLM for Vietnamese user input).
_SYSTEM_PROMPT = """\
Bạn là task parser cho ứng dụng gia đình. User mô tả task bằng tiếng Việt tự nhiên.
Nhiệm vụ của bạn: gọi tool create_task với thông tin đã extract.

Quy tắc:
- title: ngắn gọn 3-8 từ, giữ nguyên tiếng Việt.
- deadline: ISO 8601 với timezone +07:00. Thời gian hiện tại: {now_iso}.
- Nếu user không nêu giờ cụ thể (vd: "mai", "thứ 5"), dùng giờ mặc định: {morning_default}.
- "mai" = ngày mai. "tuần sau thứ 2" = thứ 2 tuần sau. "tháng sau" = cùng ngày tháng sau.
- recurring_rule: chỉ điền khi user muốn lặp lại rõ ràng (vd: "mỗi ngày", "thứ 2-6").
  Format: 'weekly:MON,TUE,WED,THU,FRI@07:00' hoặc 'daily@21:00'.
- category='study' nếu liên quan học tập, lịch học, bài tập của trẻ.
- Nếu KHÔNG đủ thông tin để xác định deadline → KHÔNG gọi tool, trả lời text giải thích.\
"""


# ── Domain types ──────────────────────────────────────────────────────────────

@dataclass
class ParsedTask:
    """Structured result of a successful task parse."""

    title: str
    deadline_iso: str           # ISO 8601 with explicit +07:00 offset
    recurring_rule: str | None  # None = one-shot task
    category: str               # 'task' | 'study' | 'reminder'


class ParseError(Exception):
    """Raised when the parser cannot produce a valid ParsedTask.

    Attributes:
        code: machine-readable error code.
            ``'parse_failed'``  — LLM returned no tool_use block (ambiguous input, D19).
            ``'past_deadline'`` — extracted deadline is in the past (D17).
            ``'llm_error'``     — Anthropic API call raised an exception.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── Parser ────────────────────────────────────────────────────────────────────

class TaskParser:
    """Parse free-form Vietnamese task descriptions via Haiku 4.5 tool-use (D15).

    The Anthropic client is injectable so tests can run without network access.
    """

    def __init__(
        self,
        client: Any | None = None,
        model: str = HAIKU_MODEL,
    ) -> None:
        if client is None:
            import anthropic
            from config import ANTHROPIC_API_KEY
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._client = client
        self._model = model

    def parse(
        self,
        free_form: str,
        morning_default: str = "09:00",
        now: datetime | None = None,
    ) -> ParsedTask:
        """Parse a free-form task description into a structured ParsedTask.

        Args:
            free_form: raw text after the command prefix, e.g. ``"mua sữa 5h chiều mai"``.
            morning_default: HH:MM used when the user omits an explicit time (D16).
            now: reference datetime for prompt and deadline validation;
                 defaults to current VN time.

        Returns:
            :class:`ParsedTask` with title, deadline_iso, recurring_rule, category.

        Raises:
            ParseError: ``code='parse_failed'`` — LLM gave no tool_use block.
            ParseError: ``code='past_deadline'`` — deadline is not in the future.
            ParseError: ``code='llm_error'`` — Anthropic API raised.
        """
        now = now or datetime.now(VIETNAM_TZ)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S+07:00")
        system = _SYSTEM_PROMPT.format(now_iso=now_iso, morning_default=morning_default)

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=system,
                tools=[_CREATE_TASK_TOOL],
                messages=[{"role": "user", "content": free_form.strip()}],
            )
        except Exception as exc:
            logger.warning("task_parser: API error: %s", exc)
            raise ParseError("llm_error", f"Lỗi kết nối LLM: {exc}") from exc

        # Extract the tool_use block (if any).
        tool_input = _extract_tool_input(response)
        if tool_input is None:
            logger.info(
                "task_parser: no tool_use block for input=%r — ambiguous", free_form[:80]
            )
            raise ParseError(
                "parse_failed",
                "Mình chưa rõ deadline — vui lòng gõ lại với thời gian cụ thể, "
                "ví dụ '5h chiều mai' hoặc 'thứ 5 tuần sau lúc 9h'.",
            )

        title = (tool_input.get("title") or "").strip()
        deadline_iso = (tool_input.get("deadline_iso") or "").strip()
        recurring_rule = tool_input.get("recurring_rule") or None
        category = tool_input.get("category") or "task"

        if not title:
            raise ParseError("parse_failed", "LLM trả về title trống.")
        if not deadline_iso:
            raise ParseError("parse_failed", "LLM trả về deadline trống.")

        # Parse and validate deadline (D17: past deadline → reject).
        try:
            deadline_dt = datetime.fromisoformat(deadline_iso)
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=VIETNAM_TZ)
        except ValueError as exc:
            raise ParseError(
                "parse_failed", f"Deadline không đúng định dạng ISO: '{deadline_iso}'"
            ) from exc

        if deadline_dt <= now:
            raise ParseError(
                "past_deadline",
                f"Deadline '{deadline_iso}' đã qua. "
                "Vui lòng nhập lại với thời gian trong tương lai.",
            )

        return ParsedTask(
            title=title,
            deadline_iso=deadline_iso,
            recurring_rule=recurring_rule if recurring_rule else None,
            category=category,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_tool_input(response: Any) -> dict | None:
    """Return the ``input`` dict from the first tool_use content block, or None."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    return None
