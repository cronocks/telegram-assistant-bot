import json
import os
import httpx
from datetime import datetime
from config import (
    COST_FILE, BUDGET_LIMIT, ALERT_80, ALERT_90,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    MODEL,
)

# Giá per 1M tokens (USD) — claude-haiku-4-5
PRICE_INPUT_PER_M  = 1.0
PRICE_OUTPUT_PER_M = 5.0

# Trạng thái alert trong session (reset khi restart)
_alert_sent = {"80": False, "90": False, "last_month": None}


def _load_tracker() -> dict:
    """Đọc file tracking chi phí."""
    if os.path.exists(COST_FILE):
        with open(COST_FILE, "r") as f:
            return json.load(f)
    return {"month": _current_month(), "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _save_tracker(data: dict):
    """Lưu file tracking chi phí."""
    with open(COST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def record_usage(input_tokens: int, output_tokens: int):
    """Ghi nhận token đã dùng và tính chi phí."""
    data = _load_tracker()

    # Reset nếu sang tháng mới
    if data["month"] != _current_month():
        data = {"month": _current_month(), "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        _alert_sent["80"] = False
        _alert_sent["90"] = False

    data["input_tokens"]  += input_tokens
    data["output_tokens"] += output_tokens
    data["cost_usd"] = (
        data["input_tokens"]  / 1_000_000 * PRICE_INPUT_PER_M +
        data["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_M
    )
    _save_tracker(data)
    return data["cost_usd"]


def get_current_cost() -> dict:
    """Lấy thông tin chi phí hiện tại."""
    data = _load_tracker()
    if data["month"] != _current_month():
        return {"month": _current_month(), "cost_usd": 0.0, "percent": 0.0}
    pct = (data["cost_usd"] / BUDGET_LIMIT) * 100
    return {
        "month": data["month"],
        "cost_usd": round(data["cost_usd"], 4),
        "percent": round(pct, 1),
        "input_tokens": data["input_tokens"],
        "output_tokens": data["output_tokens"],
    }


def check_and_alert():
    """Kiểm tra ngưỡng chi phí và gửi cảnh báo Telegram nếu cần."""
    info = get_current_cost()
    cost = info["cost_usd"]

    if cost >= ALERT_90 and not _alert_sent["90"]:
        msg = (
            f"🔴 *CẢNH BÁO 90%*\n"
            f"Đã dùng: `${cost:.4f}` / `${BUDGET_LIMIT}` ({info['percent']}%)\n"
            f"Gần đạt giới hạn tháng! Hãy kiểm tra usage."
        )
        _send_telegram_alert(msg)
        _alert_sent["90"] = True

    elif cost >= ALERT_80 and not _alert_sent["80"]:
        msg = (
            f"🟡 *Lưu ý 80%*\n"
            f"Đã dùng: `${cost:.4f}` / `${BUDGET_LIMIT}` ({info['percent']}%)\n"
            f"Đã qua 80% ngân sách tháng."
        )
        _send_telegram_alert(msg)
        _alert_sent["80"] = True


def _send_telegram_alert(text: str):
    """Gửi tin nhắn cảnh báo về Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        httpx.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[cost_monitor] Lỗi gửi alert: {e}")
