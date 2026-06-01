# Deployment Guide

This guide covers deploying the Telegram Claude Bot to **Render.com** with **Cloudflare R2** as the SQLite backup backend. It also covers local development setup.

---

## 1. Prerequisites

You will need accounts and credentials from the following services before starting:

| Service | Purpose | Free tier? |
|---------|---------|-----------|
| [Telegram](https://telegram.org) | Create bots via BotFather | ✅ |
| [Anthropic](https://console.anthropic.com) | Claude API key | Pay-as-you-go |
| [Google](https://console.cloud.google.com) | OAuth for Google Drive | ✅ |
| [Cloudflare](https://cloudflare.com) | R2 object storage (SQLite backup) | ✅ (10 GB free) |
| [Render](https://render.com) | Docker hosting | ✅ (750 h/month) |

---

## 2. Telegram Bot Setup

### Create a bot
1. Open Telegram → search for **@BotFather**
2. Send `/newbot` and follow the prompts (set name and username)
3. BotFather replies with your **bot token** — copy it as a tappable code block to avoid partial copy

**Token format:** `1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ-abc123`
(number + colon + 35-character string)

### Create a separate bot for staging
Repeat the steps above to get a second token for staging. Two services sharing one bot token will conflict — Telegram only delivers messages to one webhook at a time.

### Get your chat ID
1. Start a conversation with your bot
2. Call: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Send any message to the bot, then call `getUpdates` again
4. Look for `message.chat.id` in the response — this is your `TELEGRAM_CHAT_ID`

> **Telegram API URL gotcha:** The `bot` prefix is a literal part of every API URL path.
> Correct: `https://api.telegram.org/bot8735442823:ABC.../getMe`
> Wrong: `https://api.telegram.org/8735442823:ABC.../getMe` (missing `bot`)

---

## 3. Cloudflare R2 Setup

### Create a bucket
1. Cloudflare dashboard → **R2 Object Storage** → **Create bucket**
2. Name it (e.g. `telegram-bot-db`)
3. Leave all other settings as default

### Get your Account ID and Endpoint
1. R2 overview page → locate **Account ID** in the right sidebar (32-char hex string)
2. Your S3-compatible endpoint: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

### Create an API token
1. R2 overview → **Manage R2 API Tokens** → **Create API token**
2. Name it (e.g. `litestream-bot`)
3. Permissions: **Object Read & Write**
4. Scope: **Specific bucket** → select your bucket
5. Click **Create API Token**
6. Copy **Access Key ID** and **Secret Access Key** — shown **once only**

---

## 4. Google OAuth Setup

The bot uses Google Drive to store notes and wiki pages.

1. Create a Google Cloud project and enable the **Drive API**
2. Create OAuth 2.0 credentials (Desktop app type), download `credentials.json`
3. Place `credentials.json` in the project root
4. Run the setup script:
   ```bash
   python oauth_setup.py
   ```
5. Follow the browser prompt to authorize Drive access
6. The script prints a base64 string — copy it as `GOOGLE_OAUTH_TOKEN_B64`

> The token is stored in the environment variable instead of a file so it works on Render's ephemeral filesystem.

---

## 5. Environment Variables Reference

Copy `.env.example` to `.env` (local), `.env.staging`, or `.env.production` and fill in values.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_TOKEN` | ✅ | — | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Your Telegram chat ID (bootstrap admin) |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic API key |
| `MODEL` | ✅ | — | Claude model ID (e.g. `claude-haiku-4-5-20251001`) |
| `GOOGLE_OAUTH_TOKEN_B64` | ✅ | — | Base64-encoded Google OAuth token |
| `OWNER_EMAIL` | ✅ | — | Google account email for Drive ownership |
| `APP_ENV` | ✅ | `local` | `local` \| `staging` \| `production` |
| `SQLITE_PATH` | ✅ (non-local) | `./bot.db` | Path to SQLite file. Required for staging/production. |
| `LITESTREAM_BUCKET` | staging/prod | — | R2 bucket name |
| `LITESTREAM_DB_NAME` | staging/prod | — | Path within bucket: `staging/bot.db` or `production/bot.db` |
| `LITESTREAM_ENDPOINT` | staging/prod | — | `https://<account-id>.r2.cloudflarestorage.com` |
| `LITESTREAM_ACCESS_KEY_ID` | staging/prod | — | R2 API token Access Key ID |
| `LITESTREAM_SECRET_ACCESS_KEY` | staging/prod | — | R2 API token Secret Access Key |
| `WEB_SECRET_KEY` | ✅ (non-local) | — | Secret key for web session cookies. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Use a different key per environment. |
| `WEB_SESSION_TTL_DAYS` | ❌ | `7` | Web session cookie TTL in days |
| `GDRIVE_FOLDER_ID` | ❌ | `""` | Pin notes to a specific Drive folder ID |
| `CLAUDE_NOTES_FOLDER` | ❌ | `Claude-Notes` | Notes subfolder name |
| `WIKI_SUBFOLDER` | ❌ | `Wiki` | Wiki subfolder name |
| `BUDGET_LIMIT` | ❌ | `10.0` | Monthly LLM spend cap in USD |
| `MAX_FILES_PER_HOUR` | ❌ | `20` | Drive write rate limit |
| `ENABLE_OWNERSHIP_TRANSFER` | ❌ | `true` | Allow Drive ownership transfer |

---

## 6. Deploy to Render

### 6.1 Create two services (production + staging)

Repeat for each environment:

1. Render dashboard → **New** → **Web Service**
2. Connect your GitHub repo
3. Set **Branch**: `main` (production) or `dev` (staging)
4. Set **Runtime**: **Docker**
5. Set **Dockerfile Path**: `./Dockerfile`
6. Set **Plan**: Free
7. Set **Name**: e.g. `telegram-bot-prod` / `telegram-bot-staging`

### 6.2 Set environment variables

In the **Environment** tab of each service:

**Option A — Import from file (faster):**
- Click **Add from .env file**
- Paste the contents of your `.env.production` or `.env.staging`

**Option B — Set manually:**
Set each variable from the reference table above. Per-environment differences:

| Variable | Production | Staging |
|----------|-----------|---------|
| `APP_ENV` | `production` | `staging` |
| `SQLITE_PATH` | `/data/bot.db` | `/data/bot.db` |
| `LITESTREAM_DB_NAME` | `production/bot.db` | `staging/bot.db` |
| `TELEGRAM_TOKEN` | production bot token | staging bot token (different bot!) |

> `LITESTREAM_BUCKET`, `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`, and `LITESTREAM_SECRET_ACCESS_KEY` are the same for both services — they share the same R2 bucket but write to different paths.

### 6.3 Set the webhook

After the service is live, register the webhook with Telegram by opening this URL in a browser:

```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://<your-service>.onrender.com/webhook
```

Expected response:
```json
{"ok": true, "result": true, "description": "Webhook was set"}
```

Verify the webhook is correctly registered:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo
```

### 6.4 Free tier notes

- Render free tier provides **750 instance-hours/month** shared across all services
- **Only attach UptimeRobot to production** — keeping staging awake would exhaust the free pool
- Staging will sleep after ~15 minutes of inactivity; the first message after sleep may take a few seconds

---

## 7. Local Development

```bash
# Clone
git clone https://github.com/<your-username>/telegram-claude-bot.git
cd telegram-claude-bot

# Install dependencies
pip install -r requirements.txt

# Copy and fill in the env file
cp .env.example .env
# Edit .env — fill in TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, etc.

# Run
python main.py
```

For local development, `APP_ENV=local` is the default. SQLite writes to `./bot.db` and Litestream is not used.

To run tests:
```bash
pytest
```

---

## 8. Boot Sequence

When the Docker container starts (`docker-entrypoint.sh`):

1. **`litestream restore`** — downloads the latest SQLite snapshot from R2
   - First ever boot (empty bucket): no-op, bot creates a fresh DB and runs migrations
   - Subsequent boots: restores the latest WAL-replicated state
2. **`litestream replicate`** — starts WAL streaming to R2 (~1 second lag), then launches `uvicorn`

You can verify a successful boot in Render Logs:
```
time=... msg="no matching backups found"   ← first boot only
time=... msg="initialized db" path=/data/bot.db
[bot] DB ready — admin: <name> (id=1)
[bot] Drive OK: {...}
INFO: Application startup complete.
==> Your service is live 🎉
```

---

## 9. Troubleshooting

### Bot does not respond after deploy

**Check 1 — Is the service actually running?**
Render dashboard → Logs — look for `Application startup complete` and `Your service is live`.

**Check 2 — Is the webhook pointing to the right URL?**
```
https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```
The `url` field must match your Render service URL + `/webhook`.
If it points to an old/wrong URL, re-run `setWebhook`.

**Check 3 — Is the bot sleeping?**
Free tier services sleep after 15 minutes. Send a message and wait ~10 seconds for wake-up.

### `getWebhookInfo` / `getMe` returns 404 or 401

| Error | Cause | Fix |
|-------|-------|-----|
| `404 Not Found` | Missing `bot` prefix in URL | Use `https://api.telegram.org/bot<TOKEN>/getMe` |
| `401 Unauthorized` | Token is invalid or revoked | Get a fresh token from BotFather → `/mybots` → API Token |

> Always copy the token by tapping BotFather's code block — manual selection can miss characters or include line breaks.

### Litestream restore fails on boot

Check that all `LITESTREAM_*` variables are set correctly on Render. The most common mistake is `LITESTREAM_ENDPOINT` missing the `https://` prefix or the wrong Account ID.

### Google Drive token expired

The token in `GOOGLE_OAUTH_TOKEN_B64` has a refresh token that auto-refreshes. If Drive fails, re-run `oauth_setup.py` locally, get a new base64 string, and update the env var on Render.

### `APP_ENV` not set for staging/production

If `SQLITE_PATH` is missing on a non-local environment, the app will fail fast with:
```
RuntimeError: APP_ENV=staging requires SQLITE_PATH to be set explicitly
```
Set `SQLITE_PATH=/data/bot.db` in the Render environment.
