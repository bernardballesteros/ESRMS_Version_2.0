from datetime import datetime, timedelta, timezone

import pytest

from esrms import catalog, report
from esrms.report import Location, ReportError, build_incident, parse_manual_location

UID = "11111111-1111-1111-1111-111111111111"


def build(**kw):
    args = dict(reporter_id=UID, category_key="medical", incident_type="Difficulty breathing")
    args.update(kw)
    return build_incident(**args)


def test_gps_report_keeps_real_coordinates_and_category():
    row = build(location=Location("gps", 7.0732, 125.6129, accuracy_m=12.5), description="Asthma attack")
    assert row["emergency_type"] == "Medical & EMS: Difficulty breathing"
    assert (row["latitude"], row["longitude"], row["location_accuracy_m"]) == (7.0732, 125.6129, 12.5)
    assert row["location_source"] == "gps"
    assert row["description"] == "Asthma attack"
    assert row["reporter_id"] == UID


def test_dispatcher_fields_are_never_set_by_citizen():
    row = build(location=Location("gps", 7.0, 125.0))
    assert row["status"] == "new" and row["location_verified"] is False
    for col in ("priority", "acknowledged_at", "acknowledged_by", "resolved_at", "archived_at", "archived_by", "updated_by"):
        assert col not in row


def test_no_location_is_explicit_not_invented():
    row = build(location=None)
    assert row["latitude"] is None and row["longitude"] is None
    assert row["location_source"] == "none"
    assert "No location" in row["description"]


@pytest.mark.parametrize("lat,lng", [(0, 0), (91, 10), (10, 181), (float("nan"), 1), ("x", 2)])
def test_invalid_coordinates_rejected(lat, lng):
    assert not report.valid_coordinates(lat, lng)
    with pytest.raises(ReportError):
        build(location=Location("gps", lat, lng))


def test_manual_location_with_landmark_only():
    loc = parse_manual_location("", "", "In front of Davao City Hall")
    row = build(location=loc)
    assert row["latitude"] is None and row["location_source"] == "manual"
    assert "Davao City Hall" in row["description"]


def test_manual_location_coordinates_validated():
    assert parse_manual_location("7.07", "125.61", "").latitude == 7.07
    with pytest.raises(ReportError):
        parse_manual_location("7.07", "", "")
    with pytest.raises(ReportError):
        parse_manual_location("95", "125", "")
    with pytest.raises(ReportError):
        parse_manual_location("", "", "")


def test_mock_location_flagged_for_dispatcher():
    row = build(location=Location("gps", 7.0, 125.0, mocked=True))
    assert "mock location" in row["description"]


T0 = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)  # 2:00 PM in Manila


def test_fix_is_current_only_when_recent_and_timed():
    assert Location("gps", 7.0, 125.0, fixed_at=T0).is_current(T0 + timedelta(seconds=90))
    assert not Location("gps", 7.0, 125.0, fixed_at=T0).is_current(T0 + timedelta(minutes=14))
    assert not Location("gps", 7.0, 125.0).is_current(T0)                       # unknown time is never current
    assert not Location("gps", 7.0, 125.0, fixed_at=T0, cached=True).is_current(T0)  # last-known is never current
    assert Location("gps", 7.0, 125.0, fixed_at=T0).freshness(T0 + timedelta(minutes=14)) == "OLD, 14 min ago"


def test_old_fix_is_flagged_for_dispatcher_with_its_time():
    loc = Location("gps", 7.0, 125.0, fixed_at=T0)
    row = build(location=loc, now=T0 + timedelta(minutes=14))
    assert "GPS position taken Oct 04, 2026 02:00 PM PHT, 14 min before" in row["description"]
    assert build(location=loc, now=T0 + timedelta(seconds=30))["description"] is None


def test_offline_report_carries_real_written_time_and_cached_flag():
    loc = Location("gps", 7.0, 125.0, accuracy_m=40, fixed_at=T0, cached=True)
    row = build(location=loc, description="Fell on the trail", queued_at=T0 + timedelta(hours=1))
    d = row["description"]
    assert d.startswith("Fell on the trail")
    assert "LAST KNOWN position" in d and "1 h 00 min before" in d
    assert "OFFLINE REPORT: written Oct 04, 2026 03:00 PM PHT" in d
    assert (row["latitude"], row["location_accuracy_m"], row["status"]) == (7.0, 40.0, "new")


def test_sms_text_has_coordinates_and_never_invents_them():
    row = build(location=Location("gps", 7.07321, 125.612901, accuracy_m=9, fixed_at=T0),
                description="Broken leg", citizen_name="Juan", contact_number="0917 123 4567", queued_at=T0)
    text = report.sms_text(row, fixed_at=T0, written_at=T0)
    assert "7.073210,125.612901 +/-9m" in text and "maps.google.com/?q=7.073210,125.612901" in text
    assert "Juan 0917 123 4567" in text and "Broken leg" in text and "OFFLINE" not in text
    assert "NO GPS COORDINATES" in report.sms_text(build(location=None))
    assert report.sms_url("+63 917", "a b&c").startswith("sms:+63%20917?body=a%20b%26c")
    assert report.sms_url("", "x") == "sms:?body=x"


def test_category_and_type_required():
    with pytest.raises(ReportError):
        build(incident_type="Not a real type")
    with pytest.raises(ReportError):
        build(reporter_id=None)


def test_general_sos_without_category():
    row = build(category_key=None, incident_type=None)
    assert row["emergency_type"] == catalog.GENERAL_SOS


def test_every_catalog_type_fits_database_limit():
    for c in catalog.CATEGORIES:
        for it in c.incident_types:
            assert 1 <= len(catalog.emergency_type(c.key, it)) <= 80


def test_client_request_id_is_reused_for_retries():
    a = build(client_request_id="22222222-2222-2222-2222-222222222222")
    b = build(client_request_id="22222222-2222-2222-2222-222222222222")
    assert a["client_request_id"] == b["client_request_id"]


def test_photo_url_is_a_bracketed_line_for_the_dispatcher():
    row = build_incident(reporter_id="u1", category_key=None, incident_type=None,
                         photo_url="https://x.supabase.co/storage/v1/object/public/esrms-media/incidents/u1/a.jpg")
    assert "[Photo attached by citizen: https://x.supabase.co/" in row["description"]
