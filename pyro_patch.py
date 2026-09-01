"""Patch Pyrogram for modern Telegram servers.

1. Newer channel IDs don't crash update handling (MIN/MAX peer-id constants).
2. Unknown TL constructors (Telegram layers newer than the installed schema)
   are dropped with a one-time log line instead of traceback spam.
3. handle_updates guards only swallow known peer-resolution failures.
"""

import logging

log = logging.getLogger(__name__)

# Per-category one-time suppression flags. Sharing a single flag meant one
# weird constructor permanently silenced ALL update errors, including real
# bugs. Unknown-constructor ids are also logged individually (once each).
_logged_unknown_constructors: set = set()
_logged_missing_peers: set = set()


def apply_pyrogram_peer_patch() -> None:
    try:
        from pyrogram import utils as pyro_utils
        from pyrogram.client import Client
    except Exception as e:
        log.warning("pyro_patch skipped: %s", e)
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
        ("MAX_CHANNEL_ID", -100000000000000),
        ("MIN_CHAT_ID", -999999999999),
        ("MAX_CHAT_ID", -999),
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
                    cid = str(e)
                    if cid not in _logged_unknown_constructors:
                        _logged_unknown_constructors.add(cid)
                        log.warning(
                            "dropped update with unknown TL constructor (%s). "
                            "This constructor id is suppressed from now on.",
                            cid,
                        )
                    return
                raise
            except KeyError as e:
                # KeyError during deserialization = constructor id missing
                # from the installed TL schema. Track per-id, not globally.
                key = str(e)
                if key not in _logged_unknown_constructors:
                    _logged_unknown_constructors.add(key)
                    log.warning(
                        "dropped update with unknown constructor id %s "
                        "(Telegram layer newer than installed schema). "
                        "This id is suppressed from now on.",
                        key,
                    )
                return

        Session.handle_packet = handle_packet_safe
    except Exception as e:
        log.warning("session guard skipped: %s", e)

    # ---------------- 3. handle_updates guards ---------------- #

    orig_handle_updates = Client.handle_updates

    async def handle_updates_safe(self, updates):
        # Only guard known peer-resolution failures; everything else
        # propagates so real bugs aren't silently lost.
        try:
            return await orig_handle_updates(self, updates)
        except ValueError as e:
            if "Peer id invalid" in str(e):
                log.warning("ignored invalid peer: %s", e)
                return None
            raise
        except KeyError as e:
            key = str(e)
            if key not in _logged_missing_peers:
                _logged_missing_peers.add(key)
                log.warning("ignored missing peer: %s (further same-key errors suppressed)", e)
            return None

    Client.handle_updates = handle_updates_safe
    log.info("pyro_patch: applied peer-id + constructor + handle_updates guards")
