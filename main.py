import asyncio
import os
import time
from dotenv import load_dotenv

load_dotenv()

from pyro_patch import apply_pyrogram_peer_patch

apply_pyrogram_peer_patch()

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from aichat import load_data, register_ai_handler, save_data
from sendphoto import (
    clear_paid_post,
    format_paid_post,
    register_sendphoto_handler,
    save_post_from_link,
)
from stats import get_stats_text

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "nonsecularman")

START_PIC_URL = "https://images.unsplash.com/photo-1503376780353-7e6692767b70"

connected_clients = {}
user_states = {}

bot = Client("DashboardBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


def get_start_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Dashboard", callback_data="back_main")],
        [InlineKeyboardButton("📞 Contact Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ])


def get_dashboard_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Account", callback_data="add_acc"),
            InlineKeyboardButton("🗄 Manage Accounts", callback_data="manage_acc"),
        ],
        [
            InlineKeyboardButton("⚡ Toggle AI", callback_data="toggle_ai"),
            InlineKeyboardButton("📊 System Stats", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("💎 Configure Paid Photo Module", callback_data="set_photo_menu"),
        ],
        [
            InlineKeyboardButton("⬅️ Home Menu", callback_data="back_start"),
        ],
    ])


@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    text = (
        "✨ **Welcome to Control Panel** ✨\n\n"
        "Manage your active string session userbots, set custom auto-reply triggers, and control AI modules."
    )
    try:
        await message.reply_photo(photo=START_PIC_URL, caption=text, reply_markup=get_start_markup())
    except Exception:
        await message.reply_text(text, reply_markup=get_start_markup())


@bot.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    user_id = str(query.from_user.id)
    data = load_data()

    if query.data == "back_start":
        text = (
            "✨ **Welcome to Control Panel** ✨\n\n"
            "Manage your active string session userbots, set custom auto-reply triggers, and control AI modules."
        )
        if query.message.photo:
            await query.message.edit_caption(caption=text, reply_markup=get_start_markup())
        else:
            await query.message.edit_text(text, reply_markup=get_start_markup())

    elif query.data == "back_main":
        user_states.pop(user_id, None)
        text = (
            "📂 **Userbot Dashboard**\n\n"
            "• **➕ Add Account** — Connect Pyrogram String Session\n"
            "• **🗄 Manage Accounts** — View connected account status\n"
            "• **⚡ Toggle AI** — Turn AI Auto-Reply ON / OFF\n"
            "• **💎 Paid Photo Settings** — Configure Channel & IDs for auto-send\n"
            "• **📊 System Stats** — VPS & CPU Performance"
        )
        if query.message.photo:
            await query.message.delete()
            await query.message.reply_text(text, reply_markup=get_dashboard_markup())
        else:
            await query.message.edit_text(text, reply_markup=get_dashboard_markup())

    elif query.data == "add_acc":
        user_states[user_id] = "AWAITING_SESSION"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Cancel", callback_data="back_main")]
        ])
        await query.message.edit_text(
            "🔐 **Send Pyrogram String Session**\n\n"
            "Paste your String Session below to link your account to the bot.",
            reply_markup=markup
        )

    elif query.data == "set_photo_menu":
        user_states.pop(user_id, None)
        saved_users = data.get("users", {})
        if not saved_users:
            await query.answer("No accounts connected! Add an account first.", show_alert=True)
            return

        text = (
            "💎 **Paid Photo Module**\n\n"
            "Select an account. That userbot will **forward your saved channel post** "
            "when someone sends `send`, `.send`, or `.star` in a private DM.\n\n"
            "AI auto-reply is skipped for those trigger words.\n\n"
            "🟢 = Online   ⚪ = Offline   📎 = Post saved"
        )
        btns = []
        for uid, uinfo in saved_users.items():
            is_active = uid in connected_clients
            indicator = "🟢" if is_active else "⚪"
            badge = "📎" if uinfo.get("paid_photo") else "➕"
            acc_name = uinfo.get("name", f"User {uid}")
            btns.append([
                InlineKeyboardButton(
                    f"{indicator} {badge} {acc_name}",
                    callback_data=f"photo_acc_{uid}",
                )
            ])
        btns.append([InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_main")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("photo_acc_"):
        user_states.pop(user_id, None)
        target_uid = query.data.replace("photo_acc_", "")
        saved_users = data.get("users", {})
        uinfo = saved_users.get(target_uid, {})
        if not uinfo:
            await query.answer("Account data missing!", show_alert=True)
            return

        is_connected = target_uid in connected_clients
        acc_name = uinfo.get("name", "Unknown User")
        conn_status = "Connected 🟢" if is_connected else "Offline 🔴"
        post = uinfo.get("paid_photo")

        text = (
            f"💎 **Paid Photo — {acc_name}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **User ID:** `{target_uid}`\n"
            f"• **Session:** {conn_status}\n"
            f"• **Triggers:** `send`  `.send`  `.star`\n\n"
            f"📎 **Saved Post**\n{format_paid_post(post)}"
        )
        btns = [
            [InlineKeyboardButton("📎 Set / Change Post Link", callback_data=f"photo_set_{target_uid}")],
            [InlineKeyboardButton("🧹 Clear Saved Post", callback_data=f"photo_clear_{target_uid}")],
            [InlineKeyboardButton("⬅️ Back to Accounts", callback_data="set_photo_menu")],
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
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Cancel", callback_data=f"photo_acc_{target_uid}")]
        ])
        await query.message.edit_text(
            f"🔗 **Send a channel post link**\n\n"
            f"Account: **{acc_name}**\n\n"
            "Paste a Telegram post link (public or private):\n"
            "• `https://t.me/mychannel/1234`\n"
            "• `https://t.me/c/1234567890/55`\n\n"
            "The connected account must have access to that channel.",
            reply_markup=markup,
        )

    elif query.data.startswith("photo_clear_"):
        target_uid = query.data.replace("photo_clear_", "")
        if clear_paid_post(target_uid):
            await query.answer("Saved post cleared!", show_alert=True)
        else:
            await query.answer("No saved post on this account.", show_alert=True)
        query.data = f"photo_acc_{target_uid}"
        await callback_handler(client, query)

    elif query.data == "manage_acc":
        saved_users = data.get("users", {})
        text = "🗄 **Connected Accounts**\n\n"
        text += "🟢 = Active & Connected | 🔴 = AI Disabled / Disconnected\n\n"

        btns = []
        for uid, uinfo in saved_users.items():
            is_active = uid in connected_clients
            ai_on = uinfo.get("ai_enabled", True)
            indicator = "🟢" if (is_active and ai_on) else ("🔴" if is_active else "⚪ Disconnected")
            acc_name = uinfo.get("name", f"User {uid}")
            btns.append([InlineKeyboardButton(f"{indicator} {acc_name}", callback_data=f"view_acc_{uid}")])

        btns.append([InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_main")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("view_acc_"):
        target_uid = query.data.replace("view_acc_", "")
        saved_users = data.get("users", {})
        uinfo = saved_users.get(target_uid, {})

        if not uinfo:
            await query.answer("Account data missing!", show_alert=True)
            return

        is_connected = target_uid in connected_clients
        ai_status = "ON ✅" if uinfo.get("ai_enabled", True) else "OFF ❌"
        conn_status = "Connected 🟢" if is_connected else "Offline 🔴"
        acc_name = uinfo.get("name", "Unknown User")

        text = (
            f"👤 **Account Management**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• **Name:** `{acc_name}`\n"
            f"• **User ID:** `{target_uid}`\n"
            f"• **Session Status:** {conn_status}\n"
            f"• **AI Auto-Reply:** {ai_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

        btns = [
            [InlineKeyboardButton("⚡ Toggle AI Status", callback_data=f"toggle_acc_ai_{target_uid}")],
            [InlineKeyboardButton("❌ Disconnect Account", callback_data=f"term_acc_{target_uid}")],
            [InlineKeyboardButton("⬅️ Back to List", callback_data="manage_acc")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith("toggle_acc_ai_"):
        target_uid = query.data.replace("toggle_acc_ai_", "")
        if target_uid in data.get("users", {}):
            curr = data["users"][target_uid].get("ai_enabled", True)
            data["users"][target_uid]["ai_enabled"] = not curr
            save_data(data)
            status_text = "ON ✅" if not curr else "OFF ❌"
            await query.answer(f"AI Status set to {status_text}", show_alert=True)
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

        if target_uid in data.get("users", {}):
            data["users"].pop(target_uid, None)
            save_data(data)

        await query.answer("Session removed successfully!", show_alert=True)
        query.data = "manage_acc"
        await callback_handler(client, query)

    elif query.data == "toggle_ai":
        users = data.get("users", {})
        if not users:
            await query.answer("No accounts connected!", show_alert=True)
            return

        first_uid = next(iter(users))
        new_state = not users[first_uid].get("ai_enabled", True)
        for uid in users:
            users[uid]["ai_enabled"] = new_state
        save_data(data)

        status = "ON ✅" if new_state else "OFF ❌"
        await query.answer(f"Global AI Auto-Reply set to: {status}", show_alert=True)

    elif query.data == "stats":
        stats_text = get_stats_text(connected_clients, data)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Stats", callback_data="stats")],
            [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_main")]
        ])
        await query.message.edit_text(stats_text, reply_markup=markup)


@bot.on_message(filters.private & ~filters.command(["start"]))
async def user_input_handler(client, message: Message):
    user_id = str(message.from_user.id)
    state = user_states.get(user_id)

    if state == "AWAITING_SESSION":
        session_string = message.text.strip()
        status_msg = await message.reply_text("🔄 Validating & connecting string session...")

        try:
            user_client = Client(
                name=f"ub_{user_id}_{int(time.time())}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string,
                in_memory=True
            )

            await user_client.start()
            me = await user_client.get_me()
            acc_uid = str(me.id)
            acc_name = me.first_name or f"User {acc_uid}"

            # Register dynamic userbot handlers specifically for this session
            register_ai_handler(user_client, acc_uid, OPENROUTER_API_KEY)
            register_sendphoto_handler(user_client, acc_uid)
            connected_clients[acc_uid] = user_client

            data = load_data()
            existing = data.setdefault("users", {}).get(acc_uid, {})
            existing.update({
                "name": acc_name,
                "session": session_string,
                "ai_enabled": existing.get("ai_enabled", True),
            })
            data["users"][acc_uid] = existing
            save_data(data)

            user_states.pop(user_id, None)

            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗄 Manage Accounts", callback_data="manage_acc")],
                [InlineKeyboardButton("⬅️ Return to Dashboard", callback_data="back_main")]
            ])

            await status_msg.edit_text(
                f"✅ **Session Successfully Connected!**\n\n"
                f"• **Account Name:** `{acc_name}`\n"
                f"• **User ID:** `{acc_uid}`\n"
                f"• **Auto Photo Trigger:** Active (`.send`, `send`)",
                reply_markup=markup
            )

        except Exception as e:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_main")]
            ])
            await status_msg.edit_text(
                f"❌ **Failed to Connect Session:** `{e}`",
                reply_markup=markup
            )
        return

    if isinstance(state, str) and state.startswith("AWAITING_POST:"):
        target_uid = state.split(":", 1)[1]
        link = (message.text or "").strip()
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
            connected_clients[target_uid],
            target_uid,
            link,
        )

        if ok:
            user_states.pop(user_id, None)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 View Account Photo Settings", callback_data=f"photo_acc_{target_uid}")],
                [InlineKeyboardButton("⬅️ Return to Dashboard", callback_data="back_main")],
            ])
            await status_msg.edit_text(result_text, reply_markup=markup)
        else:
            await status_msg.edit_text(
                f"❌ {result_text}\n\nSend another link, or tap Cancel.",
                reply_markup=markup_back,
            )


async def main():
    await bot.start()
    print("Dashboard Control Panel Started!")

    # Restore session instances on service boot
    data = load_data()
    for uid, udata in data.get("users", {}).items():
        try:
            cli = Client(
                name=f"ub_{uid}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=udata["session"],
                in_memory=True
            )
            register_ai_handler(cli, uid, OPENROUTER_API_KEY)
            register_sendphoto_handler(cli, uid)
            await cli.start()
            connected_clients[str(uid)] = cli
            print(f"Session Active: {uid}")
        except Exception as e:
            print(f"Failed Session Restore for {uid}: {e}")

    await asyncio.Event().wait()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
