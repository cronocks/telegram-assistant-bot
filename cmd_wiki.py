"""cmd_wiki.py — Wiki, memory, profile, and weekly summary command handlers.

Covers: wiki ingest/query/list/page, xem_tri_nho, xem_ho_so, cap_nhat_tri_nho,
tom_tat_tuan.
"""
import traceback

import acl as acl_mod
from cmd_utils import _acl_filter_notes, _register_wiki_page
from config import MAX_WIKI_UPDATES
from cost_monitor import check_and_alert, record_usage
from deps import CoreDeps
from interfaces import User
from timeutils import current_week_range_str


def _update_index_after_create(
    topic: str,
    filename: str,
    topic_type: str,
    content_to_add: str,
    deps: CoreDeps,
) -> None:
    """Generate a TLDR and append it to the wiki index. Non-fatal on error."""
    try:
        tldr, tldr_tokens = deps.llm.generate_wiki_tldr(topic, content_to_add)
        record_usage(tldr_tokens // 2, tldr_tokens // 2)
        slug = filename.replace(".md", "")
        deps.wiki.add_to_index(topic, slug, topic_type, tldr)
    except Exception as e:
        print(f"[core] Wiki index update (non-fatal): {e}")


async def _cmd_wiki_ingest(chat_id: str, content: str, user: User, deps: CoreDeps) -> None:
    """wiki <content> — ingest raw content into the wiki layer.

    Flow: Claude analyzes → identifies topics → creates/appends wiki pages and
    updates the index.
    """
    if not content:
        await deps.channel.send(
            chat_id, "Cú pháp: wiki <nội dung cần lưu vào wiki>", use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, "Đang phân tích và cập nhật wiki...", use_markdown=False,
    )
    try:
        # 1. Existing topic names (lightweight).
        existing_topics = deps.wiki.get_topic_names()

        # 2. Claude returns a list of structured updates.
        updates, tokens = deps.llm.extract_wiki_updates(content, existing_topics)
        record_usage(tokens // 2, tokens // 2)

        if not updates:
            await deps.channel.send(
                chat_id,
                "Không tìm thấy thông tin đáng kể để lưu vào wiki.\n"
                "Thử nhập chi tiết hơn: tên người, dự án, khái niệm cụ thể.",
                use_markdown=False,
            )
            return

        # 3. Apply each update up to MAX_WIKI_UPDATES.
        results: list[str] = []
        for upd in updates[:MAX_WIKI_UPDATES]:
            topic = upd.get("topic", "").strip()
            topic_type = upd.get("type", "other")
            action = upd.get("action", "create")
            existing_topic_name = upd.get("existing_topic", "").strip()
            content_to_add = upd.get("content_to_add", "").strip()

            if not topic or not content_to_add:
                continue

            try:
                if action == "update" and existing_topic_name:
                    page = deps.wiki.find_page(existing_topic_name)
                    if page:
                        section = deps.wiki.build_section(content_to_add)
                        filename = deps.wiki.append_to_page(page["id"], section)
                        deps.note_index.touch_wiki_page(page["id"])
                        results.append(f"Cập nhật: {filename}")
                    else:
                        # Fall back to create + index update.
                        page_content = deps.wiki.build_new_page(
                            topic, topic_type, content_to_add,
                        )
                        filename, file_id = deps.wiki.save_page(topic, page_content)
                        slug = filename.removesuffix(".md")
                        _register_wiki_page(file_id, user.id, topic, slug, deps)
                        _update_index_after_create(
                            topic, filename, topic_type, content_to_add, deps,
                        )
                        results.append(f"Tạo mới: {filename}")
                else:
                    page_content = deps.wiki.build_new_page(
                        topic, topic_type, content_to_add,
                    )
                    filename, file_id = deps.wiki.save_page(topic, page_content)
                    slug = filename.removesuffix(".md")
                    _register_wiki_page(file_id, user.id, topic, slug, deps)
                    _update_index_after_create(
                        topic, filename, topic_type, content_to_add, deps,
                    )
                    results.append(f"Tạo mới: {filename}")
            except PermissionError as e:
                results.append(f"Từ chối ({topic}): {str(e)[:100]}")
            except Exception as e:
                traceback.print_exc()
                results.append(f"Lỗi ({topic}): {str(e)[:100]}")

        if results:
            await deps.channel.send(
                chat_id,
                "Wiki đã cập nhật:\n" + "\n".join(f"- {r}" for r in results),
                use_markdown=False,
            )
        else:
            await deps.channel.send(
                chat_id, "Không có thay đổi nào được thực hiện.",
                use_markdown=False,
            )

    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi ingest wiki: {str(e)[:400]}", use_markdown=False,
        )


async def _cmd_wiki_query(chat_id: str, question: str, user: User, deps: CoreDeps) -> None:
    """hỏi wiki <question> — answer directly from the wiki layer."""
    if not question:
        await deps.channel.send(
            chat_id, "Cú pháp: hoi wiki <câu hỏi>", use_markdown=False,
        )
        return

    await deps.channel.send(
        chat_id, "Đang tìm trong wiki...", use_markdown=False,
    )
    try:
        keywords = [w for w in question.lower().split() if len(w) > 2]
        visible_slugs = deps.note_index.visible_wiki_slugs(user.id)
        wiki_pages = deps.wiki.retrieve_pages(question, keywords, visible_slugs=visible_slugs)

        if not wiki_pages:
            await deps.channel.send(
                chat_id,
                "Không tìm thấy trang wiki liên quan.\n"
                "Hãy ingest trước bằng lệnh: wiki <noi dung>",
                use_markdown=False,
            )
            return

        reply, tokens = deps.llm.answer_from_wiki(question, wiki_pages)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()

        page_names = ", ".join(p["name"].replace(".md", "") for p in wiki_pages)
        await deps.channel.send(
            chat_id,
            f"[Wiki: {page_names}]\n\n{reply}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(
            chat_id, f"Lỗi khi query wiki: {str(e)[:400]}", use_markdown=False,
        )


async def _cmd_xem_wiki_list(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem wiki — list all wiki pages visible to the user."""
    try:
        pages = deps.wiki.list_pages()
        # ACL: keep only pages the user may read; orphans (no index row) dropped.
        visible = []
        for p in pages:
            meta = deps.note_index.get_wiki_meta(p["id"])
            if meta is None:
                continue
            allowed, is_stealth = acl_mod.can_read(
                user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
            )
            if not allowed:
                continue
            if is_stealth:
                deps.audit.log(
                    actor_user_id=user.id,
                    action="stealth_read_wiki",
                    target_type="wiki_page",
                    target_id=meta["drive_file_id"],
                    payload={"owner_user_id": meta["owner_user_id"]},
                )
            visible.append(p)
        if not visible:
            await deps.channel.send(
                chat_id,
                "Wiki chưa có trang nào. Hãy ingest bằng lệnh: wiki <noi dung>",
                use_markdown=False,
            )
            return
        lines = [f"Wiki ({len(visible)} trang):"]
        for i, p in enumerate(visible, 1):
            modified = p.get("modifiedTime", "")[:10]
            topic = p["name"].replace(".md", "").replace("_", " ")
            lines.append(f"{i}. {topic}  ({modified})")
        await deps.channel.send(chat_id, "\n".join(lines), use_markdown=False)
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _cmd_xem_wiki_page(
    chat_id: str, topic_query: str, user: User, deps: CoreDeps,
) -> None:
    """xem wiki <topic> — read one wiki page (ACL-checked)."""
    if not topic_query:
        await _cmd_xem_wiki_list(chat_id, user, deps)
        return
    not_found_msg = (
        f"Không tìm thấy wiki page cho '{topic_query}'.\n"
        f"Xem danh sách: xem wiki"
    )
    try:
        page = deps.wiki.find_page(topic_query)
        if not page:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        # ACL: an unindexed or unauthorized page returns the same "not found"
        # message so a private page's existence is never leaked.
        meta = deps.note_index.get_wiki_meta(page["id"])
        if meta is None:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        allowed, is_stealth = acl_mod.can_read(
            user, meta["scope"], meta["owner_user_id"], user_store=deps.user_store,
        )
        if not allowed:
            await deps.channel.send(chat_id, not_found_msg, use_markdown=False)
            return
        if is_stealth:
            deps.audit.log(
                actor_user_id=user.id,
                action="stealth_read_wiki",
                target_type="wiki_page",
                target_id=meta["drive_file_id"],
                payload={"owner_user_id": meta["owner_user_id"]},
            )
        content = page["content"]
        if len(content) > 3500:
            content = content[:3500] + "\n\n[...] (đã cắt)"
        topic_name = page["name"].replace(".md", "").replace("_", " ")
        await deps.channel.send(
            chat_id,
            f"=== Wiki: {topic_name} ===\n\n{content}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:400]}", use_markdown=False)


async def _cmd_xem_tri_nho(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem tri nho — display the user's rolling memory snapshot."""
    content = deps.memory_store.get(user.id, "memory")
    if not content:
        await deps.channel.send(
            chat_id,
            "Bộ nhớ của bạn chưa có gì. Dùng lệnh `cap nhat tri nho` để tạo snapshot đầu tiên.",
            use_markdown=False,
        )
        return
    meta = deps.memory_store.get_meta(user.id, "memory")
    curated = (meta or {}).get("curated_at", "chưa rõ")
    await deps.channel.send(
        chat_id,
        f"=== Bộ nhớ của bạn (cập nhật: {curated}) ===\n\n{content}",
        use_markdown=False,
    )


async def _cmd_xem_ho_so(chat_id: str, user: User, deps: CoreDeps) -> None:
    """xem ho so — display the user's profile snapshot."""
    content = deps.memory_store.get(user.id, "user")
    if not content:
        await deps.channel.send(
            chat_id,
            "Hồ sơ của bạn chưa có gì. Dùng lệnh `cap nhat tri nho` để tạo snapshot đầu tiên.",
            use_markdown=False,
        )
        return
    meta = deps.memory_store.get_meta(user.id, "user")
    curated = (meta or {}).get("curated_at", "chưa rõ")
    await deps.channel.send(
        chat_id,
        f"=== Hồ sơ của bạn (cập nhật: {curated}) ===\n\n{content}",
        use_markdown=False,
    )


async def _cmd_cap_nhat_tri_nho(chat_id: str, user: User, deps: CoreDeps) -> None:
    """cap nhat tri nho — trigger LLM curation to refresh memory + profile snapshots."""
    await deps.channel.send(
        chat_id, "Đang đọc ghi chú gần đây và cập nhật bộ nhớ...", use_markdown=False,
    )
    try:
        # Read user's own recent notes (private to them + everyone-scoped they can see).
        recent = deps.notes.get_recent_notes(days=30, max_results=20)
        recent = _acl_filter_notes(recent, user, deps)

        current_memory = deps.memory_store.get(user.id, "memory")
        current_profile = deps.memory_store.get(user.id, "user")

        new_memory, new_profile, tokens = deps.llm.curate_memory(
            recent, current_memory, current_profile,
        )
        record_usage(tokens // 2, tokens // 2)
        deps.user_store.record_usage(user.id, tokens)

        saved: list[str] = []
        if new_memory:
            deps.memory_store.set(user.id, "memory", new_memory, mark_curated=True)
            saved.append("bộ nhớ")
        if new_profile:
            deps.memory_store.set(user.id, "user", new_profile, mark_curated=True)
            saved.append("hồ sơ")

        if not saved:
            await deps.channel.send(
                chat_id,
                "Curation không sinh ra nội dung nào. Thử lại sau hoặc thêm ghi chú trước.",
                use_markdown=False,
            )
            return

        await deps.channel.send(
            chat_id,
            f"Đã cập nhật {' và '.join(saved)} từ {len(recent)} ghi chú gần đây.\n"
            f"Dùng `xem tri nho` hoặc `xem ho so` để xem.",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi khi cập nhật bộ nhớ: {str(e)[:400]}", use_markdown=False)


async def _cmd_tom_tat_tuan(chat_id: str, user: User, deps: CoreDeps) -> None:
    week_range = current_week_range_str()
    await deps.channel.send(
        chat_id,
        f"Đang đọc ghi chú tuần này ({week_range})...",
        use_markdown=False,
    )
    try:
        notes = deps.notes.get_current_week_notes(max_results=20)
        notes = _acl_filter_notes(notes, user, deps)
        if not notes:
            await deps.channel.send(
                chat_id,
                f"Không có ghi chú nào trong tuần này ({week_range}).",
                use_markdown=False,
            )
            return
        summary, tokens = deps.llm.summarize_notes(notes)
        record_usage(tokens // 2, tokens // 2)
        check_and_alert()
        await deps.channel.send(
            chat_id,
            f"Tóm tắt tuần này ({week_range}) — {len(notes)} ghi chú:\n\n{summary}",
            use_markdown=False,
        )
    except Exception as e:
        traceback.print_exc()
        await deps.channel.send(chat_id, f"Lỗi: {str(e)[:500]}", use_markdown=False)
