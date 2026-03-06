from __future__ import annotations

import asyncio
import logging
import sys

from app.config import Settings
from app.telegram_worker import TelegramPipeWorker


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


async def main() -> None:
    settings = Settings.load()
    configure_logging(settings.log_level)
    worker = TelegramPipeWorker(settings)
    try:
        await worker.start()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
