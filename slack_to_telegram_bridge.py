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
