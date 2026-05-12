"""claude_client.py — Anthropic-backed implementation of LLMClient.

The class AnthropicLLM wraps all calls to the Claude API behind the LLMClient
Protocol so the core handler is provider-agnostic. Prompts themselves remain
in Vietnamese — they are tuned for Vietnamese output and are effectively
user-facing content rather than code.
"""
import json
import re

import anthropic

from config import ANTHROPIC_API_KEY, MODEL

SYSTEM_PROMPT = """Bạn là trợ lý cá nhân thông minh, giao tiếp qua Telegram.
Bạn có thể truy cập các ghi chú của người dùng trong Obsidian vault.
Trả lời ngắn gọn, rõ ràng bằng tiếng Việt.
Khi được cung cấp context từ ghi chú, hãy tham chiếu đến chúng khi trả lời."""


# ─── Prompts (Vietnamese — drive LLM behavior) ───────────────────────────────

INTENT_PROMPT = """Bạn là module phân tích câu hỏi tiếng Việt. Phân tích câu hỏi sau và trả về JSON.

Câu hỏi: "{question}"

Trả về JSON với cấu trúc:
{{
  "needs_search": true/false,
  "keywords": [danh sách từ khóa quan trọng để tìm trong ghi chú],
  "days_back": số ngày cần tìm ngược (1=hôm nay, 7=tuần, 30=tháng, 365=năm, 0=tất cả)
}}

Quy tắc:
- needs_search = true nếu câu hỏi đề cập đến ký ức, ghi chú, trải nghiệm cá nhân, người quen, sự kiện đã xảy ra
- needs_search = false nếu là câu hỏi kiến thức tổng quát (vd: "Python là gì?", "thủ đô Pháp")
- keywords nên là tên người, chủ đề cụ thể, từ khóa đặc trưng (KHÔNG phải từ chung như "tôi", "đã", "có")
- days_back: ưu tiên dấu hiệu thời gian trong câu ("hôm qua"=1, "tuần trước"=7, "tháng này"=30); nếu không rõ → 30
- Nếu needs_search = false, keywords = [], days_back = 0

CHỈ trả về JSON thuần, KHÔNG markdown, KHÔNG giải thích, KHÔNG text thừa."""


_WIKI_TLDR_PROMPT = """Viết 1 câu mô tả ngắn gọn (tối đa 15 từ tiếng Việt) về topic sau để dùng làm wiki index entry.
Chỉ trả về 1 câu duy nhất, không giải thích, không dấu chấm câu cuối.

Topic: {topic}
Nội dung: {content}"""


_WIKI_SELECT_PROMPT = """Đây là index của wiki knowledge base:

{index_content}

Câu hỏi: {question}

Chọn tối đa 2 trang wiki liên quan nhất để trả lời câu hỏi trên.
Trả về JSON (CHỈ JSON thuần, KHÔNG markdown):
{{"pages": ["filename1.md", "filename2.md"]}}

Nếu không có trang nào liên quan: {{"pages": []}}"""


_WIKI_INGEST_PROMPT = """Bạn là Wiki Manager cho knowledge base cá nhân.

Tài liệu mới cần ingest:
{raw_content}

Các trang wiki hiện có:
{existing_topics}

Phân tích và xác định những thông tin quan trọng cần lưu vào wiki.

Trả về JSON (CHỈ JSON thuần, KHÔNG markdown):
{{
  "updates": [
    {{
      "topic": "Tên chủ đề cụ thể",
      "type": "person|project|concept|event|place|other",
      "action": "create|update",
      "existing_topic": "tên topic hiện có nếu action=update, để trống nếu create",
      "content_to_add": "Nội dung markdown ngắn gọn (bullet points, chỉ thông tin mới)"
    }}
  ],
  "summary": "Tóm tắt 1 câu những gì đã cập nhật"
}}

Quy tắc:
- Chỉ tạo/cập nhật khi thông tin đủ quan trọng và cụ thể để lưu lâu dài
- action=update nếu existing_topics có topic tương tự (không cần khớp tên chính xác)
- content_to_add: ngắn gọn, bullet points, chỉ thông tin mới
- Tối đa 3 updates mỗi lần ingest
- Nếu không có gì đáng lưu: "updates": []"""


class AnthropicLLM:
    """LLMClient impl backed by Anthropic's Claude API."""

    def __init__(self, api_key: str = ANTHROPIC_API_KEY, model: str = MODEL):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    # ─── General Q&A ─────────────────────────────────────────────────────────

    def ask(self, user_message: str, notes_context: str = "") -> tuple[str, int]:
        """Call Claude for a free-form answer, optionally with note context."""
        if notes_context:
            content = (
                f"Đây là một số ghi chú liên quan từ Obsidian vault của tôi:\n\n"
                f"{notes_context}\n\n"
                f"Với context trên, hãy trả lời câu hỏi sau: {user_message}"
            )
        else:
            content = user_message

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text
        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        return text, total_tokens

    # ─── Note summarization ──────────────────────────────────────────────────

    def summarize_notes(self, notes: list[dict]) -> tuple[str, int]:
        """Bullet-point summary of a list of notes."""
        if not notes:
            return "Không tìm thấy ghi chú nào.", 0

        notes_text = "\n\n---\n\n".join(
            [f"**{n['name']}** ({n['modified']}):\n{n['content']}" for n in notes]
        )

        prompt = f"""Hãy tóm tắt ngắn gọn các ghi chú sau theo dạng bullet points,
nêu bật những điểm quan trọng nhất:\n\n{notes_text}"""

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text
        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        return text, total_tokens

    # ─── Smart search intent extraction ──────────────────────────────────────

    def extract_search_intent(self, question: str) -> tuple[dict, int]:
        """Parse a vague question into {needs_search, keywords, days_back}.

        Returns the parsed intent and token usage. Falls back to a no-op intent
        if the model output cannot be parsed as JSON.
        """
        prompt = INTENT_PROMPT.format(question=question[:500])

        response = self._client.messages.create(
            model=self._model,
            max_tokens=200,
            system="Bạn là module phân tích, chỉ trả về JSON thuần, không giải thích.",
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        default = {"needs_search": False, "keywords": [], "days_back": 0}

        # Strip any markdown wrapping the model may have added.
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            return default, total_tokens

        try:
            intent = json.loads(json_match.group(0))
            if not isinstance(intent.get("keywords"), list):
                intent["keywords"] = []
            intent["needs_search"] = bool(intent.get("needs_search", False))
            intent["days_back"] = int(intent.get("days_back", 0))
            return intent, total_tokens
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"[claude] Intent parse error: {e}, raw={text[:200]}")
            return default, total_tokens

    # ─── Wiki: TLDR for index entry ──────────────────────────────────────────

    def generate_wiki_tldr(self, topic: str, content: str) -> tuple[str, int]:
        """Produce a 1-sentence TLDR used when creating a new wiki page."""
        prompt = _WIKI_TLDR_PROMPT.format(topic=topic, content=content[:500])
        response = self._client.messages.create(
            model=self._model,
            max_tokens=60,
            system="Bạn là module tóm tắt, chỉ trả về 1 câu ngắn.",
            messages=[{"role": "user", "content": prompt}],
        )
        tldr = response.content[0].text.strip()
        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        return tldr, total_tokens

    # ─── Wiki: pick pages from index ─────────────────────────────────────────

    def select_wiki_pages_from_index(
        self, question: str, index_content: str
    ) -> tuple[list[str], int]:
        """Have the LLM pick relevant wiki filenames given the index content."""
        prompt = _WIKI_SELECT_PROMPT.format(
            index_content=index_content[:1500],
            question=question[:300],
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=100,
            system="Bạn là module chọn trang wiki, chỉ trả về JSON thuần.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            return [], total_tokens

        try:
            result = json.loads(json_match.group(0))
            pages = result.get("pages", [])
            if not isinstance(pages, list):
                pages = []
            return pages, total_tokens
        except (json.JSONDecodeError, ValueError):
            return [], total_tokens

    # ─── Wiki: extract structured updates from raw content ───────────────────

    def extract_wiki_updates(
        self, raw_content: str, existing_topics: list[str]
    ) -> tuple[list[dict], int]:
        """Analyze raw content and return a list of wiki updates to apply."""
        topics_str = (
            "\n".join(f"- {t}" for t in existing_topics)
            if existing_topics else "(chưa có trang wiki nào)"
        )
        prompt = _WIKI_INGEST_PROMPT.format(
            raw_content=raw_content[:2000],
            existing_topics=topics_str,
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=800,
            system="Bạn là module phân tích wiki, chỉ trả về JSON thuần.",
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            print(f"[claude] Wiki ingest: no JSON found, raw={text[:200]}")
            return [], total_tokens

        try:
            result = json.loads(json_match.group(0))
            updates = result.get("updates", [])
            if not isinstance(updates, list):
                updates = []
            return updates, total_tokens
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[claude] Wiki ingest parse error: {e}, raw={text[:200]}")
            return [], total_tokens

    # ─── Wiki: Q&A from selected pages ───────────────────────────────────────

    def answer_from_wiki(
        self,
        question: str,
        wiki_pages: list[dict],
        max_chars_per_page: int = 400,
    ) -> tuple[str, int]:
        """Answer a question grounded in the provided wiki pages."""
        if not wiki_pages:
            return "Không tìm thấy thông tin liên quan trong wiki.", 0

        wiki_context = "\n\n---\n\n".join(
            f"## {p['name'].replace('.md', '')}\n{p['content'][:max_chars_per_page]}"
            for p in wiki_pages
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Đây là các trang wiki liên quan từ knowledge base của tôi:\n\n"
                    f"{wiki_context}\n\n"
                    f"Câu hỏi: {question}"
                ),
            }],
        )

        text = response.content[0].text
        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        return text, total_tokens
