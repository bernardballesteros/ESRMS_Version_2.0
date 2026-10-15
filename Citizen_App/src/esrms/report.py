"""Builds and validates the emergency_incidents row a citizen submits.

No Flet or network code here, so the rules are unit tested in tests/test_report.py.
The column names and limits match EMERGENCY/supabase/migrations/003_dispatch.sql
plus 004_citizen_app.sql (same folder).
"""
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from esrms import catalog

DESCRIPTION_MAX = 4000
NAME_MAX = 120
PHONE_MAX = 32
NOTE_MAX = 300
FRESH_SECONDS = 120     # a GPS fix older than this is shown and reported as old, not current
MANILA = timezone(timedelta(hours=8))


def local_time(dt, fmt="%b %d, %Y %I:%M %p"):
    return dt.astimezone(MANILA).strftime(fmt)


def age_text(seconds):
    """'40 sec', '12 min', '3 h 05 min', '2 days'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''}"


class ReportError(ValueError):
    pass


def valid_coordinates(lat, lng):
    """Same rule as the dispatcher (EMERGENCY/app/dispatch.py): finite WGS84, and (0, 0) is not a fix."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    return not (lat == 0 and lng == 0)


@dataclass
class Location:
    """Where the citizen is. source is 'gps' (device fix) or 'manual' (typed by the citizen)."""
    source: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    note: str = ""          # landmark / address typed by the citizen
    mocked: bool = False    # Android reported a mock-location provider
    fixed_at: Optional[datetime] = None  # when the phone determined this position (aware); None = unknown
    cached: bool = False    # the phone's last known position, not a fix taken for this request

    @property
    def has_coordinates(self):
        return self.latitude is not None and self.longitude is not None

    def age_seconds(self, now=None):
        if self.fixed_at is None:
            return None
        return max(0.0, ((now or datetime.now(timezone.utc)) - self.fixed_at).total_seconds())

    def is_current(self, now=None):
        """True only for a GPS fix whose time is known and recent. Unknown age is never 'current'."""
        age = self.age_seconds(now)
        return self.source == "gps" and not self.cached and age is not None and age <= FRESH_SECONDS

    def freshness(self, now=None):
        """Short label for the citizen: 'current, 20 sec ago' / 'OLD, 14 min ago' / 'OLD, time unknown'."""
        age = self.age_seconds(now)
        if age is None:
            return "OLD, time unknown"
        return f"{'current' if self.is_current(now) else 'OLD'}, {age_text(age)} ago"


def parse_manual_location(lat_text, lng_text, note):
    """Turn the manual-location form into a Location. Coordinates are optional; a landmark alone is allowed."""
    lat_text, lng_text, note = (lat_text or "").strip(), (lng_text or "").strip(), (note or "").strip()
    if len(note) > NOTE_MAX:
        raise ReportError(f"Landmark / address must be {NOTE_MAX} characters or fewer.")
    if not lat_text and not lng_text:
        if not note:
            raise ReportError("Enter a landmark or address, or both coordinates.")
        return Location(source="manual", note=note)
    if not lat_text or not lng_text:
        raise ReportError("Enter both latitude and longitude, or leave both empty.")
    if not valid_coordinates(lat_text, lng_text):
        raise ReportError("Coordinates are not valid. Latitude is -90..90, longitude is -180..180.")
    return Location(source="manual", latitude=float(lat_text), longitude=float(lng_text), note=note)


def build_incident(*, reporter_id, category_key, incident_type, description="", location=None,
                   citizen_name="", contact_number="", client_request_id=None, queued_at=None, now=None,
                   photo_url=None):
    """Return the row to insert. Raises ReportError with a message for the citizen.

    photo_url: public URL of a photo already uploaded for this report. The table has no photo
    column, so the dispatcher gets it as a line in the description.

    queued_at: set when the report could not be sent and is kept on the phone. The database stamps
    created_at when the row finally arrives, so the real time is written into the description.

    Dispatcher-controlled columns (status, priority, acknowledged_*, resolved_at, archived_*,
    updated_by, location_verified) are never set here; the database defaults and the RLS
    policy keep them dispatcher-only.
    """
    if not reporter_id:
        raise ReportError("Sign in before sending a report.")
    try:
        etype = catalog.emergency_type(category_key, incident_type)
    except (KeyError, ValueError) as exc:
        raise ReportError("Choose the type of emergency.") from exc

    lines = []
    description = (description or "").strip()
    if description:
        lines.append(description)

    lat = lng = accuracy = None
    source = "none"
    if location is not None:
        source = location.source
        if location.has_coordinates:
            if not valid_coordinates(location.latitude, location.longitude):
                raise ReportError("The location is not valid. Fix it or send without coordinates.")
            lat, lng = float(location.latitude), float(location.longitude)
            if location.accuracy_m is not None and location.accuracy_m >= 0 and math.isfinite(location.accuracy_m):
                accuracy = float(location.accuracy_m)
        if location.source == "manual":
            lines.append("[Location entered manually by citizen"
                         + (f": {location.note}]" if location.note else "]"))
        if location.mocked:
            lines.append("[Device reported a mock location provider]")
        if location.source == "gps" and location.has_coordinates:
            written = queued_at or now
            if location.cached:
                lines.append("[Location is the phone's LAST KNOWN position, not a fresh GPS fix]")
            if location.fixed_at is None:
                if location.cached:
                    lines.append("[Time of that position is unknown]")
            elif queued_at or not location.is_current(written):
                age = location.age_seconds(written)
                old = "" if location.is_current(written) else f", {age_text(age)} before this report was written"
                lines.append(f"[GPS position taken {local_time(location.fixed_at)} PHT{old}]")
    if source == "none":
        lines.append("[No location available from citizen]")
    if photo_url:
        lines.append(f"[Photo attached by citizen: {photo_url}]")
    if queued_at:
        lines.append(f"[OFFLINE REPORT: written {local_time(queued_at)} PHT with no connection, sent by the app "
                     "when the phone reconnected. The citizen may have been waiting since then.]")

    full_description = "\n".join(lines)
    if len(full_description) > DESCRIPTION_MAX:
        raise ReportError(f"Description is too long (max {DESCRIPTION_MAX - 200} characters).")

    name = (citizen_name or "").strip()[:NAME_MAX]
    phone = (contact_number or "").strip()[:PHONE_MAX]

    return {
        "client_request_id": str(client_request_id or uuid.uuid4()),
        "reporter_id": reporter_id,
        "emergency_type": etype,
        "description": full_description or None,
        "latitude": lat,
        "longitude": lng,
        "location_accuracy_m": accuracy,
        "location_source": source,
        "location_verified": False,
        "status": "new",
        "citizen_name": name or None,
        "contact_number": phone or None,
    }


def sms_text(row, *, fixed_at=None, cached=False, place="", written_at=None):
    """Plain-text SOS for the phone's own SMS app, short enough for a few SMS segments.

    The app only opens the composer; it never knows whether the SMS was sent or delivered.
    """
    lines = ["SOS - NEED HELP (ESRMS Citizen)", row["emergency_type"]]
    who = " ".join(x for x in (row.get("citizen_name"), row.get("contact_number")) if x)
    if who:
        lines.append(who)
    if row.get("latitude") is not None:
        lat, lng = row["latitude"], row["longitude"]
        acc = row.get("location_accuracy_m")
        kind = {"gps": "last known GPS" if cached else "GPS", "manual": "typed in"}.get(row.get("location_source"), "")
        lines.append(f"{kind} {lat:.6f},{lng:.6f}".strip() + (f" +/-{acc:.0f}m" if acc is not None else ""))
        if fixed_at:
            lines.append(f"Position time {local_time(fixed_at, '%b %d %I:%M %p')} PHT")
        lines.append(f"https://maps.google.com/?q={lat:.6f},{lng:.6f}")
    else:
        lines.append("NO GPS COORDINATES")
    if place:
        lines.append(f"Place: {place[:120]}")
    note = next((ln for ln in (row.get("description") or "").split("\n") if ln and not ln.startswith("[")), "")
    if note:
        lines.append(note[:160])
    if written_at:
        lines.append(f"Written {local_time(written_at, '%b %d %I:%M %p')} PHT")
    return "\n".join(lines)


def sms_url(number, text):
    return f"sms:{quote((number or '').strip(), safe='+')}?body={quote(text)}"


STATUS_TEXT = {
    # Only what the dispatcher has actually recorded. "new" means received, nothing more.
    "new": "Received by ESRMS - waiting for a dispatcher",
    "acknowledged": "Acknowledged by a dispatcher",
    "dispatched": "Responders dispatched",
    "en_route": "Responders en route",
    "on_scene": "Responders on scene",
    "resolved": "Resolved",
    "cancelled": "Cancelled",
}


def incident_code(row):
    n = row.get("incident_number")
    return f"INC-{int(n):06d}" if n is not None else "INC-?"
