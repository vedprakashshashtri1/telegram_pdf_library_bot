from __future__ import annotations

from .access import check_force_access
from .utils import utcnow


async def register_referral(db, bot_id: int, new_user_id: int, referrer_id: int):
    if int(new_user_id) == int(referrer_id):
        return False, "Self-referrals are not allowed."
    query = {"bot_id": int(bot_id), "referred_user_id": int(new_user_id)}
    if await db.referrals.find_one(query):
        return False, "Referral already recorded."
    await db.referrals.insert_one(
        {
            **query,
            "referrer_id": int(referrer_id),
            "status": "incomplete",
            "claimed": False,
            "created_at": utcnow(),
        }
    )
    return True, "Referral recorded."


async def refresh_referral_completion(bot, db, bot_id: int, user_id: int):
    query = {"bot_id": int(bot_id), "referred_user_id": int(user_id)}
    referral = await db.referrals.find_one(query)
    if not referral or referral.get("status") == "completed":
        return False
    if await check_force_access(bot, db, bot_id, user_id):
        return False
    result = await db.referrals.update_one(
        {**query, "status": "incomplete"},
        {"$set": {"status": "completed", "completed_at": utcnow()}},
    )
    return result.modified_count == 1


async def referral_stats(db, bot_id: int, user_id: int, rate: int):
    base = {"bot_id": int(bot_id), "referrer_id": int(user_id)}
    completed = await db.referrals.count_documents({**base, "status": "completed"})
    incomplete = await db.referrals.count_documents({**base, "status": "incomplete"})
    unclaimed = await db.referrals.count_documents({**base, "status": "completed", "claimed": False})
    rate = max(1, int(rate))
    return {
        "completed": completed,
        "incomplete": incomplete,
        "unclaimed": unclaimed,
        "days": unclaimed // rate,
        "remainder": unclaimed % rate,
    }


async def claim_referrals(db, bot_id: int, user_id: int, rate: int, name: str):
    rate = max(1, int(rate))
    docs = await db.referrals.find(
        {
            "bot_id": int(bot_id),
            "referrer_id": int(user_id),
            "status": "completed",
            "claimed": False,
        }
    ).sort("completed_at", 1).to_list(None)
    days = len(docs) // rate
    if days <= 0:
        return 0, 0, None
    consume = days * rate
    ids = [d["_id"] for d in docs[:consume]]
    result = await db.referrals.update_many(
        {"_id": {"$in": ids}, "claimed": False},
        {"$set": {"claimed": True, "claimed_at": utcnow()}},
    )
    if result.modified_count != consume:
        return 0, 0, None
    until = await db.add_premium_days(bot_id, user_id, days, name=name, added_by=user_id)
    return days, consume, until
