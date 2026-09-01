import asyncio
import json
import os
import time

import requests
from pyrogram import Client, filters
from pyrogram.types import Message

# --------------------------- CONFIG --------------------------- #

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-chat-latest")
OPENROUTER_FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK_MODEL", "openai/gpt-4o-mini")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_PERSONA = (
    "You are a friendly Telegram assistant. Respond naturally and concisely "
    "like a helpful friend."
)

MAX_HISTORY_TURNS = 6
COOLDOWN_SECONDS = 5

# ------------------------- STORAGE ----------------------------- #

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(DATA_DIR, "data.json")

_default_data = {
    "enabled": True,
    "persona": DEFAULT_PERSONA,
    "users": {},
    "history": {},
    "last_msg_time": {},
    "blocked": [],
    "rate_limited_until": 0,
}


def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        save_data(_default_data)
        return json.loads(json.dumps(_default_data))
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _default_data.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError):
        save_data(_default_data)
        return json.loads(json.dumps(_default_data))


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def _friendly_http_error(status: int, detail: str) -> str:
    detail_l = (detail or "").lower()
    if status in (401, 403) or "auth" in detail_l or "api key" in detail_l:
        return "AI API key is invalid or missing. Check OPENROUTER_API_KEY. 🙏"
    if status == 402 or "credit" in detail_l or "quota" in detail_l or "balance" in detail_l:
        return "OpenRouter credits are empty. Top up and try again. 🙏"
    if status == 404 or "not found" in detail_l or "no such model" in detail_l:
        return f"AI model is unavailable (`{OPENROUTER_MODEL}`). 🙏"
    if status == 400:
        return f"AI request was rejected: {detail[:180]} 🙏"
    return f"Currently busy ({status}), will respond in a bit! 🙏"


def _post_openrouter(payload: dict, headers: dict) -> requests.Response:
    return requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=45)


async def generate_ai_reply(persona: str, history: list, user_message: str, api_key: str) -> tuple:
    if not api_key:
        return ("AI is not configured. Please set OPENROUTER_API_KEY first. 🙏", None)

    messages = [{"role": "system", "content": persona}]
    for turn in history:
        role = "assistant" if turn.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/nonsecularman",
        "X-Title": "RAUSHAN Userbot",
    }

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
                    print(f"[aichat] 429 from OpenRouter model={model}")
                    return (
                        "Quota limit reached, please try again in a bit! 🙏",
                        retry_after,
                    )

                if resp.status_code >= 400:
                    detail = _openrouter_error_text(resp)
                    print(f"[aichat] OpenRouter {resp.status_code} model={model}: {detail}")
                    last_user_error = _friendly_http_error(resp.status_code, detail)
                    # Try fallback model on model/request errors.
                    if resp.status_code in (400, 404) and model != models[-1]:
                        break
                    if resp.status_code >= 500:
                        continue
                    return (last_user_error, None)

                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                if model != OPENROUTER_MODEL:
                    print(f"[aichat] fallback model used: {model}")
                return (text, None)
            except requests.exceptions.Timeout:
                print(f"[aichat] timeout model={model} attempt={attempt + 1}")
                last_user_error = "Service is slow right now, please resend your message. 🙏"
                continue
            except requests.exceptions.RequestException as e:
                print(f"[aichat] request error model={model}: {e}")
                last_user_error = "Currently busy, will respond in a bit! 🙏"
                continue
            except (KeyError, IndexError, TypeError, ValueError) as e:
                print(f"[aichat] bad OpenRouter payload: {e}")
                return ("Could not understand that, please try again. 🙏", None)

    return (last_user_error, None)


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

    @user_client.on_message(filters.me & filters.command("aichat", prefixes=[".", "!", "/"]))
    async def aichat_status(client: Client, message: Message):
        data = load_data()
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
            f"• **Blocked Users:** {blocked_count}\n"
            f"• **Persona:** `{persona}`\n\n"
            "📌 **Available Commands:**\n"
            "• `.aichat` — Show this help menu & status\n"
            "• `.aichaton` — Turn AI ON globally\n"
            "• `.aichatoff` — Turn AI OFF globally\n"
            "• `.aichatoff <id/username>` — Turn AI OFF for target user\n"
            "• `.aichatunblock <id/username>` — Turn AI ON for target user\n"
            "• `.aichatreset <id/username>` — Reset chat history for target user\n"
            "• `.setpersona <text>` — Set custom personality prompt"
        )
        await message.edit_text(cmd_help)

    @user_client.on_message(filters.me & filters.command("aichaton", prefixes=[".", "!", "/"]))
    async def aichat_on(client: Client, message: Message):
        data = load_data()
        target = await _resolve_target(client, message)

        if target is None:
            data.setdefault("users", {}).setdefault(owner_id_str, {})["ai_enabled"] = True
            save_data(data)
            await message.edit_text("✅ AI Auto-Reply turned **ON** globally.")
            return

        uid = str(target.id)
        blocked = data.get("blocked", [])
        if uid in blocked:
            blocked.remove(uid)
            save_data(data)
        name = target.first_name or (f"@{target.username}" if target.username else uid)
        await message.edit_text(f"✅ AI Auto-Reply turned **ON** for **{name}**.")

    @user_client.on_message(filters.me & filters.command("aichatoff", prefixes=[".", "!", "/"]))
    async def aichat_off(client: Client, message: Message):
        data = load_data()
        target = await _resolve_target(client, message)

        if target is None:
            data.setdefault("users", {}).setdefault(owner_id_str, {})["ai_enabled"] = False
            save_data(data)
            await message.edit_text("❌ AI Auto-Reply turned **OFF** globally.")
            return

        uid = str(target.id)
        blocked = data.setdefault("blocked", [])
        if uid not in blocked:
            blocked.append(uid)
            save_data(data)
        name = target.first_name or (f"@{target.username}" if target.username else uid)
        await message.edit_text(f"🚫 AI Auto-Reply disabled for **{name}**.")

    @user_client.on_message(filters.me & filters.command("aichatunblock", prefixes=[".", "!", "/"]))
    async def aichat_unblock(client: Client, message: Message):
        data = load_data()
        target = await _resolve_target(client, message)
        if target is None:
            await message.edit_text("Usage: `.aichatunblock <userid/username>`")
            return

        uid = str(target.id)
        blocked = data.get("blocked", [])
        if uid in blocked:
            blocked.remove(uid)
            save_data(data)
        name = target.first_name or (f"@{target.username}" if target.username else uid)
        await message.edit_text(f"✅ AI Auto-Reply unblocked for **{name}**.")

    @user_client.on_message(filters.me & filters.command("aichatreset", prefixes=[".", "!", "/"]))
    async def aichat_reset(client: Client, message: Message):
        data = load_data()
        target = await _resolve_target(client, message)
        if target is None:
            await message.edit_text("Usage: `.aichatreset <userid/username>`")
            return

        uid = str(target.id)
        data.get("history", {}).pop(uid, None)
        save_data(data)
        name = target.first_name or (f"@{target.username}" if target.username else uid)
        await message.edit_text(f"🧹 Cleared chat history for **{name}**.")

    @user_client.on_message(filters.me & filters.command("setpersona", prefixes=[".", "!", "/"]))
    async def set_persona(client: Client, message: Message):
        if len(message.command) < 2:
            await message.edit_text("Usage: `.setpersona <text>`")
            return

        persona_text = message.text.split(None, 1)[1]
        data = load_data()
        data["persona"] = persona_text
        save_data(data)
        await message.edit_text(f"✅ AI Persona updated:\n\n`{persona_text}`")

    @user_client.on_message(
        filters.private
        & ~filters.me
        & ~filters.bot
        & ~filters.service
        & ~filters.command(
            ["aichat", "aichaton", "aichatoff", "aichatunblock", "aichatreset", "setpersona"],
            prefixes=[".", "!", "/"],
        )
    )
    async def ai_auto_reply(client: Client, message: Message):
        data = load_data()
        user_config = data.get("users", {}).get(owner_id_str, {})

        if not user_config.get("ai_enabled", True):
            return

        if not message.from_user or not message.text:
            return

        user_id = str(message.from_user.id)

        if user_id in data.get("blocked", []):
            return

        # Paid-photo trigger words skip AI so only the saved post is sent.
        try:
            from sendphoto import account_has_paid_post, is_send_trigger

            if is_send_trigger(message.text) and account_has_paid_post(owner_id_str):
                return
        except Exception:
            pass

        now = time.time()

        if now < data.get("rate_limited_until", 0):
            return

        last_time = data.setdefault("last_msg_time", {}).get(user_id, 0)
        if now - last_time < COOLDOWN_SECONDS:
            return
        data["last_msg_time"][user_id] = now

        chat_history = data.setdefault("history", {}).get(user_id, [])
        persona = data.get("persona", DEFAULT_PERSONA)

        try:
            await client.send_chat_action(message.chat.id, "typing")
        except Exception:
            pass

        reply_text, retry_after = await generate_ai_reply(persona, chat_history, message.text, api_key)

        if retry_after:
            data["rate_limited_until"] = time.time() + retry_after
            save_data(data)

        try:
            await message.reply_text(reply_text)
        except Exception as e:
            print(f"[aichat.py] Reply error: {e}")
            return

        if retry_after:
            return

        chat_history.append({"role": "user", "text": message.text})
        chat_history.append({"role": "assistant", "text": reply_text})
        data["history"][user_id] = chat_history[-(MAX_HISTORY_TURNS * 2):]

        save_data(data)
