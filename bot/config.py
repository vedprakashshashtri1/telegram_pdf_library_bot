from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _read(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    mongo_uri: str
    mongo_db: str
    seed_owner_ids: frozenset[int]
    source_channel_id: int
    website_url: str
    app_secret: str
    app_timezone: str
    delete_after_seconds: int = 300
    default_free_limit: int = 5
    default_refer_rate: int = 2

    @classmethod
    def load(cls) -> "Settings":
        mongo_uri = _read("MONGO_URI")
        source_channel = _read("SOURCE_CHANNEL_ID")
        website_url = _read("WEBSITE_URL")
        app_secret = _read("APP_SECRET")
        owners_raw = _read("OWNER_IDS", "") or ""

        missing = []
        if not mongo_uri:
            missing.append("MONGO_URI")
        if not source_channel:
            missing.append("SOURCE_CHANNEL_ID")
        if not website_url:
            missing.append("WEBSITE_URL")
        if not app_secret:
            missing.append("APP_SECRET")
        if not owners_raw.strip():
            missing.append("OWNER_IDS")
        if missing:
            raise RuntimeError("Missing required configuration: " + ", ".join(missing))

        owners = frozenset(int(x.strip()) for x in owners_raw.split(",") if x.strip())
        tz = _read("APP_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"
        ZoneInfo(tz)

        website_url = str(website_url).rstrip("/")
        if not website_url.startswith("https://"):
            raise RuntimeError("WEBSITE_URL must use HTTPS for the Telegram menu button")

        return cls(
            bot_token=_read("BOT_TOKEN"),
            mongo_uri=str(mongo_uri),
            mongo_db=_read("MONGO_DB", "telegram_pdf_library") or "telegram_pdf_library",
            seed_owner_ids=owners,
            source_channel_id=int(str(source_channel)),
            website_url=website_url,
            app_secret=str(app_secret),
            app_timezone=tz,
            delete_after_seconds=int(_read("DELETE_AFTER_SECONDS", "300") or "300"),
            default_free_limit=int(_read("DEFAULT_FREE_LIMIT", "5") or "5"),
            default_refer_rate=int(_read("DEFAULT_REFER_RATE", "2") or "2"),
        )
