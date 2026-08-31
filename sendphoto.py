import re

from pyrogram import Client, filters
from pyrogram.types import Message

# Hardcoded DM trigger words. Matched case-insensitively as the full message.
PHOTO_TRIGGERS = {
    "send",
    ".send",
    "!send",
    "/send",
    "star",
    ".star",
    "!star",
    "/star",
}

_RESERVED_PUBLIC_PATHS = {"c", "s", "addstickers", "joinchat", "share", "boost", "proxy"}


def is_send_trigger(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower() in PHOTO_TRIGGERS


def parse_post_link(link: str):
    """
    Supports:
      https://t.me/channelusername/123     -> public channel
      https://t.me/c/1234567890/123         -> private channel (internal id)
      https://telegram.me/...               -> same forms
    Returns (chat_ref, message_id) or (None, None).
    chat_ref is a username string or a numeric -100... chat id.
    """
    if not link:
        return None, None

    link = link.strip()

    m = re.search(r"(?:t(?:elegram)?\.me)/c/(\d+)/(\d+)", link)
    if m:
        internal_id = int(m.group(1))
        msg_id = int(m.group(2))
        chat_ref = int(f"-100{internal_id}")
        return chat_ref, msg_id

    m = re.search(r"(?:t(?:elegram)?\.me)/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        username = m.group(1)
        if username.lower() in _RESERVED_PUBLIC_PATHS:
            return None, None
        return username, int(m.group(2))

    return None, None


def get_paid_post(owner_id: str):
    from aichat import load_data

    return load_data().get("users", {}).get(str(owner_id), {}).get("paid_photo")


def account_has_paid_post(owner_id: str) -> bool:
    post = get_paid_post(owner_id)
    return bool(post and post.get("chat_id") and post.get("message_id"))


def clear_paid_post(owner_id: str) -> bool:
    from aichat import load_data, save_data

    data = load_data()
    user = data.get("users", {}).get(str(owner_id))
    if not user or "paid_photo" not in user:
        return False
    user.pop("paid_photo", None)
    save_data(data)
    return True


def format_paid_post(post: dict) -> str:
    if not post:
        return "Not set ❌"
    title = post.get("title") or "Unknown"
    link = post.get("link") or ""
    chat_id = post.get("chat_id")
    msg_id = post.get("message_id")
    lines = [f"**{title}**"]
    if link:
        lines.append(f"• **Link:** `{link}`")
    lines.append(f"• **Chat ID:** `{chat_id}`")
    lines.append(f"• **Message ID:** `{msg_id}`")
    return "\n".join(lines)


async def save_post_from_link(user_client: Client, owner_id: str, link: str):
    """Resolve a t.me post link through the connected userbot and persist it.

    Returns (ok: bool, message: str).
    """
    from aichat import load_data, save_data

    chat_ref, msg_id = parse_post_link(link)
    if chat_ref is None:
        return False, (
            "Couldn't parse that link.\n\n"
            "Use a valid Telegram post link:\n"
            "• Public: `https://t.me/channel/123`\n"
            "• Private: `https://t.me/c/1234567890/55`"
        )

    try:
        chat = await user_client.get_chat(chat_ref)
        msg = await user_client.get_messages(chat.id, msg_id)
        if getattr(msg, "empty", False):
            return False, (
                "Message not found. Make sure the linked account can "
                "see that channel / post."
            )

        title = (
            getattr(chat, "title", None)
            or (f"@{chat.username}" if getattr(chat, "username", None) else None)
            or str(chat.id)
        )

        data = load_data()
        user = data.setdefault("users", {}).setdefault(str(owner_id), {})
        user["paid_photo"] = {
            "chat_id": chat.id,
            "message_id": msg_id,
            "link": link.strip(),
            "title": title,
        }
        save_data(data)

        return True, (
            f"✅ **Post saved for forwarding**\n\n"
            f"• **Channel:** `{title}`\n"
            f"• **Chat ID:** `{chat.id}`\n"
            f"• **Message ID:** `{msg_id}`\n"
            f"• **Link:** `{link.strip()}`\n\n"
            "In DMs, `send` / `.send` / `.star` will forward this post. "
            "AI will not reply on those trigger words."
        )
    except Exception as e:
        return False, (
            f"Failed to resolve that post: `{e}`\n\n"
            "The connected account must be a member of private channels."
        )


async def forward_saved_post(client: Client, owner_id: str, target_chat_id: int) -> bool:
    post = get_paid_post(owner_id)
    if not post:
        return False

    from_chat_id = post["chat_id"]
    message_id = post["message_id"]

    try:
        await client.forward_messages(
            chat_id=target_chat_id,
            from_chat_id=from_chat_id,
            message_ids=message_id,
        )
        return True
    except Exception as fwd_err:
        print(f"[sendphoto] forward failed: {fwd_err}")
        try:
            await client.copy_message(
                chat_id=target_chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            return True
        except Exception as copy_err:
            print(f"[sendphoto] copy failed: {copy_err}")
            return False


def register_sendphoto_handler(user_client: Client, owner_id: str):
    owner_id_str = str(owner_id)

    @user_client.on_message(
        filters.private
        & ~filters.me
        & ~filters.bot
        & ~filters.service
    )
    async def paid_photo_trigger(client: Client, message: Message):
        if not message.text or not is_send_trigger(message.text):
            return

        if not account_has_paid_post(owner_id_str):
            return

        try:
            await client.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        ok = await forward_saved_post(client, owner_id_str, message.chat.id)
        if not ok:
            print(f"[sendphoto] could not share saved post for account {owner_id_str}")
