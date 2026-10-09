from __future__ import annotations

import asyncio
import time

from bson import ObjectId
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden

from .access import require_owner
from .retry import tg_call
from .utils import utcnow


async def broadcast_command(update, context):
    if not await require_owner(update, context):
        return

    message = update.effective_message
    chat_id = update.effective_chat.id
    db = context.application.bot_data["db"]
    bot_id = context.application.bot_data["bot_id"]

    payload = None
    args_text = " ".join(context.args).strip()
    if message.reply_to_message:
        payload = {
            "mode": "copy",
            "source_chat_id": int(message.chat_id),
            "source_message_id": int(message.reply_to_message.message_id),
        }
    elif args_text:
        payload = {"mode": "text", "text": args_text}

    if not payload:
        await tg_call(
            context.bot.send_message,
            chat_id=chat_id,
            text="Usage: /broadcast <message>\nOr reply to any message/media/file/sticker/GIF with /broadcast.",
        )
        return

    record = {
        "bot_id": int(bot_id),
        "created_by": int(update.effective_user.id),
        "payload": payload,
        "status": "pending",
        "created_at": utcnow(),
    }
    result = await db.broadcasts.insert_one(record)
    broadcast_id = str(result.inserted_id)

    await tg_call(context.bot.send_message, chat_id=chat_id, text="📣 Broadcast Preview")
    try:
        if payload["mode"] == "text":
            await tg_call(context.bot.send_message, chat_id=chat_id, text=payload["text"])
        else:
            await tg_call(
                context.bot.copy_message,
                chat_id=chat_id,
                from_chat_id=payload["source_chat_id"],
                message_id=payload["source_message_id"],
            )
    except Exception:
        await db.broadcasts.update_one(
            {"_id": result.inserted_id},
            {"$set": {"status": "preview_failed", "updated_at": utcnow()}},
        )
        await tg_call(
            context.bot.send_message,
            chat_id=chat_id,
            text="❌ This message could not be previewed for broadcast.",
        )
        return

    controls = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve", callback_data=f"broadcast:approve:{broadcast_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"broadcast:deny:{broadcast_id}"),
        ]]
    )
    await tg_call(
        context.bot.send_message,
        chat_id=chat_id,
        text="Send this broadcast to all users of this bot?",
        reply_markup=controls,
    )


async def broadcast_callback(update, context):
    query = update.callback_query
    if not query:
        return
    if not await require_owner(update, context):
        return

    await tg_call(query.answer)
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return
    action, raw_id = parts[1], parts[2]
    try:
        oid = ObjectId(raw_id)
    except Exception:
        await tg_call(query.edit_message_text, "❌ Invalid broadcast request.")
        return

    db = context.application.bot_data["db"]
    bot_id = context.application.bot_data["bot_id"]
    record = await db.broadcasts.find_one({"_id": oid, "bot_id": int(bot_id)})
    if not record:
        await tg_call(query.edit_message_text, "❌ Broadcast request not found.")
        return
    if record.get("status") != "pending":
        await tg_call(query.edit_message_text, f"ℹ️ Broadcast is already {record.get('status')}.")
        return

    if action == "deny":
        await db.broadcasts.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "denied", "decided_at": utcnow(), "decided_by": query.from_user.id}},
        )
        await tg_call(query.edit_message_text, "❌ Broadcast cancelled.")
        return

    if action != "approve":
        return

    claimed = await db.broadcasts.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "running", "started_at": utcnow(), "approved_by": query.from_user.id}},
    )
    if claimed.modified_count != 1:
        await tg_call(query.edit_message_text, "ℹ️ Broadcast was already handled.")
        return

    total = await db.bot_users.count_documents({"bot_id": int(bot_id)})
    await tg_call(
        query.edit_message_text,
        _progress_text(total, 0, 0),
    )
    status_chat_id = int(query.message.chat_id)
    status_message_id = int(query.message.message_id)

    # Raw asyncio task is intentional: it must survive the current callback returning.
    asyncio.create_task(
        _run_broadcast(
            context.application,
            oid,
            status_chat_id,
            status_message_id,
            total,
        )
    )


def _progress_text(total: int, sent: int, failed: int, done: bool = False) -> str:
    left = max(0, total - sent - failed)
    title = "✅ Broadcast Complete" if done else "📡 Broadcasting..."
    return (
        f"{title}\n\n"
        f"Total Users: {total}\n"
        f"Sent to: {sent}\n"
        f"Left: {left}\n"
        f"Failed: {failed}"
    )


async def _run_broadcast(app, broadcast_oid, status_chat_id: int, status_message_id: int, total: int):
    db = app.bot_data["db"]
    bot_id = app.bot_data["bot_id"]
    record = await db.broadcasts.find_one({"_id": broadcast_oid, "bot_id": int(bot_id)})
    if not record:
        return
    payload = record["payload"]

    sent = 0
    failed = 0
    processed = 0
    last_edit = 0.0

    cursor = db.bot_users.find({"bot_id": int(bot_id)}).sort("user_id", 1)
    async for user_doc in cursor:
        uid = int(user_doc["user_id"])
        try:
            if payload["mode"] == "text":
                await tg_call(app.bot.send_message, chat_id=uid, text=payload["text"], retries=10)
            else:
                await tg_call(
                    app.bot.copy_message,
                    chat_id=uid,
                    from_chat_id=int(payload["source_chat_id"]),
                    message_id=int(payload["source_message_id"]),
                    retries=10,
                )
            sent += 1
        except Forbidden:
            failed += 1
            await db.bot_users.update_one(
                {"_id": user_doc["_id"]},
                {"$set": {"active": False, "blocked_at": utcnow()}},
            )
        except BadRequest:
            failed += 1
        except Exception as exc:
            failed += 1
            await db.bot_users.update_one(
                {"_id": user_doc["_id"]},
                {"$set": {"last_broadcast_error": str(exc), "last_broadcast_error_at": utcnow()}},
            )

        processed += 1
        # Keep under Telegram broadcast pressure even before RetryAfter is returned.
        await asyncio.sleep(0.045)

        now = time.monotonic()
        if processed % 10 == 0 or now - last_edit >= 2.0:
            try:
                await tg_call(
                    app.bot.edit_message_text,
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=_progress_text(total, sent, failed),
                    retries=6,
                )
            except BadRequest:
                pass
            last_edit = now

    await db.broadcasts.update_one(
        {"_id": broadcast_oid},
        {
            "$set": {
                "status": "completed",
                "finished_at": utcnow(),
                "total": total,
                "sent": sent,
                "failed": failed,
            }
        },
    )
    try:
        await tg_call(
            app.bot.edit_message_text,
            chat_id=status_chat_id,
            message_id=status_message_id,
            text=_progress_text(total, sent, failed, done=True),
            retries=6,
        )
    except BadRequest:
        pass
