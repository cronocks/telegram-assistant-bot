"""core_handler.py — Channel-agnostic message dispatcher.

The single public entry point is `handle_message(msg, deps)`. It assumes a
caller (a channel adapter) has already authorized the inbound message and
normalized it to a ChannelMessage. All side effects (LLM calls, storage
access, replies) go through `deps`, which holds the active adapters.

User-facing strings remain Vietnamese; everything else is English.

Command handlers themselves live in `cmd_*.py` modules. This file keeps the
command table, the main dispatcher, and a few system-level commands (start,
help, cost, test, security, general free-form question, and backup export).
"""
import traceback

from cmd_audit import (
    _cmd_khoi_phuc,
    _cmd_xem_audit,
    _cmd_xem_thung_rac,
    _cmd_xoa_han,
)
from cmd_notes import (
    _cmd_bo_chia_se,
    _cmd_chia_se,
    _cmd_ghi_nho,
    _cmd_ghi_nho_vao,
    _cmd_liet_ke,
    _cmd_nhat_ky,
    _cmd_tim,
    _cmd_whoami,
    _cmd_xem,
    _cmd_xem_nhat_ky,
    _cmd_xem_scope,
)
from cmd_sudo import (
    _cmd_dat_mat_khau,
    _cmd_dat_web_pass,
    _cmd_doi_web_pass_self,
    _cmd_sudo,
    _cmd_thoat_sudo,
)
from cmd_task import (
    _cmd_cau_hinh_gio_mac_dinh,
    _cmd_cau_hinh_tong_ket,
    _cmd_danh_sach_lich_hoc,
    _cmd_danh_sach_task,
    _cmd_hoan_task,
    _cmd_huy_lich_hoc,
    _cmd_huy_task,
    _cmd_lich_hoc,
    _cmd_sua_lich_hoc,
    _cmd_tao_task,
    _cmd_tom_tat_hom_nay,
    _cmd_xem_task,
    _cmd_xong_task,
    _handle_callback,
)
from cmd_anniversary import (
    _cmd_danh_sach_ky_niem,
    _cmd_sua_ky_niem,
    _cmd_them_ky_niem,
    _cmd_xem_ky_niem,
    _cmd_xoa_ky_niem,
)
from cmd_family import (
    _cmd_danh_sach_nguoi_than,
    _cmd_sua_mo_phan,
    _cmd_sua_nguoi_than,
    _cmd_them_mo_phan,
    _cmd_them_nguoi_than,
    _cmd_tim_mo,
    _cmd_xem_nguoi_than,
    _cmd_xoa_mo_phan,
    _cmd_xoa_nguoi_than,
)
from cmd_ledger import (
    _cmd_bao_cao_nam,
    _cmd_bao_cao_thang,
    _cmd_chi,
    _cmd_dat_han_muc_chi,
    _cmd_dat_muc_tieu_tiet_kiem,
    _cmd_danh_sach_ghi_chep,
    _cmd_ghi_chep_xem,
    _cmd_huy_ghi_chep,
    _cmd_chi_the,
    _cmd_sua_danh_muc,
    _cmd_sua_ghi_chep,
    _cmd_them_danh_muc,
    _cmd_them_the,
    _cmd_thu,
    _cmd_tra_the,
    _cmd_xem_chi_tieu,
    _cmd_xem_danh_muc,
    _cmd_xem_han_muc,
    _cmd_xem_the,
    _cmd_xoa_danh_muc,
    _cmd_xoa_the,
)
from cmd_user import (
    _cmd_dat_birthdate,
    _cmd_dat_cha,
    _cmd_dat_quota,
    _cmd_dat_username,
    _cmd_doi_role,
    _cmd_duyet_birthdate,
    _cmd_duyet_username,
    _cmd_reset_quota,
    _cmd_them_user,
    _cmd_xem_cha,
    _cmd_xem_danh_sach_user,
    _cmd_xem_quota,
    _cmd_xoa_user,
)
from cmd_utils import _acl_filter_notes, _norm, _try_resolve_pending
from cmd_wiki import (
    _cmd_cap_nhat_tri_nho,
    _cmd_tom_tat_tuan,
    _cmd_wiki_ingest,
    _cmd_wiki_query,
    _cmd_xem_ho_so,
    _cmd_xem_tri_nho,
    _cmd_xem_wiki_list,
    _cmd_xem_wiki_page,
)
from cost_monitor import check_and_alert, get_current_cost, record_usage
from deps import CoreDeps
from interfaces import ChannelMessage, User
from permissions import has_role
from security import get_security_status
from text_utils import match_command


# ═══════════════════════════════════════════════════════════════════════════════
# System / help / cost commands
# ═══════════════════════════════════════════════════════════════════════════════

async def _cmd_start(chat_id: str, deps: CoreDeps) -> None:
    await deps.channel.send(chat_id, (
        "Xin chao! Toi la Claude Bot.\n\n"
        "Chon nhom lenh de xem chi tiet:\n\n"
        "📝 *Ghi chu & Nhat ky* — `/help ghi chu`\n"
        "📚 *Wiki* — `/help wiki`\n"
        "🧠 *Tri nho* — `/help tri nho`\n"
        "📋 *Cong viec* — `/help cong viec`\n"
        "📅 *Ky niem* — `/help ky niem`\n"
        "🌳 *Gia pha* — `/help gia pha`\n"
        "💰 *Chi tieu* — `/help chi tieu`\n"
        "👥 *Nguoi dung* — `/help nguoi dung`\n"
        "💰 *Quota* — `/help quota`\n"
        "🔍 *Tim kiem & Xem* — `/help xem`\n"
        "🔐 *Quan tri (sudo)* — `/help sudo`\n"
        "⚙️ *He thong* — `/help he thong`\n\n"
        "💬 *Hoi dap tu do:* Gõ cau hoi bat ky — bot tim wiki + vault roi tra loi."
    ))


_HELP_PAGES: dict[str, tuple[str, str]] = {
    "ghi chu": (
        "📝 *GHI CHU & NHAT KY*",
        "`ghi nho [noi dung]` — Tao file ghi chu moi (Claude tu dat ten)\n"
        "`ghi nho vao [ten]: [noi dung]` — Them vao file co san (fuzzy match)\n"
        "`nhat ky [noi dung]` — Them vao file nhat ky hom nay (GMT+7)\n"
        "`chia se [ten-file]` — Chia se file voi ca nha (scope = everyone)\n"
        "`bo chia se [ten-file]` — Chuyen file ve rieng tu (scope = private)",
    ),
    "wiki": (
        "📚 *WIKI*",
        "`wiki [noi dung]` — Ingest vao wiki (Claude tu to chuc theo topic)\n"
        "`hoi wiki [cau hoi]` — Hoi truc tiep tu wiki\n"
        "`xem wiki` — Liet ke tat ca wiki pages\n"
        "`xem wiki [topic]` — Doc 1 wiki page",
    ),
    "nguoi dung": (
        "👥 *NGUOI DUNG*",
        "`them user: [ten], [role]` — Them user moi (admin)\n"
        "`xem danh sach user` — Liet ke tat ca user (admin)\n"
        "`xoa user: [ten/id]` — Xoa user (admin)\n"
        "`doi role: [ten/id] [role moi]` — Doi role cua user (admin)\n"
        "`dat username: [ten]` — Dat username cua ban\n"
        "`duyet username` — Duyet yeu cau doi username (admin)\n"
        "`dat birthdate: [YYYY-MM-DD]` — Dat ngay sinh\n"
        "`duyet birthdate` — Duyet yeu cau doi ngay sinh (admin/manager)\n"
        "`dat cha: [ten/id-con] [ten/id-cha]` — Gan quan he cha/me — con (admin)\n"
        "`xem cha: [ten/id]` — Xem quan he cha/me cua user (admin)\n"
        "`toi la ai` — Xem tai khoan ban dang dung",
    ),
    "quota": (
        "💰 *QUOTA*",
        "`xem quota` — Xem muc su dung token cua ban\n"
        "`xem quota [ten/id]` — Xem quota cua user khac (admin)\n"
        "`dat quota: [ten/id] [so-token]` — Dat gioi han token (admin)\n"
        "`reset quota: [ten/id]` — Reset so dung ve 0 (admin)",
    ),
    "xem": (
        "🔍 *TIM KIEM & XEM*",
        "`xem nhat ky` — Doc nhat ky hom nay\n"
        "`xem [ten]` — Doc 1 file (fuzzy match)\n"
        "`xem scope [ten]` — Xem scope/owner cua 1 file\n"
        "`liet ke` — Liet ke tat ca file (phan trang, moi nhat truoc)\n"
        "`liet ke [trang]` — Xem trang cu the\n"
        "`tim [tu khoa]` — Tim trong noi dung file\n"
        "`tom tat tuan nay` — Tom tat ghi chu 7 ngay",
    ),
    "tri nho": (
        "🧠 *TRI NHO*",
        "`xem tri nho` — Xem snapshot bo nho cua ban\n"
        "`xem ho so` — Xem ho so ca nhan cua ban\n"
        "`cap nhat tri nho` — Cap nhat bo nho tu ghi chu gan day (LLM curation)",
    ),
    "cong viec": (
        "📋 *CONG VIEC (TASK)*",
        "`tao task: [mo ta]` — Tao task moi (LLM parse deadline tu mo ta tu nhien)\n"
        "`task: [mo ta]` — Tuong duong tao task\n"
        "`xong task: [id]` — Danh dau task hoan thanh\n"
        "`huy task: [id]` — Huy task\n"
        "`task [id]` — Xem chi tiet task\n"
        "`danh sach task` — Liet ke task dang cho\n"
        "`lich hoc: [mo ta]` — Tao lich hoc (recurring; category=study)\n"
        "`danh sach lich hoc` — Xem tat ca lich hoc dang hoat dong\n"
        "`sua lich hoc: [id] [mo ta moi]` — Cap nhat lich hoc (thay doi gio/ngay)\n"
        "`huy lich hoc: [id]` — Huy mot lich hoc\n"
        "`hoan task: [id] [phut]` — Hoan task them N phut\n"
        "`tom tat hom nay` — Tong ket task hom nay\n"
        "`cau hinh tong ket: [HH:MM | tat]` — Doi gio gui tong ket hang ngay\n"
        "`cau hinh gio mac dinh: [HH:MM]` — Doi gio mac dinh cho task 'mai'",
    ),
    "ky niem": (
        "📅 *KY NIEM (ANNIVERSARY)*",
        "`them ky niem: [ten], am/duong DD/MM[/YYYY][ nhuan], [loai]` — Tao moi (loai: gio/cuoi/khac)\n"
        "  Vi du: `them ky niem: Gio ong noi, am 10/3, gio`\n"
        "  Vi du: `them ky niem: Ky niem cuoi, duong 15/8, cuoi`\n"
        "  Vi du: `them ky niem: Gio ba, am 9/5/1987, gio` (co nam)\n"
        "  Vi du: `them ky niem: Gio co, am 3/3 nhuan, gio` (thang nhuan)\n"
        "`danh sach ky niem` — Liet ke tat ca ky niem cua ban\n"
        "`ky niem [id]` — Xem chi tiet 1 ky niem\n"
        "`sua ky niem: [id], ten=..., ngay=am/duong DD/MM[/YYYY][ nhuan], loai=..., nhac=...` — Cap nhat\n"
        "`xoa ky niem: [id]` — Xoa (soft-delete)\n\n"
        "Mac dinh nhac truoc: 30/15/7/3/1 ngay + dung ngay (08:00 sang).\n"
        "Ngay am lich tu dong quy doi sang duong moi nam.",
    ),
    "gia pha": (
        "🌳 *GIA PHA (FAMILY TREE)*",
        "`them nguoi than: [ten], doi [n], sinh [ngay], mat [ngay], gioi tinh [nam/nu], ten goi [..], chi [..], ghi chu [..]` — Tao ho so nguoi than (admin/manager)\n"
        "  Ngay: `am DD/MM/YYYY`, `duong DD/MM/YYYY`, `YYYY` hoac `khoang YYYY`\n"
        "  Vi du: `them nguoi than: Nguyen Van A, doi 3, sinh am 10/2/1920, mat am 15/7/1990`\n"
        "`danh sach nguoi than [doi n]` — Liet ke nguoi than (loc theo doi)\n"
        "`xem nguoi than [id | ten]` — Xem ho so day du + mo phan\n"
        "`sua nguoi than: [id], [muc]=[gia tri], ...` — Cap nhat (muc: ten, doi, sinh, mat, gioi tinh, ten goi, chi, ghi chu)\n"
        "`xoa nguoi than: [id]` — Xoa (soft-delete)\n\n"
        "`them mo phan: [id nguoi than], [nghia trang], dia chi [..], gps [lat],[lng], lo [..], ghi chu [..]` — Luu thong tin mo phan\n"
        "  Vi du: `them mo phan: 5, Nghia trang Van Dien, gps 20.9456,105.8231, lo B3 hang 12`\n"
        "`tim mo [ten | id]` — Tra cuu mo phan nhanh (kem link Google Maps)\n"
        "`sua mo phan: [id], [muc]=[gia tri], ...` — Cap nhat mo phan\n"
        "`xoa mo phan: [id]` — Xoa ban ghi mo phan",
    ),
    "sudo": (
        "🔐 *QUAN TRI (SUDO)*",
        "`sudo: [mat khau]` — Nang quyen len admin trong 15 phut (chi role manager)\n"
        "`thoat sudo` — Ha quyen admin ngay lap tuc\n"
        "`dat mat khau: [mat khau]` — Dat/doi mat khau admin (chi tu tai khoan admin goc)\n"
        "`xem audit` — Xem audit log gan day (admin); ho tro phan trang va filter\n"
        "`xem audit [trang]` — Trang cu the (vd `xem audit 2`)\n"
        "`xem audit [action]` — Filter theo action (vd `xem audit sudo_elevate`)\n"
        "`xem audit [type] [id]` — Filter theo target (vd `xem audit note 42`)\n"
        "`xem thung rac` — Liet ke item da xoa (user/note/wiki) (admin)\n"
        "`khoi phuc: [kind] [id]` — Khoi phuc item (vd `khoi phuc: user 3`) (admin)\n"
        "`xoa han: [kind] [id]` — Xoa han khoi he thong (vd `xoa han: note 12`) (admin)\n"
        "`doi web pass: [mat khau]` — Tu dat mat khau web cua ban (min 8 ky tu; moi user)\n"
        "`xuat du lieu` — Export du lieu cua ban len Drive (ZIP) (moi user; gioi han 5 phut)\n"
        "`xuat du lieu: [ten]` — Admin export du lieu cua nguoi khac len Drive (admin only)\n"
        "Luu y: tin nhan chua mat khau se duoc bot tu dong xoa khoi chat.",
    ),
    "he thong": (
        "⚙️ *HE THONG*",
        "`/cost` — Chi phi su dung thang nay\n"
        "`/test` — Kiem tra ket noi Drive\n"
        "`/security` — Cau hinh bao mat",
    ),
    "chi tieu": (
        "💰 *CHI TIEU (EXPENSE TRACKING)*",
        "`chi: <so> <mo ta>` — Ghi chi tieu (vd: chi: 50k an trua)\n"
        "`thu: <so> <mo ta>` — Ghi thu nhap (vd: thu: 5tr luong)\n"
        "`ghi chep: <id>` — Xem chi tiet but toan\n"
        "`danh sach ghi chep` — Liet ke 20 but toan gan nhat\n"
        "`sua ghi chep: <id>, so=<so>[, mo ta=<text>][, danh muc=<id>]` — Cap nhat but toan\n"
        "`huy ghi chep: <id>` — Huy (soft-delete) but toan\n"
        "`xem danh muc` — Xem danh muc chi/thu\n"
        "`them danh muc: <ten>, chi|thu[, chung]` — Them danh muc moi (chung: chi admin/manager)\n"
        "`xoa danh muc: <id>` — Xoa danh muc\n"
        "`sua danh muc: <id> <ten moi>` — Doi ten danh muc\n"
        "`bao cao thang [YYYY-MM]` — Bao cao thang (mac dinh thang hien tai)\n"
        "`bao cao nam` — Bao cao nam hien tai\n"
        "`xem chi tieu` — Chi tieu 7 ngay qua\n"
        "`dat han muc chi: <so>` — Dat han muc chi thang nay\n"
        "`dat muc tieu tiet kiem: <so>` — Dat muc tieu tiet kiem\n"
        "`xem han muc` — Xem han muc chi va muc tieu tiet kiem\n"
        "`them the: <ten>` — Them the tin dung (vd: them the: Visa)\n"
        "`xem the` — Liet ke the + du no tung the\n"
        "`xoa the: <id>` — Xoa the tin dung\n"
        "`chi the <ten>: <so> <mo ta>` — Ghi mot khoan tieu bang the (van tinh la chi)\n"
        "`tra the <ten>: <so>` — Ghi tra tien sao ke the (KHONG tinh la chi)",
    ),
}

# Alias map: normalized input → canonical key in _HELP_PAGES
_HELP_ALIASES: dict[str, str] = {
    "ghi chu": "ghi chu",
    "ghi chú": "ghi chu",
    "wiki": "wiki",
    "nguoi dung": "nguoi dung",
    "người dùng": "nguoi dung",
    "quota": "quota",
    "xem": "xem",
    "tri nho": "tri nho",
    "trí nhớ": "tri nho",
    "cong viec": "cong viec",
    "công việc": "cong viec",
    "task": "cong viec",
    "ky niem": "ky niem",
    "kỷ niệm": "ky niem",
    "gia pha": "gia pha",
    "gia phả": "gia pha",
    "he thong": "he thong",
    "hệ thống": "he thong",
    "sudo": "sudo",
    "quan tri": "sudo",
    "quản trị": "sudo",
    "chi tieu": "chi tieu",
    "chi tiêu": "chi tieu",
    "expense": "chi tieu",
    "ledger": "chi tieu",
}


async def _cmd_help(chat_id: str, group: str, deps: CoreDeps) -> None:
    key = _HELP_ALIASES.get(_norm(group).strip())
    if key is None:
        groups = "  ".join(f"`/help {k}`" for k in _HELP_PAGES)
        await deps.channel.send(
            chat_id,
            f"Nhom lenh '{group}' khong ton tai.\n\nCac nhom: {groups}",
            use_markdown=False,
        )
        return
    title, body = _HELP_PAGES[key]
    await deps.channel.send(chat_id, f"{title}\n\n{body}")


async def _cmd_cost(chat_id: str, deps: CoreDeps) -> None:
    info = get_current_cost()
    bar_filled = int(info["percent"] / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    await deps.channel.send(chat_id, (
        f"Chi phi thang {info['month']}\n\n"
        f"`{bar}` {info['percent']}%\n"
        f"Da dung: `${info['cost_usd']}` / `$10.00`\n"
        f"Input tokens: `{info.get('input_tokens', 0):,}`\n"
        f"Output tokens: `{info.get('output_tokens', 0):,}`"
    ))


async def _cmd_test(chat_id: str, deps: CoreDeps) -> None:
    await deps.channel.send(chat_id, "Dang kiem tra Drive...")
    try:
        result = deps.notes.test_connection()
        await deps.channel.send(
            chat_id,
            f"OK Drive\nFolder: {result.get('name')}\nID: {result.get('id')}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Drive Error: {str(e)[:500]}", use_markdown=False,
        )


async def _cmd_security(chat_id: str, deps: CoreDeps) -> None:
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
        await deps.channel.send(chat_id, msg, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Free-form question fallback
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_general_question(
    chat_id: str, text: str, deps: CoreDeps, user: User | None = None,
) -> None:
    """Free-form question fallback — smart search + Claude answer.

    Pipeline:
      1. extract_search_intent(text) → keywords + days_back + needs_search
      2. If needs_search: retrieve wiki pages + smart_search raw notes
      3. ask Claude with the combined context
    """
    await deps.channel.send(chat_id, "Dang xu ly...")
    try:
        # Step 1: intent extraction.
        intent, intent_tokens = deps.llm.extract_search_intent(text)
        record_usage(intent_tokens // 2, intent_tokens // 2)

        context_parts: list[str] = []

        # Step 2: wiki pages via index (ACL-filtered).
        if intent.get("needs_search"):
            try:
                visible_slugs = (
                    deps.note_index.visible_wiki_slugs(user.id)
                    if user is not None else None
                )
                wiki_pages = deps.wiki.retrieve_pages(
                    text, intent.get("keywords", []), visible_slugs=visible_slugs,
                )
                if wiki_pages:
                    wiki_block = "\n\n".join(
                        f"[Wiki: {p['name'].replace('.md', '')}]\n{p['content']}"
                        for p in wiki_pages
                    )
                    context_parts.append(wiki_block)
            except Exception as e:
                print(f"[core] Wiki search error (non-fatal): {e}")

        # Step 3: smart search raw notes (ACL-filtered).
        if intent.get("needs_search") and intent.get("keywords"):
            try:
                notes = deps.notes.smart_search(
                    keywords=intent["keywords"],
                    days_back=intent.get("days_back", 0) or 0,
                )
                if user is not None:
                    notes = _acl_filter_notes(notes, user, deps)
                if notes:
                    notes_block = "\n\n".join(
                        [f"[{n['name']}]\n{n['content']}" for n in notes[:5]]
                    )
                    context_parts.append(notes_block)
            except Exception as e:
                print(f"[core] Smart search error: {e}")
                try:
                    fallback_notes = deps.notes.search_notes(
                        intent["keywords"][0], max_results=2,
                    )
                    if user is not None:
                        fallback_notes = _acl_filter_notes(fallback_notes, user, deps)
                    if fallback_notes:
                        context_parts.append("\n\n".join(
                            [f"[{n['name']}]\n{n['content']}" for n in fallback_notes]
                        ))
                except Exception:
                    pass

        notes_context = "\n\n".join(context_parts)

        # Step 4: prepend L1 memory snapshot (if any) so Claude knows the user.
        if user is not None:
            memory_content = deps.memory_store.get(user.id, "memory")
            if memory_content:
                memory_block = f"[Bộ nhớ cá nhân]\n{memory_content}"
                notes_context = (
                    memory_block + ("\n\n" + notes_context if notes_context else "")
                )

        # Step 5: ask Claude.
        reply, tokens = deps.llm.ask(text, notes_context)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        if user is not None:
            deps.user_store.record_usage(user.id, tokens)
        await deps.channel.send(chat_id, reply, use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Loi: {str(e)[:500]}", use_markdown=False)


def _is_over_quota(user: User, deps: CoreDeps) -> bool:
    """Return True if the user has exceeded their monthly token quota."""
    if has_role(user, "admin"):
        return False
    quota = deps.user_store.get_quota(user.id)
    if quota is None or quota["monthly_token_limit"] == 0:
        return False
    return quota["used_tokens"] >= quota["monthly_token_limit"]


# ═══════════════════════════════════════════════════════════════════════════════
# Backup / export commands (FR-6)
# ═══════════════════════════════════════════════════════════════════════════════

def _export_zip_filename(user_name: str) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user_name)
    return f"export_{safe}_{ts}.zip"


async def _cmd_xuat_du_lieu_self(chat_id: str, remainder: str, user: "User", deps: CoreDeps) -> None:
    """xuat du lieu — export caller's own data; upload ZIP to Drive; reply with link."""
    if remainder.strip():
        await deps.channel.send(
            chat_id,
            f"De export du lieu cua nguoi khac, dung: xuat du lieu: <ten>\n"
            f"Vi du: xuat du lieu: {remainder.strip()}",
            use_markdown=False,
        )
        return

    if deps.backup_engine is None:
        await deps.channel.send(chat_id, "Tinh nang backup chua duoc cau hinh.", use_markdown=False)
        return

    remaining = deps.backup_engine.export_cooldown_remaining(user.id)
    if remaining > 0:
        await deps.channel.send(
            chat_id,
            f"Vui long doi {remaining} giay truoc khi export lan tiep theo.",
            use_markdown=False,
        )
        return

    await deps.channel.send(chat_id, "Dang tao backup, vui long cho...", use_markdown=False)
    try:
        zip_bytes, manifest = deps.backup_engine.generate_export(user.id)
        filename = _export_zip_filename(user.name)
        _file_id, link = deps.backup_engine.upload_to_drive(filename, zip_bytes)
    except Exception as exc:
        await deps.channel.send(
            chat_id,
            f"Export that bai: {str(exc)[:300]}",
            use_markdown=False,
        )
        return

    stats = manifest.get("stats", {})
    await deps.channel.send(
        chat_id,
        f"Da tao backup thanh cong!\n"
        f"  Ghi chu: {stats.get('notes', 0)}\n"
        f"  Wiki: {stats.get('wiki_pages', 0)}\n"
        f"  Cuoc tro chuyen: {stats.get('web_conversations', 0)}\n"
        f"Link: {link}",
        use_markdown=False,
    )


async def _cmd_xuat_du_lieu_admin(
    chat_id: str, remainder: str, user: "User", deps: CoreDeps,
) -> None:
    """xuat du lieu: <ten> — admin exports data for a named user; uploads to Drive."""
    if not user.is_admin:
        await deps.channel.send(chat_id, "Chi admin moi co the xuat du lieu cho nguoi khac.", use_markdown=False)
        return

    if deps.backup_engine is None:
        await deps.channel.send(chat_id, "Tinh nang backup chua duoc cau hinh.", use_markdown=False)
        return

    target_name = remainder.strip()
    if not target_name:
        await deps.channel.send(chat_id, "Cu phap: xuat du lieu: <ten>", use_markdown=False)
        return

    target = deps.user_store.find_by_username_or_name(target_name)
    if target is None or not target.is_active:
        await deps.channel.send(
            chat_id, f"Khong tim thay user: {target_name}", use_markdown=False,
        )
        return

    remaining = deps.backup_engine.export_cooldown_remaining(target.id)
    if remaining > 0:
        await deps.channel.send(
            chat_id,
            f"Rate limit: doi {remaining} giay (cooldown cua user {target.name}).",
            use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, f"Dang tao backup cho {target.name}, vui long cho...", use_markdown=False,
    )
    try:
        zip_bytes, manifest = deps.backup_engine.generate_export(target.id)
        # Override audit delivery field logged by generate_export.
        deps.audit.log(
            actor_user_id=user.id,
            action="data_export",
            target_type="user",
            target_id=target.id,
            payload={"size_bytes": len(zip_bytes), "delivery": "telegram_drive"},
        )
        filename = _export_zip_filename(target.name)
        _file_id, link = deps.backup_engine.upload_to_drive(filename, zip_bytes)
    except Exception as exc:
        await deps.channel.send(
            chat_id,
            f"Export that bai: {str(exc)[:300]}",
            use_markdown=False,
        )
        return

    stats = manifest.get("stats", {})
    await deps.channel.send(
        chat_id,
        f"Da tao backup cho {target.name}!\n"
        f"  Ghi chu: {stats.get('notes', 0)}\n"
        f"  Wiki: {stats.get('wiki_pages', 0)}\n"
        f"  Cuoc tro chuyen: {stats.get('web_conversations', 0)}\n"
        f"Link: {link}",
        use_markdown=False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

# Command table — {command_id: [prefix, ...]}
# Longer prefixes must be listed before shorter ones within the same command
# so longest-prefix-first in match_command resolves ambiguity correctly.
# Names that map directly to Vietnamese user input stay Vietnamese per CLAUDE.md.
_COMMAND_TABLE: dict[str, list[str]] = {
    "THEM_USER":          ["thêm user: ", "them user: ", "add user: "],
    "XEM_DANH_SACH_USER": ["xem danh sach user", "xem danh sách user", "list users"],
    "XOA_USER":           ["xoa user: ", "xóa user: ", "delete user: "],
    "DOI_ROLE":           ["doi role: ", "đổi role: ", "change role: "],
    "DAT_BIRTHDATE":      ["dat birthdate: ", "đặt birthdate: ", "set birthdate: "],
    "DUYET_BIRTHDATE":    ["duyet birthdate", "duyệt birthdate", "approve birthdate"],
    "DAT_USERNAME":       ["dat username: ", "đặt username: ", "set username: "],
    "DUYET_USERNAME":     ["duyet username", "duyệt username", "approve username"],
    "DAT_CHA":            ["dat cha: ", "đặt cha: ", "set parent: "],
    "XEM_CHA":            ["xem cha: ", "view parent: "],
    "XEM_QUOTA":          ["xem quota", "view quota"],
    "DAT_QUOTA":          ["dat quota: ", "đặt quota: ", "set quota: "],
    "RESET_QUOTA":        ["reset quota: "],
    "XEM_AUDIT":          ["xem audit"],
    "XEM_THUNG_RAC":      ["xem thung rac", "xem thùng rác"],
    "KHOI_PHUC":          ["khoi phuc: ", "khôi phục: "],
    "XOA_HAN":            ["xoa han: ", "xóa hẳn: "],
    "BO_CHIA_SE_FILE":    ["bỏ chia sẻ ", "bo chia se "],
    "CHIA_SE_FILE":       ["chia sẻ ", "chia se "],
    "HOI_WIKI":           ["hỏi wiki ", "hoi wiki ", "ask wiki "],
    "XEM_WIKI_PAGE":      ["xem wiki "],
    "XEM_WIKI":           ["xem wiki"],
    "WIKI":               ["wiki "],
    "GHI_NHO_VAO":        ["ghi nhớ vào ", "ghi nho vao "],
    "GHI_NHO":            ["ghi nhớ ", "ghi nho "],
    "NHAT_KY":            ["nhật ký ", "nhat ky "],
    "XEM_NHAT_KY":        ["xem nhật ký", "xem nhat ky"],
    "XEM_TRI_NHO":        ["xem trí nhớ", "xem tri nho"],
    "XEM_HO_SO":          ["xem hồ sơ", "xem ho so"],
    "XEM_SCOPE":          ["xem scope "],
    "TOI_LA_AI":          ["toi la ai", "tôi là ai", "tai khoan", "tài khoản", "who am i", "whoami"],
    "DAT_MAT_KHAU":       ["dat mat khau: ", "đặt mật khẩu: ", "set password: "],
    "DAT_WEB_PASS":       ["dat web pass: ", "đặt web pass: "],
    "DOI_WEB_PASS_SELF":  ["doi web pass: ", "đổi web pass: "],
    "THOAT_SUDO":         ["thoat sudo", "thoát sudo", "exit sudo"],
    "SUDO":               ["sudo: "],
    "CAP_NHAT_TRI_NHO":   ["cập nhật trí nhớ", "cap nhat tri nho"],
    "LIET_KE":            ["liệt kê", "liet ke"],
    "TIM":                ["tìm ", "tim ", "search "],
    # "xem danh muc / xem chi tieu / xem han muc" must come before the catch-all "xem " below.
    "XEM_DANH_MUC":         ["xem danh muc", "xem danh mục"],
    "XEM_CHI_TIEU":         ["xem chi tieu", "xem chi tiêu"],
    "XEM_HAN_MUC":          ["xem han muc", "xem hạn mức"],
    "XEM":                ["xem "],
    "TOM_TAT_TUAN":       ["tóm tắt tuần này", "tom tat tuan nay", "tóm tắt tuần", "tom tat tuan"],
    # Longer prefix first: "xuat du lieu: <ten>" must match before "xuat du lieu"
    "XUAT_DU_LIEU_ADMIN": ["xuat du lieu: ", "xuất dữ liệu: "],
    "XUAT_DU_LIEU_SELF":  ["xuat du lieu", "xuất dữ liệu"],
    # ── FR-7 task commands ────────────────────────────────────────────────────
    # Longer prefixes first within each group to avoid swallowing shorter ones.
    "TAO_TASK":           ["tạo task: ", "tao task: ", "task: "],
    "XONG_TASK":          ["xong task: ", "done task: "],
    "HUY_TASK":           ["hủy task: ", "huy task: ", "xóa task: ", "xoa task: "],
    "DANH_SACH_TASK":     ["danh sách task", "danh sach task", "list task"],
    # Study schedule commands — longer prefixes before shorter "lich hoc:"
    "DANH_SACH_LICH_HOC": ["danh sách lịch học", "danh sach lich hoc"],
    "HUY_LICH_HOC":       ["hủy lịch học: ", "huy lich hoc: "],
    "SUA_LICH_HOC":       ["sửa lịch học: ", "sua lich hoc: ", "đổi lịch học: ", "doi lich hoc: "],
    "LICH_HOC":           ["lịch học: ", "lich hoc: "],
    "HOAN_TASK":          ["hoãn task: ", "hoan task: ", "snooze: "],
    # "task <id>" (space, no colon) — must be listed AFTER "task: " variant above
    # so the table ordering keeps longest-prefix logic intact in match_command.
    "XEM_TASK":           ["task "],
    # ── FR-7 daily summary commands ───────────────────────────────────────────
    # Longer prefixes first within each group.
    "CAU_HINH_TONG_KET":     ["cấu hình tổng kết: ", "cau hinh tong ket: "],
    "CAU_HINH_GIO_MAC_DINH": ["cấu hình giờ mặc định: ", "cau hinh gio mac dinh: "],
    "TOM_TAT_HOM_NAY":       ["tóm tắt hôm nay", "tom tat hom nay"],
    # ── FR-8 anniversary commands ────────────────────────────────────────────
    # Longer prefixes first within each group to avoid swallowing shorter ones.
    "THEM_KY_NIEM":     ["thêm kỷ niệm: ", "them ky niem: "],
    "XOA_KY_NIEM":      ["xóa kỷ niệm: ", "xoa ky niem: "],
    "SUA_KY_NIEM":      ["sửa kỷ niệm: ", "sua ky niem: ", "đổi kỷ niệm: ", "doi ky niem: "],
    "DANH_SACH_KY_NIEM": ["danh sách kỷ niệm", "danh sach ky niem"],
    "XEM_KY_NIEM":      ["kỷ niệm ", "ky niem "],
    # ── FR-9 ledger commands ──────────────────────────────────────────────────
    "CHI":                  ["chi: "],
    "THU":                  ["thu: "],
    "GHI_CHEP_XEM":         ["ghi chep: ", "ghi chép: "],
    "DANH_SACH_GHI_CHEP":   ["danh sach ghi chep", "danh sách ghi chép"],
    "SUA_GHI_CHEP":         ["sua ghi chep: ", "sửa ghi chép: "],
    "HUY_GHI_CHEP":         ["huy ghi chep: ", "hủy ghi chép: "],
    "THEM_DANH_MUC":        ["them danh muc: ", "thêm danh mục: "],
    "XOA_DANH_MUC":         ["xoa danh muc: ", "xóa danh mục: "],
    "SUA_DANH_MUC":         ["sua danh muc: ", "sửa danh mục: "],
    "BAO_CAO_THANG":        ["bao cao thang", "báo cáo tháng"],
    "BAO_CAO_NAM":          ["bao cao nam", "báo cáo năm"],
    "DAT_HAN_MUC":          ["dat han muc chi: ", "đặt hạn mức chi: "],
    "DAT_MUC_TIEU":         ["dat muc tieu tiet kiem: ", "đặt mục tiêu tiết kiệm: "],
    # ── FR-9 credit card commands ─────────────────────────────────────────────
    "CHI_THE":              ["chi the ", "chi thẻ "],
    "TRA_THE":              ["tra the ", "trả thẻ "],
    "THEM_THE":             ["them the: ", "thêm thẻ: "],
    "XOA_THE":              ["xoa the: ", "xóa thẻ: "],
    "XEM_THE":              ["xem the", "xem thẻ"],
    # ── FR-11 family tree commands ────────────────────────────────────────────
    # Longer prefixes first within each group.
    "THEM_NGUOI_THAN":      ["thêm người thân: ", "them nguoi than: "],
    "SUA_NGUOI_THAN":       ["sửa người thân: ", "sua nguoi than: "],
    "XOA_NGUOI_THAN":       ["xóa người thân: ", "xoa nguoi than: "],
    "DANH_SACH_NGUOI_THAN": ["danh sách người thân", "danh sach nguoi than"],
    "XEM_NGUOI_THAN":       ["xem người thân ", "xem nguoi than "],
    "THEM_MO_PHAN":         ["thêm mộ phần: ", "them mo phan: "],
    "SUA_MO_PHAN":          ["sửa mộ phần: ", "sua mo phan: "],
    "XOA_MO_PHAN":          ["xóa mộ phần: ", "xoa mo phan: "],
    "TIM_MO":               ["tìm mộ ", "tim mo "],
}


async def handle_message(msg: ChannelMessage, user: User, deps: CoreDeps) -> None:
    """Dispatch a normalized inbound message to the appropriate handler."""
    chat_id = msg.chat_id

    # ── Step 0: inline keyboard callback_query (no text, callback_data in raw) ─
    if msg.raw.get("callback_data"):
        await _handle_callback(msg, user, deps)
        return

    text = msg.text.strip()
    if not text:
        return

    # ── Step 1: try to resolve a pending state first ────────────────────────
    if await _try_resolve_pending(chat_id, text, user, deps):
        return

    # ── Step 2: slash commands (not normalized) ────────────────────────────
    if text == "/start":
        await _cmd_start(chat_id, deps); return
    if text.startswith("/help"):
        group = text[len("/help"):].strip()
        await _cmd_help(chat_id, group, deps); return
    if text == "/cost":
        await _cmd_cost(chat_id, deps); return
    if text == "/test":
        await _cmd_test(chat_id, deps); return
    if text == "/security":
        await _cmd_security(chat_id, deps); return

    # ── Step 2.5: quota enforcement for LLM-heavy operations ──────────────────
    # Non-LLM commands (user management, quota admin) bypass this check.
    _QUOTA_EXEMPT = {
        "THEM_USER", "XEM_DANH_SACH_USER", "XOA_USER", "DOI_ROLE",
        "DAT_BIRTHDATE", "DUYET_BIRTHDATE",
        "DAT_USERNAME", "DUYET_USERNAME",
        "DAT_CHA", "XEM_CHA",
        "XEM_QUOTA", "DAT_QUOTA", "RESET_QUOTA",
        "XEM_AUDIT",
        "XEM_THUNG_RAC", "KHOI_PHUC", "XOA_HAN",
        "CHIA_SE_FILE", "BO_CHIA_SE_FILE",
        "XEM_TRI_NHO", "XEM_HO_SO",
        "DAT_MAT_KHAU", "DOI_WEB_PASS_SELF", "SUDO", "THOAT_SUDO",
        "XUAT_DU_LIEU_SELF", "XUAT_DU_LIEU_ADMIN",
        # FR-7 non-LLM task commands.
        "XONG_TASK", "HUY_TASK", "DANH_SACH_TASK", "XEM_TASK", "HOAN_TASK",
        "TOM_TAT_HOM_NAY", "CAU_HINH_TONG_KET", "CAU_HINH_GIO_MAC_DINH",
        # FR-8 anniversary commands — no LLM, all structured parse.
        "THEM_KY_NIEM", "XOA_KY_NIEM", "SUA_KY_NIEM",
        "DANH_SACH_KY_NIEM", "XEM_KY_NIEM",
        # FR-11 family tree commands — no LLM, all structured parse.
        "THEM_NGUOI_THAN", "SUA_NGUOI_THAN", "XOA_NGUOI_THAN",
        "DANH_SACH_NGUOI_THAN", "XEM_NGUOI_THAN",
        "THEM_MO_PHAN", "SUA_MO_PHAN", "XOA_MO_PHAN", "TIM_MO",
        # FR-9 ledger commands — no LLM, all structured.
        "CHI", "THU", "GHI_CHEP_XEM", "DANH_SACH_GHI_CHEP",
        "SUA_GHI_CHEP", "HUY_GHI_CHEP",
        "XEM_DANH_MUC", "THEM_DANH_MUC", "XOA_DANH_MUC", "SUA_DANH_MUC",
        "BAO_CAO_THANG", "BAO_CAO_NAM", "XEM_CHI_TIEU",
        "DAT_HAN_MUC", "DAT_MUC_TIEU", "XEM_HAN_MUC",
    }
    _matched = match_command(text, _COMMAND_TABLE)
    if _matched is None or _matched[0] not in _QUOTA_EXEMPT:
        if _is_over_quota(user, deps):
            quota = deps.user_store.get_quota(user.id)
            await deps.channel.send(
                chat_id,
                f"Bạn đã dùng hết quota tháng này ({quota['used_tokens']:,}/{quota['monthly_token_limit']:,} tokens). "
                "Liên hệ admin để được reset hoặc tăng giới hạn.",
                use_markdown=False,
            )
            return

    # ── Step 3: prefix-based dispatch (longest-prefix-first, diacritic-agnostic) ──
    result = match_command(text, _COMMAND_TABLE)
    if result:
        cmd_id, remainder = result

        if cmd_id == "THEM_USER":
            await _cmd_them_user(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_DANH_SACH_USER":
            await _cmd_xem_danh_sach_user(chat_id, user, deps); return
        if cmd_id == "XOA_USER":
            await _cmd_xoa_user(chat_id, remainder, user, deps); return
        if cmd_id == "DOI_ROLE":
            await _cmd_doi_role(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_BIRTHDATE":
            await _cmd_dat_birthdate(chat_id, remainder, user, deps); return
        if cmd_id == "DUYET_BIRTHDATE":
            await _cmd_duyet_birthdate(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_USERNAME":
            await _cmd_dat_username(chat_id, remainder, user, deps); return
        if cmd_id == "DUYET_USERNAME":
            await _cmd_duyet_username(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_CHA":
            await _cmd_dat_cha(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_CHA":
            await _cmd_xem_cha(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_QUOTA":
            await _cmd_xem_quota(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_QUOTA":
            await _cmd_dat_quota(chat_id, remainder, user, deps); return
        if cmd_id == "RESET_QUOTA":
            await _cmd_reset_quota(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_AUDIT":
            await _cmd_xem_audit(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_THUNG_RAC":
            await _cmd_xem_thung_rac(chat_id, user, deps); return
        if cmd_id == "KHOI_PHUC":
            await _cmd_khoi_phuc(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_HAN":
            await _cmd_xoa_han(chat_id, remainder, user, deps); return
        if cmd_id == "CHIA_SE_FILE":
            await _cmd_chia_se(chat_id, remainder, user, deps); return
        if cmd_id == "BO_CHIA_SE_FILE":
            await _cmd_bo_chia_se(chat_id, remainder, user, deps); return
        if cmd_id == "HOI_WIKI":
            await _cmd_wiki_query(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_WIKI_PAGE":
            await _cmd_xem_wiki_page(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_WIKI":
            await _cmd_xem_wiki_list(chat_id, user, deps); return
        if cmd_id == "WIKI":
            await _cmd_wiki_ingest(chat_id, remainder, user, deps); return
        if cmd_id == "GHI_NHO_VAO":
            await _cmd_ghi_nho_vao(chat_id, remainder, deps); return
        if cmd_id == "GHI_NHO":
            await _cmd_ghi_nho(chat_id, remainder, user, deps); return
        if cmd_id == "NHAT_KY":
            await _cmd_nhat_ky(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_NHAT_KY":
            await _cmd_xem_nhat_ky(chat_id, deps); return
        if cmd_id == "LIET_KE":
            await _cmd_liet_ke(chat_id, remainder, user, deps); return
        if cmd_id == "TIM":
            await _cmd_tim(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_SCOPE":
            await _cmd_xem_scope(chat_id, remainder, user, deps); return
        if cmd_id == "XEM":
            await _cmd_xem(chat_id, remainder, user, deps); return
        if cmd_id == "TOI_LA_AI":
            await _cmd_whoami(chat_id, user, deps); return
        if cmd_id == "DAT_MAT_KHAU":
            message_id = msg.raw.get("message_id") if msg.raw else None
            await _cmd_dat_mat_khau(chat_id, remainder, user, message_id, deps); return
        if cmd_id == "DAT_WEB_PASS":
            await _cmd_dat_web_pass(chat_id, remainder, user, deps); return
        if cmd_id == "DOI_WEB_PASS_SELF":
            message_id = msg.raw.get("message_id") if msg.raw else None
            await _cmd_doi_web_pass_self(chat_id, remainder, user, message_id, deps); return
        if cmd_id == "SUDO":
            message_id = msg.raw.get("message_id") if msg.raw else None
            await _cmd_sudo(chat_id, remainder, user, message_id, deps); return
        if cmd_id == "THOAT_SUDO":
            await _cmd_thoat_sudo(chat_id, user, deps); return
        if cmd_id == "XEM_TRI_NHO":
            await _cmd_xem_tri_nho(chat_id, user, deps); return
        if cmd_id == "XEM_HO_SO":
            await _cmd_xem_ho_so(chat_id, user, deps); return
        if cmd_id == "CAP_NHAT_TRI_NHO":
            await _cmd_cap_nhat_tri_nho(chat_id, user, deps); return
        if cmd_id == "TOM_TAT_TUAN":
            await _cmd_tom_tat_tuan(chat_id, user, deps); return
        if cmd_id == "XUAT_DU_LIEU_ADMIN":
            await _cmd_xuat_du_lieu_admin(chat_id, remainder, user, deps); return
        if cmd_id == "XUAT_DU_LIEU_SELF":
            await _cmd_xuat_du_lieu_self(chat_id, remainder, user, deps); return
        if cmd_id == "TAO_TASK":
            await _cmd_tao_task(chat_id, remainder, user, deps); return
        if cmd_id == "XONG_TASK":
            await _cmd_xong_task(chat_id, remainder, user, deps); return
        if cmd_id == "HUY_TASK":
            await _cmd_huy_task(chat_id, remainder, user, deps); return
        if cmd_id == "DANH_SACH_TASK":
            await _cmd_danh_sach_task(chat_id, user, deps); return
        if cmd_id == "DANH_SACH_LICH_HOC":
            await _cmd_danh_sach_lich_hoc(chat_id, user, deps); return
        if cmd_id == "HUY_LICH_HOC":
            await _cmd_huy_lich_hoc(chat_id, remainder, user, deps); return
        if cmd_id == "SUA_LICH_HOC":
            await _cmd_sua_lich_hoc(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_TASK":
            await _cmd_xem_task(chat_id, remainder, user, deps); return
        if cmd_id == "LICH_HOC":
            await _cmd_lich_hoc(chat_id, remainder, user, deps); return
        if cmd_id == "HOAN_TASK":
            await _cmd_hoan_task(chat_id, remainder, user, deps); return
        if cmd_id == "TOM_TAT_HOM_NAY":
            await _cmd_tom_tat_hom_nay(chat_id, user, deps); return
        if cmd_id == "CAU_HINH_TONG_KET":
            await _cmd_cau_hinh_tong_ket(chat_id, remainder, user, deps); return
        if cmd_id == "CAU_HINH_GIO_MAC_DINH":
            await _cmd_cau_hinh_gio_mac_dinh(chat_id, remainder, user, deps); return
        if cmd_id == "THEM_KY_NIEM":
            await _cmd_them_ky_niem(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_KY_NIEM":
            await _cmd_xoa_ky_niem(chat_id, remainder, user, deps); return
        if cmd_id == "SUA_KY_NIEM":
            await _cmd_sua_ky_niem(chat_id, remainder, user, deps); return
        if cmd_id == "DANH_SACH_KY_NIEM":
            await _cmd_danh_sach_ky_niem(chat_id, user, deps); return
        if cmd_id == "XEM_KY_NIEM":
            await _cmd_xem_ky_niem(chat_id, remainder, user, deps); return
        if cmd_id == "CHI":
            await _cmd_chi(chat_id, remainder, user, deps); return
        if cmd_id == "THU":
            await _cmd_thu(chat_id, remainder, user, deps); return
        if cmd_id == "GHI_CHEP_XEM":
            await _cmd_ghi_chep_xem(chat_id, remainder, user, deps); return
        if cmd_id == "DANH_SACH_GHI_CHEP":
            await _cmd_danh_sach_ghi_chep(chat_id, user, deps); return
        if cmd_id == "SUA_GHI_CHEP":
            await _cmd_sua_ghi_chep(chat_id, remainder, user, deps); return
        if cmd_id == "HUY_GHI_CHEP":
            await _cmd_huy_ghi_chep(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_DANH_MUC":
            await _cmd_xem_danh_muc(chat_id, user, deps); return
        if cmd_id == "THEM_DANH_MUC":
            await _cmd_them_danh_muc(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_DANH_MUC":
            await _cmd_xoa_danh_muc(chat_id, remainder, user, deps); return
        if cmd_id == "SUA_DANH_MUC":
            await _cmd_sua_danh_muc(chat_id, remainder, user, deps); return
        if cmd_id == "BAO_CAO_THANG":
            await _cmd_bao_cao_thang(chat_id, remainder, user, deps); return
        if cmd_id == "BAO_CAO_NAM":
            await _cmd_bao_cao_nam(chat_id, user, deps); return
        if cmd_id == "XEM_CHI_TIEU":
            await _cmd_xem_chi_tieu(chat_id, user, deps); return
        if cmd_id == "DAT_HAN_MUC":
            await _cmd_dat_han_muc_chi(chat_id, remainder, user, deps); return
        if cmd_id == "DAT_MUC_TIEU":
            await _cmd_dat_muc_tieu_tiet_kiem(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_HAN_MUC":
            await _cmd_xem_han_muc(chat_id, user, deps); return
        if cmd_id == "CHI_THE":
            await _cmd_chi_the(chat_id, remainder, user, deps); return
        if cmd_id == "TRA_THE":
            await _cmd_tra_the(chat_id, remainder, user, deps); return
        if cmd_id == "THEM_THE":
            await _cmd_them_the(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_THE":
            await _cmd_xoa_the(chat_id, remainder, user, deps); return
        if cmd_id == "THEM_NGUOI_THAN":
            await _cmd_them_nguoi_than(chat_id, remainder, user, deps); return
        if cmd_id == "SUA_NGUOI_THAN":
            await _cmd_sua_nguoi_than(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_NGUOI_THAN":
            await _cmd_xoa_nguoi_than(chat_id, remainder, user, deps); return
        if cmd_id == "DANH_SACH_NGUOI_THAN":
            await _cmd_danh_sach_nguoi_than(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_NGUOI_THAN":
            await _cmd_xem_nguoi_than(chat_id, remainder, user, deps); return
        if cmd_id == "THEM_MO_PHAN":
            await _cmd_them_mo_phan(chat_id, remainder, user, deps); return
        if cmd_id == "SUA_MO_PHAN":
            await _cmd_sua_mo_phan(chat_id, remainder, user, deps); return
        if cmd_id == "XOA_MO_PHAN":
            await _cmd_xoa_mo_phan(chat_id, remainder, user, deps); return
        if cmd_id == "TIM_MO":
            await _cmd_tim_mo(chat_id, remainder, user, deps); return
        if cmd_id == "XEM_THE":
            await _cmd_xem_the(chat_id, user, deps); return

    # ── Step 4: free-form question → wiki + smart search + Claude ──────────
    await _handle_general_question(chat_id, text, deps, user=user)
