import json
import re
import anthropic
from config import ANTHROPIC_API_KEY, MODEL

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Bạn là trợ lý cá nhân thông minh, giao tiếp qua Telegram.
Bạn có thể truy cập các ghi chú của người dùng trong Obsidian vault.
Trả lời ngắn gọn, rõ ràng bằng tiếng Việt.
Khi được cung cấp context từ ghi chú, hãy tham chiếu đến chúng khi trả lời."""


def ask_claude(user_message: str, notes_context: str = "") -> tuple[str, int]:
    """
    Gọi Claude API, trả về (response_text, total_tokens).
    notes_context: nội dung ghi chú liên quan từ Obsidian.
    """
    messages = []

    if notes_context:
        messages.append({
            "role": "user",
            "content": (
                f"Đây là một số ghi chú liên quan từ Obsidian vault của tôi:\n\n"
                f"{notes_context}\n\n"
                f"Với context trên, hãy trả lời câu hỏi sau: {user_message}"
            ),
        })
    else:
        messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    text = response.content[0].text
    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    return text, total_tokens


def summarize_notes(notes: list[dict]) -> tuple[str, int]:
    """Tóm tắt danh sách ghi chú."""
    if not notes:
        return "Không tìm thấy ghi chú nào.", 0

    notes_text = "\n\n---\n\n".join(
        [f"**{n['name']}** ({n['modified']}):\n{n['content']}" for n in notes]
    )

    prompt = f"""Hãy tóm tắt ngắn gọn các ghi chú sau theo dạng bullet points,
nêu bật những điểm quan trọng nhất:\n\n{notes_text}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    return text, total_tokens


# ── Smart Search: trích xuất ý định tìm kiếm từ câu hỏi mơ hồ ────────────────

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


# ── Wiki: Ingest ──────────────────────────────────────────────────────────────

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


def extract_wiki_updates(raw_content: str, existing_topics: list[str]) -> tuple[list[dict], int]:
    """
    Phân tích raw content, trả về danh sách wiki updates cần thực hiện.
    Returns (updates, total_tokens).
    updates = [{topic, type, action, existing_topic, content_to_add}, ...]
    """
    topics_str = (
        "\n".join(f"- {t}" for t in existing_topics)
        if existing_topics else "(chưa có trang wiki nào)"
    )
    prompt = _WIKI_INGEST_PROMPT.format(
        raw_content=raw_content[:2000],
        existing_topics=topics_str,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system="Bạn là module phân tích wiki, chỉ trả về JSON thuần.",
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    total_tokens = response.usage.input_tokens + response.usage.output_tokens

    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        print(f"[claude] Wiki ingest: khong tim thay JSON, raw={text[:200]}")
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


# ── Wiki: Query ───────────────────────────────────────────────────────────────

def answer_from_wiki(question: str, wiki_pages: list[dict], max_chars_per_page: int = 400) -> tuple[str, int]:
    """
    Trả lời câu hỏi dựa trên các trang wiki.
    wiki_pages: [{name, content}, ...]
    Returns (answer, total_tokens).
    """
    if not wiki_pages:
        return "Không tìm thấy thông tin liên quan trong wiki.", 0

    wiki_context = "\n\n---\n\n".join(
        f"## {p['name'].replace('.md', '')}\n{p['content'][:max_chars_per_page]}"
        for p in wiki_pages
    )

    response = client.messages.create(
        model=MODEL,
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


# ── Smart Search Intent ───────────────────────────────────────────────────────

def extract_search_intent(question: str) -> tuple[dict, int]:
    """
    Phân tích câu hỏi → trả về (intent_dict, total_tokens).

    intent_dict có dạng: {"needs_search": bool, "keywords": [...], "days_back": int}
    Nếu parse JSON lỗi → trả về intent default (needs_search=False).
    """
    prompt = INTENT_PROMPT.format(question=question[:500])

    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system="Bạn là module phân tích, chỉ trả về JSON thuần, không giải thích.",
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    total_tokens = response.usage.input_tokens + response.usage.output_tokens

    # Default fallback
    default = {"needs_search": False, "keywords": [], "days_back": 0}

    # Bóc JSON nếu Claude lỡ wrap trong markdown
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        return default, total_tokens

    try:
        intent = json.loads(json_match.group(0))
        # Validate keys
        if not isinstance(intent.get("keywords"), list):
            intent["keywords"] = []
        intent["needs_search"] = bool(intent.get("needs_search", False))
        intent["days_back"] = int(intent.get("days_back", 0))
        return intent, total_tokens
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"[claude] Intent parse error: {e}, raw={text[:200]}")
        return default, total_tokens
