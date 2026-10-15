"""High-Risk Area Detection for the Analytics tab: where reported incidents concentrate.

Free of Flask/Supabase so the calculation can be unit tested (tests/test_risk.py). Every figure
is computed from public.emergency_incidents rows; nothing here is sample data.

This is an analytical aid built on historical reports. It does not declare an area dangerous,
dispatch anyone or move resources: those decisions stay with authorized personnel.

AREAS
  emergency_incidents stores a GPS point, not a barangay, so nearby incidents are grouped into
  square grid cells of CELL_KM x CELL_KM (default 1 km). An area is one cell that holds at least
  one incident of the selected period. Incidents without a valid GPS point cannot be placed and
  are reported as "unlocated", never guessed. An area is named after the nearest registered
  organization base when one lies within NAME_WITHIN_KM, otherwise by its coordinates.

RISK SCORE (per area, over the incidents reported in the selected period)
  score = frequency + severity + recency
    frequency  1 point per incident.
    severity   extra points per incident by priority: P1 +2, P2 +1, P3 +0.5, P4 or not set +0.
    recency    0 to 1 extra point per incident: 1 for an incident reported at the very end of the
               period, falling in a straight line to 0 for one at its very start.
  So one incident is worth between 1 and 4 points, and a recent critical one counts most.

RISK LEVEL
  concentration = area score / average score of all areas in the period.
    HIGH      concentration >= HIGH_RATIO (1.5) and at least HIGH_MIN_INCIDENTS (3) incidents
    MODERATE  concentration >= MODERATE_RATIO (1.0), i.e. at or above the average area
    LOW       below the average area
  A level is only given when the period holds at least MIN_INCIDENTS (10) located incidents in at
  least MIN_AREAS (3) areas. Below that the areas are still listed with their real counts, but the
  level is "Insufficient historical data": too few reports to call any place high-risk.

TREND (per area)
  The area's incident count in the selected period against the same cell in the period of equal
  length just before it. Stable when the change is under STABLE_PCT (10%); a percentage is only
  shown when the previous count is above zero. "Not enough historical data" when the previous
  period holds no located incidents at all, so there is nothing to compare with.

RESOURCE COVERAGE (per area, from the fixed organization bases; no responder is ever located)
  Counts verified, active bases within COVERAGE_KM (5 km, straight line) of the area centre and
  how many of them are Available. GOOD: 3 or more available; LIMITED: 1 or 2; NONE: 0.
  "Potential resource gap" is raised only for a HIGH area whose coverage is LIMITED or NONE, or
  that has no available base of a type that usually answers its most common emergency.

RADAR (Risk Detection Radar: the same areas, incidents and bases drawn around one centre)
  The centre is the middle of the located incidents' bounding box. Every incident, area and base is
  placed by its own coordinates as a distance east and north of that centre; nothing is positioned
  by hand. The range reaches the farthest incident and any base within COVERAGE_KM of an area.
"""
import math
from collections import Counter
from dataclasses import dataclass

from app import organizations as orgs
from app.analytics import category_key, category_of, dedupe, parse_ts, pct, pct_change
from app.dispatch import CLOSED, PRIORITIES, incident_code, valid_coordinates
from app.hospital import distance_km

HIGH, MODERATE, LOW, INSUFFICIENT = "high", "moderate", "low", "insufficient"
LEVELS = {HIGH: "High", MODERATE: "Moderate", LOW: "Low", INSUFFICIENT: "Insufficient historical data"}
TRENDS = {"increasing": "Increasing", "decreasing": "Decreasing", "stable": "Stable",
          "insufficient": "Not enough historical data"}
COVERAGE = {"good": "Good", "limited": "Limited", "none": "None"}
SEVERITY_POINTS = {1: 2.0, 2: 1.0, 3: 0.5, 4: 0.0}
INSUFFICIENT_TEXT = "Not enough historical data to determine reliable high-risk areas."


@dataclass(frozen=True)
class Settings:
    """Thresholds of the method above. Overridable from the environment (see config.py, RISK_*)."""
    cell_km: float = 1.0
    high_ratio: float = 1.5
    moderate_ratio: float = 1.0
    high_min_incidents: int = 3
    min_incidents: int = 10
    min_areas: int = 3
    stable_pct: float = 10.0
    coverage_km: float = 5.0
    name_within_km: float = 2.0


DEFAULTS = Settings()


def settings_from(config):
    """Settings from the Flask config; a missing or unusable value keeps its default."""
    values = {}
    for name, default in DEFAULTS.__dict__.items():
        raw = config.get(f"RISK_{name.upper()}")
        try:
            value = type(default)(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values[name] = value
    return Settings(**values)


# ---------------------------------------------------------------- areas

def cell_of(lat, lng, cell_km):
    """Grid cell (row, column) of a point. Columns are sized at the row's own latitude, so cells stay square."""
    step_lat = cell_km / 110.574
    row = math.floor(float(lat) / step_lat)
    step_lng = cell_km / (111.320 * math.cos(math.radians((row + 0.5) * step_lat)))
    return row, math.floor(float(lng) / step_lng)


def area_id(cell):
    return f"{cell[0]}_{cell[1]}"


def _located(row):
    return valid_coordinates(row.get("latitude"), row.get("longitude"))


def matches(row, category=None, priority=None):
    """The panel's own filters: an Incidents-by-Category key and a priority 1-4."""
    if category and category_key(row.get("emergency_type")) != category:
        return False
    return not priority or row.get("priority") == priority


def split(rows, period, category=None, priority=None):
    """(current, previous) filtered rows by created_at, like analytics.split_periods()."""
    current, previous = [], []
    for row in dedupe(rows):
        created = parse_ts(row.get("created_at"))
        if not created or not matches(row, category, priority):
            continue
        if period.start <= created < period.end:
            current.append(row)
        elif period.prev_start <= created < period.start:
            previous.append(row)
    return current, previous


def _recency(row, period):
    span = (period.end - period.start).total_seconds()
    return max(0.0, min(1.0, (parse_ts(row["created_at"]) - period.start).total_seconds() / span))


def _trend(count, previous, history, stable_pct):
    if not history:
        return "insufficient", None
    change = pct_change(count, previous)
    if change is None:   # nothing in this cell before, although the previous period has incidents elsewhere
        return ("increasing" if count else "stable"), None
    return ("stable" if abs(change) < stable_pct else "increasing" if change > 0 else "decreasing"), change


def _name(center, bases, within_km):
    """The nearest organization base, when it is close enough to describe where the area is."""
    nearest = None
    for base in bases:
        if base.get("is_active") is False or base.get("verified") is False:
            continue
        distance = distance_km(center[0], center[1], base.get("latitude"), base.get("longitude"))
        if distance is not None and distance <= within_km and (nearest is None or distance < nearest[0]):
            nearest = (distance, base)
    return nearest


def _categories(rows):
    counts, spellings, kinds = Counter(), {}, {}
    for row in rows:
        key = category_key(row.get("emergency_type"))
        counts[key] += 1
        spellings.setdefault(key, Counter())[category_of(row.get("emergency_type"))] += 1
        kinds.setdefault(key, Counter())[row.get("emergency_type") or ""] += 1
    out = [{"key": key, "label": spellings[key].most_common(1)[0][0], "count": n, "pct": pct(n, len(rows)),
            "example_type": kinds[key].most_common(1)[0][0]} for key, n in counts.items()]
    return sorted(out, key=lambda c: (-c["count"], c["label"].lower()))


def analyze(rows, period, bases=(), settings=DEFAULTS, category=None, priority=None):
    """Areas of the selected period, ranked by risk score. `rows` cover [period.prev_start, period.end)."""
    current, previous = split(rows, period, category, priority)
    located = [r for r in current if _located(r)]
    cells, earlier = {}, Counter()
    for row in located:
        cells.setdefault(cell_of(row["latitude"], row["longitude"], settings.cell_km), []).append(row)
    for row in previous:
        if _located(row):
            earlier[cell_of(row["latitude"], row["longitude"], settings.cell_km)] += 1
    history = sum(earlier.values())

    areas = []
    for cell, members in cells.items():
        severity = sum(SEVERITY_POINTS.get(r.get("priority"), 0.0) for r in members)
        recency = sum(_recency(r, period) for r in members)
        center = (sum(float(r["latitude"]) for r in members) / len(members),
                  sum(float(r["longitude"]) for r in members) / len(members))
        trend, change = _trend(len(members), earlier[cell], history, settings.stable_pct)
        near = _name(center, bases, settings.name_within_km)
        cats = _categories(members)
        areas.append({
            "id": area_id(cell),
            "label": f"Near {near[1].get('name')}" if near else f"Area {center[0]:.4f}, {center[1]:.4f}",
            "named_after": near and {"name": near[1].get("name"), "distance_km": near[0]},
            "center": {"latitude": round(center[0], 6), "longitude": round(center[1], 6)},
            "cell_km": settings.cell_km,
            "count": len(members),
            "previous": earlier[cell],
            "change": change,
            "trend": trend,
            "trend_label": TRENDS[trend],
            "critical": sum(1 for r in members if r.get("priority") == 1),
            "score": round(len(members) + severity + recency, 2),
            "score_parts": {"frequency": len(members), "severity": round(severity, 2), "recency": round(recency, 2)},
            "top_category": cats[0],
            "categories": cats,
            "_members": members,
        })

    sufficient = len(located) >= settings.min_incidents and len(areas) >= settings.min_areas
    average = sum(a["score"] for a in areas) / len(areas) if areas else 0.0
    for area in areas:
        ratio = area["score"] / average if average else 0.0
        area["concentration"] = round(ratio, 2)
        if not sufficient:
            level = INSUFFICIENT
        elif ratio >= settings.high_ratio and area["count"] >= settings.high_min_incidents:
            level = HIGH
        elif ratio >= settings.moderate_ratio:
            level = MODERATE
        else:
            level = LOW
        area.update(level=level, level_label=LEVELS[level])
    areas.sort(key=lambda a: (-a["score"], -a["count"], a["id"]))
    for rank, area in enumerate(areas, start=1):
        area["rank"] = rank

    reason = None
    if not located:
        reason = "No incident with a GPS location was reported in this range." if current else "No incidents were reported in this range."
    elif not sufficient:
        reason = (f"{INSUFFICIENT_TEXT} This range holds {len(located)} located incident{'' if len(located) == 1 else 's'} "
                  f"in {len(areas)} area{'' if len(areas) == 1 else 's'}; at least {settings.min_incidents} incidents "
                  f"in {settings.min_areas} areas are needed.")
    return {
        "sufficient": sufficient,
        "reason": reason,
        "totals": {"incidents": len(current), "located": len(located), "unlocated": len(current) - len(located),
                   "areas": len(areas), "previous_located": history, "average_score": round(average, 2)},
        "areas": areas,
        "method": method(settings),
    }


def method(settings=DEFAULTS):
    """The formula and thresholds in force, shown under the panel so the ranking can be checked by hand."""
    return {
        "cell_km": settings.cell_km,
        "score": "Risk score = 1 per incident + severity (P1 +2, P2 +1, P3 +0.5) + recency (0 to 1, newest highest).",
        "levels": (f"High: score at least {settings.high_ratio:g}x the average area and {settings.high_min_incidents}+ incidents. "
                   f"Moderate: at or above {settings.moderate_ratio:g}x the average. Low: below it."),
        "minimum": f"Levels need at least {settings.min_incidents} located incidents in {settings.min_areas} areas.",
        "areas": f"Areas are {settings.cell_km:g} km grid cells of the incidents' GPS points.",
        "coverage_km": settings.coverage_km,
        "severity_points": {PRIORITIES[p]: points for p, points in SEVERITY_POINTS.items()},
    }


def public_area(area):
    """An area without the incident rows it was built from."""
    return {k: v for k, v in area.items() if not k.startswith("_")}


# ---------------------------------------------------------------- one area: detail, coverage, recommendation

def nearby_organizations(area, bases, settings=DEFAULTS, limit=8):
    """Verified, active organization bases by straight-line distance from the area centre. Bases only, never people."""
    center = area["center"]
    out = []
    for base in bases:
        if base.get("is_active") is False or base.get("verified") is False:
            continue
        distance = distance_km(center["latitude"], center["longitude"], base.get("latitude"), base.get("longitude"))
        if distance is None:
            continue
        kind = base.get("org_type") or "hospital"
        out.append({
            "id": base.get("id"), "name": base.get("name"), "org_type": kind,
            "org_type_label": orgs.ORG_TYPES.get(kind, kind), "base_label": orgs.BASE_LABELS.get(kind, kind),
            "latitude": base.get("latitude"), "longitude": base.get("longitude"), "distance_km": distance,
            "availability": base.get("availability"), "status_label": orgs.AVAILABILITY.get(base.get("availability"), "Unknown"),
            "available": base.get("availability") == orgs.AVAILABLE, "within": distance <= settings.coverage_km,
        })
    out.sort(key=lambda o: (o["distance_km"], (o["name"] or "").lower()))
    return out[:limit], out


def coverage(area, organizations, settings=DEFAULTS):
    """How many bases are near the area, how many are Available, and whether that looks thin for a HIGH area."""
    within = [o for o in organizations if o["within"]]
    available = [o for o in within if o["available"]]
    level = "good" if len(available) >= 3 else "limited" if available else "none"
    _, _, primary, _ = orgs.rule_for(area["top_category"]["example_type"])
    missing = [kind for kind in primary if not any(o["org_type"] == kind for o in available)]
    gap = area["level"] == HIGH and (level != "good" or bool(missing))
    return {
        "radius_km": settings.coverage_km, "within": len(within), "available": len(available),
        "level": level, "level_label": COVERAGE[level],
        "usual_responders": [orgs.ORG_TYPES[k] for k in primary],
        "missing_types": [orgs.ORG_TYPES[k] for k in missing],
        "potential_gap": gap,
    }


def recommendations(area, cover, organizations):
    """Review prompts for a HIGH area, each tied to a figure above. Advice only: nothing is dispatched or moved."""
    if area["level"] != HIGH:
        return []
    radius = f"{cover['radius_km']:g} km"
    out = ["Historical incident concentration is elevated in this area. Review emergency organization coverage and preparedness resources."]
    for kind in cover["missing_types"]:
        out.append(f"Review {kind} coverage: no available {kind} base is registered within {radius}.")
    top = area["top_category"]
    if top["count"] >= 3 and (top["pct"] or 0) >= 50:
        out.append(f"Monitor the recurring incident type: {top['label']} ({top['count']} of {area['count']} incidents).")
    barangay = next((o for o in organizations if o["org_type"] == "barangay" and o["within"]), None)
    if barangay:
        out.append(f"Coordinate with {barangay['name']} ({barangay['distance_km']:.1f} km away).")
    if any(word in top["label"].lower() for word in ("medic", "ems", "health")):
        hospital = next((o for o in organizations if o["org_type"] == "hospital" and o["within"]), None)
        where = f" at {hospital['name']}" if hospital else ""
        out.append(f"Review hospital receiving capacity{where}: most incidents here are medical.")
    if area["trend"] == "increasing":
        out.append("Incidents here are increasing against the previous period.")
    return out


def detail(area, bases, settings=DEFAULTS, recent=10, max_points=500):
    """Everything the expanded panel shows for one area."""
    members = sorted(area["_members"], key=lambda r: r["created_at"], reverse=True)
    nearest, every = nearby_organizations(area, bases, settings)
    cover = coverage(area, every, settings)
    radius = max([distance_km(area["center"]["latitude"], area["center"]["longitude"], r["latitude"], r["longitude"]) or 0.0
                  for r in members] + [settings.cell_km / 2])
    return {
        "area": public_area(area),
        "radius_km": round(radius, 2),
        "recent": [{"id": r["id"], "code": incident_code(r), "emergency_type": r.get("emergency_type"),
                    "category": category_of(r.get("emergency_type")), "status": r.get("status"), "priority": r.get("priority"),
                    "created_at": r["created_at"], "archived": bool(r.get("archived_at"))} for r in members[:recent]],
        # Where the incidents were reported, for the map. Coordinates only: no names, numbers or descriptions.
        "points": [{"latitude": r["latitude"], "longitude": r["longitude"], "priority": r.get("priority")} for r in members[:max_points]],
        "organizations": nearest,
        "coverage": cover,
        "recommendations": recommendations(area, cover, every),
    }


# ---------------------------------------------------------------- radar: every area, incident and base around one centre

def nearest_by_type(organizations):
    """The closest base of each organization type, preferring one that is Available. `organizations` is by distance."""
    best = {}
    for org in organizations:
        held = best.get(org["org_type"])
        if held is None or (org["available"] and not held["available"]):
            best[org["org_type"]] = org
    return [{k: best[kind][k] for k in ("org_type", "org_type_label", "name", "distance_km", "available", "status_label")}
            for kind in orgs.ORG_TYPES if kind in best]


def _offset(center, lat, lng):
    """(east, north) km of a point from the radar centre. A flat projection: exact enough at city scale."""
    east = (float(lng) - center[1]) * 111.320 * math.cos(math.radians(center[0]))
    return round(east, 3), round((float(lat) - center[0]) * 110.574, 3)


def radar(result, bases, settings=DEFAULTS, max_points=500):
    """What the Risk Detection Radar draws, from an analyze() result and the organization bases.

    Areas gain their coverage, nearest bases and position; incidents and bases are listed with theirs.
    Only fixed organization bases are placed. No responder, vehicle or device is located.
    """
    areas = result["areas"]
    members = [(area, row) for area in areas for row in area["_members"]]
    if not members:
        return {"center": None, "range_km": None, "areas": [], "incidents": [], "organizations": [], "plotted": 0,
                "summary": {"high_areas": None, "active_zones": None, "most_common": None, "highest": None}}
    lats = [float(row["latitude"]) for _, row in members]
    lngs = [float(row["longitude"]) for _, row in members]
    center = ((min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2)
    reach = [math.hypot(*_offset(center, row["latitude"], row["longitude"])) for _, row in members]

    zones, organizations = [], {}
    for area in areas:
        _, every = nearby_organizations(area, bases, settings)
        for org in every:
            organizations.setdefault(org["id"], org)
            if org["within"]:
                reach.append(math.hypot(*_offset(center, org["latitude"], org["longitude"])))
        spread = [distance_km(area["center"]["latitude"], area["center"]["longitude"], r["latitude"], r["longitude"]) or 0.0
                  for r in area["_members"]]
        east, north = _offset(center, area["center"]["latitude"], area["center"]["longitude"])
        zones.append({**public_area(area), "east_km": east, "north_km": north,
                      "radius_km": round(max(spread + [settings.cell_km / 2]), 2),
                      "open": sum(1 for r in area["_members"] if r.get("status") not in CLOSED),
                      "coverage": coverage(area, every, settings), "nearest": nearest_by_type(every)})

    newest = sorted(members, key=lambda pair: pair[1]["created_at"], reverse=True)[:max_points]
    incidents = []
    for area, row in newest:
        east, north = _offset(center, row["latitude"], row["longitude"])
        incidents.append({"id": row["id"], "code": incident_code(row), "emergency_type": row.get("emergency_type"),
                          "category": category_of(row.get("emergency_type")), "priority": row.get("priority"),
                          "priority_label": PRIORITIES.get(row.get("priority"), "Not set"), "status": row.get("status"),
                          "latitude": row["latitude"], "longitude": row["longitude"], "east_km": east, "north_km": north,
                          "created_at": row["created_at"], "archived": bool(row.get("archived_at")),
                          "area": area["id"], "area_label": area["label"]})

    placed = []
    for org in organizations.values():
        east, north = _offset(center, org["latitude"], org["longitude"])
        placed.append({**{k: v for k, v in org.items() if k not in ("distance_km", "within")},
                       "east_km": east, "north_km": north,
                       "center_km": distance_km(center[0], center[1], org["latitude"], org["longitude"])})
    placed.sort(key=lambda o: (o["center_km"], (o["name"] or "").lower()))

    graded = result["sufficient"]
    top = zones[0]
    return {
        "center": {"latitude": round(center[0], 6), "longitude": round(center[1], 6)},
        "range_km": round(max(max(reach) * 1.1, 1.0), 2),
        "areas": zones,
        "incidents": incidents,
        "organizations": placed,
        "plotted": len(incidents),
        # Counts of graded areas are None, not 0, while there is too little data to grade any area.
        "summary": {
            "high_areas": sum(1 for z in zones if z["level"] == HIGH) if graded else None,
            "active_zones": sum(1 for z in zones if z["level"] in (HIGH, MODERATE) and z["open"]) if graded else None,
            "most_common": _categories([row for _, row in members])[0],
            "highest": {"id": top["id"], "label": top["label"], "level": top["level"], "level_label": top["level_label"],
                        "count": top["count"], "coverage": top["coverage"]} if graded else None,
        },
    }
