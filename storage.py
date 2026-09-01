"""Central storage: locked, atomic read-modify-write on data.json."""

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

LEGACY_KEY = "__legacy__"  # bucket that keeps pre-migration flat entries

_default_data = {
    "persona": DEFAULT_PERSONA,
    "users": {},
    "history": {},             # {owner_id: {sender_id: [turns]}}   per account
    "last_msg_time": {},       # {owner_id: {sender_id: unix_ts}}  per account
    "rate_limited_until": {},  # {sender_id: unix_ts}              global
    "blocked": [],             # global blocked senders
}

# LAZY lock — created on first use, NOT at import time.
# asyncio.Lock() at import binds to the loop current then (eagerly on Python
# <= 3.9), which can differ from the loop the app runs on. A wrong-loop lock
# makes handlers hang or raise "attached to a different loop" — silently
# killing AI replies AND post sending. Creating it inside the running loop is
# safe on every Python version regardless of import order in main.py.
_lock = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _migrate(data: dict) -> None:
    """Upgrade legacy flat {sender_id: value} stores to the per-account
    layout {owner_id: {sender_id: value}}. Idempotent; runs on every read."""
    for key in ("last_msg_time", "history"):
        store = data.get(key)
        if not isinstance(store, dict):
            data[key] = {}
            continue
        for k, v in list(store.items()):
            if isinstance(v, dict):
                continue  # already per-account
            if not isinstance(store.get(LEGACY_KEY), dict):
                store[LEGACY_KEY] = {}
            if k not in store[LEGACY_KEY]:
                store[LEGACY_KEY][k] = v
            store.pop(k, None)


def _write_sync(data: dict) -> None:
    """Atomic write: temp file in the same dir, then os.replace (POSIX-atomic)."""
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".data_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
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
        try:
            os.replace(DATA_FILE, DATA_FILE + ".corrupt")
            print(f"[storage] corrupt data.json backed up -> {DATA_FILE}.corrupt")
        except OSError:
            pass
        _write_sync(deepcopy(_default_data))
        return deepcopy(_default_data)

    for k, v in _default_data.items():
        data.setdefault(k, v)
    _migrate(data)
    return data


async def load_data() -> dict:
    """Load data for read-only checks. For mutations use update_data()."""
    async with _get_lock():
        return await asyncio.to_thread(_read_sync)


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
        data = await asyncio.to_thread(_read_sync)
        result = mutation(data)
        await asyncio.to_thread(_write_sync, data)
        return result
