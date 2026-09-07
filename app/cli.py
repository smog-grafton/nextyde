"""
CLI for one-off operations (e.g. process a single t.me link).
Usage:
  python -m app.cli process-link "https://t.me/jozzmovies/45"
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.link_parser import TelegramMessageReference, parse_telegram_reference


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    if len(sys.argv) < 2 or sys.argv[1] != "process-link":
        print("Usage: python -m app.cli process-link <t.me URL>", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 3:
        print("Error: provide a t.me message URL", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[2].strip()
    try:
        reference = parse_telegram_reference(url)
    except ValueError as exc:
        print(f"Error: invalid Telegram message reference: {exc}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_run_process_link(url, reference))


async def _run_process_link(url: str, reference: TelegramMessageReference) -> None:
    from app.config import Settings
    from app.telegram_worker import TelegramPipeWorker

    settings = Settings.load()
    worker = TelegramPipeWorker(settings)
    await worker.store.init()
    worker.settings.temp_dir.mkdir(parents=True, exist_ok=True)
    await worker.client.connect()

    if not await worker.client.is_user_authorized():
        await worker.client.send_code_request(settings.tg_phone)
        code = (settings.tg_login_code or "").strip() or input("Enter Telegram login code: ").strip()
        if not code:
            print("Error: login code required (set TG_LOGIN_CODE or run interactively)", file=sys.stderr)
            await worker.client.disconnect()
            sys.exit(1)
        try:
            await worker.client.sign_in(settings.tg_phone, code)
        except Exception as e:  # noqa: BLE001
            print(f"Login failed: {e}", file=sys.stderr)
            await worker.client.disconnect()
            sys.exit(1)

    try:
        _, message = await worker.resolve_message_reference(reference)
        if not worker._is_supported_media(message):
            print(
                f"Message {reference.message_id} exists but has no supported downloadable video/document",
                file=sys.stderr,
            )
            sys.exit(1)
        await worker._handle_message(
            message,
            catch_up=False,
            intake_metadata={
                "telegram_url": reference.canonical_url(),
                "telegram_reference_type": reference.type,
                "telegram_peer_id": reference.peer_id,
                "telegram_channel_internal_id": reference.channel_internal_id,
                "telegram_topic_id": reference.topic_id,
            },
        )
    finally:
        await worker.cdn.close()
        await worker.client.disconnect()
