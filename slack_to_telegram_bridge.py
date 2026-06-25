import html
import os
import re
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


# --- Slack mrkdwn -> Telegram HTML ------------------------------------------
# Slack already HTML-escapes literal &, <, > as entities and wraps links/mentions
# in real angle brackets, so we can parse those tokens out and keep the entities
# (Telegram's HTML parse mode expects exactly &amp;/&lt;/&gt;).
_RE_USER = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]+))?>")
_RE_CHANNEL = re.compile(r"<#[A-Z0-9]+(?:\|([^>]+))?>")
_RE_SPECIAL = re.compile(r"<!(\w+)(?:\|([^>]+))?>")
_RE_LINK = re.compile(r"<((?:https?|mailto|tel):[^|>]+)\|([^>]+)>")
_RE_BARE_LINK = re.compile(r"<((?:https?|mailto|tel):[^|>]+)>")


def _format_inline(text: str) -> str:
    """Convert Slack's *bold* / _italic_ / ~strike~ markers to HTML tags."""
    text = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<b>\1</b>", text)
    text = re.sub(r"(?<![_\w])_(?=\S)(.+?)(?<=\S)_(?![_\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![~\w])~(?=\S)(.+?)(?<=\S)~(?![~\w])", r"<s>\1</s>", text)
    return text


def convert_mrkdwn_to_html(text: str) -> str:
    """Render Slack mrkdwn as Telegram-flavoured HTML (parse_mode=HTML)."""
    if not text:
        return text

    # Stash fragments that must not be touched by the inline formatter
    # (code, links, mentions — any of which may legitimately contain * _ ~).
    stash: list[str] = []

    def _stash(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```(.*?)```", lambda m: _stash(f"<pre>{m.group(1)}</pre>"), text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", lambda m: _stash(f"<code>{m.group(1)}</code>"), text)

    text = _RE_USER.sub(lambda m: _stash("@" + (m.group(2) or m.group(1))), text)
    text = _RE_CHANNEL.sub(lambda m: _stash("#" + (m.group(1) or "channel")), text)
    text = _RE_SPECIAL.sub(lambda m: _stash("@" + (m.group(2) or m.group(1))), text)
    text = _RE_LINK.sub(lambda m: _stash(f'<a href="{m.group(1)}">{m.group(2)}</a>'), text)
    text = _RE_BARE_LINK.sub(lambda m: _stash(f'<a href="{m.group(1)}">{m.group(1)}</a>'), text)

    text = _format_inline(text)

    for i, fragment in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", fragment)
    return text


def _html_to_plain(html_text: str) -> str:
    """Strip the HTML we emit back to readable plain text (used as a send fallback)."""
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", html_text, flags=re.DOTALL)
    text = re.sub(r"</?(?:b|i|s|code|pre)>", "", text)
    return html.unescape(text)


def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(
    text: str,
    message_thread_id: int | None = None,
    parse_mode: str | None = None,
    disable_notification: bool = False,
) -> None:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "disable_web_page_preview": False,
        "disable_notification": disable_notification,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    response = requests.post(
        tg_api("sendMessage"),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def send_slack_text(
    raw_text: str,
    suffix: str = "",
    message_thread_id: int | None = None,
    disable_notification: bool = False,
) -> None:
    """Forward Slack-authored text, rendering its mrkdwn as Telegram HTML.

    ``suffix`` is appended verbatim (already-formatted, e.g. a timestamp line).
    If Telegram rejects the HTML (malformed tags), retry once as plain text.
    """
    body_html = convert_mrkdwn_to_html(raw_text.strip()) or "[no text]"
    html_text = f"{body_html}{suffix}"
    try:
        send_telegram_message(
            html_text,
            message_thread_id=message_thread_id,
            parse_mode="HTML",
            disable_notification=disable_notification,
        )
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 400:
            raise
        plain_text = f"{_html_to_plain(body_html)}{suffix}"
        send_telegram_message(
            plain_text,
            message_thread_id=message_thread_id,
            disable_notification=disable_notification,
        )


def send_telegram_file(
    path: Path,
    caption: str,
    mime_type: str | None = None,
    message_thread_id: int | None = None,
    disable_notification: bool = False,
) -> None:
    is_image = bool(mime_type and mime_type.startswith("image/"))
    method = "sendPhoto" if is_image else "sendDocument"
    field = "photo" if is_image else "document"

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption[:1024],
        "disable_notification": disable_notification,
    }
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
    if event.get("subtype") in IGNORED_SUBTYPES:
        return

    user_id = event.get("user")
    if user_id != TARGET_SLACK_USER_ID:
        return

    text = event.get("text", "")

    # Forward only the message itself — no Slack header, no permalink line.
    send_slack_text(text, message_thread_id=TELEGRAM_LIVE_THREAD_ID)

    for file_obj in event.get("files", []) or []:
        try:
            path = download_slack_file(file_obj)
            if not path:
                continue

            # Any non-image (PDF, etc.) is relayed as a document; images as photos.
            caption = str(file_obj.get("title") or "")
            send_telegram_file(
                path=path,
                caption=caption,
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
    ts = msg.get("ts", "")
    text = msg.get("text") or ""

    when = ""
    if ts:
        try:
            when = datetime.fromtimestamp(float(ts), tz=EASTERN).strftime("%Y-%m-%d %I:%M %p %Z")
        except (ValueError, OverflowError):
            when = ""

    # Format: the message, then a blank line, then the timestamp. Backfill posts
    # silently (disable_notification) so subscribers aren't pinged for old messages.
    suffix = f"\n\n{when}" if when else ""

    try:
        send_slack_text(
            text,
            suffix=suffix,
            message_thread_id=TELEGRAM_HISTORY_THREAD_ID,
            disable_notification=True,
        )
    except Exception as exc:
        print(f"Failed to send message {ts} to Telegram: {exc}")
        return False

    for file_obj in msg.get("files", []) or []:
        try:
            path = download_slack_file(file_obj)
            if not path:
                continue

            # Images go as photos, everything else (PDF, etc.) as a document.
            send_telegram_file(
                path=path,
                caption=str(file_obj.get("title") or ""),
                mime_type=file_obj.get("mimetype"),
                message_thread_id=TELEGRAM_HISTORY_THREAD_ID,
                disable_notification=True,
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
        print("Starting Slack to Telegram bridge...")
        SocketModeHandler(app, SLACK_APP_TOKEN).start()
