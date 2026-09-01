"""Central storage: locked, atomic read-modify-write on data.json.

• The asyncio.Lock is created lazily on FIRST async use, never at import —
  this removes any chance of the lock binding to a different event loop than
  the running one (a classic silent deadlock: handlers await load_data() and
  freeze forever, which looks exactly like "nothing replies").
• data.json is written with 0600 permissions — it contains full-control
  session strings and the OpenRouter key.
"""

import asyncio
import json
import os
import tempfile
from copy import deepcopy

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(DATA_DIR, "data.json")

DEFAULT_PERSONA = (
    "You are a friendly Telegram assistant. Respond naturally and concisely "
    "like a helpful friend."
)

_default_data = {
    "enabled": True,
    "persona": DEFAULT_PERSONA,
    "users": {},
    "history": {},
    "last_msg_time": {},
    "rate_limited_until": {},  # per-user: {user_id: unix_ts}
    "blocked": [],
    "last_ai_error": {},       # diagnostics: last DM crash / error reply
}

# Lazy singleton: created on first use INSIDE the running loop.
_lock = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _write_sync(data: dict) -> None:
    """Atomic write: temp file in the same dir, then os.replace (POSIX-atomic)."""
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".data_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # sessions + API key live here
        os.replace(tmp, DATA_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_sync() -> dict:
    if not os.path.exists(DATA_FILE):
        _write_sync(deepcopy(_default_data))
        return deepcopy(_default_data)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt file: back it up instead of silently destroying it,
        # so sessions can be manually recovered.
        try:
            os.replace(DATA_FILE, DATA_FILE + ".corrupt")
            print(f"[storage] corrupt data.json backed up -> {DATA_FILE}.corrupt")
        except OSError:
            pass
        _write_sync(deepcopy(_default_data))
        return deepcopy(_default_data)

    for k, v in _default_data.items():
        data.setdefault(k, v)
    return data


async def load_data() -> dict:
    """Load data for read-only checks. For mutations use update_data()."""
    async with _get_lock():
        return _read_sync()


async def save_data(data: dict) -> None:
    """Overwrite the whole store (rarely needed directly; prefer update_data)."""
    async with _get_lock():
        await asyncio.to_thread(_write_sync, data)


async def update_data(mutation):
    """Atomic read-modify-write.

    `mutation` is a sync callable: mutation(data_dict) -> optional result.
    The mutated dict is persisted before the lock is released.
    """
    async with _get_lock():
        data = _read_sync()
        result = mutation(data)
        await asyncio.to_thread(_write_sync, data)
        return result
