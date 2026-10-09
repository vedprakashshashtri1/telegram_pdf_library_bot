from __future__ import annotations

from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, TEXT, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .utils import generate_obfuscated_post_token, utcnow


class Database:
    def __init__(self, uri: str, db_name: str):
        self.client = AsyncMongoClient(uri)
        self.db = self.client[db_name]

        self.pdfs = self.db.pdfs
        self.bots = self.db.bots
        self.owners = self.db.owners
        self.bot_users = self.db.bot_users
        self.roles = self.db.roles
        self.premium = self.db.premium
        self.usage_daily = self.db.usage_daily
        self.referrals = self.db.referrals
        self.force_channels = self.db.force_channels
        self.join_requests = self.db.join_requests
        self.index_state = self.db.index_state
        self.delete_jobs = self.db.delete_jobs
        self.settings = self.db.settings
        self.bot_runtime = self.db.bot_runtime
        self.broadcasts = self.db.broadcasts

    async def init(self):
        await self.client.admin.command("ping")

        await self.pdfs.create_index(
            [("source_chat_id", ASCENDING), ("post_id", ASCENDING)],
            unique=True,
            name="source_post_unique",
        )
        await self.pdfs.create_index(
            [("start_token", ASCENDING)], unique=True, sparse=True, name="start_token_unique"
        )
        try:
            await self.pdfs.create_index(
                [("title", TEXT), ("description", TEXT)],
                weights={"title": 10, "description": 3},
                default_language="none",
                name="catalog_text",
            )
        except Exception:
            pass

        await self.bots.create_index([("username", ASCENDING)], sparse=True)
        await self.bot_users.create_index(
            [("bot_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="bot_user_unique"
        )
        await self.roles.create_index(
            [("bot_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="bot_role_unique"
        )
        await self.premium.create_index(
            [("bot_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="bot_premium_unique"
        )
        await self.premium.create_index([("premium_until", ASCENDING)])
        await self.usage_daily.create_index(
            [("bot_id", ASCENDING), ("user_id", ASCENDING), ("date", ASCENDING)], unique=True
        )
        await self.referrals.create_index(
            [("bot_id", ASCENDING), ("referred_user_id", ASCENDING)], unique=True
        )
        await self.referrals.create_index(
            [("bot_id", ASCENDING), ("referrer_id", ASCENDING), ("status", ASCENDING)]
        )
        await self.force_channels.create_index(
            [("bot_id", ASCENDING), ("channel_id", ASCENDING)], unique=True
        )
        await self.join_requests.create_index(
            [("bot_id", ASCENDING), ("channel_id", ASCENDING), ("user_id", ASCENDING)], unique=True
        )
        await self.delete_jobs.create_index([("bot_id", ASCENDING), ("delete_at", ASCENDING)])
        await self.broadcasts.create_index([("bot_id", ASCENDING), ("status", ASCENDING)])

    async def seed_owners(self, owner_ids: set[int] | frozenset[int]):
        # OWNER_IDS is bootstrap-only. Once at least one owner exists, runtime
        # /add_owner and /rem_owner changes are authoritative and survive restarts.
        if await self.owners.count_documents({}) > 0:
            return
        for uid in owner_ids:
            await self.owners.update_one(
                {"_id": int(uid)},
                {"$setOnInsert": {"created_at": utcnow(), "seeded": True}},
                upsert=True,
            )

    async def is_owner(self, user_id: int) -> bool:
        return await self.owners.find_one({"_id": int(user_id)}, {"_id": 1}) is not None

    async def owner_count(self) -> int:
        return await self.owners.count_documents({})

    async def upsert_bot_user(self, bot_id: int, user):
        if not user:
            return
        key = f"{int(bot_id)}:{int(user.id)}"
        await self.bot_users.update_one(
            {"_id": key},
            {
                "$set": {
                    "bot_id": int(bot_id),
                    "user_id": int(user.id),
                    "name": " ".join(x for x in [user.first_name, user.last_name] if x).strip(),
                    "username": user.username,
                    "last_seen_at": utcnow(),
                    "active": True,
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )

    async def ensure_bot_settings(self, bot_id: int, default_free: int, default_refer: int):
        await self.settings.update_one(
            {"_id": int(bot_id)},
            {
                "$setOnInsert": {
                    "bot_id": int(bot_id),
                    "free_daily_limit": int(default_free),
                    "referrals_per_premium_day": int(default_refer),
                    "created_at": utcnow(),
                }
            },
            upsert=True,
        )

    async def get_settings(self, bot_id: int) -> dict[str, Any]:
        return await self.settings.find_one({"_id": int(bot_id)}) or {}

    async def ensure_pdf_token(self, source_chat_id: int, post_id: int) -> str:
        existing = await self.pdfs.find_one(
            {"source_chat_id": int(source_chat_id), "post_id": int(post_id)}, {"start_token": 1}
        )
        if existing and existing.get("start_token"):
            return existing["start_token"]

        for _ in range(30):
            token = generate_obfuscated_post_token(post_id)
            try:
                claimed = await self.pdfs.find_one_and_update(
                    {
                        "source_chat_id": int(source_chat_id),
                        "post_id": int(post_id),
                        "$or": [{"start_token": {"$exists": False}}, {"start_token": None}],
                    },
                    {"$set": {"start_token": token}},
                    return_document=ReturnDocument.AFTER,
                )
                if claimed and claimed.get("start_token"):
                    return claimed["start_token"]
            except DuplicateKeyError:
                continue
            existing = await self.pdfs.find_one(
                {"source_chat_id": int(source_chat_id), "post_id": int(post_id)}, {"start_token": 1}
            )
            if existing and existing.get("start_token"):
                return existing["start_token"]
        raise RuntimeError("Could not assign a unique PDF token")

    async def add_premium_days(
        self, bot_id: int, user_id: int, days: int, name: str = "", added_by: int | None = None
    ):
        now = utcnow()
        query = {"bot_id": int(bot_id), "user_id": int(user_id)}
        doc = await self.premium.find_one(query)
        current_until = doc.get("premium_until") if doc else None
        base = current_until if current_until and current_until > now else now
        new_until = base + timedelta(days=int(days))
        set_doc = {
            "bot_id": int(bot_id),
            "user_id": int(user_id),
            "premium_until": new_until,
            "name": name or (doc or {}).get("name", ""),
            "updated_at": now,
            "added_by": added_by,
        }
        if not doc or not doc.get("premium_since") or not current_until or current_until <= now:
            set_doc["premium_since"] = now
        await self.premium.update_one(query, {"$set": set_doc}, upsert=True)
        return new_until

    async def active_premium(self, bot_id: int, user_id: int):
        return await self.premium.find_one(
            {
                "bot_id": int(bot_id),
                "user_id": int(user_id),
                "premium_until": {"$gt": utcnow()},
            }
        )
