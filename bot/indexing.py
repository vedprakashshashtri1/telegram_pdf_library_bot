from __future__ import annotations

from .utils import clean_catalog_text, clean_pdf_title, is_pdf_name, utcnow


async def index_botapi_message(db, message, source_channel_id: int, indexed_by: str = "live"):
    if not message or int(message.chat_id) != int(source_channel_id):
        return False
    doc = getattr(message, "document", None)
    if not doc or not is_pdf_name(doc.file_name, doc.mime_type):
        return False

    post_id = int(message.message_id)
    title = clean_pdf_title(doc.file_name) or f"PDF {post_id}"
    description = clean_catalog_text(message.caption or "")
    await db.pdfs.update_one(
        {"source_chat_id": int(source_channel_id), "post_id": post_id},
        {
            "$set": {
                "source_chat_id": int(source_channel_id),
                "post_id": post_id,
                "title": title,
                "description": description,
                "file_name": clean_catalog_text(doc.file_name or ""),
                "mime_type": doc.mime_type or "application/pdf",
                "file_size": doc.file_size,
                "telegram_file_id": doc.file_id,
                "telegram_file_unique_id": doc.file_unique_id,
                "message_date": message.date,
                "indexed_by": indexed_by,
                "updated_at": utcnow(),
                "active": True,
            },
            "$setOnInsert": {"indexed_at": utcnow()},
        },
        upsert=True,
    )
    await db.ensure_pdf_token(source_channel_id, post_id)
    await db.index_state.update_one(
        {"_id": str(source_channel_id)},
        {
            "$max": {"last_scanned_message_id": post_id, "last_indexed_pdf_id": post_id},
            "$set": {"updated_at": utcnow(), "last_writer": indexed_by},
        },
        upsert=True,
    )
    return True


async def index_update(update, context):
    message = update.channel_post or update.edited_channel_post
    if not message:
        return
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    bot_id = context.application.bot_data["bot_id"]
    try:
        await index_botapi_message(db, message, settings.source_channel_id, indexed_by=f"bot:{bot_id}")
    except Exception as exc:
        print(f"[index:{bot_id}] post={getattr(message, 'message_id', '?')} error={exc}")
