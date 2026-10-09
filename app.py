import streamlit as st
from pymongo import MongoClient

from bot.config import Settings
from bot.runtime import start_supervisor_thread

st.set_page_config(page_title="Telegram Bot Manager", page_icon="🤖", layout="wide")
settings = Settings.load()


@st.cache_resource(show_spinner=False)
def boot_supervisor():
    return start_supervisor_thread()


runner = boot_supervisor()

st.title("🤖 Telegram Bot Manager")
st.caption("Multi-bot status")

if runner.thread.is_alive():
    st.success("Bot manager is running.")
else:
    st.error("Bot manager stopped. Check the application logs.")

try:
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    db = client[settings.mongo_db]
    client.admin.command("ping")

    bots = list(db.bots.find({}, {"token_enc": 0}).sort("name", 1))
    pdf_count = db.pdfs.count_documents({})
    user_links = db.bot_users.count_documents({})
    unique_users = len(db.bot_users.distinct("user_id"))

    a, b, c, d = st.columns(4)
    a.metric("Registered bots", len(bots))
    b.metric("Indexed PDFs", f"{pdf_count:,}")
    c.metric("Bot-user links", f"{user_links:,}")
    d.metric("Unique users", f"{unique_users:,}")

    st.subheader("Bots")
    if not bots:
        st.info("No bot is registered yet. Add BOT_TOKEN to secrets for the first bot.")
    else:
        for bot in bots:
            runtime = db.bot_runtime.find_one({"_id": int(bot["_id"])}) or {}
            status = "🟢 Running" if runtime.get("running") else "⚪ Stopped"
            username = f"@{bot.get('username')}" if bot.get("username") else "No username"
            st.write(f"**{bot.get('name') or 'Bot'}** — {username} — `{bot['_id']}` — {status}")

except Exception as exc:
    st.warning(f"Status unavailable: {exc}")

if st.button("Refresh status"):
    st.rerun()
