from __future__ import annotations

import re

# Matches https://t.me/channelname/123 or https://t.me/c/1234567890/45
TELEGRAM_LINK_RE = re.compile(
    r"https?://(?:www\.)?t\.me/((?:c/\d+|[^/\s]+))/(\d+)",
    re.IGNORECASE,
)


def parse_telegram_message_link(url_or_channel: str, message_id: int | None = None) -> str:
    """
    Parse a t.me URL or channel reference for use with Telethon get_entity / get_messages.
    Returns the entity reference (e.g. "jozzmovies" or "c/1234567890").
    If url_or_channel is a full URL, message_id is ignored; the pair is extracted from the URL.
    """
    url_or_channel = url_or_channel.strip()
    match = TELEGRAM_LINK_RE.search(url_or_channel)
    if match:
        return match.group(1).strip()
    return url_or_channel


def parse_telegram_link(url: str) -> tuple[str, int] | None:
    """
    Parse a full t.me message URL into (channel_ref, message_id).
    Returns None if the URL does not match.
    """
    url = url.strip()
    match = TELEGRAM_LINK_RE.search(url)
    if not match:
        return None
    channel_ref = match.group(1).strip()
    try:
        msg_id = int(match.group(2))
    except ValueError:
        return None
    return (channel_ref, msg_id)
