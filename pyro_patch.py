"""Patch Pyrogram for modern Telegram servers.

1. Newer channel IDs don't crash update handling (MIN/MAX peer-id constants).
2. Unknown TL constructors (Telegram layers newer than the installed schema)
   are dropped with a one-time log line instead of traceback spam.
3. handle_updates guards only swallow known peer-resolution failures.
"""

_LOGGED_UNKNOWN_CONSTRUCTOR = False


def apply_pyrogram_peer_patch() -> None:
    global _LOGGED_UNKNOWN_CONSTRUCTOR

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
    # Fires BEFORE deserialization (Session.handle_packet / mtproto.unpack),
    # which is where "The server sent an unknown constructor" originates.

    try:
        from pyrogram.session import Session

        orig_handle_packet = Session.handle_packet

        async def handle_packet_safe(self, packet):
            try:
                return await orig_handle_packet(self, packet)
            except ValueError as e:
                if "unknown constructor" in str(e):
                    if not _LOGGED_UNKNOWN_CONSTRUCTOR:
                        print(
                            "[pyrogram] dropped update with unknown TL constructor "
                            "(Telegram layer newer than installed schema). "
                            "Further occurrences suppressed."
                        )
                        _LOGGED_UNKNOWN_CONSTRUCTOR = True
                    return
                raise
            except KeyError as e:
                if not _LOGGED_UNKNOWN_CONSTRUCTOR:
                    print(
                        f"[pyrogram] dropped update with unknown constructor id {e}. "
                        "Further occurrences suppressed."
                    )
                    _LOGGED_UNKNOWN_CONSTRUCTOR = True
                return

        Session.handle_packet = handle_packet_safe
    except Exception as e:
        print(f"[pyro_patch] session guard skipped: {e}")

    # ---------------- 3. handle_updates guards ---------------- #

    orig_handle_updates = Client.handle_updates

    async def handle_updates_safe(self, updates):
        # Only guard known peer-resolution failures; everything else
        # propagates so real bugs aren't silently lost.
        try:
            return await orig_handle_updates(self, updates)
        except ValueError as e:
            if "Peer id invalid" in str(e):
                print(f"[pyrogram] ignored invalid peer: {e}")
                return None
            raise
        except KeyError as e:
            print(f"[pyrogram] ignored missing peer: {e}")
            return None

    Client.handle_updates = handle_updates_safe
    print("[pyro_patch] applied peer-id + constructor + handle_updates guards")
