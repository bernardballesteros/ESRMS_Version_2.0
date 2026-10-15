"""Dispatcher rules for emergency incidents and responder hotlines.

Free of Flask/Supabase so they can be unit tested (tests/test_dispatch.py).
"""
import math
import re

STATUSES = {
    "new": "New",
    "acknowledged": "Acknowledged",
    "dispatched": "Dispatched",
    "en_route": "Units En Route",
    "on_scene": "On Scene",
    "resolved": "Resolved",
    "cancelled": "Cancelled",
}
CLOSED = {"resolved", "cancelled"}
PRIORITIES = {1: "P1 - Critical", 2: "P2 - High", 3: "P3 - Moderate", 4: "P4 - Low"}

HOTLINE_CATEGORIES = {
    "police": "Police / Law Enforcement",
    "fire": "Fire & Rescue",
    "medical": "Medical / EMS",
    "rescue": "Search & Rescue",
    "disaster": "Disaster Risk Reduction (DRRMO)",
    "coast_guard": "Coast Guard",
    "utility": "Utility / Power / Water",
    "other": "Other",
}
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{2,24}$")
# The line the Citizen App adds to a report's description for an attached photo, and the
# <citizen id>/<photo id>.<ext> part of its URL.
PHOTO_LINE_RE = re.compile(r"^\[Photo attached by citizen: (\S+)\][ \t]*\n?", re.MULTILINE)
PHOTO_PATH_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9-]+\.(png|jpe?g|webp)$")


def incident_code(row):
    n = row.get("incident_number")
    return f"INC-{int(n):06d}" if n is not None else "INC-?"


def valid_coordinates(lat, lng):
    """True only for real, finite WGS84 coordinates. (0, 0) is treated as a missing GPS fix."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    return not (lat == 0 and lng == 0)


def split_photos(description, media_prefix):
    """Separate citizen photos from the report text: (description without them, [photo URLs]).

    The Citizen App has no photo column to write to, so it appends the photo's public URL to the
    description as "[Photo attached by citizen: <url>]". The description is citizen-controlled, so
    only URLs under media_prefix (this project's own incidents/ storage folder) count as photos;
    any other such line stays in the text and is never loaded as an image.
    """
    photos = []

    def take(match):
        url = match.group(1)
        if url.startswith(media_prefix) and PHOTO_PATH_RE.match(url[len(media_prefix):]) and url not in photos:
            photos.append(url)
            return ""
        return match.group(0)

    text = PHOTO_LINE_RE.sub(take, description or "").strip()
    return (text or None), photos


def is_open(row):
    return row.get("status") not in CLOSED


def sort_incidents(rows):
    """Unacknowledged first, then other unresolved, then closed. Within each: priority, then newest.

    Two stable sorts: newest first, then by group and priority.
    """
    def group(row):
        status = row.get("status")
        return 0 if status == "new" else 2 if status in CLOSED else 1

    newest = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)
    return sorted(newest, key=lambda r: (group(r), r.get("priority") or 9))


def counts(rows):
    """Counter definitions, all from incident status (no unit table exists yet).

    active: not resolved/cancelled. critical: active and priority 1.
    en_route: status en_route. queue: awaiting acknowledgment or dispatch (new, acknowledged).
    """
    active = [r for r in rows if is_open(r)]
    return {
        "active": len(active),
        "critical": sum(1 for r in active if r.get("priority") == 1),
        "en_route": sum(1 for r in active if r.get("status") == "en_route"),
        "queue": sum(1 for r in active if r.get("status") in ("new", "acknowledged")),
        "new": sum(1 for r in active if r.get("status") == "new"),
    }


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def validate_hotline(payload):
    errors = {}

    def text(name, limit):
        value = payload.get(name)
        value = value.strip() if isinstance(value, str) else ""
        if len(value) > limit:
            errors[name] = f"Must be {limit} characters or fewer."
        return value

    name = text("organization_name", 120)
    phone = text("phone_number", 25)
    category = text("category", 40)
    description = text("description", 500)
    hours = text("operating_hours", 80)
    if not name:
        errors["organization_name"] = "Organization name is required."
    if not phone:
        errors["phone_number"] = "Hotline number is required."
    elif not PHONE_RE.match(phone) or sum(ch.isdigit() for ch in phone) < 3:
        errors["phone_number"] = "Use digits, spaces, dashes or parentheses, optionally starting with +."
    if category not in HOTLINE_CATEGORIES:
        errors["category"] = "Choose a category."
    if errors:
        raise ValidationError(errors)
    return {
        "organization_name": name,
        "phone_number": phone,
        "category": category,
        "description": description or None,
        "operating_hours": hours or None,
    }


def tel_href(phone):
    """tel: URI with only + and digits."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"tel:{'+' if phone.strip().startswith('+') else ''}{digits}"
