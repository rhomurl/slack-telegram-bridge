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

- Slack messages from a specific user
- Slack image attachments via Telegram `sendPhoto`
- Other Slack file attachments via Telegram `sendDocument`
- Slack message permalink when available

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
```

Do not commit this file to GitHub.

---

## `slack_to_telegram_bridge.py`

```python
import os
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_SLACK_USER_ID = os.environ["TARGET_SLACK_USER_ID"]

app = App(token=SLACK_BOT_TOKEN)


def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(text: str) -> None:
    response = requests.post(
        tg_api("sendMessage"),
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4096],
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def send_telegram_file(path: Path, caption: str, mime_type: str | None = None) -> None:
    is_image = bool(mime_type and mime_type.startswith("image/"))
    method = "sendPhoto" if is_image else "sendDocument"
    field = "photo" if is_image else "document"

    with path.open("rb") as f:
        response = requests.post(
            tg_api(method),
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
            files={field: (path.name, f, mime_type or "application/octet-stream")},
            timeout=120,
        )
    response.raise_for_status()


def download_slack_file(file_obj: dict) -> Path | None:
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        return None

    name = file_obj.get("name") or file_obj.get("title") or file_obj.get("id") or "slack_file"
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    path = Path(tempfile.gettempdir()) / safe_name

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        timeout=120,
    )
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


@app.event("message")
def handle_message_events(event, say, logger):
    ignored_subtypes = {
        "message_changed",
        "message_deleted",
        "bot_message",
        "channel_join",
        "channel_leave",
    }
    if event.get("subtype") in ignored_subtypes:
        return

    user_id = event.get("user")
    if user_id != TARGET_SLACK_USER_ID:
        return

    channel = event.get("channel", "unknown-channel")
    text = event.get("text", "")
    ts = event.get("ts", "")
    permalink = None

    try:
        permalink_result = app.client.chat_getPermalink(channel=channel, message_ts=ts)
        permalink = permalink_result.get("permalink")
    except Exception as exc:
        logger.warning(f"Could not get permalink: {exc}")

    header = f"Slack message from <@{user_id}> in {channel}"
    body = text.strip() or "[no text]"
    if permalink:
        body += f"\n\nSlack link: {permalink}"

    send_telegram_message(f"{header}\n\n{body}")

    for file_obj in event.get("files", []) or []:
        try:
            path = download_slack_file(file_obj)
            if not path:
                continue

            caption_parts = [f"Attachment from <@{user_id}>"]
            if file_obj.get("title"):
                caption_parts.append(str(file_obj["title"]))
            if permalink:
                caption_parts.append(permalink)

            send_telegram_file(
                path=path,
                caption="\n".join(caption_parts),
                mime_type=file_obj.get("mimetype"),
            )
        except Exception as exc:
            logger.exception(f"Failed to relay Slack file {file_obj.get('id')}: {exc}")
            send_telegram_message(
                "Failed to relay an attachment from Slack: "
                f"{file_obj.get('name') or file_obj.get('id')}\n"
                f"Error: {exc}"
            )


if __name__ == "__main__":
    print("Starting Slack to Telegram bridge...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
```

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
