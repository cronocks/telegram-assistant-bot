"""
main.py — Telegram bot handler (v5).

Tính năng:
- Lệnh cũ: ghi nhớ, tìm, tóm tắt tuần, /cost, /test, /security, /start
- Lệnh mới:
  * ghi nhớ vào <tên-file>: <nội dung>     — append vào file (fuzzy match)
  * nhật ký <nội dung>                       — append vào file ngày (GMT+7)
  * xem nhật ký                              — đọc file nhật ký hôm nay
  * xem <tên-file>                           — đọc file (fuzzy match)
  * liệt kê                                  — 10 file gần nhất
- Smart search: câu hỏi mơ hồ → Claude trích intent → search có timeframe
- State pending choice: bot hỏi chọn 1/2/yes/no, user trả lời → resolve
"""
import time
import re
import httpx
import uvicorn
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    PENDING_CHOICE_TIMEOUT_SEC, FUZZY_SHOW_LIMIT,
)
from claude_client import ask_claude, summarize_notes, extract_search_intent
from drive_client import (
    save_note, search_notes, get_recent_notes, test_drive_connection,
    find_files_fuzzy, append_to_file, read_file_by_id, list_recent_files,
    add_to_daily_journal, get_today_journal, smart_search,
    get_current_week_notes,
)
from cost_monitor import record_usage, get_current_cost, check_and_alert
from security import get_security_status
from timeutils import current_week_range_str

scheduler = AsyncIOScheduler()


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN & APP
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM SEND
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# PENDING STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════
# State structure (per chat_id):
# {
#   "type": "fuzzy_append" | "fuzzy_view" | "create_new_confirm",
#   "expires_at": float (unix ts),
#   "data": {...}   # tùy theo type
# }

_pending: dict = {}


def _set_pending(chat_id: str, ptype: str, data: dict):
    _pending[chat_id] = {
        "type": ptype,
        "expires_at": time.time() + PENDING_CHOICE_TIMEOUT_SEC,
        "data": data,
    }


def _get_pending(chat_id: str) -> dict | None:
    p = _pending.get(chat_id)
    if not p:
        return None
    if time.time() > p["expires_at"]:
        _pending.pop(chat_id, None)
        return None
    return p


def _clear_pending(chat_id: str):
    _pending.pop(chat_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND PARSING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _norm(text: str) -> str:
    """Lowercase + strip. KHÔNG bỏ dấu (giữ tiếng Việt)."""
    return text.strip().lower()


def _starts_with_any(text: str, prefixes: list) -> str | None:
    """Trả về prefix khớp, None nếu không khớp."""
    low = _norm(text)
    for p in prefixes:
        if low.startswith(p.lower()):
            return p
    return None


def _strip_prefix(text: str, prefix: str) -> str:
    """Cắt prefix khỏi đầu (case-insensitive), trả về phần còn lại đã strip."""
    if text.lower().startswith(prefix.lower()):
        return text[len(prefix):].strip()
    return text.strip()


def _parse_choice_number(text: str) -> int | None:
    """Parse '1', '2', '10' từ tin nhắn ngắn. Trả về None nếu không phải số đơn lẻ."""
    cleaned = text.strip().rstrip(".").rstrip(")")
    if cleaned.isdigit():
        n = int(cleaned)
        if 1 <= n <= 99:
            return n
    return None


def _parse_yes_no(text: str) -> bool | None:
    """Parse yes/no từ tiếng Việt. Trả về True/False/None."""
    low = _norm(text)
    yes_words = {"yes", "y", "co", "có", "ok", "đồng ý", "dong y", "tao moi", "tạo mới", "tạo", "tao"}
    no_words = {"no", "n", "khong", "không", "huy", "hủy", "thoi", "thôi"}
    if low in yes_words:
        return True
    if low in no_words:
        return False
    return None


def _sanitize_filename(name: str) -> str:
    """Bỏ ký tự không hợp lệ trong tên file Drive. Cắt còn 80 ký tự."""
    name = name.strip()
    # Bỏ ký tự đặc biệt cho an toàn (giữ chữ Việt, số, space, dash, underscore)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = name.strip()
    return name[:80] if name else "untitled"


# ═══════════════════════════════════════════════════════════════════════════════
# HANDLERS — pending state resolvers
# ═══════════════════════════════════════════════════════════════════════════════

async def _resolve_fuzzy_append(chat_id: str, pending: dict, text: str) -> bool:
    """
    Xử lý phản hồi cho pending fuzzy_append.
    User chọn số → append vào file đó.
    Trả về True nếu đã handle, False nếu để fallback.
    """
    data = pending["data"]
    matches = data["matches"]
    content = data["content"]

    # Số → chọn file
    n = _parse_choice_number(text)
    if n is not None:
        if 1 <= n <= len(matches):
            chosen = matches[n - 1]
            _clear_pending(chat_id)
            await send_message(chat_id, f"Dang them vao file: {chosen['name']}...", use_markdown=False)
            try:
                from timeutils import time_str
                entry = f"\n## {time_str()}\n{content}\n"
                filename = append_to_file(chosen["id"], entry)
                await send_message(chat_id, f"Da them vao: {filename}", use_markdown=False)
            except Exception as e:
                traceback.print_exc()
                await send_message(chat_id, f"Loi khi them: {str(e)[:400]}", use_markdown=False)
            return True
        else:
            await send_message(chat_id, f"So khong hop le. Hay chon tu 1 den {len(matches)}.", use_markdown=False)
            return True

    # "huy" / "hủy" / "thoi"
    yn = _parse_yes_no(text)
    if yn is False:
        _clear_pending(chat_id)
        await send_message(chat_id, "Da huy.", use_markdown=False)
        return True

    return False  # không match → fallback xử lý như lệnh thường


async def _resolve_fuzzy_view(chat_id: str, pending: dict, text: str) -> bool:
    """Xử lý phản hồi cho pending fuzzy_view (xem file)."""
    matches = pending["data"]["matches"]

    n = _parse_choice_number(text)
    if n is not None:
        if 1 <= n <= len(matches):
            chosen = matches[n - 1]
            _clear_pending(chat_id)
            try:
                file_data = read_file_by_id(chosen["id"])
                content = file_data["content"]
                # Cắt nếu quá dài (Telegram limit 4096)
                if len(content) > 3500:
                    content = content[:3500] + "\n\n[...] (file qua dai, da cat)"
                await send_message(chat_id, f"=== {file_data['name']} ===\n\n{content}", use_markdown=False)
            except Exception as e:
                traceback.print_exc()
                await send_message(chat_id, f"Loi khi doc: {str(e)[:400]}", use_markdown=False)
            return True
        else:
            await send_message(chat_id, f"So khong hop le. Hay chon tu 1 den {len(matches)}.", use_markdown=False)
            return True

    yn = _parse_yes_no(text)
    if yn is False:
        _clear_pending(chat_id)
        await send_message(chat_id, "Da huy.", use_markdown=False)
        return True

    return False


async def _resolve_create_new_confirm(chat_id: str, pending: dict, text: str) -> bool:
    """
    Xử lý confirm tạo file mới (sau khi fuzzy không match).
    Yes → tạo file. No → hủy.
    """
    data = pending["data"]
    yn = _parse_yes_no(text)

    if yn is True:
        _clear_pending(chat_id)
        filename = data["filename"]
        content = data["content"]
        await send_message(chat_id, f"Dang tao file: {filename}...", use_markdown=False)
        try:
            saved_name = save_note(
                title=filename, content=content,
                custom_filename=_sanitize_filename(filename),
            )
            await send_message(chat_id, f"Da tao: {saved_name}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi khi tao: {str(e)[:400]}", use_markdown=False)
        return True

    if yn is False:
        _clear_pending(chat_id)
        await send_message(chat_id, "Da huy.", use_markdown=False)
        return True

    return False


async def _try_resolve_pending(chat_id: str, text: str) -> bool:
    """Nếu có pending state, thử resolve. Trả về True nếu đã handle."""
    pending = _get_pending(chat_id)
    if not pending:
        return False

    ptype = pending["type"]
    if ptype == "fuzzy_append":
        return await _resolve_fuzzy_append(chat_id, pending, text)
    if ptype == "fuzzy_view":
        return await _resolve_fuzzy_view(chat_id, pending, text)
    if ptype == "create_new_confirm":
        return await _resolve_create_new_confirm(chat_id, pending, text)

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS — lệnh chính
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_start(chat_id: str):
    await send_message(chat_id, (
        "Xin chao! Toi la Claude Bot.\n\n"
        "*LENH GHI CHU:*\n"
        "`ghi nho [noi dung]` — Tao file moi (Claude tu dat ten)\n"
        "`ghi nho vao [ten]: [noi dung]` — Them vao file co san (fuzzy match)\n"
        "`nhat ky [noi dung]` — Them vao file nhat ky hom nay (GMT+7)\n\n"
        "*LENH XEM:*\n"
        "`xem nhat ky` — Doc nhat ky hom nay\n"
        "`xem [ten]` — Doc 1 file (fuzzy match)\n"
        "`liet ke` — Liet ke 10 file gan nhat\n"
        "`tim [tu khoa]` — Tim trong noi dung file\n"
        "`tom tat tuan nay` — Tom tat ghi chu 7 ngay\n\n"
        "*LENH HE THONG:*\n"
        "`/cost` — Chi phi thang\n"
        "`/test` — Kiem tra Drive\n"
        "`/security` — Cau hinh bao mat\n\n"
        "*HOI DAP:*\n"
        "Cau hoi tu nhien — bot tu tim trong vault va tra loi"
    ))


async def _cmd_cost(chat_id: str):
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


async def _cmd_test(chat_id: str):
    await send_message(chat_id, "Dang kiem tra Drive...")
    try:
        result = test_drive_connection()
        await send_message(
            chat_id,
            f"OK Drive\nFolder: {result.get('name')}\nID: {result.get('id')}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Drive Error: {str(e)[:500]}", use_markdown=False)


async def _cmd_security(chat_id: str):
    try:
        s = get_security_status()
        msg = (
            f"Cau hinh bao mat:\n\n"
            f"Scope: {s['scope']}\n"
            f"Folder ID: {s.get('configured_folder_id', 'N/A')}\n"
            f"Trusted folders: {s.get('trusted_folders_count', 0)}\n"
            f"Owner email: {s['owner_email']}\n"
            f"Transfer ownership: {s['ownership_transfer_enabled']}\n"
            f"Rate limit: {s['rate_limit_used']} files/hour\n"
            f"Allowed extensions: {s['allowed_extensions']}\n"
            f"Allowed mimetypes: {s['allowed_mimetypes']}"
        )
        await send_message(chat_id, msg, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_ghi_nho(chat_id: str, content: str):
    """ghi nhớ <nội dung> → tạo file mới, Claude đặt tên."""
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
    except PermissionError as e:
        traceback.print_exc()
        await send_message(chat_id, f"Tu choi vi ly do bao mat: {str(e)[:400]}", use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi khi luu: {str(e)[:500]}", use_markdown=False)


async def _cmd_ghi_nho_vao(chat_id: str, body: str):
    """
    ghi nhớ vào <tên-file>: <nội dung>
    Tách theo dấu ':' đầu tiên.
    """
    if ":" not in body:
        await send_message(chat_id,
            "Cu phap: ghi nho vao <ten-file>: <noi dung>\n"
            "Vi du: ghi nho vao kiem tra: them cau hoi moi",
            use_markdown=False)
        return

    name_part, content = body.split(":", 1)
    name_part = name_part.strip()
    content = content.strip()

    if not name_part:
        await send_message(chat_id, "Thieu ten file.", use_markdown=False)
        return
    if not content:
        await send_message(chat_id, "Thieu noi dung.", use_markdown=False)
        return

    await send_message(chat_id, f"Dang tim file '{name_part}'...", use_markdown=False)

    try:
        matches = find_files_fuzzy(name_part)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False)
        return

    # Trường hợp 1: Tìm thấy đúng 1 file → append luôn
    if len(matches) == 1:
        chosen = matches[0]
        try:
            from timeutils import time_str
            entry = f"\n## {time_str()}\n{content}\n"
            filename = append_to_file(chosen["id"], entry)
            await send_message(chat_id, f"Da them vao: {filename}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi khi them: {str(e)[:400]}", use_markdown=False)
        return

    # Trường hợp 2: Nhiều match → hỏi user chọn
    if len(matches) > 1:
        shown = matches[:FUZZY_SHOW_LIMIT]
        msg_lines = [f"Tim thay {len(matches)} file khop voi '{name_part}':"]
        for i, f in enumerate(shown, 1):
            msg_lines.append(f"{i}. {f['name']}")
        if len(matches) > FUZZY_SHOW_LIMIT:
            msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khac)")
        msg_lines.append(f"\nTra loi 1-{len(shown)} de chon, hoac 'huy'.")
        msg_lines.append(f"(Het han sau {PENDING_CHOICE_TIMEOUT_SEC}s)")

        _set_pending(chat_id, "fuzzy_append", {
            "matches": shown,
            "content": content,
        })
        await send_message(chat_id, "\n".join(msg_lines), use_markdown=False)
        return

    # Trường hợp 3: Không tìm thấy → hỏi tạo mới
    _set_pending(chat_id, "create_new_confirm", {
        "filename": name_part,
        "content": content,
    })
    await send_message(chat_id,
        f"Khong tim thay file '{name_part}'.\n"
        f"Tao file moi voi ten do? (yes/no)\n"
        f"(Het han sau {PENDING_CHOICE_TIMEOUT_SEC}s)",
        use_markdown=False)


async def _cmd_nhat_ky(chat_id: str, content: str):
    """nhật ký <nội dung> → append vào file ngày hôm nay."""
    if not content:
        await send_message(chat_id, "Vui long nhap noi dung.", use_markdown=False)
        return

    await send_message(chat_id, "Dang ghi nhat ky...")
    try:
        filename, action = add_to_daily_journal(content)
        verb = "Da tao moi" if action == "created" else "Da them vao"
        await send_message(chat_id, f"{verb}: {filename}", use_markdown=False)
    except PermissionError as e:
        traceback.print_exc()
        await send_message(chat_id, f"Tu choi vi ly do bao mat: {str(e)[:400]}", use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_xem_nhat_ky(chat_id: str):
    """xem nhật ký → đọc file nhật ký hôm nay."""
    try:
        journal = get_today_journal()
        if not journal:
            from timeutils import today_str
            await send_message(chat_id,
                f"Chua co nhat ky cho ngay {today_str()}. "
                f"Hay tao bang lenh: nhat ky <noi dung>",
                use_markdown=False)
            return
        content = journal["content"]
        if len(content) > 3500:
            content = content[:3500] + "\n\n[...] (qua dai, da cat)"
        await send_message(chat_id, f"=== {journal['name']} ===\n\n{content}", use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_xem(chat_id: str, name_query: str):
    """xem <tên-file> → đọc file (fuzzy match)."""
    if not name_query:
        await send_message(chat_id, "Cu phap: xem <ten-file>", use_markdown=False)
        return

    try:
        matches = find_files_fuzzy(name_query)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi khi tim: {str(e)[:400]}", use_markdown=False)
        return

    if not matches:
        await send_message(chat_id,
            f"Khong tim thay file nao khop voi '{name_query}'.",
            use_markdown=False)
        return

    if len(matches) == 1:
        chosen = matches[0]
        try:
            file_data = read_file_by_id(chosen["id"])
            content = file_data["content"]
            if len(content) > 3500:
                content = content[:3500] + "\n\n[...] (qua dai, da cat)"
            await send_message(chat_id, f"=== {file_data['name']} ===\n\n{content}", use_markdown=False)
        except Exception as e:
            traceback.print_exc()
            await send_message(chat_id, f"Loi khi doc: {str(e)[:400]}", use_markdown=False)
        return

    # Nhiều match → set pending
    shown = matches[:FUZZY_SHOW_LIMIT]
    msg_lines = [f"Tim thay {len(matches)} file khop voi '{name_query}':"]
    for i, f in enumerate(shown, 1):
        msg_lines.append(f"{i}. {f['name']}")
    if len(matches) > FUZZY_SHOW_LIMIT:
        msg_lines.append(f"... ({len(matches) - FUZZY_SHOW_LIMIT} file khac)")
    msg_lines.append(f"\nTra loi 1-{len(shown)} de chon, hoac 'huy'.")

    _set_pending(chat_id, "fuzzy_view", {"matches": shown})
    await send_message(chat_id, "\n".join(msg_lines), use_markdown=False)


async def _cmd_liet_ke(chat_id: str):
    """liệt kê → 10 file gần nhất."""
    try:
        files = list_recent_files()
        if not files:
            await send_message(chat_id, "Vault trong, chua co ghi chu nao.", use_markdown=False)
            return
        msg_lines = [f"10 file gan nhat:"]
        for i, f in enumerate(files, 1):
            modified = f.get("modifiedTime", "")[:10]
            msg_lines.append(f"{i}. {f['name']}  ({modified})")
        await send_message(chat_id, "\n".join(msg_lines), use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _cmd_tim(chat_id: str, keyword: str):
    if not keyword:
        await send_message(chat_id, "Vui long nhap tu khoa.")
        return
    await send_message(chat_id, f"Dang tim '{keyword}'...", use_markdown=False)
    try:
        notes = search_notes(keyword)
        if not notes:
            await send_message(chat_id, "Khong tim thay ghi chu nao.", use_markdown=False)
            return
        summary, tokens = summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await send_message(chat_id, f"Ket qua:\n\n{summary}", use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi khi tim: {str(e)[:500]}", use_markdown=False)


async def _cmd_tom_tat_tuan(chat_id: str):
    week_range = current_week_range_str()
    await send_message(chat_id, f"Dang doc ghi chu tuan nay ({week_range})...", use_markdown=False)
    try:
        notes = get_current_week_notes(max_results=20)
        if not notes:
            await send_message(chat_id,
                f"Khong co ghi chu nao trong tuan nay ({week_range}).",
                use_markdown=False)
            return
        summary, tokens = summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await send_message(chat_id,
            f"Tom tat tuan nay ({week_range}) — {len(notes)} ghi chu:\n\n{summary}",
            use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


async def _handle_general_question(chat_id: str, text: str):
    """
    Câu hỏi không match lệnh nào → smart search + Claude trả lời.

    Pipeline:
    1. extract_search_intent(text) → keywords + days_back + needs_search
    2. Nếu needs_search=True → smart_search → notes
    3. ask_claude(text, notes_context) → reply
    """
    await send_message(chat_id, "Dang xu ly...")
    try:
        # Step 1: Extract intent
        intent, intent_tokens = extract_search_intent(text)
        record_usage(intent_tokens // 2, intent_tokens // 2)

        notes_context = ""
        # Step 2: Smart search nếu cần
        if intent.get("needs_search") and intent.get("keywords"):
            try:
                notes = smart_search(
                    keywords=intent["keywords"],
                    days_back=intent.get("days_back", 0) or 0,
                )
                if notes:
                    notes_context = "\n\n".join(
                        [f"[{n['name']}]\n{n['content']}" for n in notes[:5]]
                    )
            except Exception as e:
                print(f"[bot] Smart search error: {e}")
                # Fallback: search keyword đầu tiên kiểu cũ
                try:
                    fallback_notes = search_notes(intent["keywords"][0], max_results=2)
                    if fallback_notes:
                        notes_context = "\n\n".join(
                            [f"[{n['name']}]\n{n['content']}" for n in fallback_notes]
                        )
                except Exception:
                    pass

        # Step 3: Hỏi Claude
        reply, tokens = ask_claude(text, notes_context)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await send_message(chat_id, reply, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await send_message(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

# Prefix command — định nghĩa cả phiên bản có dấu và không dấu
PREFIX_GHI_NHO_VAO = ["ghi nhớ vào ", "ghi nho vao "]
PREFIX_GHI_NHO     = ["ghi nhớ ", "ghi nho "]
PREFIX_NHAT_KY     = ["nhật ký ", "nhat ky "]
PREFIX_TIM         = ["tìm ", "tim "]
PREFIX_XEM         = ["xem ", "xem "]   # cần check riêng "xem nhật ký" trước
EXACT_XEM_NHAT_KY  = {"xem nhật ký", "xem nhat ky"}
EXACT_LIET_KE      = {"liệt kê", "liet ke"}


async def handle_message(chat_id: str, text: str):
    text = text.strip()
    if not text:
        return

    # ── BƯỚC 1: Nếu có pending state → thử resolve trước ──────────────────────
    # User đang trong context "chọn 1/2" hoặc "yes/no"
    if await _try_resolve_pending(chat_id, text):
        return

    # ── BƯỚC 2: Lệnh hệ thống ─────────────────────────────────────────────────
    low = _norm(text)

    if text == "/start":
        await _cmd_start(chat_id); return
    if text == "/cost":
        await _cmd_cost(chat_id); return
    if text == "/test":
        await _cmd_test(chat_id); return
    if text == "/security":
        await _cmd_security(chat_id); return

    # ── BƯỚC 3: Lệnh exact-match (xem nhật ký, liệt kê) ──────────────────────
    if low in EXACT_XEM_NHAT_KY:
        await _cmd_xem_nhat_ky(chat_id); return
    if low in EXACT_LIET_KE:
        await _cmd_liet_ke(chat_id); return

    # ── BƯỚC 4: Lệnh prefix (theo thứ tự ưu tiên) ────────────────────────────
    # ghi nhớ vào (PHẢI check trước "ghi nhớ")
    matched = _starts_with_any(text, PREFIX_GHI_NHO_VAO)
    if matched:
        body = _strip_prefix(text, matched)
        await _cmd_ghi_nho_vao(chat_id, body); return

    # ghi nhớ
    matched = _starts_with_any(text, PREFIX_GHI_NHO)
    if matched:
        content = _strip_prefix(text, matched)
        await _cmd_ghi_nho(chat_id, content); return

    # nhật ký
    matched = _starts_with_any(text, PREFIX_NHAT_KY)
    if matched:
        content = _strip_prefix(text, matched)
        await _cmd_nhat_ky(chat_id, content); return

    # tìm
    matched = _starts_with_any(text, PREFIX_TIM)
    if matched:
        keyword = _strip_prefix(text, matched)
        await _cmd_tim(chat_id, keyword); return

    # xem (sau khi đã check xem nhật ký exact)
    matched = _starts_with_any(text, PREFIX_XEM)
    if matched:
        name_query = _strip_prefix(text, matched)
        await _cmd_xem(chat_id, name_query); return

    # tóm tắt tuần
    if ("tóm tắt" in low or "tom tat" in low) and ("tuần" in low or "tuan" in low):
        await _cmd_tom_tat_tuan(chat_id); return

    # ── BƯỚC 5: Câu hỏi tự nhiên → smart search + Claude ─────────────────────
    await _handle_general_question(chat_id, text)


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK & HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"ok": True}
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "")

        # Lớp 7: Telegram Chat ID lock
        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"[security] Rejected message from unauthorized chat_id={chat_id}")
            return {"ok": True}

        if text:
            await handle_message(chat_id, text)
    except Exception as e:
        traceback.print_exc()
    return {"ok": True}


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "running", "version": "v5-features"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
