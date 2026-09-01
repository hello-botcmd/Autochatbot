import logging
import re

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import Message

from storage import load_data, update_data

log = logging.getLogger(__name__)

# Trigger words are matched ANYWHERE inside a sentence as standalone words,
# case-insensitively. "please send it" and "bro star" match;
# "sendphoto", "resend", "sender" do NOT (word-boundary regex).
TRIGGER_WORDS = ("send", "star")
_TRIGGER_RE = re.compile(
    r"\b(?:[.!/])?(?:" + "|".join(TRIGGER_WORDS) + r")\b",
    re.IGNORECASE,
)

_RESERVED_PUBLIC_PATHS = {"c", "s", "addstickers", "joinchat", "share", "boost", "proxy"}


def is_send_trigger(text: str) -> bool:
    if not text:
        return False
    return bool(_TRIGGER_RE.search(text))


def parse_post_link(link: str):
    """
    Supports:
      https://t.me/channelusername/123  -> public channel
      https://t.me/c/1234567890/123     -> private channel (internal id)
      https://telegram.me/...           -> same forms
    Returns (chat_ref, message_id) or (None, None).
    chat_ref is a username string or a numeric -100... chat id.
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


def _migrate(user: dict) -> None:
    """Upgrade a legacy single 'paid_photo' entry to the 'paid_posts' list.
    Call inside update_data (lock held) or on a loaded read-only copy."""
    if "paid_posts" not in user:
        posts = []
        legacy = user.pop("paid_photo", None)
        if legacy and legacy.get("chat_id") and legacy.get("message_id"):
            posts.append(legacy)
        user["paid_posts"] = posts
    user.setdefault("paid_post_idx", 0)


async def get_paid_posts(owner_id: str) -> list:
    data = await load_data()
    user = data.get("users", {}).get(str(owner_id), {})
    _migrate(user)
    return user.get("paid_posts", [])


async def account_has_paid_post(owner_id: str) -> bool:
    return bool(await get_paid_posts(owner_id))


async def clear_paid_post(owner_id: str) -> bool:
    def _clear(d):
        user = d.get("users", {}).get(str(owner_id))
        if not user:
            return False
        _migrate(user)
        had = bool(user.get("paid_posts"))
        user["paid_posts"] = []
        user["paid_post_idx"] = 0
        user.pop("paid_photo", None)
        return had

    return bool(await update_data(_clear))


def format_paid_posts(posts: list) -> str:
    if not posts:
        return "Not set ❌"
    lines = []
    for i, post in enumerate(posts, 1):
        title = post.get("title") or "Unknown"
        lines.append(f"**{i}. {title}**")
        if post.get("link"):
            lines.append(f"   • **Link:** `{post['link']}`")
        lines.append(
            f"   • **Chat ID:** `{post.get('chat_id')}` · "
            f"**Msg:** `{post.get('message_id')}`"
        )
    return "\n".join(lines)


async def save_post_from_link(user_client: Client, owner_id: str, link: str):
    """Resolve a t.me post link through the connected userbot and APPEND it
    to the account's rotation list. Returns (ok: bool, message: str).
    """
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

        def _save(d):
            user = d.setdefault("users", {}).setdefault(str(owner_id), {})
            _migrate(user)
            posts = user.setdefault("paid_posts", [])

            # Skip exact duplicates (same chat + message id).
            posts = [
                p for p in posts
                if not (p.get("chat_id") == chat.id and p.get("message_id") == msg_id)
            ]
            posts.append({
                "chat_id": chat.id,
                "message_id": msg_id,
                "link": link.strip(),
                "title": title,
                "username": getattr(chat, "username", None),
            })
            user["paid_posts"] = posts
            # Keep rotation index in range after a possible re-add.
            user["paid_post_idx"] = int(user.get("paid_post_idx", 0)) % len(posts)
            return len(posts)

        count = await update_data(_save)

        return True, (
            f"✅ **Post #{count} saved to rotation**\n\n"
            f"• **Channel:** `{title}`\n"
            f"• **Chat ID:** `{chat.id}`\n"
            f"• **Message ID:** `{msg_id}`\n"
            f"• **Link:** `{link.strip()}`\n\n"
            "Each `send` / `star` trigger in DMs sends the **next** post "
            "in the list (round-robin), with no forward header. AI will "
            "not reply to those trigger words."
        )
    except Exception as e:
        return False, (
            f"Failed to resolve that post: `{e}`\n\n"
            "The connected account must be a member of private channels."
        )


async def _resolve_source_chat(client: Client, post: dict):
    """Re-resolve the saved source so the peer exists in this in-memory session."""
    username = post.get("username")
    if username:
        try:
            chat = await client.get_chat(username)
            return chat.id
        except Exception as e:
            log.warning("get_chat(@%s) failed: %s", username, e)

    link = post.get("link") or ""
    chat_ref, _ = parse_post_link(link)
    if chat_ref is not None:
        try:
            chat = await client.get_chat(chat_ref)
            return chat.id
        except Exception as e:
            log.warning("get_chat(link) failed: %s", e)

    chat_id = post.get("chat_id")
    try:
        chat = await client.get_chat(int(chat_id))
        return chat.id
    except Exception as e:
        log.warning("get_chat(%s) failed: %s", chat_id, e)
        return chat_id


async def forward_saved_post(client: Client, owner_id: str, target_chat_id: int) -> bool:
    """Send the NEXT saved post (round-robin) to target_chat_id.

    Uses copy_message, NOT forward_messages, so the message arrives as a
    clean copy — no "Forwarded from <channel>" header. The rotation index
    is advanced atomically under the storage lock.
    """
    def _pick_next(d):
        user = d.get("users", {}).get(str(owner_id))
        if not user:
            return None
        _migrate(user)
        posts = user.get("paid_posts") or []
        if not posts:
            return None
        idx = int(user.get("paid_post_idx", 0)) % len(posts)
        user["paid_post_idx"] = (idx + 1) % len(posts)  # advance rotation
        return posts[idx]

    post = await update_data(_pick_next)
    if not post:
        return False

    from_chat_id = await _resolve_source_chat(client, post)
    message_id = post["message_id"]

    try:
        await client.copy_message(
            chat_id=target_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
        return True
    except Exception as copy_err:
        log.warning("copy failed: %s", copy_err)
        return False

def register_sendphoto_handler(user_client: Client, owner_id: str):
    owner_id_str = str(owner_id)

    @user_client.on_message(
        filters.private & ~filters.me & ~filters.bot & ~filters.service,
        group=-1,  # runs BEFORE aichat's group-0 handler
    )
    async def paid_photo_trigger(client: Client, message: Message):
        if not message.text or not is_send_trigger(message.text):
            return

        posts = await get_paid_posts(owner_id_str)
        if not posts:
            log.debug("[sendphoto] trigger but no posts set, falling through to AI: owner=%s",
                      owner_id_str)
            return  # no post set -> fall through, AI auto-reply handles it

        log.info("[sendphoto] trigger: owner=%s from=%s posts=%s text=%r",
                 owner_id_str,
                 message.from_user.id if message.from_user else "?",
                 len(posts), message.text[:40])

        try:
            await client.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        ok = await forward_saved_post(client, owner_id_str, message.chat.id)
        if ok:
            log.info("[sendphoto] post sent: owner=%s to=%s", owner_id_str, message.chat.id)
        else:
            log.warning("[sendphoto] could not share saved post for account %s", owner_id_str)
            # Never leave the user with silence: the copy failed.
            try:
                await message.reply_text(
                    "Post is unavailable right now, please try again later. 🙏"
                )
            except Exception as e:
                log.warning("fallback error reply failed: %s", e)

        # Post delivered (or attempted) — stop AI from also replying.
        raise StopPropagation
