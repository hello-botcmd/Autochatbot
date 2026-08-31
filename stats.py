import platform
import time
import psutil

START_TIME = time.time()

def get_formatted_uptime() -> str:
    uptime_seconds = int(time.time() - START_TIME)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_stats_text(connected_clients: dict, data: dict) -> str:
    total_connected = len(connected_clients)
    total_saved_users = len(data.get("users", {}))
    ai_active_count = sum(
        1 for u in data.get("users", {}).values() if u.get("ai_enabled", True)
    )
    blocked_users_count = len(data.get("blocked", []))

    cpu_usage = psutil.cpu_percent(interval=None)
    ram_info = psutil.virtual_memory()
    ram_usage = ram_info.percent
    ram_used_mb = int(ram_info.used / (1024 * 1024))
    ram_total_mb = int(ram_info.total / (1024 * 1024))

    uptime_str = get_formatted_uptime()
    system_os = platform.system()

    text = (
        "📊 **Advanced System & Bot Statistics**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 **Bot Status**\n"
        f"• **Active Connected Sessions:** `{total_connected}`\n"
        f"• **Total Saved Accounts:** `{total_saved_users}`\n"
        f"• **AI Auto-Reply Enabled:** `{ai_active_count}` Accounts\n"
        f"• **Global Blocked Users:** `{blocked_users_count}`\n\n"
        "🖥️ **Server Performance**\n"
        f"• **Uptime:** `{uptime_str}`\n"
        f"• **CPU Usage:** `{cpu_usage}%`\n"
        f"• **RAM Usage:** `{ram_usage}%` (`{ram_used_mb}MB / {ram_total_mb}MB`)\n"
        f"• **OS Environment:** `{system_os}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return text
