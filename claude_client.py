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
