from __future__ import annotations

import calendar
import json
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .config import GUIDE_RAPIDE_HOST_SUFFIX, MONTHS


def normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()

def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )

def parse_int(value: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d]", "", value)
    if not cleaned:
        return None
    return int(cleaned)

def parse_french_date(raw: str) -> Optional[datetime]:
    value = normalize_text(raw).lower()
    value = value.replace("1er", "1")
    value = strip_accents(value)
    value = re.sub(r"\b(vers|environ|sortie|prevue|prévue)\b", " ", value)
    match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", value)
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS.get(match.group(2))
    year = int(match.group(3))
    if not month:
        return None

    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None

def dt_to_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.date().isoformat()

def parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None

def parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default

def write_json(path: Path, payload: object) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

def atomic_write_text(path: Path, content: str) -> None:
    """Write a file without exposing a partially written JSON/HTML artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

def split_list(value: str) -> list[str]:
    parts = [normalize_text(x) for x in re.split(r"\s*,\s*", value)]
    return [x for x in parts if x]

def normalize_image_url(url: str) -> str:
    cleaned = normalize_text(url)
    if not cleaned:
        return ""
    if cleaned.startswith("http://www.guide-rapide.com/"):
        return "https://www.guide-rapide.com/" + cleaned.removeprefix("http://www.guide-rapide.com/")
    if cleaned.startswith("http://guide-rapide.com/"):
        return "https://guide-rapide.com/" + cleaned.removeprefix("http://guide-rapide.com/")
    return cleaned

def is_guide_rapide_url(url: str) -> bool:
    host = (urlparse(normalize_text(url)).hostname or "").lower()
    return host == GUIDE_RAPIDE_HOST_SUFFIX or host.endswith(f".{GUIDE_RAPIDE_HOST_SUFFIX}")

def normalize_provider_image_url(url: str) -> str:
    """Return an image URL only when it is not hosted by the source site."""
    normalized = normalize_image_url(url)
    lowered = normalized.lower()
    if lowered in {"n/a", "na", "none", "null"}:
        return ""
    if is_guide_rapide_url(normalized):
        return ""
    return normalized

def subtract_months(src: date, months: int) -> date:
    month_index = src.month - 1 - months
    year = src.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(src.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

def add_months(src: date, months: int) -> date:
    month_index = src.month - 1 + months
    year = src.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(src.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

def log(message: str) -> None:
    print(message, flush=True)
