"""Public-safe copy of High-Risk Area Detection for the Citizen App's Safety Radar.

This is NOT a second analysis. It takes the result of risk.analyze() (the same areas, levels and
thresholds the Analytics tab shows) and keeps only what a citizen may see. Free of Flask/Supabase
so the reduction can be unit tested (tests/test_public_risk.py).

What is removed, and why
  * Incident rows, codes, counts, scores, categories, trends, coverage and recommendations: none
    of it is published. A citizen gets a level and a sentence.
  * The area centre. risk.analyze() places an area at the average of its incidents' GPS points,
    which for a small area is close to a real incident. The public zone is drawn at the centre of
    the 1 km grid cell instead, with a radius that covers the cell, so no published coordinate is
    derived from any single report.
  * Small areas. An area with fewer than MIN_INCIDENTS located incidents is not published at all,
    whatever its level, so one household's emergencies can't be read off the map.
  * Organization names, except a barangay hall: an area is called "Near <barangay>" only when the
    base it was named after is a barangay. Hospitals, EMS, fire and police bases are never named,
    and no base position or availability is published.
  * Everything, when there is too little data: if the analysis is "insufficient" nothing is graded
    and no area is published; the app then says so instead of showing "low risk".

Nothing here describes an active emergency. The levels summarise past reports only.
"""
import math

from app import risk

MIN_INCIDENTS = 3            # areas with fewer located incidents are never published
PUBLIC_RANGE = "30d"         # the period the public copy is always computed for (no filters)
LEVELS = (risk.HIGH, risk.MODERATE, risk.LOW)
SUMMARIES = {
    risk.HIGH: "A high concentration of historical emergency activity has been observed in this area.",
    risk.MODERATE: "Elevated emergency activity has been observed in this area.",
    risk.LOW: "No significant concentration of historical emergency activity detected.",
}


def cell_center(area_id, cell_km):
    """Centre of the grid cell an area id ("row_column") names: the inverse of risk.cell_of()."""
    row, column = (int(part) for part in area_id.split("_"))
    step_lat = cell_km / 110.574
    latitude = (row + 0.5) * step_lat
    step_lng = cell_km / (111.320 * math.cos(math.radians(latitude)))
    return round(latitude, 5), round((column + 0.5) * step_lng, 5)


def _name(area, bases):
    named = area.get("named_after")
    if not named:
        return None
    for base in bases:
        if base.get("name") == named["name"] and (base.get("org_type") or "") == "barangay":
            return f"Near {named['name']}"
    return None


def public_areas(result, bases=(), settings=risk.DEFAULTS, min_incidents=MIN_INCIDENTS):
    """The areas a citizen may see, from a risk.analyze() result. Empty when the data is insufficient."""
    if not result.get("sufficient"):
        return []
    radius = round(settings.cell_km * math.sqrt(2) / 2, 2)   # covers the whole cell
    out = []
    for area in result["areas"]:
        if area["level"] not in LEVELS or area["count"] < min_incidents:
            continue
        latitude, longitude = cell_center(area["id"], settings.cell_km)
        out.append({
            "area_id": area["id"],
            "area_name": _name(area, bases),
            "risk_level": area["level"],
            "center_latitude": latitude,
            "center_longitude": longitude,
            "radius_km": radius,
            "public_summary": SUMMARIES[area["level"]],
        })
    return out


def payload(result, period, bases=(), settings=risk.DEFAULTS, min_incidents=MIN_INCIDENTS):
    """What publish_public_risk_areas() stores: the public areas plus how they were produced."""
    return {
        "areas": public_areas(result, bases, settings, min_incidents),
        "meta": {"sufficient": bool(result.get("sufficient")), "period_days": period.days,
                 "cell_km": settings.cell_km},
    }
