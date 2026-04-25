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
