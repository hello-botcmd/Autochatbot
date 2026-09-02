"""Group-chat (GC) AI auto-reply — fully separate module.

• Registered independently: register_gc_handler(client, owner_id, api_key).
• Uses its OWN data.json keys (gc_enabled, gc_ignored, gc_history,
  gc_last_msg_time, gc_rate_limited_until, gc_quota_notify_until,
  gc_last_ai_error) — the existing DM pipeline is never read or written here.
• Trigger: replies when the userbot is MENTIONED (@username / text mention)
  or REPLIED TO in a group. Set GC_TRIGGER=all in .env to reply to every
  human text message instead (spammy and API-expensive — think first).
• Commands (owner's outgoing messages, group-only):
    .gcon             — GC AI ON
    .gcoff            — GC AI OFF
    .gcignore <id>    — toggle-ignore a user (reply-based: `.gcignore`)
    .gcdiag           — GC diagnostics (config + last GC crash/error)
• Paid-post triggers (sendphoto) are private-only, so GC never clashes.
"""

import asyncio
import os
import time
import traceback

from pyrogram import Client, filters
from pyrogram.types import Message

from aichat import (
    OPENROUTER_MODEL,
    OPENROUTER_FALLBACK_MODEL,
    _credits_text,
    generate_ai_reply,
    get_openrouter_credits,
)
from storage import DEFAULT_PERSONA, load_data, update_data

GC_COOLDOWN_SECONDS = int(os.getenv("GC_COOLDOWN_SECONDS", "10"))
GC_MAX_HISTORY_TURNS = int(os.getenv("GC_MAX_HISTORY_TURNS", "6"))
GC_TRIGGER = os.getenv("GC_TRIGGER", "mention").lower()  # mention | all
GC_QUOTA_NOTIFY_COOLDOWN_HOURS = int(os.getenv("QUOTA_NOTIFY_COOLDOWN_HOURS", "6"))

_GC_COMMANDS = ["gcon", "gcoff", "gcignore", "gcdiag"]


def register_gc_handler(user_client: Client, owner_id: str, api_key: str):
    owner_id_str = str(owner_id)
    _me_cache = {}

    async def _get_me():
        # get_me() is an RPC: only callable while the client is started, which
        # is always true inside handlers — so cache lazily here, never at
        # registration (registration happens before cli.start() in main.py).
        if owner_id_str not in _me_cache:
            _me_cache[owner_id_str] = await user_client.get_me()
        return _me_cache[owner_id_str]

    async def _store_error(err: str, kind: str):
        def _set(d):
            d["gc_last_ai_error"] = {
                "kind": kind,
                "error": err[:800],
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        try:
            await update_data(_set)
        except Exception as e:
            print(f"[gcchat] error-store failed: {e}")

    def _is_mentioned(message: Message, me) -> bool:
        # Replied to my message
        rt = message.reply_to_message
        if rt and rt.from_user and rt.from_user.id == me.id:
            return True
        # Text mention / @username entities
        for e in (message.entities or []) + (message.caption_entities or []):
            t = str(getattr(e, "type", ""))
            user = getattr(e, "user", None)
            if t == "text_mention" and user and user.id == me.id:
                return True
            if t == "mention" and me.username and message.text:
                tag = message.text[e.offset:e.offset + e.length]
                if tag.lower() == f"@{me.username.lower()}":
                    return True
        # Raw fallback (some forks don't populate entities reliably)
        if me.username and message.text:
            if f"@{me.username}".lower() in message.text.lower():
                return True
        return False

    async def _notify_gc_quota(model: str):
        now = time.time()

        def _gate(d):
            u = d.setdefault("users", {}).setdefault(owner_id_str, {})
            if now < u.get("gc_quota_notify_until", 0):
                return False
            u["gc_quota_notify_until"] = now + GC_QUOTA_NOTIFY_COOLDOWN_HOURS * 3600
            return True

        if not await update_data(_gate):
            return

        lines = [
            "🚨 **GC AI Credits / Quota Exhausted** 🚨",
            "",
            "OpenRouter returned **429** while replying in a **group**.",
            f"• **Model:** `{model}`",
            f"• **Time:** `{time.strftime('%Y-%m-%d %H:%M:%S')}`",
            "",
            f"Next GC quota alert in `{GC_QUOTA_NOTIFY_COOLDOWN_HOURS}h`.",
        ]
        ok, credits = await get_openrouter_credits(api_key)
        if ok:
            lines.insert(6, "**Balance:**")
            lines.insert(7, _credits_text(credits))
        try:
            await user_client.send_message("me", "\n".join(lines))
        except Exception as e:
            print(f"[gcchat] quota notify failed: {e}")

    async def _resolve_gc_target(client: Client, message: Message):
        if message.reply_to_message and message.reply_to_message.from_user:
            return message.reply_to_message.from_user
        if len(message.command) >= 2:
            try:
                return await client.get_users(message.command[1])
            except Exception:
                return None
        return None

    def _display_name(u) -> str:
        return u.first_name or (f"@{u.username}" if u.username else str(u.id))

    # -------------------------- COMMANDS ---------------------------- #

    @user_client.on_message(filters.me & filters.group
                            & filters.command("gcon", prefixes=[".", "!", "/"]))
    async def gcon_cmd(client: Client, message: Message):
        def _on(d):
            d.setdefault("users", {}).setdefault(owner_id_str, {})["gc_enabled"] = True
        await update_data(_on)
        await message.edit_text("✅ **GC AI** turned **ON** for this account.\n"
                                "Trigger: mention / reply-to-me"
                                + (" (GC_TRIGGER=all)" if GC_TRIGGER == "all" else ""))

    @user_client.on_message(filters.me & filters.group
                            & filters.command("gcoff", prefixes=[".", "!", "/"]))
    async def gcoff_cmd(client: Client, message: Message):
        def _off(d):
            d.setdefault("users", {}).setdefault(owner_id_str, {})["gc_enabled"] = False
        await update_data(_off)
        await message.edit_text("❌ **GC AI** turned **OFF** for this account.")

    @user_client.on_message(filters.me & filters.group
                            & filters.command("gcignore", prefixes=[".", "!", "/"]))
    async def gcignore_cmd(client: Client, message: Message):
        target = await _resolve_gc_target(client, message)
        if target is None:
            await message.edit_text(
                "Usage: `.gcignore <userid/username>` — or reply to the person "
                "with `.gcignore`.\n(Toggling: ignore again to un-ignore.)"
            )
            return

        uid = str(target.id)

        def _toggle(d):
            u = d.setdefault("users", {}).setdefault(owner_id_str, {})
            ign = u.setdefault("gc_ignored", [])
            if uid in ign:
                ign.remove(uid)
                return False
            ign.append(uid)
            return True

        now_ignored = await update_data(_toggle)
        if now_ignored:
            await message.edit_text(
                f"🚫 **GC AI ignore-list:** added **{_display_name(target)}** "
                f"(`{uid}`). I won't reply to them in groups."
            )
        else:
            await message.edit_text(
                f"✅ **GC AI ignore-list:** removed **{_display_name(target)}** "
                f"(`{uid}`). I'll reply to them again."
            )

    @user_client.on_message(filters.me & filters.group
                            & filters.command("gcdiag", prefixes=[".", "!", "/"]))
    async def gcdiag_cmd(client: Client, message: Message):
        data = await load_data()
        uc = data.get("users", {}).get(owner_id_str, {})
        ignored = uc.get("gc_ignored", [])
        now = time.time()
        active_rl = sum(
            1 for v in data.get("gc_rate_limited_until", {}).values() if v > now
        )
        last_err = data.get("gc_last_ai_error", {})
        try:
            me = await _get_me()
            ver = f"@{me.username}" if me.username else str(me.id)
        except Exception:
            ver = "unknown"

        lines = [
            "🩺 GC DIAGNOSTICS",
            "━━━━━━━━━━━━━━━━",
            f"me: {ver} | account id: {owner_id_str}",
            f"gc_enabled: {uc.get('gc_enabled', False)}",
            f"trigger: {GC_TRIGGER} | cooldown: {GC_COOLDOWN_SECONDS}s",
            f"ignored users: {ignored if ignored else 'none'}",
            f"active 429 locks: {active_rl}",
            f"openrouter key: {'set' if api_key else 'MISSING'}",
            "━━━━━━━━━━━━━━━━",
            "Last GC crash/error:",
            (f"[{last_err.get('ts', '?')}] {last_err.get('kind', '?')}: "
             f"{last_err.get('error', 'none recorded')}"),
        ]
        await message.edit_text("\n".join(lines), parse_mode=None)

    # ----------------------- GROUP AUTO-REPLY ------------------------ #

    async def _gc_auto_reply_impl(client: Client, message: Message):
        data = await load_data()
        user_config = data.get("users", {}).get(owner_id_str, {})

        if not user_config.get("gc_enabled", False):
            return

        if not message.from_user or not message.text:
            return

        sender_id = str(message.from_user.id)

        if sender_id in user_config.get("gc_ignored", []):
            return

        # Trigger gate: mention/reply-to-me by default; GC_TRIGGER=all replies
        # to every human text message (documented as the spammy option).
        if GC_TRIGGER != "all":
            me = await _get_me()
            if not _is_mentioned(message, me):
                return

        now = time.time()

        if now < data.get("gc_rate_limited_until", {}).get(sender_id, 0):
            return

        last_time = data.get("gc_last_msg_time", {}).get(sender_id, 0)
        if now - last_time < GC_COOLDOWN_SECONDS:
            return

        def _set_cooldown(d):
            d.setdefault("gc_last_msg_time", {})[sender_id] = now

        await update_data(_set_cooldown)

        chat_key = str(message.chat.id)
        gc_history = data.get("gc_history", {}).get(chat_key, [])
        persona = data.get("persona", DEFAULT_PERSONA)

        try:
            await client.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        # Prefix the speaker's name so the AI knows who's talking in the group.
        speaker = _display_name(message.from_user)
        ai_input = f"{speaker}: {message.text}"

        reply_text, retry_after, is_real, quota_model = await generate_ai_reply(
            persona, gc_history, ai_input, api_key
        )

        if retry_after:
            asyncio.create_task(_notify_gc_quota(quota_model or OPENROUTER_MODEL))

            def _set_rl(d):
                d.setdefault("gc_rate_limited_until", {})[sender_id] = time.time() + retry_after
            await update_data(_set_rl)

        try:
            await message.reply_text(reply_text)
        except Exception as e:
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
            print(f"[gcchat] reply send failed: {err}")
            await _store_error(err, kind="reply_send")
            return

        if not is_real:
            await _store_error(f"error reply: {reply_text}", kind="api_error")
            return

        def _append_history(d):
            h = d.setdefault("gc_history", {}).setdefault(chat_key, [])
            h.append({"role": "user", "text": ai_input})
            h.append({"role": "assistant", "text": reply_text})
            d["gc_history"][chat_key] = h[-(GC_MAX_HISTORY_TURNS * 2):]

        await update_data(_append_history)

    @user_client.on_message(
        filters.group
        & ~filters.me
        & ~filters.bot
        & ~filters.service
        & ~filters.command(_GC_COMMANDS, prefixes=[".", "!", "/"])
    )
    async def gc_auto_reply(client: Client, message: Message):
        # HARD GUARD — same pattern as aichat: a crash here must never escape
        # the handler (it would kill the reply and only print an easy-to-miss
        # dispatcher log). Every GC crash is stored for .gcdiag.
        try:
            await _gc_auto_reply_impl(client, message)
        except Exception as e:
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
            print(f"[gcchat] GC handler CRASH: {err}")
            await _store_error(err, kind="crash")
