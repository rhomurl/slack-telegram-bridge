# Slack to Telegram Bridge

Realtime bridge that watches messages from one specific Slack user and forwards the message text plus attached images/files to a Telegram group.

> Privacy note: this only works for Slack conversations where the Slack app has access. Do not use this to secretly monitor people. Get workspace/admin approval if this is for a company.

---

## Architecture

```text
Slack Events API via Socket Mode
        ↓
Python bridge service
        ↓
Filter by TARGET_SLACK_USER_ID
        ↓
Telegram Bot API
        ↓
Telegram group
```

---

## What it forwards

- Slack messages from a specific user (just the message text — no header or permalink)
- Slack `mrkdwn` formatting (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, code blocks, links) rendered as Telegram HTML
- Slack image attachments via Telegram `sendPhoto`
- Other Slack file attachments (PDF, etc.) via Telegram `sendDocument`
- Optional one-time history backfill of the target user's past messages into a separate Telegram topic, sent silently so subscribers aren't notified
- A CSV ledger of every relayed message (live and backfill, in separate files), recording the Telegram chat + thread/topic and a `sent`/`failed` status — used both as an audit log and to skip messages already sent

---

## Slack setup

### 1. Create Slack app

Go to:

```text
https://api.slack.com/apps
```

Create a new app in your Slack workspace.

### 2. Enable Socket Mode

In the Slack app settings:

```text
Socket Mode → Enable Socket Mode
```

Socket Mode means you do not need a public webhook URL.

### 3. Create an app-level token

Go to:

```text
Basic Information → App-Level Tokens → Generate Token
```

Add this scope:

```text
connections:write
```

Copy the token. It starts with:

```text
xapp-
```

This is your:

```bash
SLACK_APP_TOKEN
```

### 4. Add bot token scopes

Go to:

```text
OAuth & Permissions → Bot Token Scopes
```

Add:

```text
channels:history
channels:read
groups:history
groups:read
files:read
users:read
```

Optional, only if Slack/admin permissions allow DMs:

```text
im:history
mpim:history
im:read
mpim:read
```

Install the app to the workspace.

Copy the Bot User OAuth Token. It starts with:

```text
xoxb-
```

This is your:

```bash
SLACK_BOT_TOKEN
```

### 5. Subscribe to events

Go to:

```text
Event Subscriptions → Subscribe to bot events
```

Add:

```text
message.channels
message.groups
```

Optional DM events if allowed:

```text
message.im
message.mpim
```

### 6. Invite the app to channels

In every Slack channel you want to monitor:

```text
/invite @YourBotName
```

### 7. Get target Slack user ID

In Slack:

```text
Open user profile → More → Copy member ID
```

Example:

```text
U012ABCDEF
```

This is your:

```bash
TARGET_SLACK_USER_ID
```

---

## Telegram setup

### 1. Create Telegram bot

In Telegram, message:

```text
@BotFather
```

Run:

```text
/newbot
```

Copy the bot token. This is your:

```bash
TELEGRAM_BOT_TOKEN
```

### 2. Add bot to group

Add the bot to the Telegram group where messages should be forwarded.

### 3. Get Telegram group chat ID

Send a test message in the group, then open:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

Look for:

```json
"chat": {
  "id": -1001234567890
}
```

This is your:

```bash
TELEGRAM_CHAT_ID
```

---

## Files

Recommended project structure:

```text
slack-telegram-bridge/
├── slack_to_telegram_bridge.py
├── requirements.txt
├── ecosystem.config.js
└── .env
```

---

## `requirements.txt`

```txt
slack_bolt>=1.18.0
requests>=2.31.0
python-dotenv>=1.0.0
```

---

## `.env`

Create `.env` beside the script:

```bash
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_APP_TOKEN=xapp-your-slack-app-token
TELEGRAM_BOT_TOKEN=123456789:your-telegram-bot-token
TELEGRAM_CHAT_ID=-1001234567890
TARGET_SLACK_USER_ID=U012ABCDEF

# Optional: forum topic (thread) in the supergroup for LIVE forwards.
# Leave blank for a normal group / the General topic.
TELEGRAM_LIVE_THREAD_ID=

# Optional: separate forum topic for the historical backfill.
TELEGRAM_HISTORY_THREAD_ID=

# Optional: override the CSV ledger paths (defaults shown).
# BACKFILL_CSV=backfill_messages.csv
# REALTIME_CSV=realtime_messages.csv
```

Do not commit this file to GitHub. See `.env.example` for the full list of variables.

---

## `slack_to_telegram_bridge.py`

The full bridge implementation lives in [`slack_to_telegram_bridge.py`](slack_to_telegram_bridge.py). Run it with no arguments to start the live forwarder, or with the `backfill` subcommand (see below) to relay a channel's history.

---

## History backfill

To relay all of the target user's past messages from a channel into Telegram (one time), run:

```bash
python slack_to_telegram_bridge.py backfill <channel_id>
```

- Backfilled messages are formatted as the message text followed by a blank line and a US Eastern timestamp, e.g.:

  ```text
  Test

  2026-06-25 03:50 AM EDT
  ```

- They are sent with notifications disabled so existing subscribers are not pinged.
- Set `TELEGRAM_HISTORY_THREAD_ID` to post the backfill into its own forum topic, keeping it separate from live forwards.
- Already-relayed messages are tracked in a state file (`.backfill_state`) **and** in `backfill_messages.csv` (see below). Re-running the command skips any message recorded as sent in either. Delete both to force a full re-send.

---

## CSV ledgers

Every relayed message is appended to a CSV, one row per Slack message. Live forwards and backfill use **separate** files so the two flows stay independent:

| File | Written by |
| --- | --- |
| `realtime_messages.csv` | the live forwarder (no arguments) |
| `backfill_messages.csv` | the `backfill` subcommand |

Columns:

```text
logged_at, source, slack_channel, slack_ts, slack_user,
telegram_chat_id, telegram_thread_id, status, text
```

- `telegram_thread_id` is the exact forum topic the message was sent to (blank for the General topic), so you can filter a CSV to validate what already landed in a given thread/topic.
- `status` is `sent` on success or `failed` if the Telegram send raised. Only `sent` rows count toward de-duplication; `failed` rows are retried on the next run.
- These files are used as the "already sent" record:
  - **Realtime** seeds an in-memory set from `realtime_messages.csv` at startup and skips any `channel:ts` already sent — so socket-mode redeliveries don't double-post.
  - **Backfill** unions `backfill_messages.csv` with the legacy `.backfill_state` file before sending.
- Both files are git-ignored (per-deployment data) and override-able via `BACKFILL_CSV` / `REALTIME_CSV`.

> De-dup keys on `channel:ts`, not the thread id, so re-pointing forwarding at a different topic will **not** re-send old messages there. The thread id is still recorded per row for inspection.

---

## `ecosystem.config.js` for PM2

```js
module.exports = {
  apps: [
    {
      name: "slack-telegram-bridge",
      script: "slack_to_telegram_bridge.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
```

The script loads `.env` itself, so secrets do not need to be written into PM2 config.

---

## Install and run

```bash
mkdir -p slack-telegram-bridge
cd slack-telegram-bridge

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

pm2 start ecosystem.config.js
pm2 save
```

---

## PM2 operations

Check status:

```bash
pm2 status
```

View logs:

```bash
pm2 logs slack-telegram-bridge
```

Restart:

```bash
pm2 restart slack-telegram-bridge
```

Stop:

```bash
pm2 stop slack-telegram-bridge
```

Delete from PM2:

```bash
pm2 delete slack-telegram-bridge
```

Enable PM2 startup after reboot:

```bash
pm2 startup
```

PM2 will print a command. Copy and run that command, then run:

```bash
pm2 save
```

---

## Verification checklist

1. `pm2 status` shows `slack-telegram-bridge` as `online`.
2. `pm2 logs slack-telegram-bridge` shows `Starting Slack to Telegram bridge...`.
3. The Slack bot is invited to the target channel.
4. The target Slack user posts a test message.
5. The Telegram group receives the message.
6. The target Slack user posts an image.
7. The Telegram group receives the image.

---

## Troubleshooting

### Telegram message does not arrive

Check:

- Bot is added to the Telegram group
- `TELEGRAM_CHAT_ID` is correct
- `TELEGRAM_BOT_TOKEN` is correct
- Group chat ID usually starts with `-100`

### Slack message does not trigger

Check:

- Slack app is installed to the workspace
- Socket Mode is enabled
- `SLACK_APP_TOKEN` starts with `xapp-`
- `SLACK_BOT_TOKEN` starts with `xoxb-`
- Bot is invited to the channel
- `TARGET_SLACK_USER_ID` is the actual member ID, not display name
- Event subscriptions include `message.channels` or `message.groups`

### Attachments do not forward

Check:

- Bot token has `files:read`
- Slack event payload includes `files`
- Telegram file/image size is within Telegram Bot API limits

---

## Security notes

- Keep `.env` private.
- Do not commit Slack or Telegram tokens.
- Rotate tokens if they are exposed.
- Make sure forwarding Slack content to Telegram is approved by the workspace owner/admin.
