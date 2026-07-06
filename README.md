# 🤖 Premium Telegram Bot

A production-ready, single-file Telegram bot built with **python-telegram-bot (async, v21)**,
**APScheduler**, and **SQLite**. It provides a premium admin experience for managing a
Telegram channel: join-request approvals, a step-by-step post creator with auto-formatting,
instant and scheduled publishing, and a live statistics dashboard.

---

## ✨ Features

| # | Feature |
|---|---------|
| 1 | Join Request Approval (Approve ✅ / Reject ❌ from inline buttons) |
| 2 | Smart Post Creator (`/createpost`) — text → media → format → preview → publish/schedule |
| 3 | Auto-editing: cleans whitespace/blank lines, preserves emojis, links, hashtags |
| 4 | Instant publish to your channel, with a success receipt (message ID, channel, time) |
| 5 | Scheduled posts with date/time/timezone picker, stored in SQLite, auto-published |
| 6 | Scheduled Posts Manager (`/scheduled`) — Edit ✏ / Delete 🗑 / Publish Now ▶ |
| 7 | Admin Dashboard (`/admin`) with full navigation |
| 8 | Live statistics: users, pending requests, approvals/rejections today, scheduled & published posts |
| 9 | Full logging to console, `bot.log`, and the SQLite `logs` table |
| 10 | SQLite database, created automatically on first run |
| 11 | `.env`-based configuration |
| 12 | Defensive error handling everywhere — the bot never crashes on a bad update |
| 13 | Admin-only access control, parameterized SQL (no injection risk), duplicate-publish protection |
| 14 | Confirmation dialogs, Cancel/Home/Back navigation, conversation timeouts |
| 15 | Clean, typed, PEP8-formatted async code |

---

## 📦 Project structure

```
.
├── bot.py              # Complete bot implementation
├── requirements.txt     # Pinned dependencies
├── .env.example         # Configuration template
├── README.md            # This file
└── bot_database.db      # Created automatically on first run
```

---

## 🚀 Quick start

### 1. Clone / download the project files

Place `bot.py`, `requirements.txt`, and `.env.example` in one folder.

### 2. Create a virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
BOT_TOKEN=123456789:AAExampleTokenFromBotFather
ADMIN_ID=987654321
CHANNEL_ID=-1001234567890
TIMEZONE=Asia/Kolkata
```

### 5. Run the bot

```bash
python bot.py
```

The SQLite database (`bot_database.db`) and all required tables are created automatically
on first launch. You should see log output confirming startup, and the admin account will
receive a "🤖 Bot is online!" message.

---

## 🔑 How to get a Bot Token

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and a username ending in `bot`).
3. BotFather will reply with a token that looks like `123456789:AAH...`.
4. Paste it into `.env` as `BOT_TOKEN`.

## 🆔 How to get your Admin ID

1. Open Telegram and search for **@userinfobot** (or **@RawDataBot**).
2. Send it any message — it will reply with your numeric user ID.
3. Paste it into `.env` as `ADMIN_ID`.

## 📢 How to get your Channel ID

1. Create a channel (or use an existing one) and add your bot as an **administrator**.
2. Post any message in the channel, then forward that message to **@userinfobot** — it will
   show the channel's numeric ID (looks like `-1001234567890`).
   - Alternatively, if the channel is public, you can use its `@username` directly as `CHANNEL_ID`.
3. Paste the ID into `.env` as `CHANNEL_ID`.

## 🛡 Making the bot an admin of your channel

For **publishing** and **join-request approval** to work, the bot must be a channel
administrator with at least these permissions enabled:

- ✅ Post Messages
- ✅ Edit Messages of Others (optional, for future editing features)
- ✅ Invite Users via Link / **Manage/Approve Join Requests**
- ✅ Add New Admins is **not** required for the bot itself

To enable **Join Request Approval**, your channel must have
**"Approve new members"** turned on:
`Channel → Manage Channel → Channel Type → (Private channel) → Approve New Members`.
Only channels/groups configured to require approval generate join requests that the
bot can act on.

---

## 🕹 Usage

### For everyone
- `/start` — welcome message.

### For the admin only (`ADMIN_ID` in `.env`)
- `/admin` — opens the Admin Dashboard (Statistics, Pending Requests, Scheduled Posts,
  Create Post, Settings, Publish).
- `/createpost` — launches the Smart Post Creator:
  1. Send the post text.
  2. Send a photo/video/document, or tap **⏭ Skip**.
  3. Choose a formatting style (Bold, Italic, Code, raw HTML, raw Markdown).
  4. Review the live preview, then:
     - **✏ Edit Text** — replace the text and refresh the preview.
     - **🖼 Change Media** — replace or remove the attached media.
     - **👁 Preview** — refresh the preview.
     - **✅ Publish Now** — publish immediately to `CHANNEL_ID`.
     - **📅 Schedule** — pick a date, time, and timezone; the post is stored and
       published automatically by APScheduler.
     - **❌ Cancel** — abort at any point.
- `/scheduled` — lists all pending scheduled posts with **✏ Edit / 🗑 Delete / ▶ Publish Now**
  buttons for each.
- `/cancel` — cancels whatever multi-step flow is currently active.

### Join requests
When a user requests to join your channel, the bot automatically notifies the admin with
the requester's name, username, and ID, plus **✅ Approve** / **❌ Reject** buttons. Tapping
either button resolves the request directly through the Telegram API — no manual steps needed.

---

## 🗄 Database schema

Created automatically in `bot_database.db`:

| Table | Purpose |
|---|---|
| `users` | Every user who has interacted with the bot |
| `join_requests` | All join requests and their resolution status |
| `scheduled_posts` | Draft/scheduled/published posts, including retry count |
| `settings` | Reserved for future runtime-configurable settings |
| `logs` | Full audit trail of bot actions and errors |

---

## 🧰 Troubleshooting

| Problem | Solution |
|---|---|
| `RuntimeError: BOT_TOKEN is missing` | Make sure `.env` exists and contains a valid `BOT_TOKEN`. |
| Bot doesn't respond to `/admin` | Confirm `ADMIN_ID` in `.env` matches your numeric Telegram user ID exactly. |
| "Failed to publish" errors | Make sure the bot is an **administrator** of `CHANNEL_ID` with posting rights. |
| Join requests never arrive | Enable **"Approve New Members"** in your channel's privacy settings, and make sure the bot is an admin with permission to invite/approve users. |
| Scheduled post didn't fire on time | Check `bot.log` — the scheduler retries up to 3 times on failure and logs every attempt. Ensure the bot process stayed running (use a process manager like `systemd`, `pm2`, or `supervisord` for 24/7 uptime). |
| `telegram.error.Forbidden: bot was blocked` | The admin must start a private chat with the bot at least once so it can send notifications. |
| Timezone errors when scheduling | Use valid IANA timezone names, e.g. `Asia/Kolkata`, `Europe/London`, `America/New_York`. Type `default` to use the `.env` `TIMEZONE` value. |

---

## 🔒 Security notes

- All admin commands and callback actions are protected by an `@admin_only` decorator that
  checks `update.effective_user.id == ADMIN_ID`.
- All SQL queries use parameterized statements — no string concatenation, no injection risk.
- Publishing a scheduled post checks its status before sending, preventing duplicate posts
  if a job fires more than once (e.g., after a restart).

---

## 🏗 Running in production

For 24/7 uptime, run the bot under a process manager, for example with `systemd`:

```ini
[Unit]
Description=Premium Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/project
ExecStart=/path/to/project/venv/bin/python bot.py
Restart=always
RestartSec=5
EnvironmentFile=/path/to/project/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now premium-telegram-bot
```

---

## 🚂 Deploying via GitHub + Railway

### Step 1 — Push code to GitHub

```bash
cd premium-telegram-bot
git init
git add .
git commit -m "Initial commit: premium telegram bot"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

`.env` is already excluded via `.gitignore` — **never commit your real token**.

### Step 2 — Create a Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Select your repository. Railway will detect Python automatically (Nixpacks) using
   `requirements.txt`.
3. This repo includes `railway.json` and a `Procfile`, both pointing to:
   ```
   python bot.py
   ```
   so Railway runs it as a **background worker** — no web port needed, no health-check
   issues.

### Step 3 — Add environment variables

In Railway → your service → **Variables** tab, add exactly the same keys as `.env.example`:

```
BOT_TOKEN=your_real_token
ADMIN_ID=your_numeric_id
CHANNEL_ID=-100xxxxxxxxxx
TIMEZONE=Asia/Kolkata
DB_PATH=/data/bot_database.db
LOG_PATH=/data/bot.log
```

### Step 4 — Add a persistent Volume (important!)

Railway's default filesystem is **wiped on every redeploy**. Since this bot stores its
SQLite database and logs on disk, attach a **Volume** so your users, join requests, and
scheduled posts survive redeploys/restarts:

1. In your Railway service → **Settings** → **Volumes** → **New Volume**.
2. Mount path: `/data`
3. Make sure `DB_PATH` and `LOG_PATH` (Step 3) point inside `/data`, exactly as shown above.

Without this step, every redeploy resets your database to empty.

### Step 5 — Deploy

Railway auto-deploys on every push to `main`. Watch the **Deployments → Logs** tab —
you should see:

```
... | INFO | premium_bot | Database initialized at /data/bot_database.db
... | INFO | premium_bot | Starting bot polling...
```

and your admin account will receive **"🤖 Bot is online!"** on Telegram.

### Updating the bot later

```bash
git add .
git commit -m "Update bot"
git push
```

Railway redeploys automatically — your data stays intact because it lives on the Volume.

---

## 📄 License

Provided as-is for your own deployment and customization.
