"""Safety Radar: placing the published public risk areas around the citizen. No Flet or network code;
tested in tests/test_radar.py.

Nothing here decides how risky an area is. Levels come from ESRMS Analytics (the dashboard's
app/risk.py), reduced to public zones by its app/public_risk.py and read from the public_risk_areas
table. This module only measures distances on the phone. The citizen's position never leaves the
phone for this: the whole public list is downloaded and compared locally.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from esrms.report import local_time, valid_coordinates

HIGH, MODERATE, LOW, UNKNOWN = "high", "moderate", "low", "unknown"
RANK = {HIGH: 3, MODERATE: 2, LOW: 1}
NEAR_KM = 0.5           # a zone counts as "your area" when its edge is within this distance
MAX_RANGE_KM = 10.0     # zones farther than this are listed, not drawn, when centred on the citizen
RANGES = (1, 2, 5, 10, 20, 50, 100, 200)

LEVEL_TITLE = {HIGH: "HIGH RISK", MODERATE: "MODERATE RISK", LOW: "LOW RISK", UNKNOWN: "NOT ENOUGH DATA"}
AREA_TITLE = {HIGH: "HIGH-RISK AREA", MODERATE: "MODERATE-RISK AREA", LOW: "LOW-RISK AREA"}
# Wording is about past reports only; it never says an area is dangerous or that something is happening now.
NEARBY_TEXT = {
    HIGH: "A high concentration of historical emergency activity has been observed nearby.",
    MODERATE: "Elevated emergency activity has been observed nearby.",
    LOW: "No elevated emergency activity detected nearby.",
    UNKNOWN: "ESRMS does not have enough past reports to grade areas yet. This does not mean an area is safe "
             "or unsafe.",
}
LEVEL_MEANING = {
    HIGH: "High concentration of historical emergency activity has been observed in this area.",
    MODERATE: "Elevated emergency activity has been observed in this area.",
    LOW: "No significant concentration of historical emergency activity detected.",
}
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


@dataclass
class Zone:
    area_id: str
    name: Optional[str]
    level: str
    latitude: float
    longitude: float
    radius_km: float
    summary: str
    updated_at: Optional[str] = None
    # filled by place(): position relative to the radar centre
    east_km: float = 0.0
    north_km: float = 0.0
    distance_km: Optional[float] = None

    @property
    def edge_km(self):
        """Distance from the centre to the nearest edge of the zone (0 when inside it)."""
        return max(0.0, (self.distance_km or 0.0) - self.radius_km)


def parse(rows):
    """Zones from public_risk_areas rows. Rows that are malformed or carry an unknown level are skipped."""
    zones = []
    for row in rows or []:
        try:
            level, radius = row["risk_level"], float(row["radius_km"])
            if level not in RANK or radius <= 0 or not valid_coordinates(row["center_latitude"],
                                                                         row["center_longitude"]):
                continue
            zones.append(Zone(str(row["area_id"]), (row.get("area_name") or "").strip() or None, level,
                              float(row["center_latitude"]), float(row["center_longitude"]), radius,
                              (row.get("public_summary") or LEVEL_MEANING[level]).strip(), row.get("updated_at")))
        except (KeyError, TypeError, ValueError):
            continue
    return zones


def offset(center, latitude, longitude):
    """(east, north) km of a point from `center`. Flat projection: exact enough at city scale."""
    east = (longitude - center[1]) * 111.320 * math.cos(math.radians(center[0]))
    return east, (latitude - center[0]) * 110.574


def place(zones, center):
    """Zones with their position and distance from `center` (lat, lng), nearest edge first."""
    for zone in zones:
        zone.east_km, zone.north_km = offset(center, zone.latitude, zone.longitude)
        zone.distance_km = math.hypot(zone.east_km, zone.north_km)
    return sorted(zones, key=lambda z: (z.edge_km, -RANK[z.level]))


def general_center(zones):
    """Middle of the published zones, for the general map shown without the citizen's location."""
    if not zones:
        return None
    lats, lngs = [z.latitude for z in zones], [z.longitude for z in zones]
    return (min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2


def nearby(placed):
    return [z for z in placed if z.edge_km <= NEAR_KM]


def status(placed, sufficient):
    """The citizen's own area: the highest level among zones at or next to the centre, else LOW.
    UNKNOWN when ESRMS had too little data to grade any area."""
    if not sufficient:
        return UNKNOWN
    close = nearby(placed)
    return max((z.level for z in close), key=RANK.get) if close else LOW


def range_km(placed, cap=MAX_RANGE_KM):
    """Radar radius: the smallest round distance that shows every zone within `cap` (None = no cap)."""
    reach = [z.distance_km + z.radius_km for z in placed if cap is None or z.distance_km <= cap]
    need = max(reach) if reach else 2.0
    limit = cap if cap is not None else RANGES[-1]
    return float(next((r for r in RANGES if r >= need and r <= max(limit, RANGES[0])), limit))


def direction(zone):
    angle = math.degrees(math.atan2(zone.east_km, zone.north_km)) % 360
    return COMPASS[int((angle + 22.5) // 45) % 8]


def distance_text(zone):
    """Rounded on purpose: 'Approximately 1.8 km away', never exact metres or coordinates."""
    if zone.distance_km is None:
        return ""
    if zone.edge_km <= 0:
        return "You are in or next to this area"
    if zone.distance_km < 0.95:   # above this it rounds to 1.0 km
        return f"Approximately {max(100, round(zone.distance_km * 10) * 100)} m away"
    if zone.distance_km < 10:
        return f"Approximately {zone.distance_km:.1f} km away"
    return f"Approximately {round(zone.distance_km)} km away"


def zone_name(zone, located=True):
    if zone.name:
        return zone.name
    if located and zone.distance_km is not None and zone.edge_km > 0:
        return f"Area to the {direction(zone)}"
    return "Area near you" if located else "Unnamed area"


def _time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def updated_text(published_at, now=None):
    """'Updated 5 minutes ago' from the server's publish time; the date once it is over a day old."""
    when = _time(published_at)
    if not when:
        return "Not published yet"
    minutes = max(0, int(((now or datetime.now(timezone.utc)) - when).total_seconds() // 60))
    if minutes < 1:
        return "Updated just now"
    if minutes < 60:
        return f"Updated {minutes} minute{'' if minutes == 1 else 's'} ago"
    if minutes < 24 * 60:
        return f"Updated {minutes // 60} hour{'' if minutes < 120 else 's'} ago"
    return "Updated " + local_time(when, "%b %d, %Y")


def updated_date(value):
    when = _time(value)
    return local_time(when, "%b %d, %Y") if when else "Unavailable"
