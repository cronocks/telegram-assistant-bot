import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from claude_client import ask_claude, summarize_notes
from drive_client import save_note, search_notes, get_recent_notes
from cost_monitor import record_usage, get_current_cost, check_and_alert

# ── Scheduler chạy check_and_alert mỗi 6 tiếng ──────────────────────────────
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(check_and_alert, "interval", hours=6, id="cost_alert")
    scheduler.start()
    print("[bot] Scheduler started — cost check every 6h")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ── Gửi tin nhắn về Telegram ─────────────────────────────────────────────────
async def send_message(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=15)


# ── Xử lý lệnh ───────────────────────────────────────────────────────────────
async def handle_message(chat_id: str, text: str):
    text = text.strip()

    # ── /start ────────────────────────────────────────────────────────────────
    if text == "/start":
        await send_message(chat_id, (
            "👋 Xin chào! Tôi là Claude Bot của bạn.\n\n"
            "*Các lệnh hỗ trợ:*\n"
            "`ghi nhớ [nội dung]` — Lưu ghi chú vào Obsidian\n"
            "`tìm [từ khóa]` — Tìm kiếm trong vault\n"
            "`tóm tắt tuần này` — Xem ghi chú 7 ngày gần đây\n"
            "`/cost` — Xem chi phí tháng hiện tại\n"
            "Hoặc hỏi bất cứ điều gì — tôi sẽ trả lời!"
        ))
        return

    # ── /cost ─────────────────────────────────────────────────────────────────
    if text == "/cost":
        info = get_current_cost()
        bar_filled = int(info["percent"] / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        await send_message(chat_id, (
            f"📊 *Chi phí tháng {info['month']}*\n\n"
            f"`{bar}` {info['percent']}%\n"
            f"Đã dùng: `${info['cost_usd']}` / `$10.00`\n"
            f"Input tokens: `{info.get('input_tokens', 0):,}`\n"
            f"Output tokens: `{info.get('output_tokens', 0):,}`"
        ))
        return

    # ── ghi nhớ ───────────────────────────────────────────────────────────────
    if text.lower().startswith("ghi nhớ "):
        content = text[8:].strip()
        if not content:
            await send_message(chat_id, "⚠️ Vui lòng nhập nội dung cần ghi nhớ.")
            return
        await send_message(chat_id, "💾 Đang lưu...")
        try:
            # Dùng Claude tạo tiêu đề ngắn
            title, tokens = ask_claude(
                f"Tạo tiêu đề ngắn (tối đa 6 từ) cho ghi chú sau, chỉ trả về tiêu đề: {content}"
            )
            record_usage(tokens // 2, tokens // 2)
            filename = save_note(title.strip(), content)
            await send_message(chat_id, f"✅ Đã lưu: *{filename}*")
        except Exception as e:
            await send_message(chat_id, f"❌ Lỗi khi lưu: {e}")
        return

    # ── tìm ──────────────────────────────────────────────────────────────────
    if text.lower().startswith("tìm "):
        keyword = text[4:].strip()
        if not keyword:
            await send_message(chat_id, "⚠️ Vui lòng nhập từ khóa cần tìm.")
            return
        await send_message(chat_id, f"🔍 Đang tìm *{keyword}*...")
        try:
            notes = search_notes(keyword)
            if not notes:
                await send_message(chat_id, f"Không tìm thấy ghi chú nào chứa *{keyword}*.")
                return
            summary, tokens = summarize_notes(notes)
            record_usage(tokens // 2, tokens // 2)
            check_and_alert()
            await send_message(chat_id, f"📝 Kết quả tìm kiếm *{keyword}*:\n\n{summary}")
        except Exception as e:
            import traceback
            print(f"❌ ERROR: {e}")
            traceback.print_exc()
            await send_message(chat_id, f"❌ Lỗi khi tìm kiếm: {e}")
        return

    # ── tóm tắt tuần này ─────────────────────────────────────────────────────
    if "tóm tắt" in text.lower() and "tuần" in text.lower():
        await send_message(chat_id, "📖 Đang đọc ghi chú tuần này...")
        try:
            notes = get_recent_notes(days=7)
            if not notes:
                await send_message(chat_id, "Không có ghi chú nào trong 7 ngày qua.")
                return
            summary, tokens = summarize_notes(notes)
            record_usage(tokens // 2, tokens // 2)
            check_and_alert()
            await send_message(chat_id, f"📋 *Tóm tắt tuần này:*\n\n{summary}")
        except Exception as e:
            await send_message(chat_id, f"❌ Lỗi: {e}")
        return

    # ── câu hỏi thông thường → Claude ────────────────────────────────────────
    try:
        await send_message(chat_id, "⏳ Đang xử lý...")

        # Tìm ghi chú liên quan làm context
        keywords = text.split()[:3]
        context_notes = []
        for kw in keywords:
            if len(kw) > 3:
                context_notes.extend(search_notes(kw, max_results=2))
                break

        notes_context = ""
        if context_notes:
            notes_context = "\n\n".join(
                [f"[{n['name']}]\n{n['content']}" for n in context_notes[:2]]
            )

        reply, tokens = ask_claude(text, notes_context)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await send_message(chat_id, reply)

    except Exception as e:
        await send_message(chat_id, f"❌ Lỗi: {e}")


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    message = data.get("message") or data.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = str(message["chat"]["id"])
    text = message.get("text", "")

    # Chỉ xử lý tin nhắn từ chính bạn
    if chat_id != str(TELEGRAM_CHAT_ID):
        return {"ok": True}

    if text:
        await handle_message(chat_id, text)

    return {"ok": True}


# ── Health check ──────────────────────────────────────────────────────────────
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "running", "bot": "telegram-claude-obsidian"}


# ── Chạy local ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
