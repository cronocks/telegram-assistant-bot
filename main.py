import httpx
import uvicorn
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from claude_client import ask_claude, summarize_notes
from drive_client import save_note, search_notes, get_recent_notes, test_drive_connection
from cost_monitor import record_usage, get_current_cost, check_and_alert

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[bot] Testing Google Drive connection at startup...")
    try:
        result = test_drive_connection()
        print(f"[bot] Drive OK: {result}")
    except Exception as e:
        print(f"[bot] Drive ERROR at startup: {e}")
        traceback.print_exc()

    scheduler.add_job(check_and_alert, "interval", hours=6, id="cost_alert")
    scheduler.start()
    print("[bot] Scheduler started — cost check every 6h")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


async def send_message(chat_id: str, text: str, use_markdown: bool = True):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if use_markdown:
        payload["parse_mode"] = "Markdown"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=15)
        except Exception as e:
            print(f"[bot] Telegram send error: {e}")


async def handle_message(chat_id: str, text: str):
    text = text.strip()

    if text == "/start":
        await send_message(chat_id, (
            "Xin chao! Toi la Claude Bot cua ban.\n\n"
            "*Cac lenh ho tro:*\n"
            "`ghi nho [noi dung]` - Luu ghi chu vao Obsidian\n"
            "`tim [tu khoa]` - Tim kiem trong vault\n"
            "`tom tat tuan nay` - Xem ghi chu 7 ngay gan day\n"
            "`/cost` - Xem chi phi thang\n"
            "`/test` - Kiem tra ket noi Drive"
        ))
        return

    if text == "/cost":
        info = get_current_cost()
        bar_filled = int(info["percent"] / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        await send_message(chat_id, (
            f"Chi phi thang {info['month']}\n\n"
            f"`{bar}` {info['percent']}%\n"
            f"Da dung: `${info['cost_usd']}` / `$10.00`\n"
            f"Input tokens: `{info.get('input_tokens', 0):,}`\n"
            f"Output tokens: `{info.get('output_tokens', 0):,}`"
        ))
        return

    if text == "/test":
        await send_message(chat_id, "Dang kiem tra Drive...")
        try:
            result = test_drive_connection()
            await send_message(
                chat_id,
                f"OK Drive\nFolder: {result.get('name')}\nID: {result.get('id')}",
                use_markdown=False
            )
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Drive Error: {str(e)[:500]}", use_markdown=False)
        return

    if text.lower().startswith("ghi nhớ ") or text.lower().startswith("ghi nho "):
        content = text[8:].strip()
        if not content:
            await send_message(chat_id, "Vui long nhap noi dung can ghi nho.")
            return
        await send_message(chat_id, "Dang luu...")
        try:
            title, tokens = ask_claude(
                f"Tao tieu de ngan (toi da 6 tu) cho ghi chu sau, chi tra ve tieu de: {content}"
            )
            record_usage(tokens // 2, tokens // 2)
            filename = save_note(title.strip(), content)
            await send_message(chat_id, f"Da luu: {filename}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi khi luu: {str(e)[:500]}", use_markdown=False)
        return

    if text.lower().startswith("tìm ") or text.lower().startswith("tim "):
        keyword = text[4:].strip()
        if not keyword:
            await send_message(chat_id, "Vui long nhap tu khoa.")
            return
        await send_message(chat_id, f"Dang tim {keyword}...")
        try:
            notes = search_notes(keyword)
            if not notes:
                await send_message(chat_id, f"Khong tim thay ghi chu nao.", use_markdown=False)
                return
            summary, tokens = summarize_notes(notes)
            record_usage(tokens // 2, tokens // 2)
            check_and_alert()
            await send_message(chat_id, f"Ket qua:\n\n{summary}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi khi tim: {str(e)[:500]}", use_markdown=False)
        return

    if ("tóm tắt" in text.lower() or "tom tat" in text.lower()) and ("tuần" in text.lower() or "tuan" in text.lower()):
        await send_message(chat_id, "Dang doc ghi chu tuan nay...")
        try:
            notes = get_recent_notes(days=7)
            if not notes:
                await send_message(chat_id, "Khong co ghi chu nao trong 7 ngay qua.")
                return
            summary, tokens = summarize_notes(notes)
            record_usage(tokens // 2, tokens // 2)
            check_and_alert()
            await send_message(chat_id, f"Tom tat tuan nay:\n\n{summary}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)
        return

    try:
        await send_message(chat_id, "Dang xu ly...")
        keywords = text.split()[:3]
        context_notes = []
        for kw in keywords:
            if len(kw) > 3:
                try:
                    context_notes.extend(search_notes(kw, max_results=2))
                except Exception:
                    pass
                break
        notes_context = ""
        if context_notes:
            notes_context = "\n\n".join(
                [f"[{n['name']}]\n{n['content']}" for n in context_notes[:2]]
            )
        reply, tokens = ask_claude(text, notes_context)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await send_message(chat_id, reply, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"ok": True}
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "")
        if chat_id != str(TELEGRAM_CHAT_ID):
            return {"ok": True}
        if text:
            await handle_message(chat_id, text)
    except Exception as e:
        traceback.print_exc()
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "running", "version": "v3"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)