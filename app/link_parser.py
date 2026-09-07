from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit


TELEGRAM_WEB_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}
TELEGRAM_LINK_RE = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>()]+|tg:(?://)?(?:resolve|privatepost)\?[^\s<>()]+)",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
TRAILING_TEXT_PUNCTUATION = ".,;:!?)]}'\""
BOT_API_CHANNEL_PREFIX = 1_000_000_000_000


@dataclass(frozen=True, slots=True)
class TelegramMessageReference:
    """Canonical identity of a Telegram message, independent of URL shape."""

    type: str
    message_id: int
    original_url: str
    username: str | None = None
    channel_internal_id: int | None = None
    peer_id: int | None = None
    topic_id: int | None = None

    @property
    def entity_reference(self) -> str | int:
        if self.username:
            return self.username
        if self.peer_id is None:
            raise ValueError("Telegram reference has no resolvable peer")
        return self.peer_id

    @property
    def key(self) -> str:
        peer = self.username.lower() if self.username else str(self.peer_id)
        return f"{peer}:{self.message_id}"

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "type": self.type,
            "peer_id": self.peer_id,
            "channel_internal_id": self.channel_internal_id,
            "username": self.username,
            "topic_id": self.topic_id,
            "message_id": self.message_id,
            "original_url": self.original_url,
        }

    def canonical_url(self) -> str:
        if self.username:
            path = f"{self.username}/{self.message_id}"
            if self.topic_id is not None:
                path = f"{self.username}/{self.topic_id}/{self.message_id}"
        else:
            path = f"c/{self.channel_internal_id}/{self.message_id}"
            if self.topic_id is not None:
                path = f"c/{self.channel_internal_id}/{self.topic_id}/{self.message_id}"
        return f"https://t.me/{path}"


def channel_internal_id_to_peer_id(channel_internal_id: int) -> int:
    if channel_internal_id <= 0:
        raise ValueError("Telegram channel ID must be a positive integer")
    return -(BOT_API_CHANNEL_PREFIX + channel_internal_id)


def peer_id_to_channel_internal_id(peer_id: int) -> int:
    if peer_id >= -BOT_API_CHANNEL_PREFIX:
        raise ValueError("Telegram private channel peer ID must start with -100")
    channel_internal_id = abs(peer_id) - BOT_API_CHANNEL_PREFIX
    if channel_internal_id <= 0:
        raise ValueError("Invalid Telegram private channel peer ID")
    return channel_internal_id


def _positive_int(value: str | int | None, label: str) -> int:
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Telegram {label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"Telegram {label} must be a positive integer")
    return parsed


def _topic_from_query(query: dict[str, list[str]]) -> int | None:
    raw = (query.get("thread") or [None])[0]
    return _positive_int(raw, "topic ID") if raw is not None else None


def _private_reference(
    channel: str | int,
    message: str | int,
    *,
    topic: str | int | None,
    original_url: str,
) -> TelegramMessageReference:
    channel_internal_id = _positive_int(channel, "channel ID")
    message_id = _positive_int(message, "message ID")
    topic_id = _positive_int(topic, "topic ID") if topic is not None else None
    return TelegramMessageReference(
        type="private_channel_topic_message" if topic_id is not None else "private_channel_message",
        channel_internal_id=channel_internal_id,
        peer_id=channel_internal_id_to_peer_id(channel_internal_id),
        topic_id=topic_id,
        message_id=message_id,
        original_url=original_url,
    )


def _public_reference(
    username: str,
    message: str | int,
    *,
    topic: str | int | None,
    original_url: str,
) -> TelegramMessageReference:
    username = username.lstrip("@").strip()
    if not USERNAME_RE.fullmatch(username) or username.lower() == "c":
        raise ValueError("Invalid Telegram public channel username")
    message_id = _positive_int(message, "message ID")
    topic_id = _positive_int(topic, "topic ID") if topic is not None else None
    return TelegramMessageReference(
        type="public_channel_topic_message" if topic_id is not None else "public_channel_message",
        username=username,
        topic_id=topic_id,
        message_id=message_id,
        original_url=original_url,
    )


def parse_telegram_reference(
    value: str | None = None,
    *,
    telegram_chat_id: int | str | None = None,
    telegram_message_id: int | str | None = None,
    telegram_topic_id: int | str | None = None,
) -> TelegramMessageReference:
    """Parse web/deep links or explicit IDs into one normalized message reference."""

    original = (value or "").strip()
    if not original:
        if telegram_chat_id is None or telegram_message_id is None:
            raise ValueError("Provide a Telegram message URL or both telegram_chat_id and telegram_message_id")
        chat_id = int(telegram_chat_id)
        channel_internal_id = (
            peer_id_to_channel_internal_id(chat_id)
            if chat_id < 0
            else _positive_int(chat_id, "channel ID")
        )
        reference = _private_reference(
            channel_internal_id,
            telegram_message_id,
            topic=telegram_topic_id,
            original_url="",
        )
        return TelegramMessageReference(**{**reference.as_dict(), "original_url": reference.canonical_url()})

    candidate = original.rstrip(TRAILING_TEXT_PUNCTUATION)
    if candidate.lower().startswith(("t.me/", "www.t.me/", "telegram.me/", "telegram.dog/")):
        candidate = f"https://{candidate}"

    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()

    if scheme in {"http", "https"}:
        if parsed.netloc.lower() not in TELEGRAM_WEB_HOSTS:
            raise ValueError("Unsupported Telegram URL host")
        segments = [segment for segment in parsed.path.split("/") if segment]
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_topic = _topic_from_query(query)
        if len(segments) in {3, 4} and segments[0].lower() == "c":
            topic = segments[2] if len(segments) == 4 else query_topic
            message = segments[3] if len(segments) == 4 else segments[2]
            return _private_reference(segments[1], message, topic=topic, original_url=original)
        if len(segments) in {2, 3}:
            topic = segments[1] if len(segments) == 3 else query_topic
            message = segments[2] if len(segments) == 3 else segments[1]
            return _public_reference(segments[0], message, topic=topic, original_url=original)
        raise ValueError("Unsupported Telegram message URL format")

    if scheme == "tg":
        action = (parsed.netloc or parsed.path.lstrip("/")).lower()
        query = parse_qs(parsed.query, keep_blank_values=True)
        topic = (query.get("thread") or [None])[0]
        if action == "privatepost":
            channel = (query.get("channel") or [None])[0]
            post = (query.get("post") or [None])[0]
            if channel is None or post is None:
                raise ValueError("Private Telegram deep link requires channel and post")
            return _private_reference(channel, post, topic=topic, original_url=original)
        if action == "resolve":
            username = (query.get("domain") or [None])[0]
            post = (query.get("post") or [None])[0]
            if username is None or post is None:
                raise ValueError("Public Telegram deep link requires domain and post")
            return _public_reference(username, post, topic=topic, original_url=original)
        raise ValueError("Unsupported Telegram deep link format")

    raise ValueError("Unsupported Telegram message reference")


def find_telegram_message_reference(text: str) -> TelegramMessageReference | None:
    for match in TELEGRAM_LINK_RE.finditer(text):
        try:
            return parse_telegram_reference(match.group(0))
        except ValueError:
            continue
    return None


def parse_telegram_message_link(url_or_channel: str, message_id: int | None = None) -> str | int:
    """Backward-compatible helper returning the normalized entity reference."""
    if message_id is None:
        return parse_telegram_reference(url_or_channel).entity_reference
    return url_or_channel.strip()


def parse_telegram_link(url: str) -> tuple[str | int, int] | None:
    """Backward-compatible tuple API. New code should use parse_telegram_reference."""
    try:
        reference = parse_telegram_reference(url)
    except ValueError:
        return None
    return reference.entity_reference, reference.message_id
