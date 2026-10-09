from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from telegram import Bot, MenuButtonWebApp, Update, WebAppInfo
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .broadcasts import broadcast_callback, broadcast_command
from .commands import (
    add_admin,
    add_bot,
    add_channel,
    add_owner,
    add_premium,
    add_request,
    add_super,
    chat_join_request,
    chat_member_changed,
    check_premium,
    error_handler,
    general_callback,
    help_cmd,
    list_admin,
    list_bot,
    list_channel,
    list_owner,
    list_premium,
    list_request,
    list_super,
    ping,
    refer,
    rem_admin,
    rem_bot,
    rem_channel,
    rem_owner,
    rem_premium,
    rem_request,
    rem_super,
    restart,
    set_free,
    set_refer,
    start,
    track_update_user,
    uptime,
    website_prompt,
)
from .config import Settings
from .crypto import TokenVault
from .db import Database
from .indexing import index_update
from .retry import tg_call
from .utils import utcnow


@dataclass
class RuntimeInstance:
    app: Application
    token: str
    bot_id: int
    tasks: list[asyncio.Task] = field(default_factory=list)


@dataclass
class SupervisorRunner:
    thread: threading.Thread
    supervisor: "BotSupervisor | None" = None


class BotSupervisor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.vault = TokenVault(settings.app_secret)
        self.db = Database(settings.mongo_uri, settings.mongo_db)
        self.instances: dict[int, RuntimeInstance] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def boot(self):
        self.loop = asyncio.get_running_loop()
        await self.db.init()
        await self.db.seed_owners(self.settings.seed_owner_ids)

        if self.settings.bot_token:
            try:
                await self._register_token(self.settings.bot_token, added_by="environment", start_now=False)
            except Exception as exc:
                print(f"[boot] primary BOT_TOKEN rejected: {exc}")

        docs = await self.db.bots.find({"enabled": True}).sort("_id", 1).to_list(None)
        for doc in docs:
            try:
                token = self.vault.decrypt(doc["token_enc"])
                await self.start_bot(token)
            except Exception as exc:
                print(f"[boot] failed to start bot {doc.get('_id')}: {exc}")
                await self.db.bots.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"last_error": str(exc), "last_error_at": utcnow()}},
                )

    async def _register_token(self, token: str, added_by, start_now: bool):
        async with Bot(token=token) as probe:
            me = await tg_call(probe.get_me, retries=6)
        info = {
            "bot_id": int(me.id),
            "name": me.full_name,
            "username": me.username or "",
        }
        await self.db.bots.update_one(
            {"_id": int(me.id)},
            {
                "$set": {
                    "name": me.full_name,
                    "username": me.username,
                    "token_enc": self.vault.encrypt(token),
                    "enabled": True,
                    "updated_at": utcnow(),
                    "added_by": added_by,
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )
        await self.db.ensure_bot_settings(
            int(me.id), self.settings.default_free_limit, self.settings.default_refer_rate
        )
        if start_now:
            existing = self.instances.get(int(me.id))
            if existing and existing.token != token:
                await self.stop_bot(int(me.id))
                await self.start_bot(token)
            elif not existing:
                await self.start_bot(token)
        return info

    async def add_bot_token(self, token: str, added_by: int):
        return await self._register_token(token.strip(), added_by=added_by, start_now=True)

    def is_running(self, bot_id: int) -> bool:
        return int(bot_id) in self.instances

    async def start_bot(self, token: str):
        app = self._build_application(token)
        await app.initialize()
        me = await tg_call(app.bot.get_me, retries=6)
        bot_id = int(me.id)
        if bot_id in self.instances:
            await app.shutdown()
            return self.instances[bot_id]

        app.bot_data.update(
            {
                "db": self.db,
                "settings": self.settings,
                "supervisor": self,
                "bot_id": bot_id,
                "bot_name": me.full_name,
                "bot_username": me.username,
                "started_at": utcnow(),
            }
        )

        await self.db.bots.update_one(
            {"_id": bot_id},
            {"$set": {"name": me.full_name, "username": me.username, "enabled": True, "last_started_at": utcnow(), "last_error": None}},
            upsert=True,
        )
        await self.db.ensure_bot_settings(bot_id, self.settings.default_free_limit, self.settings.default_refer_rate)

        try:
            await tg_call(
                app.bot.set_chat_menu_button,
                menu_button=MenuButtonWebApp(
                    text="Open Website",
                    web_app=WebAppInfo(url=self.settings.website_url),
                ),
                retries=6,
            )
        except Exception as exc:
            print(f"[menu:{bot_id}] {exc}")

        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            bootstrap_retries=10,
        )
        await app.start()

        runtime = RuntimeInstance(app=app, token=token, bot_id=bot_id)
        runtime.tasks = [
            asyncio.create_task(self._delete_worker(runtime), name=f"delete-{bot_id}"),
            asyncio.create_task(self._heartbeat_worker(runtime), name=f"heartbeat-{bot_id}"),
        ]
        self.instances[bot_id] = runtime
        print(f"[bot] started @{me.username} ({bot_id})")
        return runtime

    async def stop_bot(self, bot_id: int):
        runtime = self.instances.pop(int(bot_id), None)
        if not runtime:
            return
        for task in runtime.tasks:
            task.cancel()
        await asyncio.gather(*runtime.tasks, return_exceptions=True)
        try:
            if runtime.app.updater and runtime.app.updater.running:
                await runtime.app.updater.stop()
        except Exception as exc:
            print(f"[stop:{bot_id}] updater: {exc}")
        try:
            if runtime.app.running:
                await runtime.app.stop()
        except Exception as exc:
            print(f"[stop:{bot_id}] app: {exc}")
        try:
            await runtime.app.shutdown()
        except Exception as exc:
            print(f"[stop:{bot_id}] shutdown: {exc}")
        await self.db.bot_runtime.update_one(
            {"_id": int(bot_id)},
            {"$set": {"running": False, "stopped_at": utcnow()}},
            upsert=True,
        )
        print(f"[bot] stopped {bot_id}")

    async def restart_bot(self, bot_id: int):
        doc = await self.db.bots.find_one({"_id": int(bot_id), "enabled": True})
        if not doc:
            raise RuntimeError("Bot is not active")
        token = self.vault.decrypt(doc["token_enc"])
        await self.stop_bot(bot_id)
        await asyncio.sleep(0.8)
        await self.start_bot(token)

    async def remove_bot(self, bot_id: int):
        await self.stop_bot(bot_id)
        await self.db.bots.delete_one({"_id": int(bot_id)})
        await self.db.bot_runtime.delete_one({"_id": int(bot_id)})

    def schedule_restart(self, bot_id: int):
        async def later():
            await asyncio.sleep(1.0)
            try:
                await self.restart_bot(int(bot_id))
            except Exception as exc:
                print(f"[restart:{bot_id}] {exc}")
        asyncio.create_task(later())

    def schedule_remove(self, bot_id: int):
        async def later():
            await asyncio.sleep(1.0)
            try:
                await self.remove_bot(int(bot_id))
            except Exception as exc:
                print(f"[remove:{bot_id}] {exc}")
        asyncio.create_task(later())

    async def _delete_worker(self, runtime: RuntimeInstance):
        while True:
            try:
                jobs = await self.db.delete_jobs.find(
                    {"bot_id": runtime.bot_id, "delete_at": {"$lte": utcnow()}}
                ).sort("delete_at", 1).limit(50).to_list(None)
                for job in jobs:
                    try:
                        await tg_call(
                            runtime.app.bot.delete_message,
                            chat_id=int(job["chat_id"]),
                            message_id=int(job["message_id"]),
                            retries=8,
                        )
                        await self.db.delete_jobs.delete_one({"_id": job["_id"]})
                    except (BadRequest, Forbidden):
                        await self.db.delete_jobs.delete_one({"_id": job["_id"]})
                    except Exception as exc:
                        attempts = int(job.get("attempts", 0)) + 1
                        if attempts >= 8:
                            await self.db.delete_jobs.delete_one({"_id": job["_id"]})
                        else:
                            await self.db.delete_jobs.update_one(
                                {"_id": job["_id"]},
                                {"$set": {"attempts": attempts, "last_error": str(exc), "updated_at": utcnow()}},
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[delete:{runtime.bot_id}] {exc}")
            await asyncio.sleep(10)

    async def _heartbeat_worker(self, runtime: RuntimeInstance):
        while True:
            try:
                me = await tg_call(runtime.app.bot.get_me, retries=6)
                await self.db.bot_runtime.update_one(
                    {"_id": runtime.bot_id},
                    {
                        "$set": {
                            "bot_id": runtime.bot_id,
                            "name": me.full_name,
                            "username": me.username,
                            "heartbeat_at": utcnow(),
                            "started_at": runtime.app.bot_data["started_at"],
                            "running": True,
                        }
                    },
                    upsert=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[heartbeat:{runtime.bot_id}] {exc}")
            await asyncio.sleep(60)

    def _build_application(self, token: str) -> Application:
        app = Application.builder().token(token).build()

        # Track every user who interacts with this specific bot.
        app.add_handler(TypeHandler(Update, track_update_user), group=-20)

        # Everyone
        app.add_handler(CommandHandler("start", start), group=0)
        app.add_handler(CommandHandler("help", help_cmd), group=0)
        app.add_handler(CommandHandler("refer", refer), group=0)
        app.add_handler(CommandHandler("ping", ping), group=0)
        app.add_handler(CommandHandler("uptime", uptime), group=0)

        # Owner-only global management
        app.add_handler(CommandHandler("add_owner", add_owner), group=0)
        app.add_handler(CommandHandler("rem_owner", rem_owner), group=0)
        app.add_handler(CommandHandler("list_owner", list_owner), group=0)
        app.add_handler(CommandHandler("add_bot", add_bot), group=0)
        app.add_handler(CommandHandler("list_bot", list_bot), group=0)
        app.add_handler(CommandHandler("rem_bot", rem_bot), group=0)
        app.add_handler(CommandHandler("restart", restart), group=0)
        app.add_handler(CommandHandler("broadcast", broadcast_command), group=0)

        # Owner controls per-bot Admin/Super roles
        app.add_handler(CommandHandler("add_admin", add_admin), group=0)
        app.add_handler(CommandHandler("rem_admin", rem_admin), group=0)
        app.add_handler(CommandHandler("list_admin", list_admin), group=0)
        app.add_handler(CommandHandler("add_super", add_super), group=0)
        app.add_handler(CommandHandler("rem_super", rem_super), group=0)
        app.add_handler(CommandHandler("list_super", list_super), group=0)

        # Admin (Super/Owner inherit)
        app.add_handler(CommandHandler("add_premium", add_premium), group=0)
        app.add_handler(CommandHandler("rem_premium", rem_premium), group=0)
        app.add_handler(CommandHandler("check_premium", check_premium), group=0)
        app.add_handler(CommandHandler("list_premium", list_premium), group=0)

        # Super (Owner inherits)
        app.add_handler(CommandHandler("add_channel", add_channel), group=0)
        app.add_handler(CommandHandler("rem_channel", rem_channel), group=0)
        app.add_handler(CommandHandler("list_channel", list_channel), group=0)
        app.add_handler(CommandHandler("add_request", add_request), group=0)
        app.add_handler(CommandHandler("rem_request", rem_request), group=0)
        app.add_handler(CommandHandler("list_request", list_request), group=0)
        app.add_handler(CommandHandler("set_free", set_free), group=0)
        app.add_handler(CommandHandler("set_refer", set_refer), group=0)

        app.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^broadcast:"), group=1)
        app.add_handler(CallbackQueryHandler(general_callback, pattern=r"^(access:|refer:|chdetail:|chremove:|chcancel$)"), group=1)
        app.add_handler(ChatJoinRequestHandler(chat_join_request), group=1)
        app.add_handler(
            ChatMemberHandler(chat_member_changed, chat_member_types=ChatMemberHandler.CHAT_MEMBER),
            group=1,
        )

        # Unknown commands and ordinary private messages/media point users back to the website.
        # The unknown-command handler is deliberately in group 0 after all known commands,
        # so it only catches commands that none of the handlers above matched.
        app.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & filters.COMMAND, website_prompt),
            group=0,
        )
        app.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, website_prompt),
            group=10,
        )

        # Live PDF catalog updates.
        app.add_handler(TypeHandler(Update, index_update), group=20)
        app.add_error_handler(error_handler)
        return app


_runner_lock = threading.Lock()
_runner: SupervisorRunner | None = None


def _thread_target(runner: SupervisorRunner):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    settings = Settings.load()
    supervisor = BotSupervisor(settings)
    runner.supervisor = supervisor

    async def main():
        await supervisor.boot()
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


def start_supervisor_thread() -> SupervisorRunner:
    global _runner
    with _runner_lock:
        if _runner and _runner.thread.is_alive():
            return _runner
        placeholder = SupervisorRunner(thread=None)  # type: ignore[arg-type]
        thread = threading.Thread(
            target=_thread_target,
            args=(placeholder,),
            name="telegram-multibot-supervisor",
            daemon=True,
        )
        placeholder.thread = thread
        thread.start()
        _runner = placeholder
        return placeholder
