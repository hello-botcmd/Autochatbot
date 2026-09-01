"""Patch Pyrogram for modern Telegram servers.

1. Newer channel IDs don't crash update handling (MIN/MAX peer-id constants).
2. Unknown TL constructors (Telegram layers newer than the installed schema)
   are dropped instead of traceback spam.
3. handle_updates guards only swallow known peer-resolution failures.
4. NEW: unknown-constructor drops are COUNTED. If the count climbs fast it
   warns that the installed Pyrogram schema is too old for Telegram's current
   layer — the classic "nothing replies, no errors" failure mode. The fix is
   a current-layer fork:  pip install -U pyrofork  (drop-in, same `pyrogram`
   import; all code in this repo works unchanged).
"""

import os

_UNKNOWN_CONSTRUCTOR_DROPS = 0
_PEER_GUARD_DROPS = 0

_WARN_EVERY = int(os.getenv("PYRO_PATCH_WARN_EVERY", "50"))


def _warn_unknown(count: int) -> None:
    if count == 1 or count % _WARN_EVERY == 0:
        print(
            f"[pyrogram] dropped {count} update(s) with unknown TL constructor "
            f"(Telegram layer newer than installed schema)."
        )
        if count >= _WARN_EVERY * 10:
            print(
                "[pyrogram] ⚠️ DROP COUNT IS CLIMBING — your Pyrogram schema is too "
                "old to decode most updates, so handlers rarely/never fire "
                "(AI replies and triggers look dead with NO errors). Fix: "
                "pip install -U pyrofork"
            )


def apply_pyrogram_peer_patch() -> None:
    global _UNKNOWN_CONSTRUCTOR_DROPS, _PEER_GUARD_DROPS

    try:
        from pyrogram import utils as pyro_utils
        from pyrogram.client import Client
    except Exception as e:
        print(f"[pyro_patch] skip: {e}")
        return

    # ---------------- 1. Peer-id constants ---------------- #

    def get_peer_type(peer_id: int) -> str:
        peer_id_str = str(peer_id)
        if not peer_id_str.startswith("-"):
            return "user"
        if peer_id_str.startswith("-100"):
            return "channel"
        return "chat"

    pyro_utils.get_peer_type = get_peer_type

    for name, value in (
        ("MIN_CHANNEL_ID", -100999999999999),
        ("MIN_CHAT_ID", -999999999999),
        ("MAX_USER_ID", 999999999999),
    ):
        if hasattr(pyro_utils, name):
            setattr(pyro_utils, name, value)

    # ---------------- 2. Unknown TL constructors ---------------- #

    try:
        from pyrogram.session import Session

        orig_handle_packet = Session.handle_packet

        async def handle_packet_safe(self, packet):
            # Declared global INSIDE this nested function, else the increments
            # below make the names local -> UnboundLocalError.
            global _UNKNOWN_CONSTRUCTOR_DROPS
            try:
                return await orig_handle_packet(self, packet)
            except ValueError as e:
                if "unknown constructor" in str(e):
                    _UNKNOWN_CONSTRUCTOR_DROPS += 1
                    _warn_unknown(_UNKNOWN_CONSTRUCTOR_DROPS)
                    return
                raise
            except KeyError as e:
                _UNKNOWN_CONSTRUCTOR_DROPS += 1
                _warn_unknown(_UNKNOWN_CONSTRUCTOR_DROPS)
                return

        Session.handle_packet = handle_packet_safe
    except Exception as e:
        print(f"[pyro_patch] session guard skipped: {e}")

    # ---------------- 3. handle_updates guards ---------------- #

    orig_handle_updates = Client.handle_updates

    async def handle_updates_safe(self, updates):
        global _PEER_GUARD_DROPS
        try:
            return await orig_handle_updates(self, updates)
        except ValueError as e:
            if "Peer id invalid" in str(e):
                _PEER_GUARD_DROPS += 1
                if _PEER_GUARD_DROPS == 1 or _PEER_GUARD_DROPS % _WARN_EVERY == 0:
                    print(f"[pyrogram] peer drops={_PEER_GUARD_DROPS}: {e} "
                          f"(session hasn't cached this peer yet)")
                return None
            raise
        except KeyError as e:
            _PEER_GUARD_DROPS += 1
            if _PEER_GUARD_DROPS == 1 or _PEER_GUARD_DROPS % _WARN_EVERY == 0:
                print(f"[pyrogram] peer drops={_PEER_GUARD_DROPS}: missing peer {e}")
            return None

    Client.handle_updates = handle_updates_safe
    print("[pyro_patch] applied peer-id + constructor + handle_updates guards "
          "(drop counting enabled)")
