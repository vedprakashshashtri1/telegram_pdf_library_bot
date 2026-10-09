from __future__ import annotations

import html
import random
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

USERNAME_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}", re.IGNORECASE)
TG_URL_RE = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:[A-Za-z0-9_+./-]+)"
)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
SPACE_RE = re.compile(r"[ \t]{2,}")
EMPTY_LINES_RE = re.compile(r"\n{3,}")
JUNK_EDGE_RE = re.compile(r"^[\s|•·\-–—_:;,]+|[\s|•·\-–—_:;,]+$")

LETTERS = string.ascii_letters


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean_catalog_text(value: str | None) -> str:
    if not value:
        return ""
    text = ZERO_WIDTH_RE.sub("", value)
    text = TG_URL_RE.sub(" ", text)
    text = USERNAME_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = SPACE_RE.sub(" ", line).strip()
        line = JUNK_EDGE_RE.sub("", line)
        if line:
            lines.append(line)
    text = "\n".join(lines)
    text = EMPTY_LINES_RE.sub("\n\n", text)
    return text.strip()


def clean_pdf_title(filename: str | None) -> str:
    name = clean_catalog_text(filename or "")
    name = re.sub(r"(?i)\.pdf$", "", name).strip()
    return name


def is_pdf_name(filename: str | None, mime_type: str | None = None) -> bool:
    if mime_type and mime_type.lower() == "application/pdf":
        return True
    return bool(filename and filename.lower().endswith(".pdf"))


def generate_obfuscated_post_token(post_id: int) -> str:
    """
    Keeps ONLY the post ID digits as digits. Everything else is random letters.
    Example: 256 -> aaQ2gdH5kLm6zz

    Max random run is intentionally bounded so the token stays well under
    Telegram's 64-character start-parameter limit.
    """
    digits = str(int(post_id))
    chunks = ["".join(secrets.choice(LETTERS) for _ in range(secrets.randbelow(4) + 2))]
    for digit in digits:
        chunks.append(digit)
        chunks.append(
            "".join(secrets.choice(LETTERS) for _ in range(secrets.randbelow(4) + 1))
        )
    chunks.append("".join(secrets.choice(LETTERS) for _ in range(secrets.randbelow(4) + 2)))
    return "".join(chunks)


def post_id_from_token(token: str) -> Optional[int]:
    digits = "".join(ch for ch in token if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def user_display_name(user) -> str:
    full = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    if full.strip():
        return full.strip()
    username = getattr(user, "username", None)
    return f"@{username}" if username else str(getattr(user, "id", "Unknown"))


def safe_html(value) -> str:
    return html.escape(str(value if value is not None else ""))


def days_left(until: datetime | None) -> int:
    if not until:
        return 0
    now = utcnow()
    seconds = (until - now).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 86399) // 86400))
