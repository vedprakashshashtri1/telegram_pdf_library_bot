from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup

from .retry import tg_call
from .utils import utcnow

ROLE_LEVEL = {"free": 0, "premium": 1, "admin": 2, "super": 3, "owner": 4}


async def get_role(db, bot_id: int, user_id: int) -> str:
    if await db.is_owner(user_id):
        return "owner"
    role_doc = await db.roles.find_one({"bot_id": int(bot_id), "user_id": int(user_id)})
    if role_doc and role_doc.get("role") in {"admin", "super"}:
        return role_doc["role"]
    if await db.active_premium(bot_id, user_id):
        return "premium"
    return "free"


async def require_role(update, context, minimum: str) -> bool:
    db = context.application.bot_data["db"]
    bot_id = context.application.bot_data["bot_id"]
    user = update.effective_user
    if not user:
        return False
    role = await get_role(db, bot_id, user.id)
    if ROLE_LEVEL[role] < ROLE_LEVEL[minimum]:
        await tg_call(
            context.bot.send_message,
            chat_id=update.effective_chat.id,
            text=f"⛔ This command requires {minimum.title()} access.",
        )
        return False
    return True


async def require_owner(update, context) -> bool:
    db = context.application.bot_data["db"]
    user = update.effective_user
    if user and await db.is_owner(user.id):
        return True
    if update.effective_chat:
        await tg_call(context.bot.send_message, chat_id=update.effective_chat.id, text="⛔ Owner access required.")
    return False


def _member_ok(member) -> bool:
    status = getattr(member, "status", "")
    if status in {ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER}:
        return True
    if status == ChatMember.RESTRICTED and getattr(member, "is_member", False):
        return True
    return False


async def check_force_access(bot, db, bot_id: int, user_id: int):
    missing = []
    cursor = db.force_channels.find({"bot_id": int(bot_id)}).sort([("kind", 1), ("title", 1)])
    async for ch in cursor:
        channel_id = int(ch["channel_id"])
        satisfied = False
        try:
            member = await tg_call(bot.get_chat_member, channel_id, int(user_id))
            satisfied = _member_ok(member)
        except Exception:
            satisfied = False

        if not satisfied and ch.get("kind") == "request":
            requested = await db.join_requests.find_one(
                {"bot_id": int(bot_id), "channel_id": channel_id, "user_id": int(user_id)}
            )
            satisfied = requested is not None
        if not satisfied:
            missing.append(ch)
    return missing


def force_join_keyboard(missing):
    rows = []
    for ch in missing:
        label = (
            f"📨 Request {ch.get('title', 'Channel')}"
            if ch.get("kind") == "request"
            else f"📢 Join {ch.get('title', 'Channel')}"
        )
        rows.append([InlineKeyboardButton(label, url=ch["invite_link"])])
    rows.append([InlineKeyboardButton("✅ Check Again", callback_data="access:check")])
    return InlineKeyboardMarkup(rows)


async def reserve_daily_quota(db, settings, bot_id: int, user_id: int, role: str):
    if ROLE_LEVEL[role] >= ROLE_LEVEL["premium"]:
        return True, None
    cfg = await db.get_settings(bot_id)
    limit = int(cfg.get("free_daily_limit", settings.default_free_limit))
    if limit <= 0:
        return False, None

    local_date = datetime.now(ZoneInfo(settings.app_timezone)).date().isoformat()
    key = f"{int(bot_id)}:{int(user_id)}:{local_date}"
    try:
        doc = await db.usage_daily.find_one_and_update(
            {
                "_id": key,
                "$or": [{"count": {"$lt": limit}}, {"count": {"$exists": False}}],
            },
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "bot_id": int(bot_id),
                    "user_id": int(user_id),
                    "date": local_date,
                    "created_at": utcnow(),
                },
                "$set": {"updated_at": utcnow()},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return False, None
    if not doc or int(doc.get("count", 0)) > limit:
        return False, None
    return True, key


async def rollback_quota(db, key: str | None):
    if key:
        await db.usage_daily.update_one(
            {"_id": key, "count": {"$gt": 0}},
            {"$inc": {"count": -1}, "$set": {"updated_at": utcnow()}},
        )
