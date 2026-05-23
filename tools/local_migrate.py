"""tools/local_migrate.py — Standalone CLI: mirror SQLite DB + Drive files to local FS.

Prepares a local copy of all bot data for future local-only deployment mode.
Runs outside the bot process; reads credentials from env vars (same as main.py).

Usage:
    python tools/local_migrate.py [options]

Options:
    --output <dir>      Destination directory (default: ./local_export)
    --users <id1,id2>   Limit Drive download to specific user IDs (comma-separated)
    --include-deleted   Include soft-deleted notes and wiki pages
    --dry-run           Print plan without writing any files

Output layout:
    <output>/
    ├── bot.db                  copy of the SQLite database
    ├── drive_files/            mirror of Drive content
    │   ├── notes/<file_id>.md
    │   └── wiki/<slug>.md
    └── manifest.json           stats + timestamp
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Bootstrap: add project root to path so imports work when run as a script ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")


# ── Constants ─────────────────────────────────────────────────────────────────

_DB_FILENAME = "bot.db"
_MANIFEST_FILENAME = "manifest.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fail(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def _check_env() -> None:
    """Fail fast if required env vars are missing."""
    required = ["GOOGLE_OAUTH_TOKEN_B64"]
    # GOOGLE_OAUTH_TOKEN_B64 or token.json must exist for Drive auth.
    token_b64 = os.environ.get("GOOGLE_OAUTH_TOKEN_B64", "").strip()
    token_file = _PROJECT_ROOT / "token.json"
    if not token_b64 and not token_file.exists():
        _fail(
            "Drive credentials not found. "
            "Set GOOGLE_OAUTH_TOKEN_B64 or ensure token.json exists in project root."
        )


def _copy_db(output_dir: Path, dry_run: bool) -> Path:
    """Copy the SQLite database to output_dir/bot.db. Returns the destination path."""
    src = _PROJECT_ROOT / _DB_FILENAME
    if not src.exists():
        _fail(f"Database not found: {src}")

    dest = output_dir / _DB_FILENAME
    if dry_run:
        print(f"[dry-run] Would copy {src} → {dest}")
        return dest

    # Open source in read-only mode to avoid corruption if bot is running.
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dest)
    src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()
    print(f"[db] Copied {src} → {dest} ({dest.stat().st_size // 1024} KB)")
    return dest


def _list_drive_files(
    conn: sqlite3.Connection,
    user_ids: list[int] | None,
    include_deleted: bool,
) -> tuple[list[dict], list[dict]]:
    """Return (notes_rows, wiki_rows) from SQLite filtered by args."""
    deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
    user_filter = ""
    params: list = []

    if user_ids:
        placeholders = ",".join("?" * len(user_ids))
        user_filter = f"AND owner_user_id IN ({placeholders})"
        params = list(user_ids)

    note_rows = conn.execute(
        f"SELECT drive_file_id, title, scope, kind, owner_user_id "
        f"FROM notes WHERE 1=1 {deleted_filter} {user_filter}",
        params,
    ).fetchall()

    wiki_rows = conn.execute(
        f"SELECT drive_file_id, slug, topic, scope, owner_user_id "
        f"FROM wiki_pages WHERE 1=1 {deleted_filter} {user_filter}",
        params,
    ).fetchall()

    return [dict(r) for r in note_rows], [dict(r) for r in wiki_rows]


def _download_drive_file(service, file_id: str) -> bytes:
    """Download a Drive file by ID. Returns raw bytes."""
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _mirror_file(
    service,
    file_id: str,
    dest_path: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    """Download file_id to dest_path (idempotent: skip if already exists).

    Returns (was_downloaded, status_label).
    """
    if dest_path.exists():
        return False, "skip"

    if dry_run:
        return False, "dry-run"

    try:
        content = _download_drive_file(service, file_id)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
        return True, "ok"
    except Exception as exc:
        return False, f"error: {exc}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror bot SQLite DB and Drive files to a local directory.",
    )
    parser.add_argument(
        "--output", default="./local_export",
        help="Destination directory (default: ./local_export)",
    )
    parser.add_argument(
        "--users", default="",
        help="Comma-separated user IDs to limit Drive download (default: all)",
    )
    parser.add_argument(
        "--include-deleted", action="store_true",
        help="Include soft-deleted notes and wiki pages",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print plan without writing any files",
    )
    args = parser.parse_args()

    _check_env()

    output_dir = Path(args.output).resolve()
    user_ids: list[int] | None = None
    if args.users.strip():
        try:
            user_ids = [int(x.strip()) for x in args.users.split(",") if x.strip()]
        except ValueError:
            _fail("--users must be comma-separated integers, e.g. --users 1,2,3")

    if args.dry_run:
        print("[dry-run] No files will be written.")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: copy database ─────────────────────────────────────────────────
    print("\n=== Step 1/3: Copy database ===")
    _copy_db(output_dir, dry_run=args.dry_run)

    # ── Step 2: resolve Drive files from SQLite ───────────────────────────────
    print("\n=== Step 2/3: Resolve Drive files ===")
    db_path = _PROJECT_ROOT / _DB_FILENAME
    if not db_path.exists():
        _fail(f"Database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    notes, wiki_pages = _list_drive_files(conn, user_ids, args.include_deleted)
    conn.close()

    print(f"Found {len(notes)} notes, {len(wiki_pages)} wiki pages to mirror.")
    if user_ids:
        print(f"  (filtered to user IDs: {user_ids})")
    if args.include_deleted:
        print("  (including soft-deleted items)")

    # ── Step 3: download Drive content ───────────────────────────────────────
    print("\n=== Step 3/3: Mirror Drive files ===")

    # Import Drive helpers at runtime so missing credentials fail cleanly here.
    try:
        from drive_client import _get_service
        service = _get_service()
    except Exception as exc:
        _fail(f"Cannot connect to Google Drive: {exc}")

    notes_dir = output_dir / "drive_files" / "notes"
    wiki_dir = output_dir / "drive_files" / "wiki"

    stats = {"notes_ok": 0, "notes_skip": 0, "notes_err": 0,
             "wiki_ok": 0, "wiki_skip": 0, "wiki_err": 0}

    for note in notes:
        fid = note["drive_file_id"]
        dest = notes_dir / f"{fid}.md"
        ok, label = _mirror_file(service, fid, dest, dry_run=args.dry_run)
        if label == "ok":
            stats["notes_ok"] += 1
        elif label == "skip":
            stats["notes_skip"] += 1
        elif label == "dry-run":
            stats["notes_skip"] += 1
        else:
            stats["notes_err"] += 1
            print(f"  [warn] note {fid}: {label}")

    for page in wiki_pages:
        fid = page["drive_file_id"]
        slug = page["slug"] or fid
        dest = wiki_dir / f"{slug}.md"
        ok, label = _mirror_file(service, fid, dest, dry_run=args.dry_run)
        if label == "ok":
            stats["wiki_ok"] += 1
        elif label in ("skip", "dry-run"):
            stats["wiki_skip"] += 1
        else:
            stats["wiki_err"] += 1
            print(f"  [warn] wiki {slug}: {label}")

    # ── Step 4: write manifest ────────────────────────────────────────────────
    manifest = {
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "local_migrate.py",
        "args": {
            "output": str(output_dir),
            "users": user_ids,
            "include_deleted": args.include_deleted,
            "dry_run": args.dry_run,
        },
        "stats": {
            "notes_total": len(notes),
            "notes_downloaded": stats["notes_ok"],
            "notes_skipped": stats["notes_skip"],
            "notes_errors": stats["notes_err"],
            "wiki_total": len(wiki_pages),
            "wiki_downloaded": stats["wiki_ok"],
            "wiki_skipped": stats["wiki_skip"],
            "wiki_errors": stats["wiki_err"],
        },
    }
    manifest_path = output_dir / _MANIFEST_FILENAME
    if not args.dry_run:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    print(f"Output:   {output_dir}")
    print(f"Notes:    {stats['notes_ok']} downloaded, {stats['notes_skip']} skipped, {stats['notes_err']} errors")
    print(f"Wiki:     {stats['wiki_ok']} downloaded, {stats['wiki_skip']} skipped, {stats['wiki_err']} errors")
    if args.dry_run:
        print("[dry-run] No files were written.")
    else:
        print(f"Manifest: {manifest_path}")
    if stats["notes_err"] + stats["wiki_err"] > 0:
        print("[warn] Some files had errors — re-run to retry (idempotent).")
    print("Done.")


if __name__ == "__main__":
    main()
