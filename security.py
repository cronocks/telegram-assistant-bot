"""
security.py — Lớp bảo mật tập trung, chống leo thang quyền.

Cung cấp 6 lớp validation cho mọi thao tác Drive:
1. Validate OAuth scope (chỉ cho phép drive.file)
2. Whitelist Folder ID (động — folder bot tự tạo hoặc folder cấu hình)
3. Whitelist file extension và MIME type
4. Validate target email khi transfer ownership
5. Rate limiting (file/giờ)
6. Audit log mọi thao tác nhạy cảm
"""
import time
from datetime import datetime
from collections import deque
from config import (
    GDRIVE_FOLDER_ID, OWNER_EMAIL, MAX_FILES_PER_HOUR,
    ENABLE_OWNERSHIP_TRANSFER,
)

# ── Constants — KHÔNG được phép thay đổi qua env ──────────────────────────────
ALLOWED_SCOPES = {"https://www.googleapis.com/auth/drive.file"}
ALLOWED_EXTENSIONS = {".md"}
ALLOWED_MIME_TYPES = {"text/markdown"}

# ── State (in-memory, reset khi restart) ─────────────────────────────────────
_create_timestamps: deque = deque()
_trusted_folders: set = set()

# Optional SQLite audit sink (injected at startup via set_audit_sink). When set,
# audit_log() persists events to the audit_log table instead of only stdout.
_audit_sink = None


def set_audit_sink(sink) -> None:
    """Inject (or clear) the persistent audit sink. Pass None to disable."""
    global _audit_sink
    _audit_sink = sink


def register_trusted_folder(folder_id: str):
    """
    Đăng ký folder đã được drive_client xác minh là an toàn.
    Gọi từ drive_client._get_or_create_notes_folder() sau khi xác định folder.
    """
    if not folder_id:
        raise ValueError("[SECURITY] Khong duoc register folder rong")
    _trusted_folders.add(folder_id)
    audit_log("folder_registered", file_id=folder_id)


def validate_scope(token_scopes):
    """Lớp 1: Đảm bảo OAuth token chỉ có scope tối thiểu."""
    token_set = set(token_scopes) if token_scopes else set()
    if token_set != ALLOWED_SCOPES:
        raise PermissionError(
            f"[SECURITY] Scope khong hop le! "
            f"Expected {ALLOWED_SCOPES}, got {token_set}"
        )
    audit_log("scope_validated", details=f"scopes={list(token_scopes)}")


def validate_folder(folder_id: str):
    """Lớp 2: Đảm bảo mọi thao tác chỉ trong folder đã được trust."""
    if not folder_id:
        raise PermissionError("[SECURITY] Folder ID rong")

    if GDRIVE_FOLDER_ID and folder_id == GDRIVE_FOLDER_ID:
        return

    if folder_id in _trusted_folders:
        return

    raise PermissionError(
        f"[SECURITY] Folder khong trong whitelist: '{folder_id}'"
    )


def validate_file_creation(filename: str, mimetype: str):
    """Lớp 3: Chỉ cho phép tạo file .md với MIME text/markdown."""
    if not filename:
        raise PermissionError("[SECURITY] Filename rong")

    if not any(filename.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise PermissionError(
            f"[SECURITY] File extension khong hop le: '{filename}'. "
            f"Chi cho phep: {ALLOWED_EXTENSIONS}"
        )

    if mimetype not in ALLOWED_MIME_TYPES:
        raise PermissionError(
            f"[SECURITY] MIME type khong hop le: '{mimetype}'. "
            f"Chi cho phep: {ALLOWED_MIME_TYPES}"
        )


def validate_transfer_target(email: str):
    """Lớp 4: Đảm bảo transfer ownership chỉ tới OWNER_EMAIL đã cấu hình."""
    if not ENABLE_OWNERSHIP_TRANSFER:
        raise PermissionError("[SECURITY] Ownership transfer is disabled")

    if not email or email != OWNER_EMAIL:
        raise PermissionError(
            f"[SECURITY] Transfer chi duoc phep toi '{OWNER_EMAIL}', "
            f"got '{email}'"
        )


def check_rate_limit():
    """Lớp 5: Giới hạn số file tạo mỗi giờ."""
    now = time.time()
    cutoff = now - 3600

    while _create_timestamps and _create_timestamps[0] < cutoff:
        _create_timestamps.popleft()

    if len(_create_timestamps) >= MAX_FILES_PER_HOUR:
        raise PermissionError(
            f"[SECURITY] Vuot rate limit: {MAX_FILES_PER_HOUR} files/gio. "
            f"Vui long thu lai sau."
        )

    _create_timestamps.append(now)


def get_rate_limit_status() -> dict:
    """Trả về trạng thái rate limit hiện tại."""
    now = time.time()
    cutoff = now - 3600
    recent = [t for t in _create_timestamps if t >= cutoff]
    return {
        "current_hour": len(recent),
        "max_per_hour": MAX_FILES_PER_HOUR,
        "remaining": MAX_FILES_PER_HOUR - len(recent),
    }


def audit_log(action: str, file_id: str = "", filename: str = "",
              user: str = "", details: str = ""):
    """Lớp 6: Log mọi thao tác nhạy cảm.

    Routes to the injected SQLite audit sink when available so Drive operations
    show up in `xem audit`. Falls back to stdout when no sink is set or the sink
    write fails (audit must never break the operation it is recording).
    """
    if _audit_sink is not None:
        payload = {
            k: v for k, v in
            {"filename": filename, "user": user, "details": details}.items() if v
        }
        try:
            _audit_sink.log(
                actor_user_id=None,
                action=action,
                target_type="drive",
                target_id=(file_id or filename or None),
                payload=payload or None,
            )
            return
        except Exception:
            # Degrade to stdout rather than letting an audit failure propagate.
            pass

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[audit] {timestamp} | action={action} | "
        f"file_id={file_id} | filename={filename} | "
        f"user={user} | details={details}",
        flush=True,
    )


def get_security_status() -> dict:
    """Trả về trạng thái cấu hình bảo mật."""
    rate = get_rate_limit_status()
    return {
        "scope": list(ALLOWED_SCOPES)[0],
        # Cả 2 key cho backward-compatible với main.py các phiên bản cũ
        "configured_folder_id": (GDRIVE_FOLDER_ID[:20] + "...") if GDRIVE_FOLDER_ID else "(empty - bot will auto-create)",
        "allowed_folder_id": (GDRIVE_FOLDER_ID[:20] + "...") if GDRIVE_FOLDER_ID else "(empty - bot will auto-create)",
        "trusted_folders_count": len(_trusted_folders),
        "owner_email": OWNER_EMAIL,
        "ownership_transfer_enabled": ENABLE_OWNERSHIP_TRANSFER,
        "rate_limit_used": f"{rate['current_hour']}/{rate['max_per_hour']}",
        "allowed_extensions": list(ALLOWED_EXTENSIONS),
        "allowed_mimetypes": list(ALLOWED_MIME_TYPES),
    }
