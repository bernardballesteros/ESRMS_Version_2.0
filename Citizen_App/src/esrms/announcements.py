"""Display rules for announcements returned by public.citizen_announcements().

Visibility (published, active, in its window, right audience) is decided in the database.
The app re-checks the time window so a post that expires while the screen is open drops off.
"""
from datetime import datetime, timezone

CATEGORY_STYLE = {
    # category: (label, colour key in ui/theme.py, icon)
    "emergency": ("EMERGENCY", "red", "WARNING_AMBER"),
    "important": ("IMPORTANT", "amber", "PRIORITY_HIGH"),
    "advisory": ("ADVISORY", "cyan", "INFO_OUTLINE"),
    "general": ("GENERAL", "slate", "CAMPAIGN"),
    "maintenance": ("MAINTENANCE", "violet", "BUILD"),
}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def parse_ts(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_live(row, now=None):
    now = now or datetime.now(timezone.utc)
    published = parse_ts(row.get("published_at"))
    expires = parse_ts(row.get("expires_at"))
    return published is not None and published <= now and (expires is None or expires > now)


def is_urgent(row):
    return row.get("category") == "emergency" or row.get("priority") in ("critical", "high")


def sort_rows(rows):
    """Pinned, then critical/high first, newest first within each (same as the dispatcher viewer)."""
    def key(r):
        ts = parse_ts(r.get("published_at"))
        return (0 if r.get("is_pinned") else 1, PRIORITY_RANK.get(r.get("priority"), 9),
                -(ts.timestamp() if ts else 0))
    return sorted(rows, key=key)


def visible(rows, now=None):
    return sort_rows([r for r in rows if is_live(r, now)])


def style(row):
    return CATEGORY_STYLE.get(row.get("category"), CATEGORY_STYLE["general"])
