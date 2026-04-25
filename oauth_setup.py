"""
oauth_setup.py — Chạy script này MỘT LẦN trên máy local để authorize bot.

Quy trình:
1. Script mở browser → bạn đăng nhập bằng tài khoản BOT (cronocks@gmail.com)
2. Cấp quyền cho ứng dụng → tạo file token.json
3. In ra chuỗi base64 của token.json để paste vào Render

QUAN TRỌNG: Đăng nhập bằng cronocks@gmail.com, KHÔNG dùng tài khoản chính.
"""
import os
import json
import base64
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDENTIALS_FILE = "oauth_credentials.json"
TOKEN_FILE = "token.json"


def main():
    print("=" * 60)
    print("OAuth Setup for Telegram Claude Bot")
    print("=" * 60)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"[ERROR] Khong tim thay {CREDENTIALS_FILE}")
        print("Hay tai file OAuth credentials tu Google Cloud Console")
        return

    print(f"[OK] Tim thay {CREDENTIALS_FILE}")
    print("\nMo browser de dang nhap...")
    print("!!! HAY DANG NHAP BANG TAI KHOAN BOT (cronocks@gmail.com)")
    print("!!! KHONG dang nhap bang tai khoan chinh (thangnm.it@gmail.com)")
    print()
    input("Nhan Enter de tiep tuc...")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save token.json
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\n[OK] Da tao {TOKEN_FILE}")

    # Validate scope khớp đúng
    if set(creds.scopes) != set(SCOPES):
        print(f"[CANH BAO] Scope khong khop!")
        print(f"   Expected: {SCOPES}")
        print(f"   Got: {creds.scopes}")
        print("   Hay xoa token.json va chay lai!")
        return
    else:
        print(f"[OK] Scope dung: {SCOPES[0]}")

    # Generate base64 cho Render env
    print()
    print("=" * 60)
    print("BASE64 TOKEN")
    print("Copy chuoi duoi day va paste vao Render env GOOGLE_OAUTH_TOKEN_B64:")
    print("=" * 60)

    with open(TOKEN_FILE, "rb") as f:
        token_b64 = base64.b64encode(f.read()).decode("utf-8")

    print(token_b64)
    print("=" * 60)

    # Cũng lưu base64 ra file để tiện copy
    with open("token_b64.txt", "w") as f:
        f.write(token_b64)
    print(f"\n[OK] Da luu base64 vao file token_b64.txt")
    print("\nHoan thanh! Buoc tiep theo:")
    print("1. Copy chuoi base64 tren (hoac mo file token_b64.txt)")
    print("2. Vao Render -> Environment -> Sua bien GOOGLE_OAUTH_TOKEN_B64")
    print("3. Xoa bien cu GOOGLE_CREDENTIALS_B64 (khong can nua)")
    print("4. Save -> Render tu deploy lai")


if __name__ == "__main__":
    main()
