"""When the screener's data is refreshed, as one rule the app can ask.

The Cloudflare Worker dispatches data-refresh.yml every 30 minutes and market-breadth.yml every
hour, only from Sunday 22:00 to Friday 17:00 New York time and never 17:30-21:30 on any day (owner,
2026-10-21). The same window lives in two other places, which cannot import this module: the
Worker's own code (deployed by hand in the Cloudflare dashboard) and market-breadth.yml's gate
(`slots` without hours 18-21, `window: "SUN:22-FRI:17"`). checks/test_refresh_schedule.py holds this
module to the gate's real decisions, hour by hour, across both daylight-saving changes.

Pure functions, no Streamlit, so the check can import it.
"""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Beyond this the walk below is pointless: a week-old snapshot is stale whatever the schedule says.
_MAX_WALK = timedelta(days=8)


def in_refresh_window(when):
    """Is the HOUR containing `when` (any tz-aware datetime) one the schedule refreshes in?

    Sunday from 22:00; Monday-Thursday every hour but 18-21; Friday until the 17:00 hour; Saturday
    never. Hour by hour, like the breadth gate's slots: the 17:00 hour counts (the Worker's last
    tick before the quiet gap is 17:00 sharp)."""
    t = when.astimezone(ET)
    day, hour = t.weekday(), t.hour          # Monday = 0
    if day == 5:
        return False
    if day == 6:
        return hour >= 22
    if day == 4:
        return hour <= 17
    return hour <= 17 or hour >= 22


def refresh_hours_since(start, now):
    """Hours between `start` and `now` (tz-aware) that fell inside the refresh window.

    The sidebar warns on THIS, not on the snapshot's raw age: from Friday 17:00 to Sunday 22:00,
    and every evening 17:30-21:30, nothing is scheduled to refresh, so a 50-hour-old Monday-morning
    snapshot is only a fault if the window's own hours have gone by without one."""
    if now <= start:
        return 0.0
    if now - start > _MAX_WALK:
        return (now - start).total_seconds() / 3600
    total = 0.0
    t = start.astimezone(timezone.utc)
    end = now.astimezone(timezone.utc)
    while t < end:
        next_hour = (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        chunk_end = min(next_hour, end)
        if in_refresh_window(t):
            total += (chunk_end - t).total_seconds() / 3600
        t = chunk_end
    return total
