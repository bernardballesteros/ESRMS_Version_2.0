"""Offline tests for the Analytics calculations, API, authorization and exports.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import csv
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import analytics as an
from app import analytics_export as export_module
from app import create_app
from app import dispatch as dsp
from app.dispatch import ValidationError
from app.routes.analytics import _db as analytics_routes_db
from tests.fake_incidents import FakeIncidentsDB

# "Now" for every test: 2026-10-05 14:30 PHT.
NOW = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)


def pht(day, hour=12, minute=0, second=0):
    """ISO timestamp (UTC, as Supabase returns it) for a PHT wall-clock time in October 2026."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=an.PHT).astimezone(timezone.utc).isoformat()


def incident(n, kind, status, created, resolved=None, priority=None, lat=7.07, lng=125.61, archived=None):
    return {"id": f"00000000-0000-0000-0000-{n:012d}", "incident_number": n, "emergency_type": kind, "status": status,
            "priority": priority, "created_at": created, "resolved_at": resolved, "latitude": lat, "longitude": lng,
            "location_verified": False, "archived_at": archived, "citizen_name": "Private Person", "contact_number": "+63 900 000 0000"}


# Known records. Current period for "7d" is Sep 29 - Oct 5; the previous one is Sep 22 - 28.
ROWS = [
    incident(1, "Fire & Hazmat: Building fire", "resolved", pht(5, 9), pht(5, 9, 20), priority=1, archived=pht(5, 10)),   # 20m
    incident(2, "Fire & Hazmat: Smoke or suspected fire", "resolved", pht(4, 8), pht(4, 8, 40)),                         # 40m
    incident(3, "Medical & EMS: Unconscious person", "new", pht(5, 13), priority=1),
    incident(4, "medical & ems: Chest pain", "acknowledged", pht(3, 23, 30)),
    incident(5, "Contacts & Roads: Road accident", "en_route", pht(2, 0, 0), lat=None, lng=None),
    incident(6, "Contacts & Roads: Road accident", "cancelled", pht(1, 7), pht(1, 7, 5)),
    incident(7, "Flooding", "on_scene", pht(5, 1)),
    # Previous period (Sep 22-28): two incidents, one resolved in 60m.
    incident(8, "Fire & Hazmat: Building fire", "resolved", "2026-09-25T02:00:00+00:00", "2026-09-25T03:00:00+00:00"),
    incident(9, "Medical & EMS: Chest pain", "dispatched", "2026-09-28T15:59:59+00:00"),   # 23:59:59 PHT on Sep 28
    # Older than both periods.
    incident(10, "Fire & Hazmat: Building fire", "resolved", "2026-09-01T02:00:00+00:00", "2026-09-01T02:10:00+00:00"),
]
EVENTS = [
    {"id": 1, "incident_id": ROWS[0]["id"], "event": "status", "to_status": "on_scene", "created_at": pht(5, 9, 6, 30)},
    {"id": 2, "incident_id": ROWS[0]["id"], "event": "status", "to_status": "on_scene", "created_at": pht(5, 9, 15)},
    {"id": 3, "incident_id": ROWS[6]["id"], "event": "status", "to_status": "on_scene", "created_at": pht(5, 2, 12)},
    {"id": 4, "incident_id": ROWS[0]["id"], "event": "status", "to_status": "resolved", "created_at": pht(5, 9, 20)},
]


class PeriodTests(unittest.TestCase):
    def test_presets_are_whole_pht_days(self):
        p = an.resolve_period("7d", now=NOW)
        self.assertEqual((p.start_date.isoformat(), p.end_date.isoformat(), p.days), ("2026-09-29", "2026-10-05", 7))
        self.assertEqual(an.utc_param(p.start), "2026-09-28T16:00:00Z")     # midnight PHT
        self.assertEqual(an.utc_param(p.end), "2026-10-05T16:00:00Z")
        self.assertEqual(p.start - p.prev_start, timedelta(days=7))

    def test_each_preset(self):
        expect = {"today": (1, "hour"), "7d": (7, "day"), "30d": (30, "day"), "90d": (90, "day"), "year": (278, "month")}
        for key, (days, granularity) in expect.items():
            p = an.resolve_period(key, now=NOW)
            self.assertEqual((p.days, p.granularity), (days, granularity), key)
            self.assertEqual(p.end_date.isoformat(), "2026-10-05")

    def test_today_follows_pht_not_utc(self):
        late = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)   # already Oct 6 in Manila
        self.assertEqual(an.resolve_period("today", now=late).start_date.isoformat(), "2026-10-06")

    def test_unknown_range_falls_back_to_default(self):
        self.assertEqual(an.resolve_period("nonsense", now=NOW).key, an.DEFAULT_RANGE)

    def test_custom(self):
        p = an.resolve_period("custom", "2026-10-01", "2026-10-03", now=NOW)
        self.assertEqual((p.days, p.granularity), (3, "day"))
        self.assertEqual(an.period_json(p)["previous"], {"from": "2026-09-28", "to": "2026-09-30"})
        self.assertEqual(an.resolve_period("custom", "2026-10-03", "2026-10-03", now=NOW).days, 1)

    def test_custom_rejects_bad_ranges(self):
        for start, end, field in [("2026-10-04", "2026-10-03", "from"), ("", "2026-10-03", "from"),
                                  ("2026-10-01", "not-a-date", "to"), ("2015-01-01", "2026-10-03", "to")]:
            with self.assertRaises(ValidationError) as cm:
                an.resolve_period("custom", start, end, now=NOW)
            self.assertIn(field, cm.exception.errors, (start, end))


class RuleTests(unittest.TestCase):
    def test_every_status_is_in_exactly_one_group(self):
        grouped = [s for _, statuses in an.GROUPS.values() for s in statuses]
        self.assertEqual(sorted(grouped), sorted(dsp.STATUSES))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_pending_matches_the_dispatch_queue_rule(self):
        rows = [{"status": s, "priority": None} for s in dsp.STATUSES]
        self.assertEqual(dsp.counts(rows)["queue"], len(an.GROUPS["pending"][1]))

    def test_category(self):
        self.assertEqual(an.category_of("Fire & Hazmat: Building fire"), "Fire & Hazmat")
        self.assertEqual(an.category_of("Flooding"), "Flooding")
        self.assertEqual(an.category_of(" : odd"), an.UNCATEGORIZED)
        self.assertEqual(an.category_key("MEDICAL & EMS: x"), an.category_key("Medical & EMS: y"))

    def test_format_duration(self):
        cases = {None: "—", 0: "0s", 59.4: "59s", 60: "1m 00s", 1115: "18m 35s", 4320: "1h 12m", 90000: "1d 1h"}
        for seconds, text in cases.items():
            self.assertEqual(an.format_duration(seconds), text)

    def test_pct_change(self):
        self.assertEqual(an.pct_change(150, 100), 50.0)
        self.assertEqual(an.pct_change(0, 4), -100.0)
        self.assertIsNone(an.pct_change(5, 0))      # nothing before: no percentage, not infinity
        self.assertIsNone(an.pct_change(None, 10))
        self.assertIsNone(an.pct_change(10, None))

    def test_resolution_seconds_only_for_valid_resolved(self):
        self.assertEqual(an.resolution_seconds(ROWS[0]), 1200)
        self.assertIsNone(an.resolution_seconds(ROWS[5]))                                           # cancelled
        self.assertIsNone(an.resolution_seconds({"status": "resolved", "created_at": pht(5), "resolved_at": None}))
        self.assertIsNone(an.resolution_seconds({"status": "resolved", "created_at": pht(5), "resolved_at": pht(4)}))

    def test_response_is_not_resolution(self):
        self.assertEqual(an.response_seconds(ROWS[0], EVENTS[0]["created_at"]), 390)
        self.assertIsNone(an.response_seconds(ROWS[0], None))


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.period = an.resolve_period("7d", now=NOW)
        self.s = an.summarize(ROWS, self.period, now=NOW)

    def test_kpis_against_known_records(self):
        k = self.s["kpis"]
        self.assertEqual(k["total"], {"value": 7, "previous": 2, "change": 250.0})
        self.assertEqual((k["resolved"]["value"], k["resolved"]["previous"], k["resolved"]["change"]), (2, 1, 100.0))
        self.assertEqual(k["resolved"]["rate"], 28.6)             # 2 of 7
        self.assertEqual((k["pending"]["value"], k["pending"]["share"]), (2, 28.6))
        self.assertIsNone(k["pending"]["change"])                 # none pending before: not comparable
        self.assertEqual(k["resolution"]["seconds"], 1800)        # (20m + 40m) / 2; the cancelled one is excluded
        self.assertEqual(k["resolution"]["label"], "30m 00s")
        self.assertEqual(k["resolution"]["sample"], 2)
        self.assertEqual(k["resolution"]["change"], -50.0)        # 30m against 60m

    def test_status_groups_add_up_and_critical_is_separate(self):
        counts = {g["key"]: g["count"] for g in self.s["status"]}
        self.assertEqual(counts, {"resolved": 2, "in_progress": 2, "pending": 2, "cancelled": 1})
        self.assertEqual(sum(counts.values()), self.s["kpis"]["total"]["value"])
        self.assertEqual(self.s["critical"], {"count": 2, "pct": 28.6})

    def test_categories_merge_case_and_sort_by_count(self):
        cats = [(c["label"], c["count"]) for c in self.s["categories"]]
        self.assertEqual(cats, [("Contacts & Roads", 2), ("Fire & Hazmat", 2), ("Medical & EMS", 2), ("Flooding", 1)])
        self.assertEqual(sum(n for _, n in cats), 7)

    def test_duplicates_do_not_inflate(self):
        doubled = an.summarize(ROWS + ROWS[:4], self.period, now=NOW)
        self.assertEqual(doubled["kpis"], self.s["kpis"])

    def test_daily_trend_includes_zero_days_and_pht_boundaries(self):
        buckets = self.s["trend"]["buckets"]
        self.assertEqual([b["key"] for b in buckets], [f"2026-09-{d}" for d in (29, 30)] + [f"2026-10-0{d}" for d in range(1, 6)])
        self.assertEqual([b["total"] for b in buckets], [0, 0, 1, 1, 1, 1, 3])   # 23:30 PHT stays on Oct 3
        self.assertEqual([b["resolved"] for b in buckets], [0, 0, 0, 0, 0, 1, 1])
        self.assertEqual([b["pending"] for b in buckets], [0, 0, 0, 0, 1, 0, 1])
        self.assertEqual(sum(b["total"] for b in buckets), 7)

    def test_hourly_trend_stops_at_now(self):
        s = an.summarize(ROWS, an.resolve_period("today", now=NOW), now=NOW)
        buckets = s["trend"]["buckets"]
        self.assertEqual((len(buckets), buckets[0]["label"], buckets[-1]["label"]), (15, "00:00", "14:00"))
        self.assertEqual({b["label"]: b["total"] for b in buckets if b["total"]}, {"01:00": 1, "09:00": 1, "13:00": 1})
        self.assertEqual(s["kpis"]["total"], {"value": 3, "previous": 1, "change": 200.0})

    def test_monthly_trend(self):
        s = an.summarize(ROWS, an.resolve_period("year", now=NOW), now=NOW)
        buckets = s["trend"]["buckets"]
        self.assertEqual((len(buckets), buckets[0]["label"], buckets[-1]["label"]), (10, "Jan 2026", "Oct 2026"))
        self.assertEqual([b["total"] for b in buckets[-2:]], [3, 7])

    def test_empty_period_has_no_made_up_numbers(self):
        s = an.summarize([], self.period, now=NOW)
        k = s["kpis"]
        self.assertEqual((k["total"]["value"], k["total"]["change"]), (0, None))
        self.assertEqual((k["resolved"]["rate"], k["pending"]["share"], k["resolution"]["seconds"], k["resolution"]["label"]),
                         (None, None, None, "—"))
        self.assertEqual(s["categories"], [])
        self.assertTrue(all(b["total"] == 0 for b in s["trend"]["buckets"]))


class AnalyticsApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()
        self.db = FakeIncidentsDB(ROWS, EVENTS)
        # Pages look up the signed-in user's display size (Settings -> Display); keep that offline too.
        display = mock.patch("app.routes.account._load_display_row", return_value=None)
        display.start()
        self.addCleanup(display.stop)
        for target in ("app.routes.analytics._db",):
            patcher = mock.patch(target, return_value=self.db)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = mock.patch("app.analytics.datetime", wraps=datetime)
        clock.start().now.side_effect = lambda tz=None: NOW.astimezone(tz) if tz else NOW
        self.addCleanup(clock.stop)

    def login(self, role="organization"):
        with self.client.session_transaction() as s:
            s["user"] = {"id": "00000000-0000-0000-0000-000000000001", "email": "d@example.com", "role": role, "name": "D"}

    def get(self, path, **params):
        return self.client.get(path, query_string=params)

    def codes(self, **params):
        res = self.get("/api/analytics/incidents", range="7d", **params)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return [i["code"] for i in res.get_json()["items"]]

    def test_requires_sign_in_and_a_dispatch_role(self):
        paths = ["/api/analytics/summary", "/api/analytics/incidents", "/api/analytics/export"]
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        for role in ("citizen", "responder"):
            self.login(role)
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 403, (role, path))
        self.assertEqual(self.db.queries, 0)

    def test_summary(self):
        self.login()
        data = self.get("/api/analytics/summary", range="7d").get_json()
        self.assertEqual(data["period"]["from"], "2026-09-29")
        self.assertEqual(data["kpis"]["total"], {"value": 7, "previous": 2, "change": 250.0})
        self.assertEqual(self.db.queries, 1)   # current and previous period come from one query

    def test_summary_pages_through_large_ranges(self):
        many = [incident(1000 + n, "Fire & Hazmat: Building fire", "resolved", pht(4, n % 24, n % 60), pht(5, 0)) for n in range(2300)]
        self.db.tables["emergency_incidents"] = many
        self.login()
        data = self.get("/api/analytics/summary", range="7d").get_json()
        self.assertEqual(data["kpis"]["total"]["value"], 2300)
        self.assertEqual(self.db.queries, 3)

    def test_custom_range_validation(self):
        self.login()
        res = self.get("/api/analytics/summary", range="custom", **{"from": "2026-10-04", "to": "2026-10-01"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("start date cannot be later", res.get_json()["error"])
        ok = self.get("/api/analytics/summary", range="custom", **{"from": "2026-10-04", "to": "2026-10-05"}).get_json()
        self.assertEqual(ok["kpis"]["total"]["value"], 4)   # the 23:30 PHT report on Oct 3 stays out

    def test_database_failure_is_an_error_not_zeros(self):
        self.login()
        with mock.patch("app.routes.analytics._db", side_effect=RuntimeError("connection refused")):
            res = self.get("/api/analytics/summary", range="7d")
        self.assertEqual(res.status_code, 502)
        self.assertIn("connection refused", res.get_json()["error"])

    def test_table_is_newest_first_and_hides_personal_data(self):
        self.login()
        res = self.get("/api/analytics/incidents", range="7d").get_json()
        self.assertEqual([i["code"] for i in res["items"]],
                         ["INC-000003", "INC-000001", "INC-000007", "INC-000002", "INC-000004", "INC-000005", "INC-000006"])
        self.assertEqual((res["total"], res["pages"]), (7, 1))
        for item in res["items"]:
            self.assertFalse({"citizen_name", "contact_number", "description"} & set(item))
        first = {i["code"]: i for i in res["items"]}
        self.assertEqual(first["INC-000001"]["response_label"], "6m 30s")      # first on_scene event, not the second
        self.assertEqual(first["INC-000001"]["archived"], True)
        self.assertIsNone(first["INC-000003"]["response_seconds"])
        self.assertEqual((first["INC-000005"]["has_location"], first["INC-000005"]["latitude"]), (False, None))

    def test_table_filters(self):
        self.login()
        self.assertEqual(self.codes(status="pending"), ["INC-000003", "INC-000004"])
        self.assertEqual(self.codes(status="in_progress"), ["INC-000007", "INC-000005"])
        self.assertEqual(self.codes(category="medical & ems"), ["INC-000003", "INC-000004"])   # either spelling
        self.assertEqual(self.codes(category="flooding"), ["INC-000007"])
        self.assertEqual(self.codes(priority="1"), ["INC-000003", "INC-000001"])
        self.assertEqual(self.codes(status="resolved", category="fire & hazmat", priority="1"), ["INC-000001"])
        self.assertEqual(self.codes(status="bogus"), self.codes())

    def test_table_filter_counts_match_the_summary(self):
        self.login()
        s = self.get("/api/analytics/summary", range="7d").get_json()
        for group in s["status"]:
            self.assertEqual(len(self.codes(status=group["key"], per_page=50)), group["count"], group["key"])
        for cat in s["categories"]:
            self.assertEqual(len(self.codes(category=cat["key"], per_page=50)), cat["count"], cat["key"])

    def test_search(self):
        self.login()
        self.assertEqual(self.codes(q="INC-000005"), ["INC-000005"])
        self.assertEqual(self.codes(q="5"), ["INC-000005"])
        self.assertEqual(self.codes(q="road accident"), ["INC-000005", "INC-000006"])
        self.assertEqual(self.codes(q="%"), self.codes())          # wildcards are stripped, not executed
        self.assertEqual(self.codes(q="zzz"), [])

    def test_sorting(self):
        self.login()
        self.assertEqual(self.codes(sort="id", dir="asc"), [f"INC-00000{n}" for n in range(1, 8)])
        self.assertEqual(self.codes(sort="reported", dir="asc")[0], "INC-000006")
        self.assertEqual(self.codes(sort="status", dir="asc")[0], "INC-000004")
        self.assertEqual(self.codes(sort="drop table"), self.codes())

    def test_pagination(self):
        self.db.tables["emergency_incidents"] = [incident(n, "Flooding", "new", pht(5, 0, n % 60, n // 60)) for n in range(1, 24)]
        self.login()
        page1 = self.get("/api/analytics/incidents", range="7d").get_json()
        self.assertEqual((len(page1["items"]), page1["total"], page1["pages"], page1["per_page"]), (10, 23, 3, 10))
        page3 = self.get("/api/analytics/incidents", range="7d", page=3).get_json()
        self.assertEqual(len(page3["items"]), 3)
        seen = {i["id"] for n in (1, 2, 3) for i in self.get("/api/analytics/incidents", range="7d", page=n).get_json()["items"]}
        self.assertEqual(len(seen), 23)                            # no row repeated or skipped across pages
        past = self.get("/api/analytics/incidents", range="7d", page=9).get_json()
        self.assertEqual((past["page"], len(past["items"])), (1, 10))   # falls back instead of failing
        self.assertEqual(self.get("/api/analytics/incidents", range="7d", per_page=25).get_json()["pages"], 1)
        self.assertEqual(self.get("/api/analytics/incidents", range="7d", per_page=9999).get_json()["per_page"], 10)

    def test_csv_export(self):
        self.login()
        res = self.get("/api/analytics/export", range="7d", format="csv", status="resolved")
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment; filename=\"esrms-analytics_2026-09-29_to_2026-10-05_", res.headers["Content-Disposition"])
        self.assertEqual((res.headers["X-Record-Count"], res.headers["X-Record-Total"]), ("2", "2"))
        rows = list(csv.reader(io.StringIO(res.get_data().decode("utf-8-sig"))))
        flat = {tuple(r) for r in rows}
        self.assertEqual(rows[0], ["ESRMS Analytics Report"])
        self.assertIn(("Total incidents", "7", "2", "+250.0%"), flat)                 # summaries cover the whole range
        self.assertIn(("Average resolution time", "30m 00s", "1h 00m", "-50.0%"), flat)
        self.assertIn(("Pending", "2", "28.6%"), flat)
        self.assertIn(("Fire & Hazmat", "2", "28.6%"), flat)
        self.assertIn(["Record filters", "status: Resolved"], rows)
        self.assertEqual(res.headers["X-Period-Total"], "7")                           # aggregate total, not the row count
        self.assertEqual(res.headers["X-Export-Truncated"], "0")
        self.assertIn(["Incidents reported in period (aggregate total)", "7"], rows)
        self.assertIn(["Incidents matching the record filters", "2"], rows)
        self.assertIn(["Incident rows in this file", "2 of 2"], rows)
        self.assertIn(["Export status", "COMPLETE - all 2 matching incident(s) are listed."], rows)
        self.assertEqual(rows[1][0], "Reporting period")
        self.assertEqual(rows[2], ["Generated", "2026-10-05 14:30:00 PHT"])
        records = rows[rows.index(["Incident records - complete: 2 of 2 matching incidents"]) + 2:]
        self.assertEqual([r[0] for r in records], ["INC-000001", "INC-000002"])       # records follow the filter
        self.assertEqual(records[0][8:12], ["6m 30s", "20m 00s", "2026-10-05 09:00:00", "2026-10-05 09:20:00"])
        self.assertNotIn("Private Person", res.get_data(as_text=True))

    def test_csv_export_neutralises_formulas(self):
        self.db.tables["emergency_incidents"] = [incident(1, "=HYPERLINK(\"http://x\")", "new", pht(5, 9))]
        self.login()
        body = self.get("/api/analytics/export", range="7d").get_data().decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)

    def test_export_of_an_empty_range_is_refused(self):
        self.login()
        res = self.get("/api/analytics/export", range="custom", **{"from": "2026-08-01", "to": "2026-08-02"})
        self.assertEqual(res.status_code, 404)
        self.assertIn("nothing to export", res.get_json()["error"])
        self.assertEqual(self.get("/api/analytics/export", range="7d", format="docx").status_code, 422)

    def test_export_says_when_filters_match_nothing(self):
        self.login()
        res = self.get("/api/analytics/export", range="7d", q="zzz")
        self.assertEqual(res.headers["X-Record-Count"], "0")
        body = res.get_data().decode("utf-8-sig")
        self.assertIn("NO RECORDS - no incident matches the record filters", body)
        self.assertIn("Incident rows in this file,0 of 0", body)
        self.assertIn("Total incidents,7,2", body)                 # the summary still covers the whole period
        self.assertNotIn("COMPLETE", body)

    def test_truncated_export_is_labelled_partial_everywhere(self):
        self.login()
        for fmt in ("csv", "pdf"):
            with mock.patch("app.routes.analytics.MAX_EXPORT_ROWS", 3):
                res = self.get("/api/analytics/export", range="7d", format=fmt)
            self.assertEqual(res.status_code, 200, fmt)
            self.assertEqual((res.headers["X-Record-Count"], res.headers["X-Record-Total"], res.headers["X-Period-Total"],
                              res.headers["X-Export-Truncated"]), ("3", "7", "7", "1"), fmt)
            self.assertIn("_PARTIAL." + fmt, res.headers["Content-Disposition"])
        with mock.patch("app.routes.analytics.MAX_EXPORT_ROWS", 3):
            body = self.get("/api/analytics/export", range="7d").get_data().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(body)))
        status = next(r[1] for r in rows if r and r[0] == "Export status")
        self.assertTrue(status.startswith("TRUNCATED - PARTIAL RECORD LIST. Only the newest 3 of 7 matching incidents"), status)
        self.assertIn("4 are NOT included", status)
        self.assertIn(["Incident rows in this file", "3 of 7"], rows)
        self.assertIn(["Incident records - PARTIAL: newest 3 of 7 matching incidents"], rows)
        self.assertEqual(rows[-1], ["*** END OF PARTIAL LIST - 4 more matching incident(s) are not in this file ***"])
        self.assertIn(["Total incidents", "7", "2", "+250.0%"], rows)                  # aggregates are not truncated
        listed = [r[0] for r in rows if r and r[0].startswith("INC-")]
        self.assertEqual(listed, ["INC-000003", "INC-000001", "INC-000007"])           # the newest three
        self.assertNotIn("COMPLETE", body)

    def test_export_at_exactly_the_limit_is_complete(self):
        self.login()
        with mock.patch("app.routes.analytics.MAX_EXPORT_ROWS", 7):
            res = self.get("/api/analytics/export", range="7d")
        self.assertEqual((res.headers["X-Record-Count"], res.headers["X-Export-Truncated"]), ("7", "0"))
        self.assertNotIn("PARTIAL", res.get_data().decode("utf-8-sig"))

    def test_export_pages_past_one_fetch_without_losing_rows(self):
        self.db.tables["emergency_incidents"] = [incident(n, "Flooding", "new", pht(5, 0, n % 60, n // 60 % 60)) for n in range(1, 2301)]
        self.login()
        res = self.get("/api/analytics/export", range="7d")
        self.assertEqual((res.headers["X-Record-Count"], res.headers["X-Record-Total"], res.headers["X-Export-Truncated"]), ("2300", "2300", "0"))
        body = res.get_data().decode("utf-8-sig")
        self.assertEqual(len({line.split(",")[0] for line in body.splitlines() if line.startswith("INC-")}), 2300)

    def test_export_build_failure_is_a_clear_error(self):
        self.login()
        with mock.patch("app.analytics_export.build_pdf", side_effect=ValueError("font table broken")):
            res = self.get("/api/analytics/export", range="7d", format="pdf")
        self.assertEqual(res.status_code, 500)
        self.assertIn("could not be generated", res.get_json()["error"])
        with mock.patch("app.analytics_export.build_pdf", side_effect=export_module.PdfUnavailable()):
            self.assertEqual(self.get("/api/analytics/export", range="7d", format="pdf").status_code, 501)

    def test_one_database_client_per_request(self):
        self.login()
        with mock.patch("app.routes.analytics._db", new=analytics_routes_db), \
                mock.patch("app.routes.analytics._new_db", return_value=self.db) as factory:
            self.assertEqual(self.get("/api/analytics/incidents", range="7d").status_code, 200)
            self.assertEqual(factory.call_count, 1)                # list query and arrival query share it
            self.assertEqual(self.get("/api/analytics/export", range="7d").status_code, 200)
            self.assertEqual(factory.call_count, 2)

    def test_pdf_export(self):
        self.login()
        res = self.get("/api/analytics/export", range="7d", format="pdf")
        self.assertEqual((res.status_code, res.mimetype), (200, "application/pdf"))
        self.assertTrue(res.get_data().startswith(b"%PDF-"))
        self.assertEqual(res.headers["X-Record-Count"], "7")

    def test_dashboard_page_contains_the_analytics_tab(self):
        self.app.config["ANNOUNCEMENT_MANAGER_ROLES"] = set()   # keep the announcement module out of this test
        self.login()
        res = self.client.get("/dashboard")
        html = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn('id="analytics"', html)
        self.assertIn("/api/analytics/summary", html)
        self.assertNotIn("not built yet", html)


if __name__ == "__main__":
    unittest.main()
