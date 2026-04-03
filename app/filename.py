from __future__ import annotations

import re
from pathlib import Path


BAD_CHARS = re.compile(r"[^\w\-.()\[\] ]+", re.UNICODE)
MULTI_SPACE = re.compile(r"\s+")
MULTI_DASH = re.compile(r"-+")
EPISODE_HINT = re.compile(r"=(\d{1,3})(?=\D|$)")
VJ_HINT = re.compile(r"\bVJ\s+([A-Z0-9 _.-]+)$", re.IGNORECASE)


def sanitize_filename(name: str) -> str:
    base = Path(name).name.strip()
    base = base.replace("/", "-").replace("\\", "-")
    base = BAD_CHARS.sub(" ", base)
    base = MULTI_SPACE.sub(" ", base).strip()
    return base or "telegram_media.bin"


def storage_safe_filename(name: str) -> str:
    cleaned = sanitize_filename(name)
    path = Path(cleaned)
    stem = MULTI_DASH.sub("-", MULTI_SPACE.sub("-", path.stem)).strip("-.")
    extension = path.suffix.lower()

    if not stem:
        stem = "telegram_media"

    return f"{stem}{extension}"



def extract_metadata(filename: str) -> dict[str, str | int | None]:
    cleaned = sanitize_filename(filename)
    stem = Path(cleaned).stem

    episode = None
    episode_match = EPISODE_HINT.search(stem)
    if episode_match:
        try:
            episode = int(episode_match.group(1))
        except ValueError:
            episode = None

    vj = None
    vj_match = VJ_HINT.search(stem)
    if vj_match:
        vj = vj_match.group(1).replace("_", " ").strip()

    title = stem
    if episode_match:
        title = title.replace(episode_match.group(0), " ")
    if vj_match:
        title = title[: vj_match.start()].strip(" -_=.")
    title = MULTI_SPACE.sub(" ", title).strip(" -_=.") or stem

    return {
        "original_filename": cleaned,
        "title_guess": title,
        "episode_guess": episode,
        "vj_guess": vj,
        "extension": Path(cleaned).suffix.lower(),
    }
