"""Patch Pyrogram so newer Telegram channel IDs don't crash update handling.

Vanilla Pyrogram 2.0 still treats MIN_CHANNEL_ID as -1002147483647, so IDs
like -1003669112369 raise ValueError('Peer id invalid') inside handle_updates.
"""


def apply_pyrogram_peer_patch() -> None:
    try:
        from pyrogram import utils as pyro_utils
        from pyrogram.client import Client
    except Exception as e:
        print(f"[pyro_patch] skip: {e}")
        return

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

    orig_handle_updates = Client.handle_updates

    async def handle_updates_safe(self, updates):
        # FIX: only guard the known peer-resolution failures.
        # All other exceptions now propagate so real bugs aren't silently lost.
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
    print("[pyro_patch] applied peer-id + handle_updates guards")
