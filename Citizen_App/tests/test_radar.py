"""Safety Radar: reading the published public risk areas and placing them around the citizen.
The rows below have the shape ESRMS publishes (dashboard app/public_risk.py, migration 012)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fake_supabase import FakeSupabase

from esrms import radar
from esrms.backend import Backend, BackendError
from esrms.config import Config

ME = (7.0700, 125.6100)
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def area(area_id, level, north_km=0.0, east_km=0.0, name=None, radius=0.71):
    return {"area_id": area_id, "area_name": name, "risk_level": level, "radius_km": radius,
            "center_latitude": ME[0] + north_km / 110.574, "center_longitude": ME[1] + east_km / 110.5,
            "public_summary": radar.LEVEL_MEANING[level], "updated_at": "2026-10-05T03:00:00Z"}


AREAS = [area("a", "high", north_km=1.8), area("b", "moderate", east_km=0.9, name="Near Talomo Barangay Hall"),
         area("c", "low", north_km=-4.0, east_km=3.0), area("d", "high", north_km=40.0)]


def placed(rows=AREAS, center=ME):
    return radar.place(radar.parse(rows), center)


def test_zones_are_placed_by_distance_and_direction():
    zones = {z.area_id: z for z in placed()}
    assert zones["a"].distance_km == pytest.approx(1.8, abs=0.02) and radar.direction(zones["a"]) == "north"
    assert radar.direction(zones["b"]) == "east" and radar.direction(zones["c"]) == "south-east"
    assert [z.area_id for z in placed()] == ["b", "a", "c", "d"]            # nearest edge first


def test_your_area_is_the_highest_level_next_to_you_else_low():
    assert radar.status(placed(), True) == "moderate"                        # b's edge is ~0.2 km away
    assert radar.status(placed([area("x", "high", north_km=0.3), area("b", "moderate", east_km=0.5)]), True) == "high"
    assert radar.status(placed([area("far", "high", north_km=5)]), True) == "low"
    assert radar.status([], True) == "low"


def test_insufficient_data_is_never_reported_as_low_risk():
    assert radar.status(placed(), False) == "unknown" and radar.status([], False) == "unknown"
    assert "does not mean an area is safe" in radar.NEARBY_TEXT["unknown"]


def test_distances_are_rounded_and_never_exact():
    zones = {z.area_id: z for z in placed()}
    assert radar.distance_text(zones["a"]) == "Approximately 1.8 km away"
    assert radar.distance_text(zones["d"]) == "Approximately 40 km away"
    assert radar.distance_text(placed([area("n", "low", north_km=1.0, radius=0.3)])[0]).endswith("km away")
    assert radar.distance_text(placed([area("m", "low", north_km=0.9, radius=0.2)])[0]) == "Approximately 900 m away"
    assert radar.distance_text(placed([area("in", "high", north_km=0.2)])[0]) == "You are in or next to this area"


def test_range_fits_nearby_zones_and_ignores_far_ones_when_centred_on_the_citizen():
    assert radar.range_km(placed()) == 10.0                                  # c reaches ~5.7 km; d (40 km) is off-radar
    assert radar.range_km(placed([area("a", "high", north_km=1.0)])) == 2.0
    assert radar.range_km([]) == 2.0
    assert radar.range_km(placed(), cap=None) == 50.0                         # general map shows everything


def test_names_fall_back_to_a_direction_not_coordinates():
    zones = {z.area_id: z for z in placed()}
    assert radar.zone_name(zones["b"]) == "Near Talomo Barangay Hall"
    assert radar.zone_name(zones["a"]) == "Area to the north"
    assert radar.zone_name(zones["a"], located=False) == "Unnamed area"
    for z in zones.values():
        assert "7.0" not in radar.zone_name(z) and "125." not in radar.zone_name(z)


def test_general_map_centre_and_bad_rows():
    assert radar.general_center([]) is None
    lat, lng = radar.general_center(radar.parse(AREAS[:2]))
    assert ME[0] < lat < ME[0] + 0.02
    bad = [{"area_id": "x"}, {**area("y", "low"), "risk_level": "extreme"}, {**area("z", "low"), "center_latitude": 0, "center_longitude": 0},
           {**area("r", "low"), "radius_km": -1}, None]
    assert radar.parse([b for b in bad if b] + AREAS[:1])[0].area_id == "a" and len(radar.parse(bad[:4])) == 0


def test_updated_text():
    assert radar.updated_text((NOW - timedelta(minutes=5)).isoformat(), NOW) == "Updated 5 minutes ago"
    assert radar.updated_text((NOW - timedelta(seconds=20)).isoformat(), NOW) == "Updated just now"
    assert radar.updated_text((NOW - timedelta(hours=3)).isoformat(), NOW) == "Updated 3 hours ago"
    assert radar.updated_text((NOW - timedelta(days=3)).isoformat(), NOW) == "Updated Oct 02, 2026"
    assert radar.updated_text(None, NOW) == "Not published yet"


def test_wording_is_about_history_not_danger_or_active_emergencies():
    for text in [*radar.NEARBY_TEXT.values(), *radar.LEVEL_MEANING.values(), *radar.AREA_TITLE.values()]:
        for word in ("dangerous", "is unsafe", "active emergency", "right now"):
            assert word not in text.lower()


def test_backend_reads_only_the_public_tables_and_sends_no_location():
    fake = FakeSupabase()
    fake.add_user("juan@example.ph", "OldPass#123")
    b = Backend(Config("https://example.supabase.co", "anon-key"), transport=fake.transport())
    asyncio.run(b.sign_in("juan@example.ph", "OldPass#123"))
    with pytest.raises(BackendError) as exc:                                  # migration 012 not applied
        asyncio.run(b.public_risk())
    assert exc.value.kind == "not_configured"
    fake.risk_areas = AREAS
    fake.risk_feed.update(version=3, published_at=NOW.isoformat(), sufficient=True, period_days=30)
    fake.calls.clear()
    data = asyncio.run(b.public_risk())
    assert len(data["areas"]) == 4 and data["feed"]["version"] == 3 and data["feed"]["sufficient"]
    assert {path for _, path in fake.calls} == {"/rest/v1/public_risk_feed", "/rest/v1/public_risk_areas"}
    assert set(data["areas"][0]) == {"area_id", "area_name", "risk_level", "center_latitude", "center_longitude",
                                     "radius_km", "public_summary", "updated_at"}
