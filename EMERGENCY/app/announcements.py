"""Announcement rules shared by the admin dashboard and the Citizen Announcement Viewer.

Kept free of Flask and Supabase so the visibility and validation rules can be
unit tested on their own (see tests/test_announcements.py).
"""
from datetime import datetime, timezone

CATEGORIES = {
    "emergency": "Emergency",
    "important": "Important",
    "advisory": "Advisory",
    "general": "General",
    "maintenance": "Maintenance",
}
PRIORITIES = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
AUDIENCES = {
    "all_citizens": "All Citizens",
    "barangay": "Specific Barangay",
    "emergency_personnel": "Emergency Personnel",
    "selected_groups": "Selected Groups",
}
# Groups come from data the app already has: the account role and the responder branch.
GROUPS = {
    "responders": "All Responders",
    "ems": "EMS / Paramedic",
    "fire": "Fire & Rescue",
    "law": "Law Enforcement",
    "sar": "Search & Rescue",
    "organizations": "911 Dispatch Organizations",
}
STATUSES = ("draft", "scheduled", "active", "archived")
# What the dashboard shows. "expired" is derived; it is never stored.
EFFECTIVE_STATUSES = ("draft", "scheduled", "active", "expired", "archived")
PERSONNEL_ROLES = {"responder", "organization", "admin"}

TITLE_MAX = 200
MESSAGE_MAX = 10000


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """Parse an ISO 8601 timestamp into an aware UTC datetime. Returns None for blanks."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset.")
    return dt.astimezone(timezone.utc)


def effective_status(row, now=None):
    """Stored status plus the clock: scheduled posts go live and active posts expire on time."""
    now = now or now_utc()
    status = row.get("status")
    if status in ("draft", "archived"):
        return status
    published_at = parse_ts(row.get("published_at"))
    if published_at is None or published_at > now:
        return "scheduled"
    expires_at = parse_ts(row.get("expires_at"))
    if expires_at is not None and expires_at <= now:
        return "expired"
    return "active"


def status_counts(rows, now=None):
    """Counts for the status panel. Archived includes expired posts, since neither is public any more."""
    counts = {"active": 0, "scheduled": 0, "archived": 0, "draft": 0, "expired": 0, "total": len(rows)}
    for row in rows:
        s = effective_status(row, now)
        counts[s] += 1
        if s == "expired":
            counts["archived"] += 1
    return counts


def viewer_context(user, barangay=None):
    """Who is looking at the public viewer. `user` is the Flask session user dict or None.

    A barangay saved on the account wins over the one picked in the viewer.
    """
    user = user or {}
    role = user.get("role")
    groups = set()
    if role == "responder":
        groups.add("responders")
        if user.get("branch") in GROUPS:
            groups.add(user["branch"])
    elif role == "organization":
        groups.add("organizations")
    elif role == "admin":
        groups.update(GROUPS)
    chosen = (user.get("barangay") or barangay or "").strip()
    return {"role": role, "barangay": chosen or None, "groups": groups}


def _same_barangay(a, b):
    return bool(a and b) and a.strip().casefold() == b.strip().casefold()


def audience_matches(row, viewer):
    audience = row.get("target_audience")
    if viewer.get("role") == "admin":
        return True
    if audience == "all_citizens":
        return True
    if audience == "barangay":
        return _same_barangay(row.get("barangay"), viewer.get("barangay"))
    if audience == "emergency_personnel":
        return viewer.get("role") in PERSONNEL_ROLES
    if audience == "selected_groups":
        return bool(set(row.get("target_groups") or []) & viewer.get("groups", set()))
    return False


def is_publicly_visible(row, viewer, now=None):
    """The single rule for the Citizen Announcement Viewer."""
    return effective_status(row, now) == "active" and audience_matches(row, viewer)


def sort_for_viewer(rows):
    """Pinned first, then critical/high before the rest, newest first within each."""
    def key(r):
        ts = parse_ts(r.get("published_at"))
        return (
            0 if r.get("is_pinned") else 1,
            PRIORITY_RANK.get(r.get("priority"), 9),
            -(ts.timestamp() if ts else 0),
        )
    return sorted(rows, key=key)


def pick_featured(rows, now=None):
    """Pinned live post if there is one, otherwise the most urgent, newest live post."""
    live = [r for r in rows if effective_status(r, now) in ("active", "scheduled")]
    pinned = [r for r in live if r.get("is_pinned")]
    if pinned:
        return sort_for_viewer(pinned)[0]
    active = [r for r in live if effective_status(r, now) == "active"]
    return sort_for_viewer(active)[0] if active else None


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def validate(payload, now=None):
    """Turn form JSON into a row for the database, or raise ValidationError(field -> message).

    payload["action"] is "publish" or "draft". Publishing with a future publication
    time stores status "scheduled"; publishing with a past or empty time goes live now.
    """
    now = now or now_utc()
    errors = {}

    def text(name):
        value = payload.get(name)
        return value.strip() if isinstance(value, str) else ""

    title, message = text("title"), text("message")
    if not title:
        errors["title"] = "Title is required."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"Title must be {TITLE_MAX} characters or fewer."
    if not message:
        errors["message"] = "Message is required."
    elif len(message) > MESSAGE_MAX:
        errors["message"] = f"Message must be {MESSAGE_MAX} characters or fewer."

    category, priority, audience = text("category"), text("priority"), text("target_audience")
    if category not in CATEGORIES:
        errors["category"] = "Choose a category."
    if priority not in PRIORITIES:
        errors["priority"] = "Choose a priority."
    if audience not in AUDIENCES:
        errors["target_audience"] = "Choose a target audience."

    barangay = text("barangay") or None
    if audience == "barangay" and not barangay:
        errors["barangay"] = "Enter the barangay this announcement is for."
    if audience != "barangay":
        barangay = None

    groups = payload.get("target_groups") or []
    if not isinstance(groups, list) or any(g not in GROUPS for g in groups):
        errors["target_groups"] = "Unknown group selected."
        groups = []
    groups = sorted(set(groups)) if audience == "selected_groups" else []
    if audience == "selected_groups" and not groups and "target_groups" not in errors:
        errors["target_groups"] = "Select at least one group."

    published_at = expires_at = None
    try:
        published_at = parse_ts(payload.get("published_at"))
    except ValueError:
        errors["published_at"] = "Enter a valid publication date and time."
    try:
        expires_at = parse_ts(payload.get("expires_at"))
    except ValueError:
        errors["expires_at"] = "Enter a valid expiration date and time."

    action = payload.get("action", "publish")
    status = text("status") or "active"
    if action == "draft":
        status = "draft"
    elif status not in STATUSES:
        errors["status"] = "Choose a status."
    elif status == "draft":
        # "Publish" with Draft selected is ambiguous; treat it as publish now.
        status = "active"

    if status == "scheduled":
        if published_at is None:
            errors["published_at"] = "Scheduled announcements need a publication date and time."
        elif published_at <= now:
            errors["published_at"] = "Scheduled publication time must be in the future."
    elif status == "active":
        if published_at is None:
            published_at = now
        elif published_at > now:
            status = "scheduled"  # a future time on an active post means it's scheduled

    if expires_at is not None:
        if published_at is not None and expires_at <= published_at:
            errors["expires_at"] = "Expiration must be after the publication time."
        elif status in ("active", "scheduled") and expires_at <= now:
            errors["expires_at"] = "Expiration time is already in the past."

    if errors:
        raise ValidationError(errors)

    return {
        "title": title,
        "message": message,
        "category": category,
        "priority": priority,
        "target_audience": audience,
        "barangay": barangay,
        "target_groups": groups,
        "status": status,
        "is_pinned": bool(payload.get("is_pinned")),
        "published_at": published_at.isoformat() if published_at else None,
        "scheduled_at": published_at.isoformat() if status == "scheduled" else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def clean_search(q):
    """Strip characters that have meaning inside a PostgREST or=() filter."""
    return "".join(ch for ch in (q or "") if ch not in ",()*%\\\"'.:").strip()[:100]
