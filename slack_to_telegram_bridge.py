import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_SLACK_USER_ID = os.environ["TARGET_SLACK_USER_ID"]

# Optional: post live forwards into a specific forum topic (thread) of the supergroup.
# Leave unset for a normal group / the General topic.
_live_thread_raw = os.environ.get("TELEGRAM_LIVE_THREAD_ID")
TELEGRAM_LIVE_THREAD_ID = int(_live_thread_raw) if _live_thread_raw else None

# Optional: separate forum topic for the historical backfill (see `backfill` subcommand).
_history_thread_raw = os.environ.get("TELEGRAM_HISTORY_THREAD_ID")
TELEGRAM_HISTORY_THREAD_ID = int(_history_thread_raw) if _history_thread_raw else None

# Telegram throttles group sends to roughly 20 messages/minute; pace the backfill to stay under it.
BACKFILL_SEND_DELAY_SECONDS = float(os.environ.get("BACKFILL_SEND_DELAY_SECONDS", "3"))

# Tracks which (channel, message-ts) pairs the backfill has already relayed, so re-runs skip them.
# Delete this file to force a full re-send.
BACKFILL_STATE_FILE = Path(
    os.environ.get("BACKFILL_STATE_FILE", Path(__file__).resolve().parent / ".backfill_state")
)

# Backfilled messages are timestamped in US Eastern Time (handles EST/EDT automatically).
EASTERN = ZoneInfo("America/New_York")

# Message subtypes we never forward (system notices, edits, deletions, bot chatter).
IGNORED_SUBTYPES = {
    "message_changed",
    "message_deleted",
    "bot_message",
    "channel_join",
    "channel_leave",
}

app = App(token=SLACK_BOT_TOKEN)


def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(text: str, message_thread_id: int | None = None) -> None:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "disable_web_page_preview": False,
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    response = requests.post(
        tg_api("sendMessage"),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def send_telegram_file(
    path: Path,
    caption: str,
    mime_type: str | None = None,
    message_thread_id: int | None = None,
) -> None:
    is_image = bool(mime_type and mime_type.startswith("image/"))
    method = "sendPhoto" if is_image else "sendDocument"
    field = "photo" if is_image else "document"

    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]}
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id

    with path.open("rb") as f:
        response = requests.post(
            tg_api(method),
            data=data,
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
    logger.info(
        "Slack message event: user=%s subtype=%s channel=%s ts=%s",
        event.get("user"),
        event.get("subtype"),
        event.get("channel"),
        event.get("ts"),
    )

    if event.get("subtype") in IGNORED_SUBTYPES:
        logger.info("  -> ignored subtype %s", event.get("subtype"))
        return

    user_id = event.get("user")
    if user_id != TARGET_SLACK_USER_ID:
        logger.info(
            "  -> skipping: user %s != TARGET_SLACK_USER_ID %s",
            user_id,
            TARGET_SLACK_USER_ID,
        )
        return

    logger.info("  -> forwarding to Telegram thread %s", TELEGRAM_LIVE_THREAD_ID)

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

    send_telegram_message(f"{header}\n\n{body}", message_thread_id=TELEGRAM_LIVE_THREAD_ID)

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
                message_thread_id=TELEGRAM_LIVE_THREAD_ID,
            )
        except Exception as exc:
            logger.exception(f"Failed to relay Slack file {file_obj.get('id')}: {exc}")
            send_telegram_message(
                "Failed to relay an attachment from Slack: "
                f"{file_obj.get('name') or file_obj.get('id')}\n"
                f"Error: {exc}",
                message_thread_id=TELEGRAM_LIVE_THREAD_ID,
            )


def _slack_call(method, **kwargs):
    """Call a Slack Web API method, transparently waiting out HTTP 429 rate limits."""
    while True:
        try:
            return method(**kwargs)
        except SlackApiError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", "5"))
                print(f"Slack rate limited; sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            raise


def _load_sent_keys() -> set:
    """Load the set of (channel:ts) keys already relayed in previous backfill runs."""
    if not BACKFILL_STATE_FILE.exists():
        return set()
    return {
        line.strip()
        for line in BACKFILL_STATE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _record_sent_key(key: str) -> None:
    """Append a relayed key to the state file (append-only so a crash can't corrupt it)."""
    with BACKFILL_STATE_FILE.open("a", encoding="utf-8") as f:
        f.write(key + "\n")


def _collect_target_messages(channel: str, msg: dict, collected: dict) -> None:
    """Record the target user's messages from a history entry, walking its thread replies."""
    ts = msg.get("ts")
    if (
        ts
        and msg.get("user") == TARGET_SLACK_USER_ID
        and msg.get("subtype") not in IGNORED_SUBTYPES
    ):
        collected[ts] = msg

    # conversations.history returns only thread parents; walk replies to catch the
    # target user's responses inside threads started by anyone.
    if not msg.get("reply_count"):
        return

    thread_ts = msg.get("thread_ts") or ts
    cursor = None
    while True:
        resp = _slack_call(
            app.client.conversations_replies,
            channel=channel,
            ts=thread_ts,
            limit=200,
            cursor=cursor,
        )
        for reply in resp.get("messages", []):
            reply_ts = reply.get("ts")
            if not reply_ts or reply_ts == thread_ts:
                continue  # the parent is already handled by the history loop
            if (
                reply.get("user") == TARGET_SLACK_USER_ID
                and reply.get("subtype") not in IGNORED_SUBTYPES
            ):
                collected[reply_ts] = reply
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break


def _relay_history_message(channel: str, msg: dict) -> bool:
    """Relay one historical message to the Telegram history topic.

    Returns True if the text message was sent (so the caller can mark it relayed),
    False if it failed and should be retried on the next run.
    """
    user_id = msg.get("user")
    ts = msg.get("ts", "")
    text = (msg.get("text") or "").strip() or "[no text]"

    when = ""
    if ts:
        try:
            when = datetime.fromtimestamp(float(ts), tz=EASTERN).strftime("%Y-%m-%d %I:%M %p %Z")
        except (ValueError, OverflowError):
            when = ""

    permalink = None
    try:
        permalink_result = app.client.chat_getPermalink(channel=channel, message_ts=ts)
        permalink = permalink_result.get("permalink")
    except Exception as exc:
        print(f"Could not get permalink for {ts}: {exc}")

    # Lead with the timestamp so the topic reads as a scannable chronological log.
    header = f"[History] {when} — from <@{user_id}> in {channel}" if when else (
        f"[History] from <@{user_id}> in {channel}"
    )
    body = text
    if permalink:
        body += f"\n\nSlack link: {permalink}"

    try:
        send_telegram_message(f"{header}\n\n{body}", message_thread_id=TELEGRAM_HISTORY_THREAD_ID)
    except Exception as exc:
        print(f"Failed to send message {ts} to Telegram: {exc}")
        return False

    for file_obj in msg.get("files", []) or []:
        try:
            path = download_slack_file(file_obj)
            if not path:
                continue

            caption_parts = [f"[History] Attachment from <@{user_id}>"]
            if file_obj.get("title"):
                caption_parts.append(str(file_obj["title"]))
            if permalink:
                caption_parts.append(permalink)

            send_telegram_file(
                path=path,
                caption="\n".join(caption_parts),
                mime_type=file_obj.get("mimetype"),
                message_thread_id=TELEGRAM_HISTORY_THREAD_ID,
            )
            time.sleep(BACKFILL_SEND_DELAY_SECONDS)
        except Exception as exc:
            print(f"Failed to relay file {file_obj.get('id')}: {exc}")

    return True


def backfill_channel(channel: str) -> None:
    print(f"Backfilling all history from {TARGET_SLACK_USER_ID} in {channel}...")
    if TELEGRAM_HISTORY_THREAD_ID is None:
        print(
            "Warning: TELEGRAM_HISTORY_THREAD_ID is not set; history will post to the "
            "General topic and mix with live forwards."
        )

    sent_keys = _load_sent_keys()

    collected: dict = {}
    cursor = None
    while True:
        resp = _slack_call(
            app.client.conversations_history,
            channel=channel,
            limit=200,
            cursor=cursor,
        )
        for msg in resp.get("messages", []):
            _collect_target_messages(channel, msg, collected)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    ordered = sorted(collected.values(), key=lambda m: float(m.get("ts", "0")))
    print(f"Found {len(ordered)} message(s) from the target user (oldest first).")

    sent = 0
    skipped = 0
    for msg in ordered:
        key = f"{channel}:{msg.get('ts', '')}"
        if key in sent_keys:
            skipped += 1
            continue
        if _relay_history_message(channel, msg):
            _record_sent_key(key)
            sent_keys.add(key)
            sent += 1
            if sent % 25 == 0:
                print(f"  relayed {sent} (skipped {skipped} already-sent)")
            time.sleep(BACKFILL_SEND_DELAY_SECONDS)

    print(f"Backfill complete. Sent {sent}, skipped {skipped} already-sent.")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        if len(sys.argv) < 3:
            print("Usage: python slack_to_telegram_bridge.py backfill <channel_id>")
            sys.exit(1)
        backfill_channel(sys.argv[2])
    else:
        logging.basicConfig(level=logging.INFO)
        print("Starting Slack to Telegram bridge...")
        print(f"  TARGET_SLACK_USER_ID   = {TARGET_SLACK_USER_ID}")
        print(f"  TELEGRAM_CHAT_ID       = {TELEGRAM_CHAT_ID}")
        print(f"  TELEGRAM_LIVE_THREAD_ID = {TELEGRAM_LIVE_THREAD_ID}")
        SocketModeHandler(app, SLACK_APP_TOKEN).start()
