"""Paid-post trigger module.

• Trigger words detected anywhere in a sentence ("please send it").
• Multiple saved posts per account, round-robin rotation.
• Delivery chain (goal: NO 'Forwarded from' header):
    1. copy_message / copy_media_group  -> clean copy, no header
    2. download + re-upload             -> clean, works where copy fails
    3. forward                          -> LAST RESORT only (header appears,
       logged loudly). Fix properly by upgrading pyrogram>=2.0.30 or unsetting
       'restrict saving content' on the source channel.
• The group -1 handler NEVER lets an exception escape: on Pyrogram, an
  exception here aborts later handler groups (aichat's group 0) for the
  same update — which is one of the ways AI replies went silent.
"""

import os
import re
from copy import deepcopy

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import Message

from storage import load_data, update_data

# Trigger words. Matched case-insensitively. Full-message match still works,
# plus word-boundary matches inside sentences ("pls send it" triggers,
# "resend" / "sending" / "starfish" don't).
PHOTO_TRIGGERS = {"send", ".send", "!send", "/send", "star", ".star", "!star", "/star"}

# Set TRIGGER_IN_SENTENCE=0 in .env to go back to exact-match only.
TRIGGER_IN_SENTENCE = os.getenv("TRIGGER_IN_SENTENCE", "1") == "1"

_TRIGGER_RES = [re.compile(rf"(?<!\w){re.escape(w)}(?!\w)") for w in PHOTO_TRIGGERS]

_RESERVED_PUBLIC_PATHS = {"c", "s", "addstickers", "joinchat", "share", "boost", "proxy"}


def is_send_trigger(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if t in PHOTO_TRIGGERS:
        return True
    if not TRIGGER_IN_SENTENCE:
        return False
    # "resend" -> 'send' preceded by word char -> no match.
    # "sending" -> 'send' followed by word char -> no match.
    # "pls .send it" -> matches.
    return any(rx.search(t) for rx in _TRIGGER_RES)


def parse_post_link(link: str):
    """
    Supports:
      https://t.me/channelusername/123  -> public channel
      https://t.me/c/1234567890/123     -> private channel (internal id)
      https://telegram.me/...           -> same forms
    Returns (chat_ref, message_id) or (None, None).
    """
    if not link:
        return None, None

    link = link.strip()

    m = re.search(r"(?:t(?:elegram)?\.me)/c/(\d+)/(\d+)", link)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))

    m = re.search(r"(?:t(?:elegram)?\.me)/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        username = m.group(1)
        if username.lower() in _RESERVED_PUBLIC_PATHS:
            return None, None
        return username, int(m.group(2))

    return None, None


# ------------------------- POST STORAGE (multiple) ------------------------- #

def _posts_of(user: dict) -> list:
    """All saved posts, with lazy backward-compat migration of old single
    `paid_photo` into the new `paid_photos` list."""
    if not user:
        return []
    if "paid_photos" in user:
        return user["paid_photos"]
    if "paid_photo" in user and user["paid_photo"]:
        user["paid_photos"] = [user["paid_photo"]]
        user.pop("paid_photo", None)
        return user["paid_photos"]
    return []


async def get_all_paid_posts(owner_id: str) -> list:
    data = await load_data()
    return deepcopy(_posts_of(data.get("users", {}).get(str(owner_id), {})))


async def get_next_paid_post(owner_id: str):
    """Round-robin pick. Selecting a post and advancing the rotation cursor
    happen inside ONE atomic update_data, so concurrent DMs can't race."""
    def _pick(d):
        user = d.get("users", {}).get(str(owner_id), {})
        posts = _posts_of(user)
        if not posts:
            return None
        idx = user.get("paid_photo_cursor", 0) % len(posts)
        user["paid_photo_cursor"] = idx + 1
        return deepcopy(posts[idx])

    return await update_data(_pick)


async def account_has_paid_post(owner_id: str) -> bool:
    return bool(await get_all_paid_posts(owner_id))


async def remove_paid_post(owner_id: str, index: int) -> bool:
    def _rm(d):
        user = d.get("users", {}).get(str(owner_id))
        posts = _posts_of(user) if user else []
        if not posts or index < 0 or index >= len(posts):
            return False
        posts.pop(index)
        if not posts:
            user.pop("paid_photo_cursor", None)
        elif "paid_photo_cursor" in user:
            user["paid_photo_cursor"] %= len(posts)
        return True

    return bool(await update_data(_rm))


async def clear_paid_post(owner_id: str) -> bool:
    def _clear(d):
        user = d.get("users", {}).get(str(owner_id))
        if not user:
            return False
        had = bool(user.get("paid_photos") or user.get("paid_photo"))
        user.pop("paid_photos", None)
        user.pop("paid_photo", None)
        user.pop("paid_photo_cursor", None)
        return had

    return bool(await update_data(_clear))


def format_paid_post(post: dict) -> str:
    if not post:
        return "Not set ❌"
    title = post.get("title") or "Unknown"
    link = post.get("link") or ""
    lines = [f"**{title}**"]
    if link:
        lines.append(f"• **Link:** `{link}`")
    lines.append(f"• **Chat ID:** `{post.get('chat_id')}`")
    lines.append(f"• **Message ID:** `{post.get('message_id')}`")
    return "\n".join(lines)


def format_paid_posts(posts: list) -> str:
    if not posts:
        return "Not set ❌"
    blocks = []
    for i, post in enumerate(posts):
        title = post.get("title") or "Unknown"
        link = post.get("link") or ""
        line = f"**#{i + 1} — {title}**"
        if link:
            line += f"\n  • `{link}`"
        line += f"\n  • Msg ID: `{post.get('message_id')}`"
        blocks.append(line)
    blocks.append("♻️ *Delivered in rotation: 1st trigger → #1, next → #2, … loops.*")
    return "\n\n".join(blocks)


# ------------------------------ SAVE FLOW ---------------------------------- #

async def save_post_from_link(user_client: Client, owner_id: str, link: str):
    """Resolve a t.me post link through the connected userbot and APPEND it to
    the account's rotation list. Returns (ok: bool, message: str)."""
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

        def _append(d):
            user = d.setdefault("users", {}).setdefault(str(owner_id), {})
            posts = _posts_of(user)
            user["paid_photos"] = posts
            posts.append({
                "chat_id": chat.id,
                "message_id": msg_id,
                "link": link.strip(),
                "title": title,
                "username": getattr(chat, "username", None),
            })
            return len(posts)

        position = await update_data(_append)

        return True, (
            f"✅ **Post #{position} added to rotation**\n\n"
            f"• **Channel:** `{title}`\n"
            f"• **Chat ID:** `{chat.id}`\n"
            f"• **Message ID:** `{msg_id}`\n"
            f"• **Link:** `{link.strip()}`\n\n"
            "Trigger words anywhere in a DM (`send`, `star`, …) will deliver "
            "posts in round-robin order — sent directly, no forward header."
        )
    except Exception as e:
        return False, (
            f"Failed to resolve that post: `{e}`\n\n"
            "The connected account must be a member of private channels."
        )


# ------------------------------ DELIVERY ----------------------------------- #

async def _resolve_source_chat(client: Client, post: dict):
    """Re-resolve the saved source so the peer exists in this in-memory session."""
    username = post.get("username")
    if username:
        try:
            chat = await client.get_chat(username)
            return chat.id
        except Exception as e:
            print(f"[sendphoto] get_chat(@{username}) failed: {e}")

    link = post.get("link") or ""
    chat_ref, _ = parse_post_link(link)
    if chat_ref is not None:
        try:
            chat = await client.get_chat(chat_ref)
            return chat.id
        except Exception as e:
            print(f"[sendphoto] get_chat(link) failed: {e}")

    chat_id = post.get("chat_id")
    try:
        chat = await client.get_chat(int(chat_id))
        return chat.id
    except Exception as e:
        print(f"[sendphoto] get_chat({chat_id}) failed: {e}")
        return chat_id


async def _reupload_message(client: Client, from_chat_id, message_id,
                            target_chat_id: int) -> bool:
    """Download the source message's media and re-send it as the userbot.
    Clean (no header), and works where copy is restricted — when downloads
    are allowed. Returns False when unsupported."""
    try:
        msg = await client.get_messages(from_chat_id, message_id)
    except Exception as e:
        print(f"[sendphoto] reupload get_messages failed: {e}")
        return False
    if getattr(msg, "media_group_id", None):
        return False  # albums: not supported here; copy_media_group is tried first
    try:
        if getattr(msg, "text", None):
            await client.send_message(target_chat_id, msg.text.markdown)
            return True
        if getattr(msg, "caption", None) and not any(
            getattr(msg, a, None) for a in
            ("photo", "video", "document", "audio", "voice", "animation")
        ):
            await client.send_message(target_chat_id, msg.caption.markdown)
            return True
        path = await msg.download()
    except Exception as e:
        print(f"[sendphoto] reupload download failed: {e}")
        return False

    caption = getattr(msg, "caption", None)
    send = None
    if msg.photo:
        send = client.send_photo
    elif msg.video:
        send = client.send_video
    elif msg.document:
        send = client.send_document
    elif msg.audio:
        send = client.send_audio
    elif msg.voice:
        send = client.send_voice
    elif msg.animation:
        send = client.send_animation
    if send is None:
        return False
    try:
        await send(target_chat_id, path, caption=caption)
        return True
    except Exception as e:
        print(f"[sendphoto] reupload send failed: {e}")
        return False


async def deliver_saved_post(client: Client, post: dict, target_chat_id: int):
    """Send the saved post AS the userbot itself so Telegram shows NO
    'Forwarded from <channel>' header. Chain: copy -> re-upload -> forward.
    Returns (ok: bool, used_forward: bool)."""
    from_chat_id = await _resolve_source_chat(client, post)
    message_id = post["message_id"]

    copy_msg = getattr(client, "copy_message", None)

    # 1. Clean copy (no header). copy_message needs pyrogram >= 2.0.30.
    if copy_msg is not None:
        try:
            msg = None
            try:
                msg = await client.get_messages(from_chat_id, message_id)
            except Exception:
                pass

            if (msg and getattr(msg, "media_group_id", None)
                    and getattr(client, "copy_media_group", None)):
                await client.copy_media_group(
                    chat_id=target_chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                return True, False

            await copy_msg(
                chat_id=target_chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            return True, False
        except Exception as e:
            print(f"[sendphoto] copy failed ({e}); trying re-upload")

        # 2. Re-upload (clean, no header) — works where copy is restricted.
        if await _reupload_message(client, from_chat_id, message_id, target_chat_id):
            return True, False
    else:
        print(
            "[sendphoto] copy_message missing (pyrogram too old, need >= 2.0.30); "
            "trying re-upload"
        )
        if await _reupload_message(client, from_chat_id, message_id, target_chat_id):
            return True, False

    # 3. LAST RESORT: forward — the header WILL be shown. Loudly logged so
    #    the cause is visible instead of the user getting silence.
    print(
        "[sendphoto] FALLING BACK TO FORWARD — 'Forwarded from' header will show. "
        "Proper fixes: pip install -U 'pyrogram>=2.0.30', or unset 'restrict "
        "saving content' on the source channel."
    )
    try:
        await client.forward_messages(
            chat_id=target_chat_id,
            from_chat_id=from_chat_id,
            message_ids=message_id,
        )
        return True, True
    except Exception as e:
        print(f"[sendphoto] forward failed: {e}")
        return False, False


# ------------------------------ HANDLER ------------------------------------ #

def register_sendphoto_handler(user_client: Client, owner_id: str):
    owner_id_str = str(owner_id)

    @user_client.on_message(
        filters.private & ~filters.me & ~filters.bot & ~filters.service,
        group=-1,  # runs BEFORE aichat's group-0 handler, else it never fires
    )
    async def paid_photo_trigger(client: Client, message: Message):
        try:
            if not message.text or not is_send_trigger(message.text):
                return

            post = await get_next_paid_post(owner_id_str)
            if post is None:
                return  # no posts set -> fall through, AI auto-reply handles it

            try:
                await client.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass

            ok, used_forward = await deliver_saved_post(
                client, post, message.chat.id
            )

            if not ok:
                # FIX (total silence): on failure fall through to AI instead of
                # raising StopPropagation — the user gets an answer either way.
                print(
                    f"[sendphoto] could not deliver post for account "
                    f"{owner_id_str}; falling through to AI"
                )
                return

            if used_forward:
                print(f"[sendphoto] delivered via forward (header shown) for {owner_id_str}")

            # Post delivered — stop AI from also replying. This is now the
            # ONLY gate between trigger messages and AI.
            raise StopPropagation

        except StopPropagation:
            raise
        except Exception as e:
            # FIX (AI killed by sendphoto crashes): an exception escaping a
            # group -1 handler aborts Pyrogram's later handler groups (AI's
            # group 0) for this update. Swallow & log so it can never do that.
            print(f"[sendphoto] trigger handler error: {e}")
