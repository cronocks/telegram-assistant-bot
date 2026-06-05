"""ledger_parser.py — Amount parsing + category classification for FR-9.

Public API:
    parse_amount(text)  -> int (VND)
        Deterministic; raises ValueError on bad input.

    LedgerParser.classify_category(description, categories) -> int | None
        Fast-path keyword lookup; LLM fallback (Haiku 4.5) when ambiguous.
        Returns category_id or None (handler falls back to "Khác").

    LedgerParser.parse_command(text, categories) -> dict
        Parses "chi: <amount> <desc>" or "thu: <amount> <desc>" into
        {kind, amount, description, category_id}.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
_AMOUNT_RE = re.compile(r"^([\d.,]+)\s*(k|tr|m)?$", re.IGNORECASE)
_COMMAND_RE = re.compile(r"^(chi|thu)\s*:\s*(.*)$", re.IGNORECASE)

_CLASSIFY_TOOL: dict = {
    "name": "classify_category",
    "description": "Classify a Vietnamese expense/income description into one of the given categories.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category_id": {
                "type": "integer",
                "description": "ID of the best matching category.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0.0-1.0.",
            },
        },
        "required": ["category_id", "confidence"],
    },
}

_SYSTEM_PROMPT = """\
Bạn là trợ lý phân loại chi tiêu gia đình. User mô tả khoản thu/chi bằng tiếng Việt.
Nhiệm vụ: gọi tool classify_category với category_id phù hợp nhất và confidence score.
Nếu không chắc chắn, đặt confidence thấp (< 0.6)."""

LLM_CONFIDENCE_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics for fuzzy matching."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_amount(text: str) -> int:
    """Parse a Vietnamese amount string to integer VND.

    Accepts: 50000, 50.000, 50,000, 50k, 2tr, 1.5tr, 2m
    Raises ValueError for invalid/zero/negative input.
    """
    text = text.strip()
    if not text:
        raise ValueError("ledger: amount text is empty")

    m = _AMOUNT_RE.match(text)
    if not m:
        raise ValueError(f"ledger: cannot parse amount {text!r}")

    num_str, suffix = m.group(1), (m.group(2) or "").lower()
    # Normalise separators: treat '.' and ',' as thousand separators when
    # the fractional part is exactly 3 digits (e.g. 50.000), otherwise as
    # a decimal point (e.g. 1.5tr).
    # Strategy: remove thousand-sep patterns first, then parse float.
    cleaned = num_str.replace(",", ".")  # unify to dot
    parts = cleaned.split(".")
    if len(parts) > 2:
        # Multiple dots — all are thousand separators (e.g. 1.000.000)
        cleaned = "".join(parts)
    elif len(parts) == 2 and len(parts[1]) == 3 and suffix == "":
        # Single dot with exactly 3-digit fraction and no suffix → thousand sep
        cleaned = "".join(parts)
    # else: single dot is a decimal point (e.g. 1.5tr)

    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"ledger: cannot parse amount {text!r}")

    multiplier = {"k": 1_000, "tr": 1_000_000, "m": 1_000_000}.get(suffix, 1)
    result = int(round(value * multiplier))
    if result <= 0:
        raise ValueError(f"ledger: amount must be positive, got {result}")
    return result


_UNSET = object()  # sentinel to distinguish "not passed" from explicit None


class LedgerParser:
    """Hybrid ledger command parser: deterministic amount + 2-tier category."""

    def __init__(
        self,
        client: Any | None = _UNSET,
        cost_monitor: Any | None = None,
    ) -> None:
        if client is _UNSET:
            # Auto-initialize real Anthropic client when caller omits argument.
            # Pass client=None explicitly to disable LLM (e.g. in tests).
            import anthropic
            from config import ANTHROPIC_API_KEY
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self._client = client
        self._cost_monitor = cost_monitor

    # ── Category classification ───────────────────────────────────────────────

    def classify_category(
        self,
        description: str,
        categories: list[dict],
    ) -> int | None:
        """Return category_id via fast-path or LLM; None if unresolvable."""
        if not categories:
            return None  # nothing to classify against — skip LLM entirely

        desc_words = {w for w in _normalize(description).split() if len(w) >= 2}
        matches = [
            cat for cat in categories
            if desc_words & set(_normalize(cat["name"]).split())
        ]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) == 0 and self._client is None:
            return None

        # LLM fallback: 0 or >1 fast-path matches
        return self._llm_classify(description, categories)

    def _llm_classify(self, description: str, categories: list[dict]) -> int | None:
        if self._client is None:
            return None

        valid_ids = {c["id"] for c in categories}
        cat_list = "\n".join(
            f"- id={c['id']} name={c['name']} kind={c['kind']}" for c in categories
        )
        user_msg = f"Danh mục:\n{cat_list}\n\nMô tả: {description}"

        try:
            response = self._client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                tools=[_CLASSIFY_TOOL],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            logger.warning("ledger_parser: LLM classify failed: %s", exc)
            return None

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "classify_category":
                cat_id = block.input.get("category_id")
                confidence = block.input.get("confidence", 0.0)
                if cat_id not in valid_ids:
                    logger.warning("ledger_parser: LLM returned unknown category_id=%s, ignoring", cat_id)
                    return None
                if confidence >= LLM_CONFIDENCE_THRESHOLD:
                    return cat_id
                return None

        return None

    # ── Command parse ─────────────────────────────────────────────────────────

    def parse_command(
        self,
        text: str,
        categories: list[dict],
    ) -> dict:
        """Parse 'chi: <amount> <desc>' / 'thu: <amount> <desc>'.

        Returns {kind, amount, description, category_id}.
        Raises ValueError on bad format/amount.
        """
        m = _COMMAND_RE.match(text.strip())
        if not m:
            raise ValueError(f"ledger: unrecognised command format: {text!r}")

        prefix, rest = m.group(1).lower(), m.group(2).strip()
        kind = "expense" if prefix == "chi" else "income"

        # Split on first whitespace: first token is amount, rest is description
        parts = rest.split(None, 1)
        if not parts:
            raise ValueError(f"ledger: missing amount in command: {text!r}")

        amount_token = parts[0]
        description = parts[1].strip() if len(parts) > 1 else ""

        # Validate that amount_token looks numeric before parsing
        if not re.match(r"^[\d.,]", amount_token):
            raise ValueError(f"ledger: expected amount, got {amount_token!r}")

        amount = parse_amount(amount_token)
        category_id = self.classify_category(description, categories) if description else None

        return {
            "kind": kind,
            "amount": amount,
            "description": description,
            "category_id": category_id,
        }
