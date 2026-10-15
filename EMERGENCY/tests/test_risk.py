"""Offline tests for High-Risk Area Detection: grouping, score, levels, trend, coverage, API and authorization.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import math
import unittest
from datetime import datetime, timezone
from unittest import mock

from app import analytics as an
from app import create_app
from app import risk
from tests.fake_incidents import FakeIncidentsDB

# "Now" for every test: 2026-10-05 14:30 PHT. "7d" is Sep 29 - Oct 5; the period before is Sep 22 - 28.
NOW = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)
PERIOD = an.resolve_period("7d", now=NOW)

A, B, C, D = (7.0700, 125.6100), (7.1000, 125.6400), (7.1500, 125.5500), (7.2000, 125.4500)


def at(month, day, hour=12):
    return datetime(2026, month, day, hour, tzinfo=an.PHT).astimezone(timezone.utc).isoformat()


def incident(n, where, created, kind="Medical & EMS: Chest pain", priority=None, status="resolved"):
    lat, lng = where or (None, None)
    return {"id": f"00000000-0000-0000-0000-{n:012d}", "incident_number": n, "emergency_type": kind, "status": status,
            "priority": priority, "created_at": created, "latitude": lat, "longitude": lng, "archived_at": None,
            "citizen_name": "Private Person", "contact_number": "+63 900 000 0000", "description": "private"}


def base(n, name, kind, where, availability="ready", **over):
    return {"id": f"org-{n}", "name": name, "org_type": kind, "latitude": where[0], "longitude": where[1],
            "availability": availability, "is_active": True, "verified": True, **over}


ROWS = [
    # Area A: 6 incidents, two critical, one high. Mostly medical.
    incident(1, A, at(10, 5, 9), priority=1), incident(2, A, at(10, 4, 9), priority=1), incident(3, A, at(10, 3, 9), priority=2),
    incident(4, A, at(10, 2, 9)), incident(5, A, at(10, 1, 9), kind="Fire & Hazmat: Building fire"),
    incident(6, A, at(9, 30, 9), kind="medical & ems: Unconscious person"),
    # Area B: 3 incidents.
    incident(7, B, at(10, 4, 10), kind="Fire & Hazmat: Smoke"), incident(8, B, at(10, 2, 10), kind="Fire & Hazmat: Smoke"),
    incident(9, B, at(9, 30, 10), kind="Contacts & Roads: Road accident"),
    # Area C: 2 incidents. Area D: one P3 incident reported exactly half-way through the period.
    incident(10, C, at(10, 3, 11)), incident(11, C, at(10, 1, 11)),
    incident(12, D, at(10, 2, 12), kind="Flooding", priority=3),
    # No GPS location: counted, never placed.
    incident(13, None, at(10, 4, 13)),
    # Previous period (Sep 22-28): A had 2, B had 3, C had 2.
    incident(20, A, at(9, 24)), incident(21, A, at(9, 26)),
    incident(22, B, at(9, 23)), incident(23, B, at(9, 25)), incident(24, B, at(9, 27)),
    incident(25, C, at(9, 23)), incident(26, C, at(9, 27)),
    # Older than both periods.
    incident(30, D, at(9, 1)),
]
BASES = [
    base(1, "Talomo EMS Base", "ems", (A[0] + 0.004, A[1])),                          # ~0.4 km from A
    base(2, "Talomo Barangay Response Center", "barangay", (A[0] + 0.02, A[1])),      # ~2.2 km
    base(3, "Central Fire Station", "fire", (A[0] + 0.03, A[1]), availability="limited"),
    base(4, "Southern Medical Center", "hospital", (A[0], A[1] + 0.03)),
    base(5, "Far Police Station", "police", (A[0] + 0.2, A[1])),                      # ~22 km: outside the 5 km radius
    base(6, "Unverified EMS", "ems", A, verified=False),
]


def by_label(result):
    return {a["id"]: a for a in result["areas"]}


class GridTests(unittest.TestCase):
    def test_nearby_points_share_an_area_and_distant_ones_do_not(self):
        row, col = risk.cell_of(*A, 1.0)
        centre = ((row + 0.5) / 110.574, None)
        self.assertEqual(risk.cell_of(centre[0], A[1], 1.0)[0], risk.cell_of(centre[0] + 0.001, A[1], 1.0)[0])   # ~110 m apart
        self.assertNotEqual(risk.cell_of(*A, 1.0), risk.cell_of(A[0] + 0.03, A[1], 1.0))                          # ~3.3 km apart
        self.assertNotEqual(risk.cell_of(*A, 1.0), risk.cell_of(A[0], A[1] + 0.03, 1.0))
        self.assertEqual(risk.cell_of(*A, 50.0), risk.cell_of(A[0] + 0.03, A[1], 50.0))                           # bigger cells merge them


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.result = risk.analyze(ROWS, PERIOD, BASES)
        self.areas = self.result["areas"]

    def area(self, where):
        return next(a for a in self.areas if a["id"] == risk.area_id(risk.cell_of(*where, 1.0)))

    def test_counts_come_from_the_records(self):
        self.assertEqual(self.result["totals"], {**self.result["totals"], "incidents": 13, "located": 12, "unlocated": 1,
                                                 "areas": 4, "previous_located": 7})
        self.assertEqual([self.area(w)["count"] for w in (A, B, C, D)], [6, 3, 2, 1])
        self.assertEqual(sum(a["count"] for a in self.areas), 12)
        self.assertEqual([a["rank"] for a in self.areas], [1, 2, 3, 4])
        self.assertEqual(self.areas[0]["id"], self.area(A)["id"])

    def test_score_is_frequency_plus_severity_plus_recency(self):
        a, d = self.area(A), self.area(D)
        self.assertEqual((a["score_parts"]["frequency"], a["score_parts"]["severity"]), (6, 5.0))   # P1 +2, P1 +2, P2 +1
        self.assertEqual(d["score_parts"], {"frequency": 1, "severity": 0.5, "recency": 0.5})      # half-way through the period
        self.assertEqual(d["score"], 2.0)
        for area in self.areas:
            self.assertAlmostEqual(area["score"], sum(area["score_parts"].values()), places=1)
        self.assertEqual(self.areas, sorted(self.areas, key=lambda x: -x["score"]))

    def test_levels_follow_the_score_not_the_name(self):
        self.assertTrue(self.result["sufficient"])
        self.assertEqual((self.area(A)["level"], self.area(D)["level"]), ("high", "low"))
        average = self.result["totals"]["average_score"]
        for area in self.areas:
            ratio = area["score"] / average
            expected = "high" if ratio >= 1.5 and area["count"] >= 3 else "moderate" if ratio >= 1.0 else "low"
            self.assertEqual(area["level"], expected, area["label"])
        # The same records with a stricter threshold give a different answer: the level is calculated.
        strict = risk.analyze(ROWS, PERIOD, BASES, risk.Settings(high_ratio=50))
        self.assertNotIn("high", [a["level"] for a in strict["areas"]])

    def test_most_common_type_and_breakdown(self):
        a = self.area(A)
        self.assertEqual((a["top_category"]["label"], a["top_category"]["count"]), ("Medical & EMS", 5))
        self.assertEqual([(c["label"], c["count"]) for c in a["categories"]], [("Medical & EMS", 5), ("Fire & Hazmat", 1)])
        self.assertEqual(self.area(B)["top_category"]["label"], "Fire & Hazmat")

    def test_trend_against_the_previous_period(self):
        a, b, c, d = (self.area(w) for w in (A, B, C, D))
        self.assertEqual((a["previous"], a["trend"], a["change"]), (2, "increasing", 200.0))
        self.assertEqual((b["previous"], b["trend"], b["change"]), (3, "stable", 0.0))
        self.assertEqual((c["previous"], c["trend"], c["change"]), (2, "stable", 0.0))
        self.assertEqual((d["previous"], d["trend"], d["change"]), (0, "increasing", None))   # no percentage is invented
        fewer = risk.analyze([r for r in ROWS if r["incident_number"] not in (7, 8)], PERIOD, BASES)
        self.assertEqual(by_label(fewer)[b["id"]]["trend"], "decreasing")

    def test_no_previous_data_means_no_trend(self):
        result = risk.analyze([r for r in ROWS if r["incident_number"] < 20], PERIOD, BASES)
        self.assertEqual({a["trend"] for a in result["areas"]}, {"insufficient"})
        self.assertEqual({a["change"] for a in result["areas"]}, {None})

    def test_filters(self):
        fire = risk.analyze(ROWS, PERIOD, BASES, category="fire & hazmat")
        self.assertEqual(fire["totals"]["located"], 3)
        self.assertEqual(sorted(a["count"] for a in fire["areas"]), [1, 2])
        critical = risk.analyze(ROWS, PERIOD, BASES, priority=1)
        self.assertEqual([a["count"] for a in critical["areas"]], [2])
        self.assertFalse(critical["sufficient"])

    def test_small_datasets_are_not_given_levels(self):
        few = risk.analyze(ROWS[:6], PERIOD, BASES)   # six incidents, all in one area
        self.assertFalse(few["sufficient"])
        self.assertIn(risk.INSUFFICIENT_TEXT, few["reason"])
        self.assertEqual([(a["count"], a["level"]) for a in few["areas"]], [(6, "insufficient")])
        empty = risk.analyze([], PERIOD, BASES)
        self.assertEqual((empty["areas"], empty["sufficient"]), ([], False))
        self.assertIn("No incidents were reported", empty["reason"])
        unlocated = risk.analyze([incident(1, None, at(10, 4))], PERIOD, BASES)
        self.assertEqual((unlocated["areas"], unlocated["totals"]["unlocated"]), ([], 1))

    def test_areas_are_named_from_real_bases_or_coordinates(self):
        self.assertEqual(self.area(A)["label"], "Near Talomo EMS Base")   # the closest verified base, not the unverified one
        self.assertEqual(self.area(D)["label"], f"Area {D[0]:.4f}, {D[1]:.4f}")
        self.assertTrue(risk.analyze(ROWS, PERIOD)["areas"][0]["label"].startswith("Area "))

    def test_settings_come_from_config(self):
        s = risk.settings_from({"RISK_CELL_KM": "2.5", "RISK_MIN_INCIDENTS": "4", "RISK_HIGH_RATIO": "nonsense", "RISK_MIN_AREAS": "-1"})
        self.assertEqual((s.cell_km, s.min_incidents, s.high_ratio, s.min_areas), (2.5, 4, 1.5, 3))


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.result = risk.analyze(ROWS, PERIOD, BASES)
        self.detail = risk.detail(self.result["areas"][0], BASES)

    def test_organizations_are_fixed_bases_by_distance(self):
        names = [o["name"] for o in self.detail["organizations"]]
        self.assertEqual(names, ["Talomo EMS Base", "Talomo Barangay Response Center", "Central Fire Station",
                                 "Southern Medical Center", "Far Police Station"])   # unverified base left out; ties by name
        ems, police = self.detail["organizations"][0], self.detail["organizations"][-1]
        self.assertAlmostEqual(ems["distance_km"], 0.4, places=1)
        self.assertEqual((ems["within"], ems["available"], police["within"]), (True, True, False))
        self.assertEqual(set(self.detail["organizations"][0]), {"id", "name", "org_type", "org_type_label", "base_label", "latitude",
                                                                "longitude", "distance_km", "availability", "status_label",
                                                                "available", "within"})

    def test_coverage_and_gap_follow_the_data(self):
        cover = self.detail["coverage"]
        self.assertEqual((cover["within"], cover["available"], cover["level"]), (4, 3, "good"))
        self.assertEqual((cover["missing_types"], cover["potential_gap"]), ([], False))   # an available EMS base is near
        without_ems = [b for b in BASES if b["id"] != "org-1"]
        thin = risk.detail(self.result["areas"][0], without_ems)
        self.assertEqual((thin["coverage"]["available"], thin["coverage"]["level"]), (2, "limited"))
        self.assertEqual((thin["coverage"]["missing_types"], thin["coverage"]["potential_gap"]), (["EMS"], True))
        self.assertTrue(any("Review EMS coverage" in text for text in thin["recommendations"]))
        # The same thin coverage around an area that is not HIGH raises nothing.
        low = risk.detail(self.result["areas"][-1], [])
        self.assertEqual((low["coverage"]["level"], low["coverage"]["potential_gap"], low["recommendations"]), ("none", False, []))

    def test_recommendations_cite_the_figures(self):
        text = " ".join(self.detail["recommendations"])
        self.assertIn("Monitor the recurring incident type: Medical & EMS (5 of 6 incidents)", text)
        self.assertIn("Coordinate with Talomo Barangay Response Center", text)
        self.assertIn("Review hospital receiving capacity at Southern Medical Center", text)
        self.assertIn("increasing", text)
        self.assertNotIn("dispatch", text.lower())

    def test_recent_incidents_and_points_carry_no_personal_data(self):
        self.assertEqual([r["code"] for r in self.detail["recent"]],
                         ["INC-000001", "INC-000002", "INC-000003", "INC-000004", "INC-000005", "INC-000006"])
        self.assertEqual(len(self.detail["points"]), 6)
        self.assertNotIn("Private Person", str(self.detail))
        self.assertNotIn("+63 900", str(self.detail))
        self.assertNotIn("_members", self.detail["area"])


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.result = risk.analyze(ROWS, PERIOD, BASES)
        self.radar = risk.radar(self.result, BASES)

    def test_points_are_placed_by_their_own_coordinates(self):
        centre = self.radar["center"]
        self.assertAlmostEqual(centre["latitude"], (A[0] + D[0]) / 2, places=5)       # middle of the incidents' bounding box
        self.assertAlmostEqual(centre["longitude"], (D[1] + B[1]) / 2, places=5)
        self.assertEqual(len(self.radar["incidents"]), 12)                             # the one without GPS is never placed
        for point in self.radar["incidents"]:
            row = next(r for r in ROWS if r["id"] == point["id"])
            self.assertEqual((point["latitude"], point["longitude"]), (row["latitude"], row["longitude"]))
            self.assertAlmostEqual(math.hypot(point["east_km"], point["north_km"]),
                                   risk.distance_km(centre["latitude"], centre["longitude"], row["latitude"], row["longitude"]), delta=0.1)
            self.assertLessEqual(math.hypot(point["east_km"], point["north_km"]), self.radar["range_km"])
        first = self.radar["incidents"][0]
        self.assertEqual((first["code"], first["priority_label"], first["category"]), ("INC-000001", "P1 - Critical", "Medical & EMS"))
        self.assertGreater(first["east_km"], 0)    # A is the southernmost area and east of the centre
        self.assertLess(first["north_km"], 0)
        self.assertNotIn("Private Person", str(self.radar))
        self.assertNotIn("+63 900", str(self.radar))

    def test_moving_an_incident_moves_its_point(self):
        moved = [dict(r, latitude=r["latitude"] + 0.01) if r["incident_number"] == 12 else r for r in ROWS]
        before = next(p for p in self.radar["incidents"] if p["code"] == "INC-000012")
        after = next(p for p in risk.radar(risk.analyze(moved, PERIOD, BASES), BASES)["incidents"] if p["code"] == "INC-000012")
        self.assertNotEqual((before["east_km"], before["north_km"]), (after["east_km"], after["north_km"]))

    def test_zones_carry_coverage_and_nearest_bases(self):
        zone = self.radar["areas"][0]
        self.assertEqual((zone["level"], zone["count"], zone["open"]), ("high", 6, 0))
        self.assertEqual([(o["org_type"], o["distance_km"]) for o in zone["nearest"]],
                         [("ems", 0.4), ("fire", 3.3), ("police", 22.2), ("barangay", 2.2), ("hospital", 3.3)])
        self.assertFalse(next(o for o in zone["nearest"] if o["org_type"] == "fire")["available"])   # the only fire station is Busy
        self.assertFalse(zone["coverage"]["potential_gap"])
        self.assertNotIn("_members", zone)

    def test_only_fixed_verified_bases_are_placed(self):
        names = {o["name"] for o in self.radar["organizations"]}
        self.assertEqual(names, {b["name"] for b in BASES if b["verified"]})
        ems = next(o for o in self.radar["organizations"] if o["name"] == "Talomo EMS Base")
        self.assertEqual((ems["latitude"], ems["longitude"]), (A[0] + 0.004, A[1]))

    def test_summary_and_gap_follow_the_data(self):
        summary = self.radar["summary"]
        self.assertEqual((summary["high_areas"], summary["active_zones"]), (1, 0))
        self.assertEqual((summary["most_common"]["label"], summary["most_common"]["count"]), ("Medical & EMS", 7))
        self.assertEqual((summary["highest"]["id"], summary["highest"]["coverage"]["level"]), (self.result["areas"][0]["id"], "good"))
        opened = [dict(r, status="dispatched") if r["incident_number"] == 1 else r for r in ROWS]
        self.assertEqual(risk.radar(risk.analyze(opened, PERIOD, BASES), BASES)["summary"]["active_zones"], 1)
        thin = risk.radar(self.result, [b for b in BASES if b["id"] != "org-1"])
        self.assertTrue(thin["areas"][0]["coverage"]["potential_gap"])
        self.assertNotIn("ems", [o["org_type"] for o in thin["areas"][0]["nearest"]])
        self.assertEqual([z["coverage"]["potential_gap"] for z in thin["areas"][1:]], [False, False, False])   # only HIGH areas

    def test_too_little_data_grades_nothing(self):
        few = risk.radar(risk.analyze(ROWS[:6], PERIOD, BASES), BASES)
        self.assertEqual((few["summary"]["high_areas"], few["summary"]["active_zones"], few["summary"]["highest"]), (None, None, None))
        self.assertEqual(len(few["incidents"]), 6)
        empty = risk.radar(risk.analyze([], PERIOD, BASES), BASES)
        self.assertEqual((empty["center"], empty["incidents"], empty["organizations"], empty["areas"]), (None, [], [], []))

    def test_only_the_newest_points_are_drawn_when_capped(self):
        capped = risk.radar(self.result, BASES, max_points=5)
        self.assertEqual([p["code"] for p in capped["incidents"]],
                         ["INC-000001", "INC-000007", "INC-000002", "INC-000010", "INC-000003"])
        self.assertEqual(capped["plotted"], 5)


class RiskApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()
        self.db = FakeIncidentsDB(ROWS, [], hospitals=BASES)
        patcher = mock.patch("app.routes.analytics._db", return_value=self.db)
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

    def test_requires_sign_in_and_a_dispatch_role(self):
        for path in ("/api/analytics/risk", "/api/analytics/risk/area"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
            for role in ("citizen", "responder", "hospital"):
                self.login(role)
                self.assertEqual(self.client.get(path).status_code, 403, (role, path))
            with self.client.session_transaction() as s:
                s.clear()
        self.assertEqual(self.db.queries, 0)

    def test_areas_follow_the_date_range_and_filters(self):
        self.login()
        week = self.get("/api/analytics/risk", range="7d").get_json()
        self.assertEqual((week["totals"]["located"], week["totals"]["areas"], week["sufficient"]), (12, 4, True))
        self.assertEqual([a["count"] for a in week["areas"]], [6, 3, 2, 1])
        self.assertEqual(week["areas"][0]["level"], "high")
        self.assertEqual(week["period"]["previous"], {"from": "2026-09-22", "to": "2026-09-28"})
        self.assertNotIn("_members", week["areas"][0])
        self.assertNotIn("Private Person", str(week))
        self.assertEqual((len(week["radar"]["incidents"]), week["radar"]["summary"]["high_areas"]), (12, 1))
        self.assertEqual(len(week["radar"]["organizations"]), 5)
        self.assertIn("coverage", week["areas"][0])

        month = self.get("/api/analytics/risk", range="30d").get_json()
        self.assertEqual(month["totals"]["located"], 19)   # Sep 22-28 is now inside the range; Sep 1 still is not
        day = self.get("/api/analytics/risk", range="custom", **{"from": "2026-10-04", "to": "2026-10-04"}).get_json()
        self.assertEqual((day["totals"]["located"], day["sufficient"]), (2, False))
        self.assertIn("Not enough historical data", day["reason"])

        fire = self.get("/api/analytics/risk", range="7d", category="Fire & Hazmat").get_json()
        self.assertEqual(fire["totals"]["located"], 3)
        p1 = self.get("/api/analytics/risk", range="7d", priority="1").get_json()
        self.assertEqual([a["count"] for a in p1["areas"]], [2])
        self.assertEqual(self.get("/api/analytics/risk", range="7d", priority="9").get_json()["totals"]["located"], 12)

    def test_area_detail(self):
        self.login()
        top = self.get("/api/analytics/risk", range="7d").get_json()["areas"][0]
        data = self.get("/api/analytics/risk/area", range="7d", id=top["id"]).get_json()
        self.assertEqual((data["area"]["count"], data["area"]["level"]), (6, "high"))
        self.assertEqual(data["organizations"][0]["name"], "Talomo EMS Base")
        self.assertEqual(data["coverage"]["available"], 3)
        self.assertEqual(len(data["recent"]), 6)
        self.assertEqual(self.get("/api/analytics/risk/area", range="7d", id="0_0").status_code, 404)
        self.assertEqual(self.get("/api/analytics/risk/area", range="7d", id=top["id"], category="flooding").status_code, 404)

    def test_bad_range_and_database_failure_are_errors_not_empty_results(self):
        self.login()
        self.assertEqual(self.get("/api/analytics/risk", range="custom", **{"from": "2026-10-04", "to": "2026-10-01"}).status_code, 422)
        self.db.fail = RuntimeError("connection refused")
        res = self.get("/api/analytics/risk", range="7d")
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("areas", res.get_json())


if __name__ == "__main__":
    unittest.main()
