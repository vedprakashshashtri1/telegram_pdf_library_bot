from __future__ import annotations

import asyncio
from datetime import timedelta

from telegram.error import NetworkError, RetryAfter, TimedOut


def _retry_seconds(value) -> float:
    if isinstance(value, timedelta):
        return max(0.0, value.total_seconds())
    try:
        return max(0.0, float(value))
    except Exception:
        return 1.0


async def tg_call(func, *args, retries: int = 6, **kwargs):
    """Retry Telegram flood-waits and transient network failures.

    Permanent API errors (BadRequest/Forbidden/etc.) are intentionally returned to
    the caller immediately so the bot does not hammer Telegram with invalid calls.
    """
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as exc:
            if attempt >= retries:
                raise
            await asyncio.sleep(_retry_seconds(exc.retry_after) + 1.0)
        except (TimedOut, NetworkError):
            if attempt >= retries:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20.0)
