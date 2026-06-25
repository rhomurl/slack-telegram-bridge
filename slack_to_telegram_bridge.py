import csv
import html
import logging
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

# How many messages to pull per Slack history/replies read during backfill. Keep this
# small (30-50) so each call is lighter and we trip Slack's rate limits less often.
BACKFILL_SLACK_PAGE_SIZE = max(
    1, min(50, int(os.environ.get("BACKFILL_SLACK_PAGE_SIZE", "40")))
)

# Minimum seconds between Slack read calls (conversations.history / .replies). Slack apps
# created after May 2025 that aren't Marketplace-approved are throttled to ~1 request/minute
# on these methods, so we pace proactively to avoid burning the whole run waiting out 429s.
# Lower this (e.g. "1.2" for the legacy ~50/min Tier 3) if your app has the older limits.
SLACK_READ_INTERVAL_SECONDS = float(os.environ.get("SLACK_READ_INTERVAL_SECONDS", "60"))

# Tracks which (channel, message-ts) pairs the backfill has already relayed, so re-runs skip them.
# Delete this file to force a full re-send.
BACKFILL_STATE_FILE = Path(
    os.environ.get("BACKFILL_STATE_FILE", Path(__file__).resolve().parent / ".backfill_state")
)

# Human-readable CSV ledgers of every message we relay. One row per Slack message,
# recording the Telegram chat + thread/topic it was sent to and a sent/failed status.
# These double as the "already sent" record used for de-duplication.
BACKFILL_CSV = Path(
    os.environ.get("BACKFILL_CSV", Path(__file__).resolve().parent / "backfill_messages.csv")
)
REALTIME_CSV = Path(
    os.environ.get("REALTIME_CSV", Path(__file__).resolve().parent / "realtime_messages.csv")
)

CSV_FIELDS = [
    "logged_at",
    "source",
    "slack_channel",
    "slack_ts",
    "slack_user",
    "telegram_chat_id",
    "telegram_thread_id",
    "status",
    "text",
]

# Realtime de-dup set, seeded from REALTIME_CSV at startup (see __main__) and grown
# in-memory as messages are forwarded, so socket-mode redeliveries don't double-post.
_realtime_sent_keys: set = set()

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
    caption: str = "",
    mime_type: str | None = None,
    message_thread_id: int | None = None,
    parse_mode: str | None = None,
    disable_notification: bool = False,
) -> None:
    is_image = bool(mime_type and mime_type.startswith("image/"))
    method = "sendPhoto" if is_image else "sendDocument"
    field = "photo" if is_image else "document"

    def _post(cap: str, mode: str | None) -> requests.Response:
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": cap[:1024],
            "disable_notification": disable_notification,
        }
        if mode:
            data["parse_mode"] = mode
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        with path.open("rb") as f:
            return requests.post(
                tg_api(method),
                data=data,
                files={field: (path.name, f, mime_type or "application/octet-stream")},
                timeout=120,
            )

    try:
        _post(caption, parse_mode).raise_for_status()
    except requests.HTTPError as exc:
        # Telegram rejects malformed caption HTML with 400; retry once as plain text.
        if parse_mode and exc.response is not None and exc.response.status_code == 400:
            _post(_html_to_plain(caption), None).raise_for_status()
        else:
            raise


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


def relay_slack_message(
    text: str,
    files: list | None,
    *,
    message_thread_id: int | None = None,
    suffix: str = "",
    disable_notification: bool = False,
    pace_seconds: float = 0.0,
    on_file_error=None,
) -> bool:
    """Forward a Slack message (text + any attachments) to Telegram.

    When the message carries an image, the text rides along as that image's
    caption so they arrive as a single Telegram message (sending an image is
    slow, and a separate text post would otherwise show up well ahead of it).
    The Slack filename is never used as a caption — only the message text is.

    Returns True if the message body was delivered (so the backfill can mark it
    relayed); False if the body send failed and should be retried later.
    """
    files = list(files or [])
    body_html = convert_mrkdwn_to_html((text or "").strip())
    if body_html and suffix:
        caption = f"{body_html}{suffix}"
    elif body_html:
        caption = body_html
    else:
        caption = suffix.lstrip("\n")

    # Telegram caps captions at 1024 chars; only ride the text along with the
    # first image when it fits, otherwise post it as its own message.
    first_image_index = next(
        (i for i, f in enumerate(files) if (f.get("mimetype") or "").startswith("image/")),
        None,
    )
    caption_on_image = first_image_index is not None and len(caption) <= 1024

    body_sent = False

    def _send_text_standalone() -> None:
        nonlocal body_sent
        send_slack_text(
            text,
            suffix=suffix,
            message_thread_id=message_thread_id,
            disable_notification=disable_notification,
        )
        body_sent = True

    if not caption_on_image:
        try:
            _send_text_standalone()
        except Exception as exc:
            if on_file_error is not None:
                on_file_error(None, exc)
            return False

    for index, file_obj in enumerate(files):
        is_caption_image = caption_on_image and index == first_image_index
        try:
            path = download_slack_file(file_obj)
            if not path:
                if is_caption_image:
                    # Couldn't fetch the image; still deliver the text so it isn't lost.
                    _send_text_standalone()
                continue
            send_telegram_file(
                path=path,
                caption=caption if is_caption_image else "",
                parse_mode="HTML" if is_caption_image else None,
                mime_type=file_obj.get("mimetype"),
                message_thread_id=message_thread_id,
                disable_notification=disable_notification,
            )
            if is_caption_image:
                body_sent = True
            if pace_seconds:
                time.sleep(pace_seconds)
        except Exception as exc:
            if is_caption_image and not body_sent:
                # The captioned image failed; fall back to a standalone text post.
                try:
                    _send_text_standalone()
                except Exception:
                    pass
            if on_file_error is not None:
                on_file_error(file_obj, exc)

    return body_sent


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

    channel = event.get("channel", "")
    ts = event.get("ts", "")
    key = f"{channel}:{ts}"
    if key in _realtime_sent_keys:
        logger.info("  -> skipping: already forwarded (key %s)", key)
        return

    logger.info("  -> forwarding to Telegram thread %s", TELEGRAM_LIVE_THREAD_ID)

    text = event.get("text", "")

    def _on_file_error(file_obj, exc):
        logger.exception("Failed to relay Slack attachment: %s", exc)
        if file_obj is None:
            return
        send_telegram_message(
            "Failed to relay an attachment from Slack: "
            f"{file_obj.get('name') or file_obj.get('id')}\n"
            f"Error: {exc}",
            message_thread_id=TELEGRAM_LIVE_THREAD_ID,
        )

    # Forward only the message itself — no Slack header, no permalink line.
    # An image carries the text as its caption (one combined Telegram message).
    try:
        sent = relay_slack_message(
            text,
            event.get("files"),
            message_thread_id=TELEGRAM_LIVE_THREAD_ID,
            on_file_error=_on_file_error,
        )
    except Exception as exc:
        logger.exception("Failed to forward Slack message %s: %s", ts, exc)
        sent = False

    _log_csv_row(
        REALTIME_CSV,
        source="realtime",
        channel=channel,
        ts=ts,
        user=user_id,
        thread_id=TELEGRAM_LIVE_THREAD_ID,
        status="sent" if sent else "failed",
        text=text,
    )
    if sent:
        _realtime_sent_keys.add(key)


# Wall-clock of the last Slack read call, so _slack_call can space calls out proactively.
_last_slack_read = 0.0
# Running count of read calls this process has made (drives the read-phase heartbeat).
_slack_read_count = 0


def _slack_call(method, **kwargs):
    """Call a Slack Web API method, pacing proactively and waiting out HTTP 429 rate limits."""
    global _last_slack_read, _slack_read_count
    while True:
        # Stay under the per-method rate limit instead of repeatedly slamming into 429s.
        wait = SLACK_READ_INTERVAL_SECONDS - (time.monotonic() - _last_slack_read)
        if wait > 0:
            # When pacing is slow (the post-May-2025 ~1/min tier), the read phase is
            # otherwise silent for minutes — emit a heartbeat so it's clearly alive.
            if SLACK_READ_INTERVAL_SECONDS >= 5:
                print(
                    f"  [slack read #{_slack_read_count + 1}] waiting {wait:.0f}s for "
                    f"rate limit ({_format_duration(SLACK_READ_INTERVAL_SECONDS)}/call)",
                    flush=True,
                )
            time.sleep(wait)
        try:
            resp = method(**kwargs)
            _last_slack_read = time.monotonic()
            _slack_read_count += 1
            return resp
        except SlackApiError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", "5"))
                print(f"Slack rate limited; sleeping {retry_after}s")
                time.sleep(retry_after)
                _last_slack_read = time.monotonic()
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


def _ensure_csv(path: Path) -> None:
    """Create the CSV ledger with its header row if it doesn't exist yet.

    Lets the file exist from first launch (rather than appearing only once the first
    message is relayed); subsequent rows are appended by ``_log_csv_row``.
    """
    if path.exists():
        return
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
    except OSError as exc:
        print(f"Failed to create CSV ledger {path}: {exc}")


def _log_csv_row(
    path: Path,
    *,
    source: str,
    channel: str,
    ts: str,
    user: str,
    thread_id: int | None,
    status: str,
    text: str,
) -> None:
    """Append one message row to a CSV ledger, writing the header if the file is new."""
    is_new = not path.exists()
    try:
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(
                {
                    "logged_at": datetime.now(tz=EASTERN).isoformat(timespec="seconds"),
                    "source": source,
                    "slack_channel": channel,
                    "slack_ts": ts,
                    "slack_user": user,
                    "telegram_chat_id": TELEGRAM_CHAT_ID,
                    "telegram_thread_id": "" if thread_id is None else thread_id,
                    "status": status,
                    "text": text,
                }
            )
    except OSError as exc:
        # Logging must never break forwarding; surface it but keep going.
        print(f"Failed to write CSV row to {path}: {exc}")


def _csv_sent_keys(path: Path) -> set:
    """Return the set of ``channel:ts`` keys already recorded as sent in a CSV ledger."""
    if not path.exists():
        return set()
    keys = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") == "sent":
                keys.add(f"{row.get('slack_channel', '')}:{row.get('slack_ts', '')}")
    return keys


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
            limit=BACKFILL_SLACK_PAGE_SIZE,
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
    # When the message has an image, the text+timestamp ride as its caption.
    suffix = f"\n\n{when}" if when else ""

    def _on_file_error(file_obj, exc):
        ident = file_obj.get("id") if file_obj else ts
        print(f"Failed to relay {ident} to Telegram: {exc}")

    return relay_slack_message(
        text,
        msg.get("files"),
        message_thread_id=TELEGRAM_HISTORY_THREAD_ID,
        suffix=suffix,
        disable_notification=True,
        pace_seconds=BACKFILL_SEND_DELAY_SECONDS,
        on_file_error=_on_file_error,
    )


def _format_duration(seconds: float) -> str:
    """Render a duration as a compact human string, e.g. ``2h05m`` / ``3m12s`` / ``45s``."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _progress_bar(done: int, total: int, width: int = 24) -> str:
    """Render a textual progress bar like ``[######----] 18/40 (45%)``."""
    if total <= 0:
        return f"[{'-' * width}] 0/0 (100%)"
    frac = min(1.0, done / total)
    filled = int(frac * width)
    return f"[{'#' * filled}{'-' * (width - filled)}] {done}/{total} ({frac * 100:.0f}%)"


def backfill_channel(channel: str) -> None:
    print(f"Backfilling all history from {TARGET_SLACK_USER_ID} in {channel}...")
    if TELEGRAM_HISTORY_THREAD_ID is None:
        print(
            "Warning: TELEGRAM_HISTORY_THREAD_ID is not set; history will post to the "
            "General topic and mix with live forwards."
        )

    # Make sure both ledgers exist up front, then dedup against what's already recorded.
    _ensure_csv(BACKFILL_CSV)
    _ensure_csv(REALTIME_CSV)

    # Union the legacy state file with the CSV ledger so an existing deployment
    # (state file present, CSV not yet) never re-sends already-relayed history.
    sent_keys = _load_sent_keys() | _csv_sent_keys(BACKFILL_CSV)

    # --- Phase 1: scan channel history (and threads) for the target user's messages.
    # We can't know the page count up front (Slack paginates blindly), so this phase
    # reports a heartbeat per read call rather than a percentage bar.
    print("Phase 1/2: scanning Slack history (read calls are rate-limited)...")
    read_start = time.monotonic()
    collected: dict = {}
    cursor = None
    page = 0
    while True:
        resp = _slack_call(
            app.client.conversations_history,
            channel=channel,
            limit=BACKFILL_SLACK_PAGE_SIZE,
            cursor=cursor,
        )
        page += 1
        for msg in resp.get("messages", []):
            _collect_target_messages(channel, msg, collected)
        print(
            f"  history page {page}: {len(collected)} target message(s) so far "
            f"({_slack_read_count} read call(s), elapsed "
            f"{_format_duration(time.monotonic() - read_start)})",
            flush=True,
        )
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    ordered = sorted(collected.values(), key=lambda m: float(m.get("ts", "0")))
    pending = [m for m in ordered if f"{channel}:{m.get('ts', '')}" not in sent_keys]
    already = len(ordered) - len(pending)
    total = len(pending)
    print(
        f"Found {len(ordered)} message(s) from the target user "
        f"({already} already relayed, {total} to send)."
    )

    # --- Phase 2: relay the pending messages to Telegram. Here we know the total,
    # so we render a real progress bar with an ETA derived from the measured rate.
    print(f"Phase 2/2: relaying {total} message(s) to Telegram...")
    sent = 0
    failed = 0
    send_start = time.monotonic()
    for i, msg in enumerate(pending, 1):
        ts = msg.get("ts", "")
        key = f"{channel}:{ts}"
        ok = _relay_history_message(channel, msg)
        _log_csv_row(
            BACKFILL_CSV,
            source="backfill",
            channel=channel,
            ts=ts,
            user=msg.get("user", ""),
            thread_id=TELEGRAM_HISTORY_THREAD_ID,
            status="sent" if ok else "failed",
            text=msg.get("text") or "",
        )
        if ok:
            _record_sent_key(key)
            sent_keys.add(key)
            sent += 1
        else:
            failed += 1

        elapsed = time.monotonic() - send_start
        eta = (elapsed / i) * (total - i)
        print(
            f"  {_progress_bar(i, total)} · ETA {_format_duration(eta)}"
            + (f" · {failed} failed" if failed else ""),
            flush=True,
        )
        if ok and i < total:
            time.sleep(BACKFILL_SEND_DELAY_SECONDS)

    print(
        f"Backfill complete. Sent {sent}, skipped {already} already-sent"
        + (f", {failed} failed" if failed else "")
        + "."
    )


def _fetch_slack_message(channel: str, ts: str) -> dict | None:
    """Re-read a single Slack message (including its ``files``) by ``channel`` + ``ts``.

    The realtime CSV stores only text, so attachments have to be recovered from
    Slack itself at replay time. Returns the message dict, or None if it can't be
    re-read (e.g. it was deleted, or it lives inside a thread that
    conversations.history doesn't surface).
    """
    if not channel or not ts:
        return None
    try:
        resp = _slack_call(
            app.client.conversations_history,
            channel=channel,
            latest=ts,
            inclusive=True,
            limit=1,
        )
    except SlackApiError as exc:
        print(f"  could not re-read {channel}:{ts} from Slack: {exc}")
        return None
    for msg in resp.get("messages", []):
        if msg.get("ts") == ts:
            return msg
    return None


def replay_failed_realtime() -> None:
    """Re-send realtime messages previously logged as ``failed`` to the live thread.

    Reads ``REALTIME_CSV``, finds rows marked ``failed`` whose ``channel:ts`` was
    never later recorded as ``sent``, and re-posts each one to the live Telegram
    thread. Each successful resend is appended to the ledger as a new ``sent`` row
    (the ledger is append-only), so the next run skips it.

    The CSV stores only text, so each message is re-read from Slack first to
    recover its attachments — image/file-only messages (blank text) are replayed
    too, with the image carrying any text as its caption, exactly as the realtime
    path forwards them. If a message can no longer be re-read from Slack, we fall
    back to replaying the text the CSV preserved.
    """
    if not REALTIME_CSV.exists():
        print(f"No realtime ledger at {REALTIME_CSV}; nothing to replay.")
        return

    # Any (channel:ts) already recorded as sent — on the original attempt or a
    # prior replay — is considered delivered and skipped.
    sent_keys = _csv_sent_keys(REALTIME_CSV)
    pending: dict = {}  # key -> row, de-duped (a key may have several failed rows)
    skipped_no_id = 0
    with REALTIME_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "failed":
                continue
            channel = row.get("slack_channel", "")
            ts = row.get("slack_ts", "")
            key = f"{channel}:{ts}"
            if key in sent_keys:
                continue  # delivered on a later attempt
            if not channel or not ts:
                skipped_no_id += 1  # no channel/ts: can't re-read or relay it
                continue
            pending[key] = row  # keep the latest failed row for this key

    if skipped_no_id:
        print(f"Skipping {skipped_no_id} failed row(s) missing a channel/ts.")

    total = len(pending)
    if not total:
        print("No failed realtime messages to replay.")
        return

    print(
        f"Replaying {total} failed realtime message(s) to Telegram thread "
        f"{TELEGRAM_LIVE_THREAD_ID}..."
    )
    sent = 0
    failed = 0
    skipped = 0
    for i, (key, row) in enumerate(pending.items(), 1):
        channel = row.get("slack_channel", "")
        ts = row.get("slack_ts", "")

        # Re-read the message so attachments (which the CSV never stored) ride
        # along; fall back to the CSV text if Slack can no longer return it.
        msg = _fetch_slack_message(channel, ts)
        if msg is not None:
            text = msg.get("text") or row.get("text") or ""
            files = msg.get("files")
        else:
            text = row.get("text") or ""
            files = None

        if not (text or "").strip() and not files:
            print(f"  [{i}/{total}] {key}: nothing to replay (no text or attachments)")
            skipped += 1
            continue

        def _on_file_error(file_obj, exc, _key=key):
            ident = file_obj.get("name") or file_obj.get("id") if file_obj else _key
            print(f"  {_key}: failed to relay attachment {ident}: {exc}")

        try:
            ok = relay_slack_message(
                text,
                files,
                message_thread_id=TELEGRAM_LIVE_THREAD_ID,
                on_file_error=_on_file_error,
            )
        except Exception as exc:
            print(f"  [{i}/{total}] {key}: still failing: {exc}")
            ok = False

        _log_csv_row(
            REALTIME_CSV,
            source="replay",
            channel=channel,
            ts=ts,
            user=row.get("slack_user", ""),
            thread_id=TELEGRAM_LIVE_THREAD_ID,
            status="sent" if ok else "failed",
            text=text,
        )
        if ok:
            _realtime_sent_keys.add(key)
            sent += 1
            print(f"  [{i}/{total}] {key}: re-sent")
        else:
            failed += 1

        if i < total:
            time.sleep(BACKFILL_SEND_DELAY_SECONDS)

    print(
        f"Replay complete. Re-sent {sent}"
        + (f", {failed} still failing" if failed else "")
        + (f", {skipped} skipped" if skipped else "")
        + "."
    )


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "backfill":
        if len(sys.argv) < 3:
            print("Usage: python slack_to_telegram_bridge.py backfill <channel_id>")
            sys.exit(1)
        backfill_channel(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "replay-failed":
        replay_failed_realtime()
    else:
        logging.basicConfig(level=logging.INFO)
        _ensure_csv(REALTIME_CSV)
        _ensure_csv(BACKFILL_CSV)
        _realtime_sent_keys = _csv_sent_keys(REALTIME_CSV)
        print("Starting Slack to Telegram bridge...")
        print(f"  TARGET_SLACK_USER_ID   = {TARGET_SLACK_USER_ID}")
        print(f"  TELEGRAM_CHAT_ID       = {TELEGRAM_CHAT_ID}")
        print(f"  TELEGRAM_LIVE_THREAD_ID = {TELEGRAM_LIVE_THREAD_ID}")
        print(f"  REALTIME_CSV           = {REALTIME_CSV} ({len(_realtime_sent_keys)} already sent)")
        SocketModeHandler(app, SLACK_APP_TOKEN).start()
