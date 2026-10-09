from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import timedelta
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.error import BadRequest

from .access import (
    ROLE_LEVEL,
    check_force_access,
    force_join_keyboard,
    get_role,
    require_owner,
    require_role,
    reserve_daily_quota,
    rollback_quota,
)
from .referrals import claim_referrals, referral_stats, refresh_referral_completion, register_referral
from .retry import tg_call
from .utils import days_left, post_id_from_token, safe_html, user_display_name, utcnow


def _bot_id(context) -> int:
    return int(context.application.bot_data["bot_id"])


def _arg(context, position=0):
    return context.args[position] if len(context.args) > position else None


async def _send(update, context, text: str, **kwargs):
    return await tg_call(
        context.bot.send_message,
        chat_id=update.effective_chat.id,
        text=text,
        **kwargs,
    )


async def track_update_user(update, context):
    user = update.effective_user
    chat = update.effective_chat
    # Broadcast audiences should contain people who actually interacted with the
    # bot in private, not unrelated channel members/admins from membership events.
    if not user or not chat or getattr(chat, "type", None) != "private":
        return
    db = context.application.bot_data["db"]
    await db.upsert_bot_user(_bot_id(context), user)


async def start(update, context):
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    bot_id = _bot_id(context)
    user = update.effective_user
    if not user:
        return
    await db.upsert_bot_user(bot_id, user)

    payload = context.args[0] if context.args else None
    if payload and payload.startswith("reff_"):
        try:
            referrer = int(payload[5:])
        except ValueError:
            referrer = 0
        if referrer > 0:
            await register_referral(db, bot_id, user.id, referrer)
        await refresh_referral_completion(context.bot, db, bot_id, user.id)
        missing = await check_force_access(context.bot, db, bot_id, user.id)
        if missing:
            await _send(
                update,
                context,
                "👋 Referral saved. Complete all channel requirements, then tap Check Again.",
                reply_markup=force_join_keyboard(missing),
            )
            return
        await _send(update, context, "✅ Referral recorded. Use the menu to open the website.")
        return

    if payload:
        post_id = post_id_from_token(payload)
        if post_id is not None:
            pdf = await db.pdfs.find_one(
                {
                    "source_chat_id": int(settings.source_channel_id),
                    "post_id": int(post_id),
                    "start_token": payload,
                    "active": True,
                }
            )
            if pdf:
                await _deliver_pdf(update, context, pdf)
                return
        await _send(update, context, "❌ This file link is invalid or no longer available.")
        return

    await refresh_referral_completion(context.bot, db, bot_id, user.id)
    missing = await check_force_access(context.bot, db, bot_id, user.id)
    if missing:
        await _send(
            update,
            context,
            "👋 Welcome. Complete all required channel requirements first.",
            reply_markup=force_join_keyboard(missing),
        )
        return

    role = await get_role(db, bot_id, user.id)
    name = context.bot.first_name or "Bot"
    await _send(
        update,
        context,
        f"👋 Welcome to {name}!\nRole: {role.title()}\n\nUse the menu to open the website or /help for commands.",
    )


async def _deliver_pdf(update, context, pdf):
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    bot_id = _bot_id(context)
    user = update.effective_user

    missing = await check_force_access(context.bot, db, bot_id, user.id)
    if missing:
        await _send(
            update,
            context,
            "🔒 Complete all channel requirements before receiving files.",
            reply_markup=force_join_keyboard(missing),
        )
        return

    await refresh_referral_completion(context.bot, db, bot_id, user.id)
    role = await get_role(db, bot_id, user.id)
    ok, quota_key = await reserve_daily_quota(db, settings, bot_id, user.id, role)
    if not ok:
        cfg = await db.get_settings(bot_id)
        limit = int(cfg.get("free_daily_limit", settings.default_free_limit))
        await _send(
            update,
            context,
            f"🚫 Your daily free limit is {limit} file(s). Use /refer to earn Premium days.",
        )
        return

    try:
        result = await tg_call(
            context.bot.copy_message,
            chat_id=user.id,
            from_chat_id=int(pdf["source_chat_id"]),
            message_id=int(pdf["post_id"]),
            retries=10,
        )
    except Exception:
        await rollback_quota(db, quota_key)
        await _send(update, context, "❌ This file is currently unavailable. Please try again later.")
        return

    await db.delete_jobs.insert_one(
        {
            "bot_id": bot_id,
            "chat_id": int(user.id),
            "message_id": int(result.message_id),
            "delete_at": utcnow() + timedelta(seconds=settings.delete_after_seconds),
            "attempts": 0,
            "created_at": utcnow(),
        }
    )
    minutes = max(1, settings.delete_after_seconds // 60)
    await _send(update, context, f"⏳ This file will be removed in about {minutes} minute(s).")


async def help_cmd(update, context):
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    role = await get_role(db, bot_id, update.effective_user.id)

    lines = [
        "📚 Commands",
        "",
        "/start - Start the bot",
        "/help - Show commands available to you",
        "/refer - Referral link and rewards",
        "/ping - Check response",
        "/uptime - Show bot uptime",
    ]
    if ROLE_LEVEL[role] >= ROLE_LEVEL["admin"]:
        lines += [
            "",
            "🛡 Premium Management",
            "/add_premium <telegram_id> <days>",
            "/rem_premium <telegram_id>",
            "/check_premium <telegram_id>",
            "/list_premium",
        ]
    if ROLE_LEVEL[role] >= ROLE_LEVEL["super"]:
        lines += [
            "",
            "⚡ Super Commands",
            "/add_channel <channel_id|@username>",
            "/rem_channel <channel_id|@username>",
            "/list_channel",
            "/add_request <channel_id|@username>",
            "/rem_request <channel_id|@username>",
            "/list_request",
            "/set_free <number>",
            "/set_refer <number>",
        ]
    if role == "owner":
        lines += [
            "",
            "👑 Owner Commands",
            "/add_admin <telegram_id>",
            "/rem_admin <telegram_id>",
            "/list_admin",
            "/add_super <telegram_id>",
            "/rem_super <telegram_id>",
            "/list_super",
            "/add_owner <telegram_id>",
            "/rem_owner <telegram_id>",
            "/list_owner [telegram_id]",
            "/add_bot <bot_token>",
            "/list_bot",
            "/rem_bot <bot_id|@username>",
            "/broadcast <message> - or reply with /broadcast",
            "/restart [bot_id|@username]",
        ]
    await _send(update, context, "\n".join(lines))


async def ping(update, context):
    started = time.perf_counter()
    await tg_call(context.bot.get_me, retries=6)
    ms = (time.perf_counter() - started) * 1000
    await _send(update, context, f"🏓 Pong! {ms:.0f} ms")


def _human_uptime(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


async def uptime(update, context):
    started_at = context.application.bot_data["started_at"]
    seconds = int((utcnow() - started_at).total_seconds())
    await _send(update, context, f"⏱ Uptime: {_human_uptime(seconds)}")


async def website_prompt(update, context):
    settings = context.application.bot_data["settings"]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Open Website", url=settings.website_url)]])
    await _send(
        update,
        context,
        "📚 To browse and search files, open the website from the bot menu.",
        reply_markup=kb,
    )


# ----- Owner management -----

async def add_owner(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, "Usage: /add_owner <telegram_id>")
        return
    uid = int(raw)
    await db.owners.update_one(
        {"_id": uid},
        {"$set": {"updated_at": utcnow(), "added_by": update.effective_user.id}, "$setOnInsert": {"created_at": utcnow()}},
        upsert=True,
    )
    await _send(update, context, f"✅ {uid} is now an Owner.")


async def rem_owner(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, "Usage: /rem_owner <telegram_id>")
        return
    uid = int(raw)
    if not await db.is_owner(uid):
        await _send(update, context, "ℹ️ That user is not an Owner.")
        return
    if await db.owner_count() <= 1:
        await _send(update, context, "❌ The last Owner cannot be removed.")
        return
    await db.owners.delete_one({"_id": uid})
    await _send(update, context, f"✅ Owner removed: {uid}")


async def list_owner(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    raw = _arg(context)
    if raw and raw.lstrip("-").isdigit():
        uid = int(raw)
        doc = await db.owners.find_one({"_id": uid})
        if not doc:
            await _send(update, context, "ℹ️ Owner not found.")
            return
        user_doc = await db.bot_users.find_one({"user_id": uid}, sort=[("last_seen_at", -1)])
        name = (user_doc or {}).get("name") or "Unknown"
        await _send(update, context, f"👑 Owner\nName: {name}\nID: {uid}")
        return

    docs = await db.owners.find({}).sort("_id", 1).to_list(None)
    lines = ["👑 Owners"]
    for doc in docs:
        uid = int(doc["_id"])
        user_doc = await db.bot_users.find_one({"user_id": uid}, sort=[("last_seen_at", -1)])
        lines.append(f"• {(user_doc or {}).get('name') or 'Unknown'} — {uid}")
    await _send(update, context, "\n".join(lines))


# ----- Multi-bot management -----

async def add_bot(update, context):
    if not await require_owner(update, context):
        return
    token = _arg(context)
    if not token:
        await _send(update, context, "Usage: /add_bot <bot_token>")
        return
    supervisor = context.application.bot_data["supervisor"]
    try:
        info = await supervisor.add_bot_token(token, added_by=update.effective_user.id)
    except Exception:
        await _send(update, context, "❌ Could not validate or start that bot. Check the token and try again.")
        return
    await _send(
        update,
        context,
        f"✅ Bot added and started.\nName: {info['name']}\nUsername: @{info['username']}\nBot ID: {info['bot_id']}",
    )


async def list_bot(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    supervisor = context.application.bot_data["supervisor"]
    docs = await db.bots.find({}).sort("name", 1).to_list(None)
    if not docs:
        await _send(update, context, "No bots are registered.")
        return
    lines = ["🤖 Bots"]
    for d in docs:
        bid = int(d["_id"])
        running = supervisor.is_running(bid)
        username = f"@{d.get('username')}" if d.get("username") else "No username"
        lines.append(f"• {d.get('name') or 'Bot'} — {username} — {bid} — {'Running' if running else 'Stopped'}")
    await _send(update, context, "\n".join(lines))


async def _resolve_bot_target(db, raw: str | None, current_bot_id: int):
    if not raw:
        return current_bot_id
    if raw.lstrip("-").isdigit():
        return int(raw)
    username = raw.lstrip("@")
    doc = await db.bots.find_one({"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}})
    return int(doc["_id"]) if doc else None


async def rem_bot(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    supervisor = context.application.bot_data["supervisor"]
    target = await _resolve_bot_target(db, _arg(context), _bot_id(context))
    if target is None or not await db.bots.find_one({"_id": target}):
        await _send(update, context, "❌ Bot not found.")
        return
    await _send(update, context, f"✅ Removing bot {target}.")
    supervisor.schedule_remove(target)


async def restart(update, context):
    if not await require_owner(update, context):
        return
    db = context.application.bot_data["db"]
    supervisor = context.application.bot_data["supervisor"]
    target = await _resolve_bot_target(db, _arg(context), _bot_id(context))
    if target is None or not await db.bots.find_one({"_id": target, "enabled": True}):
        await _send(update, context, "❌ Active bot not found.")
        return
    await _send(update, context, f"♻️ Restarting bot {target}...")
    supervisor.schedule_restart(target)


# ----- Admin / Super role management -----

async def _set_staff(update, context, role: str):
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, f"Usage: /add_{role} <telegram_id>")
        return
    uid = int(raw)
    user_doc = await db.bot_users.find_one({"bot_id": bot_id, "user_id": uid}) or {}
    await db.roles.update_one(
        {"bot_id": bot_id, "user_id": uid},
        {
            "$set": {
                "bot_id": bot_id,
                "user_id": uid,
                "role": role,
                "name": user_doc.get("name", ""),
                "updated_at": utcnow(),
                "added_by": update.effective_user.id,
            },
            "$setOnInsert": {"created_at": utcnow()},
        },
        upsert=True,
    )
    await _send(update, context, f"✅ {uid} is now {role.title()}.")


async def add_admin(update, context):
    if await require_owner(update, context):
        await _set_staff(update, context, "admin")


async def add_super(update, context):
    if await require_owner(update, context):
        await _set_staff(update, context, "super")


async def _remove_staff(update, context, role):
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, f"Usage: /rem_{role} <telegram_id>")
        return
    result = await db.roles.delete_one({"bot_id": bot_id, "user_id": int(raw), "role": role})
    await _send(update, context, "✅ Removed." if result.deleted_count else f"ℹ️ User is not a {role.title()}.")


async def rem_admin(update, context):
    if await require_owner(update, context):
        await _remove_staff(update, context, "admin")


async def rem_super(update, context):
    if await require_owner(update, context):
        await _remove_staff(update, context, "super")


async def _list_staff(update, context, role):
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    docs = await db.roles.find({"bot_id": bot_id, "role": role}).sort("name", 1).to_list(None)
    if not docs:
        await _send(update, context, f"No {role}s.")
        return
    lines = [f"{'🛡' if role == 'admin' else '⚡'} {role.title()}s"]
    for d in docs:
        user_doc = await db.bot_users.find_one({"bot_id": bot_id, "user_id": d["user_id"]}) or {}
        lines.append(f"• {user_doc.get('name') or d.get('name') or 'Unknown'} — {d['user_id']}")
    await _send(update, context, "\n".join(lines))


async def list_admin(update, context):
    if await require_owner(update, context):
        await _list_staff(update, context, "admin")


async def list_super(update, context):
    if await require_owner(update, context):
        await _list_staff(update, context, "super")


# ----- Premium -----

async def add_premium(update, context):
    if not await require_role(update, context, "admin"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    if len(context.args) != 2 or not context.args[0].lstrip("-").isdigit() or not context.args[1].isdigit():
        await _send(update, context, "Usage: /add_premium <telegram_id> <days>")
        return
    uid, days = int(context.args[0]), int(context.args[1])
    if days <= 0:
        await _send(update, context, "Days must be greater than 0.")
        return
    user_doc = await db.bot_users.find_one({"bot_id": bot_id, "user_id": uid}) or {}
    until = await db.add_premium_days(bot_id, uid, days, user_doc.get("name", ""), update.effective_user.id)
    await _send(update, context, f"✅ Added {days} Premium day(s) to {uid}.\nValid until: {until}")


async def rem_premium(update, context):
    if not await require_role(update, context, "admin"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, "Usage: /rem_premium <telegram_id>")
        return
    result = await db.premium.delete_one({"bot_id": bot_id, "user_id": int(raw)})
    await _send(update, context, "✅ Premium removed." if result.deleted_count else "ℹ️ No Premium record found.")


async def check_premium(update, context):
    if not await require_role(update, context, "admin"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw or not raw.lstrip("-").isdigit():
        await _send(update, context, "Usage: /check_premium <telegram_id>")
        return
    uid = int(raw)
    doc = await db.active_premium(bot_id, uid)
    if not doc:
        await _send(update, context, f"ℹ️ {uid} is not currently Premium.")
        return
    await _send(
        update,
        context,
        f"✅ Premium active\nID: {uid}\nName: {doc.get('name') or 'Unknown'}\n"
        f"Since: {doc.get('premium_since')}\nUntil: {doc.get('premium_until')}\n"
        f"Days left: {days_left(doc.get('premium_until'))}",
    )


async def list_premium(update, context):
    if not await require_role(update, context, "admin"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    docs = await db.premium.find({"bot_id": bot_id, "premium_until": {"$gt": utcnow()}}).sort("premium_until", 1).to_list(None)
    rows = []
    for d in docs:
        rows.append(
            "<tr>"
            f"<td>{safe_html(d.get('name') or 'Unknown')}</td>"
            f"<td>{safe_html(d['user_id'])}</td>"
            f"<td>{safe_html(d.get('premium_since'))}</td>"
            f"<td>{safe_html(d.get('premium_until'))}</td>"
            f"<td>{days_left(d.get('premium_until'))}</td>"
            "</tr>"
        )
    page = f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>Premium Users</title>
<style>body{{font-family:Arial,sans-serif;padding:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}th{{background:#f4f4f4}}</style></head>
<body><h1>Premium Users</h1><p>Generated: {escape(str(utcnow()))}</p><table><thead><tr><th>Name</th><th>Telegram ID</th><th>Premium Since</th><th>Premium Until</th><th>Days Left</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""
    bio = io.BytesIO(page.encode("utf-8"))
    bio.name = "premium_users.html"
    await tg_call(
        context.bot.send_document,
        chat_id=update.effective_chat.id,
        document=InputFile(bio, filename="premium_users.html"),
        caption=f"Premium users: {len(docs)}",
    )


# ----- Forced channels -----

async def _parse_channel_target(raw: str):
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else "@" + raw


async def _add_force_channel(update, context, kind):
    if not await require_role(update, context, "super"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw:
        await _send(update, context, f"Usage: /add_{'request' if kind == 'request' else 'channel'} <channel_id|@username>")
        return
    target = await _parse_channel_target(raw)
    try:
        chat = await tg_call(context.bot.get_chat, target)
        cid = int(chat.id)
        if kind == "request":
            invite = await tg_call(
                context.bot.create_chat_invite_link,
                cid,
                creates_join_request=True,
                name="Access Request",
            )
            link = invite.invite_link
        elif chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            invite = await tg_call(context.bot.create_chat_invite_link, cid, name="Access Link")
            link = invite.invite_link
        await db.force_channels.update_one(
            {"bot_id": bot_id, "channel_id": cid},
            {
                "$set": {
                    "bot_id": bot_id,
                    "channel_id": cid,
                    "kind": kind,
                    "title": chat.title or str(cid),
                    "username": chat.username,
                    "invite_link": link,
                    "updated_at": utcnow(),
                    "added_by": update.effective_user.id,
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )
        await _send(update, context, f"✅ Added {chat.title or cid}. Link generated automatically.")
    except Exception:
        await _send(update, context, "❌ Could not add this channel. Check the bot's channel permissions and try again.")


async def add_channel(update, context):
    await _add_force_channel(update, context, "required")


async def add_request(update, context):
    await _add_force_channel(update, context, "request")


async def _find_force_channel(db, bot_id, raw):
    if raw.lstrip("-").isdigit():
        return await db.force_channels.find_one({"bot_id": bot_id, "channel_id": int(raw)})
    username = raw.lstrip("@")
    return await db.force_channels.find_one(
        {"bot_id": bot_id, "username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}
    )


async def _remove_force_channel(update, context, kind):
    if not await require_role(update, context, "super"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    raw = _arg(context)
    if not raw:
        await _send(update, context, f"Usage: /rem_{'request' if kind == 'request' else 'channel'} <channel_id|@username>")
        return
    doc = await _find_force_channel(db, bot_id, raw)
    if not doc or doc.get("kind") != kind:
        await _send(update, context, "ℹ️ Channel not found in this list.")
        return
    await db.force_channels.delete_one({"_id": doc["_id"]})
    await _send(update, context, f"✅ Removed {doc.get('title', doc['channel_id'])}.")


async def rem_channel(update, context):
    await _remove_force_channel(update, context, "required")


async def rem_request(update, context):
    await _remove_force_channel(update, context, "request")


async def _list_force_channels(update, context, kind):
    if not await require_role(update, context, "super"):
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    docs = await db.force_channels.find({"bot_id": bot_id, "kind": kind}).sort("title", 1).to_list(None)
    if not docs:
        await _send(update, context, "No channels configured.")
        return
    rows = [[InlineKeyboardButton(f"📢 {d.get('title', d['channel_id'])}", callback_data=f"chdetail:{kind}:{d['channel_id']}")] for d in docs]
    await _send(update, context, "Select a channel:", reply_markup=InlineKeyboardMarkup(rows))


async def list_channel(update, context):
    await _list_force_channels(update, context, "required")


async def list_request(update, context):
    await _list_force_channels(update, context, "request")


async def set_free(update, context):
    if not await require_role(update, context, "super"):
        return
    raw = _arg(context)
    if raw is None or not raw.isdigit():
        await _send(update, context, "Usage: /set_free <number>")
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    await db.settings.update_one(
        {"_id": bot_id}, {"$set": {"free_daily_limit": int(raw), "updated_at": utcnow()}}, upsert=True
    )
    await _send(update, context, f"✅ Free users can receive {int(raw)} file(s) per day.")


async def set_refer(update, context):
    if not await require_role(update, context, "super"):
        return
    raw = _arg(context)
    if raw is None or not raw.isdigit() or int(raw) <= 0:
        await _send(update, context, "Usage: /set_refer <number greater than 0>")
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    await db.settings.update_one(
        {"_id": bot_id},
        {"$set": {"referrals_per_premium_day": int(raw), "updated_at": utcnow()}},
        upsert=True,
    )
    await _send(update, context, f"✅ Referral rate: {int(raw)} completed referral(s) = 1 Premium day.")


# ----- Referrals / callbacks -----

async def refer(update, context):
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    bot_id = _bot_id(context)
    user = update.effective_user
    await refresh_referral_completion(context.bot, db, bot_id, user.id)
    cfg = await db.get_settings(bot_id)
    rate = int(cfg.get("referrals_per_premium_day", settings.default_refer_rate))
    stats = await referral_stats(db, bot_id, user.id, rate)
    link = f"https://t.me/{context.bot.username}?start=reff_{user.id}"
    rows = []
    if stats["days"] > 0:
        rows.append([InlineKeyboardButton(f"🎁 Claim {stats['days']} Premium Day(s)", callback_data="refer:claim")])
    await _send(
        update,
        context,
        "👥 Referral Program\n\n"
        f"Your referral link:\n{link}\n\n"
        f"✅ Completed referrals: {stats['completed']}\n"
        f"⏳ Incomplete referrals: {stats['incomplete']}\n"
        f"🎯 {rate} completed referral(s) = 1 Premium day\n"
        f"🎁 Claimable Premium: {stats['days']} day(s)\n"
        f"♻️ Remaining referrals after claim: {stats['remainder']}",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        disable_web_page_preview=True,
    )


async def chat_join_request(update, context):
    req = update.chat_join_request
    if not req:
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    user = req.from_user
    await db.join_requests.update_one(
        {"bot_id": bot_id, "channel_id": int(req.chat.id), "user_id": int(user.id)},
        {"$set": {"bot_id": bot_id, "channel_id": int(req.chat.id), "user_id": int(user.id), "title": req.chat.title, "requested_at": utcnow()}},
        upsert=True,
    )
    await refresh_referral_completion(context.bot, db, bot_id, user.id)


async def chat_member_changed(update, context):
    changed = update.chat_member
    if not changed:
        return
    db = context.application.bot_data["db"]
    bot_id = _bot_id(context)
    channel = await db.force_channels.find_one({"bot_id": bot_id, "channel_id": int(changed.chat.id)}, {"_id": 1})
    if not channel:
        return
    target = changed.new_chat_member.user
    if target:
        await refresh_referral_completion(context.bot, db, bot_id, target.id)


async def general_callback(update, context):
    query = update.callback_query
    if not query:
        return
    await tg_call(query.answer)
    data = query.data or ""
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    bot_id = _bot_id(context)

    if data == "access:check":
        user = query.from_user
        await db.upsert_bot_user(bot_id, user)
        missing = await check_force_access(context.bot, db, bot_id, user.id)
        await refresh_referral_completion(context.bot, db, bot_id, user.id)
        if missing:
            await tg_call(query.edit_message_text, "❌ Some channel requirements are still incomplete.", reply_markup=force_join_keyboard(missing))
        else:
            await tg_call(query.edit_message_text, "✅ All requirements are complete. Return to the website and open the file again.")
        return

    if data == "refer:claim":
        cfg = await db.get_settings(bot_id)
        rate = int(cfg.get("referrals_per_premium_day", settings.default_refer_rate))
        days, consumed, until = await claim_referrals(db, bot_id, query.from_user.id, rate, user_display_name(query.from_user))
        if days <= 0:
            await tg_call(query.edit_message_text, "ℹ️ You don't currently have enough completed unclaimed referrals.")
        else:
            await tg_call(query.edit_message_text, f"✅ Added {days} Premium day(s).\nUsed referrals: {consumed}\nPremium valid until: {until}")
        return

    if data.startswith("chdetail:"):
        role = await get_role(db, bot_id, query.from_user.id)
        if ROLE_LEVEL[role] < ROLE_LEVEL["super"]:
            return
        _, kind, channel_id = data.split(":", 2)
        doc = await db.force_channels.find_one({"bot_id": bot_id, "channel_id": int(channel_id), "kind": kind})
        if not doc:
            await tg_call(query.edit_message_text, "Channel no longer exists.")
            return
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Remove Channel", callback_data=f"chremove:{kind}:{channel_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="chcancel")],
        ])
        await tg_call(query.edit_message_text, f"📢 {doc.get('title', channel_id)}\nID: {channel_id}\nType: {kind}", reply_markup=markup)
        return

    if data.startswith("chremove:"):
        role = await get_role(db, bot_id, query.from_user.id)
        if ROLE_LEVEL[role] < ROLE_LEVEL["super"]:
            return
        _, kind, channel_id = data.split(":", 2)
        doc = await db.force_channels.find_one({"bot_id": bot_id, "channel_id": int(channel_id), "kind": kind})
        if doc:
            await db.force_channels.delete_one({"_id": doc["_id"]})
            await tg_call(query.edit_message_text, f"✅ Removed {doc.get('title', channel_id)}.")
        else:
            await tg_call(query.edit_message_text, "Channel already removed.")
        return

    if data == "chcancel":
        await tg_call(query.edit_message_text, "Cancelled.")


async def error_handler(update, context):
    bot_id = context.application.bot_data.get("bot_id", "?")
    print(f"[telegram-error:{bot_id}] {context.error}")
