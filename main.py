
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from pyro_patch import apply_pyrogram_peer_patch

apply_pyrogram_peer_patch()

from pyrogram import Client, filters, idle
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from aichat import register_ai_handler
from sendphoto import (
    clear_paid_post,
    format_paid_posts,
    register_sendphoto_handler,
    save_post_from_link,
)
from stats import get_stats_text
from storage import load_data, update_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "sexyiwowu")

# Maximum number of userbot sessions this instance will host.
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "10"))

# ------------------------- ADMIN AUTH ------------------------- #
# Only these user IDs can operate the bot. Set in .env as comma-separated IDs:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

START_PIC_URL = "https://images.unsplash.com/photo-1503376780353-7e6692767b70"

# Create and install the event loop BEFORE constructing the Pyrogram Client.
# Pyrogram binds its dispatcher queues to the loop that is current at Client
# construction time. Installing our loop here guarantees the bot, all
# userbot clients (created later inside main()), and the __main__ runner all
# share ONE loop — no "attached to a different loop" shutdown crash, and no
# deprecated get_event_loop().
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

connected_clients = {}
user_states = {}
bot = Client("DashboardBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ------------------------- KEYBOARDS ------------------------- #

def get_start_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Dashboard", callback_data="back_main")],
        [InlineKeyboardButton("📞 Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ])


def get_dashboard_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Account", callback_data="add_acc"),
            InlineKeyboardButton("🗄 Accounts", callback_data="manage_acc"),
        ],
        [
            InlineKeyboardButton("⚡ Toggle AI", callback_data="toggle_ai"),
            InlineKeyboardButton("📊 Stats", callback_data="stats"),
        ],
        [InlineKeyboardButton("💎 Paid Photo", callback_data="set_photo_menu")],
        [InlineKeyboardButton("⬅️ Home", callback_data="back_start")],
    ])


def get_start_text():
    return (
        "✨ **Welcome to Control Panel** ✨\n\n"
        "Manage your active string session userbots, set custom auto-reply "
        "triggers, and control AI modules."
    )


def get_dashboard_text():
    return (
        "📂 **Userbot Dashboard**\n\n"
        "• **➕ Add Account** — Connect a Pyrogram String Session\n"
        "• **🗄 Accounts** — View connected accounts\n"
        "• **⚡ Toggle AI** — Turn AI Auto-Reply ON / OFF\n"
        "• **💎 Paid Photo** — Save channel posts; DM triggers `send` / `star` "
        "rotate through them (no forward header)\n"
        "• **📊 Stats** — VPS & CPU performance"
    )


# --------------------------- BOT HANDLERS --------------------------- #

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    if not is_admin(message.from_user.id):
        await message.reply_text("⛔ This bot is private. You are not authorized.")
        return

    try:
        await message.reply_photo(
            photo=START_PIC_URL, caption=get_start_text(), reply_markup=get_start_markup()
        )
    except Exception:
        await message.reply_text(get_start_text(), reply_markup=get_start_markup())


@bot.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    # AUTH GATE — only admins can operate anything
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    user_id = str(query.from_user.id)
    data = await load_data()

    if query.data == "back_start":
        if query.message.photo:
            await query.message.edit_caption(caption=get_start_text(), reply_markup=get_start_markup())
        else:
            await query.message.edit_text(get_start_text(), reply_markup=get_start_markup())

    elif query.data == "back_main":
        user_states.pop(user_id, None)
        if query.message.photo:
            await query.message.delete()
            await query.message.reply_text(get_dashboard_text(), reply_markup=get_dashboard_markup())
        else:
            await query.message.edit_text(get_dashboard_text(), reply_markup=get_dashboard_markup())

    elif query.data == "add_acc":
        if len(connected_clients) >= MAX_SESSIONS:
            await query.answer(
                f"Session limit reached ({MAX_SESSIONS}).", show_alert=True
            )
            return
        user_states[user_id] = "AWAITING_SESSION"
        await query.message.edit_text(
            "🔐 **Add Account**\n\n"
            "Paste your **Pyrogram String Session** as your next message.\n\n"
            "⚠️ Only paste a session you own. The bot gets full access to "
            "that Telegram account.\n\n"
            "Tap Cancel to go back.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Cancel", callback_data="back_main")]]
            ),
        )

    elif query.data == "set_photo_menu":
        user_states.pop(user_id, None)
        saved_users = data.get("users", {})
        if not saved_users:
            await query.answer("No accounts connected! Add an account first.", show_alert=True)
            return

        text = (
            "💎 **Paid Photo Module**\n\n"
            "**Step 1** — Tap an account below.\n"
            "**Step 2** — Paste a channel post link (userbot must be in the channel).\n\n"
            "Add multiple links to build a rotation: each `send` / `star` "
            "trigger in DMs sends the **next** post in the list, then wraps "
            "around. Posts are sent as clean copies — no forward header.\n\n"
            "🟢 Online · ⚪ Offline · 📎 Posts saved · ➕ No post"
        )
        btns = []
        for uid, uinfo in saved_users.items():
            indicator = "🟢" if uid in connected_clients else "⚪"
            n_posts = len(uinfo.get("paid_posts", [])) or (
                1 if uinfo.get("paid_photo") else 0  # legacy single post
            )
            badge = f"📎 {n_posts}" if n_posts else "➕"
            acc_name = uinfo.get("name", f"User {uid}")
            btns.append([
                InlineKeyboardButton(
                    f"{indicator} {badge} {acc_name}", callback_data=f"photo_acc_{uid}"
                )
            ])
        btns.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("photo_acc_"):
        user_states.pop(user_id, None)
        target_uid = query.data.replace("photo_acc_", "")
        uinfo = data.get("users", {}).get(target_uid, {})
        if not uinfo:
            await query.answer("Account data missing!", show_alert=True)
            return

        acc_name = uinfo.get("name", "Unknown User")
        conn_status = "Connected 🟢" if target_uid in connected_clients else "Offline 🔴"
        posts = uinfo.get("paid_posts", [])
        if not posts and uinfo.get("paid_photo"):  # legacy single post
            posts = [uinfo["paid_photo"]]

        text = (
            f"💎 **Paid Photo — {acc_name}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **User ID:** `{target_uid}`\n"
            f"• **Session:** {conn_status}\n"
            f"• **Triggers:** `send`  `.send`  `star`  `.star`\n"
            f"• **Posts in rotation:** {len(posts)}\n\n"
            f"📎 **Saved Posts**\n{format_paid_posts(posts)}"
        )
        btns = [
            [InlineKeyboardButton("📎 Add Post", callback_data=f"photo_set_{target_uid}")],
            [InlineKeyboardButton("🧹 Clear All Posts", callback_data=f"photo_clear_{target_uid}")],
            [InlineKeyboardButton("⬅️ Accounts", callback_data="set_photo_menu")],
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("photo_set_"):
        target_uid = query.data.replace("photo_set_", "")
        if target_uid not in data.get("users", {}):
            await query.answer("Account data missing!", show_alert=True)
            return
        if target_uid not in connected_clients:
            await query.answer("Account is offline. Reconnect the session first.", show_alert=True)
            return

        user_states[user_id] = f"AWAITING_POST:{target_uid}"
        acc_name = data["users"][target_uid].get("name", target_uid)
        await query.message.edit_text(
            f"🔗 **Add Paid Post**\n\n"
            f"**Step 1:** Open the channel where the userbot is a member.\n"
            f"**Step 2:** Copy the link of the post.\n"
            f"**Step 3:** Paste the link here as your next message.\n\n"
            f"**Account:** {acc_name}\n\n"
            f"Accepted formats:\n"
            f"• `https://t.me/mychannel/1234`\n"
            f"• `https://t.me/c/1234567890/55` (private)\n\n"
            f"You can add multiple posts — they rotate on each trigger.\n\n"
            f"Tap Cancel to go back.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Cancel", callback_data=f"photo_acc_{target_uid}")]]
            ),
        )

    elif query.data.startswith("photo_clear_"):
        target_uid = query.data.replace("photo_clear_", "")
        cleared = await clear_paid_post(target_uid)
        await query.answer(
            "All saved posts cleared!" if cleared else "No saved post on this account.",
            show_alert=True,
        )
        query.data = f"photo_acc_{target_uid}"
        await callback_handler(client, query)

    elif query.data == "manage_acc":
        saved_users = data.get("users", {})
        text = (
            "🗄 **Connected Accounts**\n\n"
            "🟢 Online & AI on\n"
            "🔴 Online but AI off\n"
            "⚪ Disconnected\n\n"
            "Tap an account to manage it."
        )
        btns = []
        for uid, uinfo in saved_users.items():
            is_active = uid in connected_clients
            ai_on = uinfo.get("ai_enabled", True)
            indicator = "🟢" if (is_active and ai_on) else ("🔴" if is_active else "⚪")
            acc_name = uinfo.get("name", f"User {uid}")
            btns.append([InlineKeyboardButton(f"{indicator} {acc_name}", callback_data=f"view_acc_{uid}")])
        btns.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("view_acc_"):
        target_uid = query.data.replace("view_acc_", "")
        uinfo = data.get("users", {}).get(target_uid, {})
        if not uinfo:
            await query.answer("Account data missing!", show_alert=True)
            return

        ai_status = "ON ✅" if uinfo.get("ai_enabled", True) else "OFF ❌"
        conn_status = "Connected 🟢" if target_uid in connected_clients else "Offline 🔴"
        acc_name = uinfo.get("name", "Unknown User")

        text = (
            f"👤 **Account Management**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **Name:** {acc_name}\n"
            f"• **User ID:** `{target_uid}`\n"
            f"• **Session:** {conn_status}\n"
            f"• **AI Auto-Reply:** {ai_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        btns = [
            [InlineKeyboardButton("⚡ Toggle AI", callback_data=f"toggle_acc_ai_{target_uid}")],
            [InlineKeyboardButton("❌ Disconnect", callback_data=f"term_ask_{target_uid}")],
            [InlineKeyboardButton("⬅️ Accounts", callback_data="manage_acc")],
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("term_ask_"):
        # Confirmation step — Disconnect permanently deletes the session.
        target_uid = query.data.replace("term_ask_", "")
        acc_name = data.get("users", {}).get(target_uid, {}).get("name", target_uid)
        await query.message.edit_text(
            f"⚠️ **Remove {acc_name}?**\n\n"
            "The session string will be **permanently deleted** and cannot "
            "be recovered.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, remove it", callback_data=f"term_acc_{target_uid}")],
                [InlineKeyboardButton("❌ No, keep it", callback_data=f"view_acc_{target_uid}")],
            ]),
        )

    elif query.data.startswith("toggle_acc_ai_"):
        target_uid = query.data.replace("toggle_acc_ai_", "")
        if target_uid not in data.get("users", {}):
            await query.answer("Account data missing!", show_alert=True)
            return

        def _toggle(d):
            curr = d["users"][target_uid].get("ai_enabled", True)
            d["users"][target_uid]["ai_enabled"] = not curr
            return not curr

        new_state = await update_data(_toggle)
        await query.answer(f"AI set to {'ON ✅' if new_state else 'OFF ❌'}", show_alert=True)
        query.data = f"view_acc_{target_uid}"
        await callback_handler(client, query)

    elif query.data.startswith("term_acc_"):
        target_uid = query.data.replace("term_acc_", "")
        if target_uid in connected_clients:
            try:
                await connected_clients[target_uid].stop()
            except Exception:
                pass
            del connected_clients[target_uid]

        def _remove(d):
            d.get("users", {}).pop(target_uid, None)
            d.get("history", {}).pop(target_uid, None)
            d.get("last_msg_time", {}).pop(target_uid, None)

        await update_data(_remove)

        await query.answer("Session removed!", show_alert=True)
        query.data = "manage_acc"
        await callback_handler(client, query)

    elif query.data == "toggle_ai":
        users = data.get("users", {})
        if not users:
            await query.answer("No accounts connected!", show_alert=True)
            return

        first_uid = next(iter(users))
        new_state = not users[first_uid].get("ai_enabled", True)

        def _set_all(d):
            for uid in d.get("users", {}):
                d["users"][uid]["ai_enabled"] = new_state

        await update_data(_set_all)
        await query.answer(
            f"Global AI Auto-Reply: {'ON ✅' if new_state else 'OFF ❌'}", show_alert=True
        )

    elif query.data == "stats":
        stats_text = get_stats_text(connected_clients, data)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")],
        ])
        await query.message.edit_text(stats_text, reply_markup=markup)


@bot.on_message(filters.private & ~filters.command(["start"]))
async def user_input_handler(client, message: Message):
    # AUTH GATE — only admins can add accounts / set posts
    if not is_admin(message.from_user.id):
        return

    # Guard non-text input (sticker/photo while in a waiting state)
    if not message.text:
        return

    user_id = str(message.from_user.id)
    state = user_states.get(user_id)

    if state == "AWAITING_SESSION":
        session_string = message.text.strip()

        # Immediately wipe the secret from the admin's chat.
        try:
            await message.delete()
        except Exception:
            pass

        status_msg = await message.reply_text("🔄 Validating & connecting string session...")

        try:
            user_client = Client(
                name=f"ub_{user_id}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string,
                in_memory=True,
            )

            await user_client.start()
            me = await user_client.get_me()
            acc_uid = str(me.id)
            acc_name = me.first_name or f"User {acc_uid}"

            # Duplicate add: stop the OLD client first, else it keeps
            # running with its own handlers -> double replies + leak.
            old = connected_clients.get(acc_uid)
            if old is not None:
                try:
                    await old.stop()
                except Exception:
                    pass
                connected_clients.pop(acc_uid, None)

            # sendphoto first (group -1), then aichat (group 0), so the
            # paid-photo trigger always gets priority over AI replies.
            register_sendphoto_handler(user_client, acc_uid)
            register_ai_handler(user_client, acc_uid, OPENROUTER_API_KEY)
            connected_clients[acc_uid] = user_client

            def _save_session(d):
                existing = d.setdefault("users", {}).setdefault(acc_uid, {})
                existing.update({
                    "name": acc_name,
                    "session": session_string,
                    "ai_enabled": existing.get("ai_enabled", True),
                })

            await update_data(_save_session)

            user_states.pop(user_id, None)

            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗄 Accounts", callback_data="manage_acc")],
                [InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")],
            ])

            await status_msg.edit_text(
                f"✅ **Session Successfully Connected!**\n\n"
                f"• **Account Name:** {acc_name}\n"
                f"• **User ID:** `{acc_uid}`\n"
                f"• **Auto Photo Trigger:** Active (`send`, `.send`, `star`, `.star`)",
                reply_markup=markup,
            )

        except Exception as e:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")]]
            )
            await status_msg.edit_text(
                f"❌ **Failed to Connect Session:** `{e}`", reply_markup=markup
            )
        return

    if isinstance(state, str) and state.startswith("AWAITING_POST:"):
        target_uid = state.split(":", 1)[1]
        link = message.text.strip()
        status_msg = await message.reply_text("🔄 Resolving channel post via the connected account...")

        markup_back = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=f"photo_acc_{target_uid}")]
        ])

        if target_uid not in connected_clients:
            user_states.pop(user_id, None)
            await status_msg.edit_text(
                "❌ That account is offline. Reconnect the session first.",
                reply_markup=markup_back,
            )
            return

        ok, result_text = await save_post_from_link(
            connected_clients[target_uid], target_uid, link
        )

        if ok:
            user_states.pop(user_id, None)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 View Photo Settings", callback_data=f"photo_acc_{target_uid}")],
                [InlineKeyboardButton("⬅️ Dashboard", callback_data="back_main")],
            ])
            await status_msg.edit_text(result_text, reply_markup=markup)
        else:
            await status_msg.edit_text(
                f"❌ {result_text}\n\nSend another link, or tap Cancel.",
                reply_markup=markup_back,
            )


async def main():
    global bot

    # Construct the bot ON the running loop so its dispatcher binds here.
    bot = Client("DashboardBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

    await bot.start()
    log.info("Dashboard Control Panel Started!")

    # Restore session instances on service boot
    data = await load_data()
    for uid, udata in data.get("users", {}).items():
        try:
            cli = Client(
                name=f"ub_{uid}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=udata["session"],
                in_memory=True,
            )
            # sendphoto first (group -1), then aichat (group 0)
            register_sendphoto_handler(cli, str(uid))
            register_ai_handler(cli, uid, OPENROUTER_API_KEY)
            await cli.start()
            connected_clients[str(uid)] = cli
            log.info("Session Active: %s", uid)
        except Exception as e:
            log.error("Failed Session Restore for %s: %s", uid, e)

    log.info("All sessions restored. Running...")
    await idle()

    # Graceful shutdown — never let a broken client or dispatcher
    # crash the exit sequence.
    log.info("Shutting down...")
    for uid, cli in list(connected_clients.items()):
        try:
            await cli.stop()
        except Exception as e:
            log.warning("failed to stop session %s: %s", uid, e)
        connected_clients.pop(uid, None)

    try:
        await bot.stop()
    except Exception as e:
        log.warning("bot.stop() error (ignored): %s", e)
    log.info("Shutdown complete.")


if __name__ == "__main__":
    # Create and install the loop BEFORE anything constructs a Pyrogram
    # Client. Pyrogram binds dispatcher queues to the loop current at
    # construction time; asyncio.run()/get_event_loop() both cause
    # "attached to a different loop" shutdown crashes on modern Python.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Interrupted by user.")
    finally:
        try:
            loop.close()
        except Exception:
            pass
