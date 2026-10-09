from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from telethon import TelegramClient, errors

from bot.utils import (
    clean_catalog_text,
    clean_pdf_title,
    generate_obfuscated_post_token,
    is_pdf_name,
    utcnow,
)

load_dotenv()


def need(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name} in environment/.env")
    return value


async def ensure_indexes(db):
    await db.pdfs.create_index(
        [("source_chat_id", ASCENDING), ("post_id", ASCENDING)],
        unique=True,
        name="source_post_unique",
    )
    await db.pdfs.create_index(
        [("start_token", ASCENDING)],
        unique=True,
        sparse=True,
        name="start_token_unique",
    )


async def token_for(db, source_chat_id: int, post_id: int) -> str:
    existing = await db.pdfs.find_one(
        {"source_chat_id": source_chat_id, "post_id": post_id},
        {"start_token": 1},
    )
    if existing and existing.get("start_token"):
        return existing["start_token"]

    for _ in range(30):
        token = generate_obfuscated_post_token(post_id)
        try:
            claimed = await db.pdfs.find_one_and_update(
                {
                    "source_chat_id": source_chat_id,
                    "post_id": post_id,
                    "$or": [
                        {"start_token": {"$exists": False}},
                        {"start_token": None},
                    ],
                },
                {"$set": {"start_token": token}},
                return_document=ReturnDocument.AFTER,
            )
            if claimed and claimed.get("start_token"):
                return claimed["start_token"]
        except DuplicateKeyError:
            continue

        existing = await db.pdfs.find_one(
            {"source_chat_id": source_chat_id, "post_id": post_id},
            {"start_token": 1},
        )
        if existing and existing.get("start_token"):
            return existing["start_token"]

    raise RuntimeError("Could not create unique start token")


def telethon_pdf_info(message):
    file = getattr(message, "file", None)
    if not file:
        return None
    name = getattr(file, "name", None)
    mime = getattr(file, "mime_type", None)
    if not is_pdf_name(name, mime):
        return None
    return {
        "name": name or f"PDF {message.id}.pdf",
        "mime": mime or "application/pdf",
        "size": getattr(file, "size", None),
    }


async def index_pdf(db, source_chat_id: int, message):
    info = telethon_pdf_info(message)
    if not info:
        return False

    post_id = int(message.id)
    await db.pdfs.update_one(
        {"source_chat_id": source_chat_id, "post_id": post_id},
        {
            "$set": {
                "title": clean_pdf_title(info["name"]) or f"PDF {post_id}",
                "description": clean_catalog_text(message.message or ""),
                "file_name": clean_catalog_text(info["name"]),
                "mime_type": info["mime"],
                "file_size": info["size"],
                "message_date": message.date,
                "indexed_by": "super_indexer",
                "updated_at": utcnow(),
                "active": True,
            },
            "$setOnInsert": {"indexed_at": utcnow()},
        },
        upsert=True,
    )
    await token_for(db, source_chat_id, post_id)
    return True


async def checkpoint(db, source_chat_id: int, last_scanned: int, last_pdf: int | None, stats: dict):
    update = {
        "$set": {
            "last_scanned_message_id": int(last_scanned),
            "updated_at": utcnow(),
            "last_writer": "super_indexer",
            "super_indexer_stats": stats,
        }
    }
    if last_pdf is not None:
        update["$max"] = {"last_indexed_pdf_id": int(last_pdf)}
    await db.index_state.update_one(
        {"_id": str(source_chat_id)},
        update,
        upsert=True,
    )


async def show_status(db, source_chat_id: int):
    state = await db.index_state.find_one({"_id": str(source_chat_id)}) or {}
    pdf_count = await db.pdfs.count_documents({"source_chat_id": source_chat_id})
    print("\n=== SUPER INDEXER STATUS ===")
    print(f"Channel:          {source_chat_id}")
    print(f"Last scanned:     {state.get('last_scanned_message_id', 0)}")
    print(f"Last PDF:         {state.get('last_indexed_pdf_id', 0)}")
    print(f"Indexed PDFs:     {pdf_count}")
    print(f"Updated:          {state.get('updated_at', '-')}")
    print("============================\n")


async def run_index(args):
    mongo = AsyncMongoClient(need("MONGO_URI"))
    db = mongo[os.getenv("MONGO_DB", "telegram_pdf_library")]
    await mongo.admin.command("ping")
    await ensure_indexes(db)

    source_channel_id = int(args.channel or need("SOURCE_CHANNEL_ID"))
    state = await db.index_state.find_one({"_id": str(source_channel_id)}) or {}
    last_scanned = 0 if args.from_start else int(state.get("last_scanned_message_id", 0))
    last_pdf = int(state.get("last_indexed_pdf_id", 0) or 0)

    if args.command == "status":
        await show_status(db, source_channel_id)
        await mongo.close()
        return

    api_id = int(need("TG_API_ID"))
    api_hash = need("TG_API_HASH")
    session_name = os.getenv("TG_SESSION", "super_indexer")

    client = TelegramClient(session_name, api_id, api_hash)
    # We handle FloodWait ourselves so we can checkpoint and print status.
    client.flood_sleep_threshold = 0

    await client.start()
    # Warm the entity cache so a numeric -100... channel ID resolves reliably
    # even on the first local session.
    await client.get_dialogs(limit=None)
    entity = await client.get_entity(source_channel_id)

    scanned_session = 0
    pdf_session = 0
    skipped_session = 0
    checkpoint_every = max(1, int(args.checkpoint_every))

    print("╔════════════════════════════════════════════╗")
    print("║            SUPER INDEXER                  ║")
    print("╚════════════════════════════════════════════╝")
    print(f"Channel:     {getattr(entity, 'title', source_channel_id)}")
    print(f"Resume from: {last_scanned + 1}")
    print("Mode:        PDF only (text-only posts are skipped)")
    print()

    while True:
        try:
            async for message in client.iter_messages(
                entity,
                min_id=last_scanned,
                reverse=True,
                wait_time=float(args.wait_time),
            ):
                # iter_messages can return service/empty objects; always advance the scan.
                if not getattr(message, "id", None):
                    continue

                current_id = int(message.id)
                scanned_session += 1
                last_scanned = max(last_scanned, current_id)

                try:
                    indexed = await index_pdf(db, source_channel_id, message)
                except Exception as exc:
                    print(f"[ERROR] post {current_id}: {exc}")
                    indexed = False

                if indexed:
                    pdf_session += 1
                    last_pdf = max(last_pdf, current_id)
                    print(
                        f"[PDF] post={current_id}  "
                        f"session_pdfs={pdf_session}  scanned={scanned_session}"
                    )
                else:
                    skipped_session += 1

                if scanned_session % checkpoint_every == 0:
                    stats = {
                        "scanned_this_run": scanned_session,
                        "pdfs_this_run": pdf_session,
                        "skipped_this_run": skipped_session,
                    }
                    await checkpoint(
                        db, source_channel_id, last_scanned, last_pdf or None, stats
                    )
                    print(
                        f"[CHECKPOINT] last_scanned={last_scanned} "
                        f"pdfs={pdf_session} skipped={skipped_session}"
                    )

            break

        except errors.FloodWaitError as exc:
            stats = {
                "scanned_this_run": scanned_session,
                "pdfs_this_run": pdf_session,
                "skipped_this_run": skipped_session,
                "flood_wait_seconds": exc.seconds,
            }
            await checkpoint(db, source_channel_id, last_scanned, last_pdf or None, stats)
            wait_for = int(exc.seconds) + 1
            print(
                f"\n[RATE LIMIT] Telegram requested {exc.seconds}s. "
                f"Checkpoint saved at {last_scanned}. Waiting automatically...\n"
            )
            await asyncio.sleep(wait_for)
            # Loop recreates the iterator from the saved in-memory last_scanned value.

        except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
            stats = {
                "scanned_this_run": scanned_session,
                "pdfs_this_run": pdf_session,
                "skipped_this_run": skipped_session,
                "last_error": str(exc),
            }
            await checkpoint(db, source_channel_id, last_scanned, last_pdf or None, stats)
            print(f"[TEMP ERROR] {exc}. Retrying in 10 seconds...")
            await asyncio.sleep(10)

    stats = {
        "scanned_this_run": scanned_session,
        "pdfs_this_run": pdf_session,
        "skipped_this_run": skipped_session,
        "finished": True,
    }
    await checkpoint(db, source_channel_id, last_scanned, last_pdf or None, stats)

    print("\n✅ Indexing complete.")
    print(f"Last scanned message: {last_scanned}")
    print(f"PDFs indexed this run: {pdf_session}")
    print(f"Non-PDF/text-only skipped: {skipped_session}")

    await client.disconnect()
    await mongo.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fast/resumable Telegram PDF Super Indexer"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="index",
        choices=["index", "sync", "status"],
    )
    parser.add_argument("--channel", help="Override SOURCE_CHANNEL_ID")
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="Ignore checkpoint and scan from the beginning (Mongo upserts prevent duplicates)",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        help="Save last scanned message every N scanned messages",
    )
    parser.add_argument(
        "--wait-time",
        type=float,
        default=1.0,
        help="Telethon history request pacing in seconds",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run_index(parse_args()))
