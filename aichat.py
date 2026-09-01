import asyncio
import logging
import os
import time

import requests
from pyrogram import Client, filters
from pyrogram.types import Message

from storage import DEFAULT_PERSONA, load_data, update_data

log = logging.getLogger(__name__)

# --------------------------- CONFIG --------------------------- #

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_FALLBACK_MODEL = os.getenv(
    "OPENROUTER_FALLBACK_MODEL", "google/gemini-2.0-flash-001"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"

MAX_HISTORY_TURNS = 6
COOLDOWN_SECONDS = 5
REQUEST_TIMEOUT = 20

# Owner notification cooldown after a quota-429 (hours).
QUOTA_NOTIFY_COOLDOWN_HOURS = int(os.getenv("QUOTA_NOTIFY_COOLDOWN_HOURS", "6"))

# --------------------------- CREDITS ----------------------------- #

def _get_openrouter_credits_sync(api_key: str):
    """Fetch OpenRouter balance. Returns (ok, remaining_or_error)."""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(CREDITS_URL, headers=headers, timeout=15)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    data = resp.json().get("data", {})
    total = float(data.get("total_credits", 0) or 0)
    usage = float(data.get("total_usage", 0) or 0)
    return True, {"total": round(total, 4), "used": round(usage, 4),
                  "remaining": round(total - usage, 4)}


async def get_openrouter_credits(api_key: str):
    return await asyncio.to_thread(_get_openrouter_credits_sync, api_key)


def _credits_text(credits: dict) -> str:
    return (
        f"• **Total:** `{credits['total']}`\n"
        f"• **Used:** `{credits['used']}`\n"
        f"• **Remaining:** `{credits['remaining']}`"
    )


# --------------------------- AI CALL ----------------------------- #

def _openrouter_error_text(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if isinstance(err, str):
            return err
        if payload.get("message"):
            return str(payload["message"])
    except Exception:
        pass
    body = (resp.text or "").strip().replace("\n", " ")
    return body[:300] if body else f"HTTP {resp.status_code}"


def _post_openrouter(payload: dict, headers: dict) -> requests.Response:
    return requests.post(
        OPENROUTER_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
    )


# Returns (text, retry_after, is_real_reply)
async def generate_ai_reply(persona: str, history: list, user_message: str, api_key: str) -> tuple:
    if not api_key:
        return ("AI is not configured. Please set OPENROUTER_API_KEY first. 🙏", None, False)

    messages = [{"role": "system", "content": persona}]
    for turn in history:
        role = "assistant" if turn.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "RAUSHAN Userbot",
    }
    # Only send a referer if explicitly configured (avoid leaking a real
    # handle by default).
    referer = os.getenv("OPENROUTER_REFERER", "")
    if referer:
        headers["HTTP-Referer"] = referer

    models = [OPENROUTER_MODEL]
    if OPENROUTER_FALLBACK_MODEL and OPENROUTER_FALLBACK_MODEL not in models:
        models.append(OPENROUTER_FALLBACK_MODEL)

    last_user_error = "Currently busy, will respond in a bit! 🙏"

    for model in models:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 200,
            "temperature": 0.9,
        }
        for attempt in range(2):
            try:
                resp = await asyncio.to_thread(_post_openrouter, payload, headers)

                if resp.status_code == 429:
                    retry_after = 60
                    try:
                        retry_after = int(resp.headers.get("Retry-After", retry_after))
                    except (TypeError, ValueError):
                        pass
                    if model != models[-1]:
                        # 429 on the primary model is often a model-specific
                        # rate limit, NOT global quota exhaustion. Try the
                        # fallback model before declaring quota death.
                        log.warning("429 from %s, trying fallback model", model)
                        break
                    log.warning("429 from OpenRouter model=%s (final)", model)
                    return (
                        "Quota limit reached, please try again in a bit! 🙏",
                        retry_after,
                        False,
                    )

                if resp.status_code >= 400:
                    detail = _openrouter_error_text(resp)
                    log.warning("OpenRouter %s model=%s: %s", resp.status_code, model, detail)
                    if resp.status_code in (400, 404) and model != models[-1]:
                        break  # try fallback model
                    if resp.status_code >= 500:
                        continue
                    return (last_user_error, None, False)

                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                if model != OPENROUTER_MODEL:
                    log.info("fallback model used: %s", model)
                return (text, None, True)

            except requests.exceptions.Timeout:
                log.warning("timeout model=%s attempt=%s", model, attempt + 1)
                last_user_error = "Service is slow right now, please resend your message. 🙏"
                continue
            except requests.exceptions.RequestException as e:
                log.warning("request error model=%s: %s", model, e)
                continue
            except (KeyError, IndexError, TypeError, ValueError) as e:
                log.warning("bad OpenRouter payload: %s", e)
                return ("Could not understand that, please try again. 🙏", None, False)

    return (last_user_error, None, False)


# ---------------------- DYNAMIC REGISTER FUNCTION ------------------ #

def register_ai_handler(user_client: Client, owner_id: str, api_key: str):
    owner_id_str = str(owner_id)

    async def _resolve_target(client: Client, message: Message):
        if message.reply_to_message and message.reply_to_message.from_user:
            return message.reply_to_message.from_user
        if len(message.command) >= 2:
            arg = message.command[1]
            try:
                return await client.get_users(arg)
            except Exception:
                return None
        return None

    def _name(target) -> str:
        return (
            target.first_name
            or (f"@{target.username}" if target.username else str(target.id))
        )

    async def _notify_owner_quota(model: str):
        """Notify the account owner (Saved Messages) that AI quota is over.

        Fires at most once per QUOTA_NOTIFY_COOLDOWN_HOURS, tracked in
        data.json under users[owner_id]["quota_notify_until"].
        """
        now = time.time()

        def _check_cooldown(d):
            u = d.setdefault("users", {}).setdefault(owner_id_str, {})
            if now < u.get("quota_notify_until", 0):
                return False
            u["quota_notify_until"] = now + QUOTA_NOTIFY_COOLDOWN_HOURS * 3600
            return True

        allowed = await update_data(_check_cooldown)
        if not allowed:
            return

        lines = [
            "🚨 **AI Credits / Quota Exhausted** 🚨",
            "",
            "OpenRouter returned **429 (quota limit)** while auto-replying.",
            f"• **Model:** `{model}`",
            f"• **Time:** `{time.strftime('%Y-%m-%d %H:%M:%S')}`",
            "",
            f"Next quota alert in `{QUOTA_NOTIFY_COOLDOWN_HOURS}h` "
            f"(use `.aicredits` to check balance).",
        ]

        ok, credits = await get_openrouter_credits(api_key)
        if ok:
            lines.insert(6, "**Balance:**")
            lines.insert(7, _credits_text(credits))
            lines.insert(8, "")
        else:
            lines.insert(6, f"Balance check failed: `{credits}`")
            lines.insert(7, "")

        try:
            # "me" = Saved Messages of the userbot account itself.
            await user_client.send_message("me", "\n".join(lines))
        except Exception as e:
            log.warning("quota notify failed: %s", e)

    # -------------------------- COMMANDS ---------------------------- #

    @user_client.on_message(filters.me & filters.command("aicredits", prefixes=[".", "!", "/"]))
    async def aicredits_cmd(client: Client, message: Message):
        if not api_key:
            await message.edit_text("❌ `OPENROUTER_API_KEY` is not set.")
            return

        await message.edit_text("🔄 Fetching OpenRouter balance...")

        ok, result = await get_openrouter_credits(api_key)
        if ok:
            await message.edit_text(
                "💳 **OpenRouter Balance**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{_credits_text(result)}\n"
                f"• **Model:** `{OPENROUTER_MODEL}`"
            )
        else:
            await message.edit_text(f"❌ Balance check failed: `{result}`")

    @user_client.on_message(filters.me & filters.command("aichat", prefixes=[".", "!", "/"]))
    async def aichat_status(client: Client, message: Message):
        data = await load_data()
        user_config = data.get("users", {}).get(owner_id_str, {})
        status = "ON ✅" if user_config.get("ai_enabled", True) else "OFF ❌"
        key_status = "Set ✅" if api_key else "Missing ❌"
        blocked_count = len(data.get("blocked", []))
        persona = data.get("persona", DEFAULT_PERSONA)

        cmd_help = (
            "🤖 **AI Chat Control Panel**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• **Status:** {status}\n"
            f"• **API Key:** {key_status}\n"
            f"• **Model:** `{OPENROUTER_MODEL}`\n"
            f"• **Blocked Users:** {blocked_count}\n\n"
            "📌 **Available Commands:**\n"
            "• `.aichat` — Show this help menu & status\n"
            "• `.aichaton` — Turn AI ON globally\n"
            "• `.aichatoff` — Turn AI OFF globally\n"
            "• `.aichatoff <id/username>` — Turn AI OFF for target user\n"
            "• `.aichatunblock <id/username>` — Turn AI ON for target user\n"
            "• `.aichatreset <id/username>` — Reset chat history for target user\n"
            "• `.setpersona <text>` — Set custom personality prompt\n"
            "• `.aicredits` — Check OpenRouter balance"
        )
        await message.edit_text(cmd_help)

    @user_client.on_message(filters.me & filters.command("aichaton", prefixes=[".", "!", "/"]))
    async def aichat_on(client: Client, message: Message):
        target = await _resolve_target(client, message)

        if target is None:
            def _global_on(d):
                d.setdefault("users", {}).setdefault(owner_id_str, {})["ai_enabled"] = True
            await update_data(_global_on)
            await message.edit_text("✅ AI Auto-Reply turned **ON** globally.")
            return

        uid = str(target.id)

        def _unblock(d):
            blocked = d.get("blocked", [])
            if uid in blocked:
                blocked.remove(uid)

        await update_data(_unblock)
        await message.edit_text(f"✅ AI Auto-Reply turned **ON** for **{_name(target)}**.")

    @user_client.on_message(filters.me & filters.command("aichatoff", prefixes=[".", "!", "/"]))
    async def aichat_off(client: Client, message: Message):
        target = await _resolve_target(client, message)

        if target is None:
            def _global_off(d):
                d.setdefault("users", {}).setdefault(owner_id_str, {})["ai_enabled"] = False
            await update_data(_global_off)
            await message.edit_text("❌ AI Auto-Reply turned **OFF** globally.")
            return

        uid = str(target.id)

        def _block(d):
            blocked = d.setdefault("blocked", [])
            if uid not in blocked:
                blocked.append(uid)

        await update_data(_block)
        await message.edit_text(f"🚫 AI Auto-Reply disabled for **{_name(target)}**.")

    @user_client.on_message(filters.me & filters.command("aichatunblock", prefixes=[".", "!", "/"]))
    async def aichat_unblock(client: Client, message: Message):
        target = await _resolve_target(client, message)
        if target is None:
            await message.edit_text("Usage: `.aichatunblock <userid/username>`")
            return

        uid = str(target.id)

        def _unblock(d):
            blocked = d.get("blocked", [])
            if uid in blocked:
                blocked.remove(uid)

        await update_data(_unblock)
        await message.edit_text(f"✅ AI Auto-Reply unblocked for **{_name(target)}**.")

    @user_client.on_message(filters.me & filters.command("aichatreset", prefixes=[".", "!", "/"]))
    async def aichat_reset(client: Client, message: Message):
        target = await _resolve_target(client, message)
        if target is None:
            await message.edit_text("Usage: `.aichatreset <userid/username>`")
            return

        uid = str(target.id)

        def _reset(d):
            # History is per userbot account now.
            d.get("history", {}).get(owner_id_str, {}).pop(uid, None)

        await update_data(_reset)
        await message.edit_text(f"🧹 Cleared chat history for **{_name(target)}**.")

    @user_client.on_message(filters.me & filters.command("setpersona", prefixes=[".", "!", "/"]))
    async def set_persona(client: Client, message: Message):
        if len(message.command) < 2:
            await message.edit_text("Usage: `.setpersona <text>`")
            return

        persona_text = message.text.split(None, 1)[1]
        await update_data(lambda d: d.update(persona=persona_text))
        await message.edit_text("✅ AI Persona updated.")
        # Echo raw without markdown parsing so backticks can't break rendering.
        await message.reply(f"Current persona:\n{persona_text}", parse_mode=None)

    # ------------------------ DM AUTO-REPLY -------------------------- #

    @user_client.on_message(
        filters.private
        & ~filters.me
        & ~filters.bot
        & ~filters.service
        & ~filters.command(
            ["aichat", "aichaton", "aichatoff", "aichatunblock", "aichatreset",
             "setpersona", "aicredits"],
            prefixes=[".", "!", "/"],
        )
    )
    async def ai_auto_reply(client: Client, message: Message):
        data = await load_data()
        user_config = data.get("users", {}).get(owner_id_str, {})

        if not user_config.get("ai_enabled", True):
            return

        # Guard non-text input (sticker/photo/video in DM)
        if not message.from_user or not message.text:
            return

        user_id = str(message.from_user.id)

        if user_id in data.get("blocked", []):
            return

        now = time.time()

        # Post-429 per-user cooldown (global across accounts).
        if now < data.get("rate_limited_until", {}).get(user_id, 0):
            return

        # Per-user cooldown: check + set atomically so two near-simultaneous
        # DMs can't both pass the check.
        def _check_cooldown(d):
            per_owner = d.setdefault("last_msg_time", {}).setdefault(owner_id_str, {})
            if now - per_owner.get(user_id, 0) < COOLDOWN_SECONDS:
                return False
            per_owner[user_id] = now
            return True

        if not await update_data(_check_cooldown):
            return

        # History is per userbot account, per sender.
        chat_history = data.get("history", {}).get(owner_id_str, {}).get(user_id, [])
        persona = data.get("persona", DEFAULT_PERSONA)

        try:
            await client.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        reply_text, retry_after, is_real = await generate_ai_reply(
            persona, chat_history, message.text, api_key
        )

        if retry_after:
            # 429 = quota exhausted: alert the owner (cooldown-protected).
            asyncio.create_task(_notify_owner_quota(OPENROUTER_MODEL))

            def _set_rl(d):
                d.setdefault("rate_limited_until", {})[user_id] = time.time() + retry_after
            await update_data(_set_rl)

        try:
            await message.reply_text(reply_text)
        except Exception as e:
            log.warning("reply error: %s", e)
            return

        # Error strings ("Currently busy...") must NOT pollute chat history
        if not is_real:
            return

        def _append_history(d):
            per_owner = d.setdefault("history", {}).setdefault(owner_id_str, {})
            h = per_owner.setdefault(user_id, [])
            h.append({"role": "user", "text": message.text})
            h.append({"role": "assistant", "text": reply_text})
            per_owner[user_id] = h[-(MAX_HISTORY_TURNS * 2):]

        await update_data(_append_history)
