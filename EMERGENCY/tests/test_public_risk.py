"""Offline tests for the public-safe copy of High-Risk Area Detection (Citizen App Safety Radar).

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from app import analytics as an
from app import create_app, public_risk, risk
from app.hospital import distance_km
from app.routes import analytics as routes
from tests.fake_incidents import FakeIncidentsDB
from tests.test_risk import A, B, BASES, C, D, NOW, PERIOD, ROWS, at, incident

ALLOWED = {"area_id", "area_name", "risk_level", "center_latitude", "center_longitude", "radius_km", "public_summary"}
PRIVATE_WORDS = ("INC-", "Private Person", "+63 900", "private", "Chest pain", "Medical", "Fire", "EMS Base",
                 "Fire Station", "Medical Center", "Police", "incident_number", "score", "count", "priority")


class PublicDB(FakeIncidentsDB):
    """Adds the publish_public_risk_areas() database function from migration 012."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tables.setdefault("public_risk_areas", [])
        self.tables.setdefault("public_risk_feed", [{"id": 1, "version": 0, "published_at": None, "sufficient": False}])

    def rpc(self, name, params):
        assert name == "publish_public_risk_areas", name
        feed = self.tables["public_risk_feed"][0]
        self.tables["public_risk_areas"] = [dict(a, updated_at=NOW.isoformat()) for a in params["p_areas"]]
        feed.update(version=feed["version"] + 1, published_at=datetime.now(timezone.utc).isoformat(), **params["p_meta"])
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=feed["version"]))


class ReductionTests(unittest.TestCase):
    def setUp(self):
        self.result = risk.analyze(ROWS, PERIOD, BASES)
        self.public = public_risk.public_areas(self.result, BASES)

    def test_same_levels_as_the_analytics_tab_for_the_areas_it_publishes(self):
        analysed = {a["id"]: a for a in self.result["areas"]}
        self.assertTrue(self.public)
        for area in self.public:
            self.assertEqual(area["risk_level"], analysed[area["area_id"]]["level"])
        self.assertEqual(self.public[0]["risk_level"], "high")           # area A, the analysis' top area

    def test_only_public_fields_and_no_private_text(self):
        for area in self.public:
            self.assertEqual(set(area), ALLOWED)
        text = json.dumps(self.public)
        for word in PRIVATE_WORDS:
            self.assertNotIn(word, text)

    def test_small_areas_are_not_published(self):
        counts = {a["id"]: a["count"] for a in self.result["areas"]}
        self.assertEqual(sorted(counts.values()), [1, 2, 3, 6])
        self.assertEqual({counts[a["area_id"]] for a in self.public}, {3, 6})      # the 1- and 2-incident areas stay private
        everything = public_risk.public_areas(self.result, BASES, min_incidents=1)
        self.assertEqual(len(everything), 4)

    def test_zone_is_the_grid_cell_not_the_incidents(self):
        for where in (A, B):
            cell = risk.cell_of(*where, 1.0)
            zone = next(a for a in self.public if a["area_id"] == risk.area_id(cell))
            centre = (zone["center_latitude"], zone["center_longitude"])
            self.assertEqual(risk.cell_of(*centre, 1.0), cell)                      # inside the same cell
            self.assertNotEqual(centre, where)                                      # not the reported point
            self.assertLessEqual(distance_km(*centre, *where), zone["radius_km"])   # but the zone covers it
        # Moving every incident inside its cell does not move the published zone.
        moved = [dict(r, latitude=r["latitude"] + 0.0005, longitude=r["longitude"] + 0.0005) if r["latitude"] else r
                 for r in ROWS]
        again = public_risk.public_areas(risk.analyze(moved, PERIOD, BASES), BASES)
        self.assertEqual([(a["area_id"], a["center_latitude"], a["center_longitude"]) for a in again],
                         [(a["area_id"], a["center_latitude"], a["center_longitude"]) for a in self.public])

    def test_only_a_barangay_is_ever_named(self):
        self.assertEqual({a["area_name"] for a in self.public}, {None})            # A is named after an EMS base: hidden
        near_hall = [dict(b, latitude=A[0] + 0.001, longitude=A[1]) if b["org_type"] == "barangay" else
                     dict(b, latitude=A[0] + 0.05) for b in BASES]
        named = public_risk.public_areas(risk.analyze(ROWS, PERIOD, near_hall), near_hall)
        self.assertEqual(named[0]["area_name"], "Near Talomo Barangay Response Center")

    def test_insufficient_data_publishes_nothing_and_says_so(self):
        few = risk.analyze(ROWS[:4], PERIOD, BASES)
        self.assertFalse(few["sufficient"])
        data = public_risk.payload(few, PERIOD, BASES)
        self.assertEqual(data["areas"], [])
        self.assertEqual(data["meta"], {"sufficient": False, "period_days": 7, "cell_km": 1.0})

    def test_wording_never_claims_danger_or_an_active_emergency(self):
        for text in public_risk.SUMMARIES.values():
            for word in ("dangerous", "unsafe", "active", "now", "avoid"):
                self.assertNotIn(word, text.lower())


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()
        self.db = PublicDB(ROWS, [], hospitals=BASES)
        patcher = mock.patch("app.routes.analytics._db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = mock.patch("app.analytics.datetime", wraps=datetime)
        clock.start().now.side_effect = lambda tz=None: NOW.astimezone(tz) if tz else NOW
        self.addCleanup(clock.stop)

    def login(self, role="organization"):
        with self.client.session_transaction() as s:
            s["user"] = {"id": "00000000-0000-0000-0000-000000000001", "email": "d@example.com", "role": role, "name": "D"}

    def test_publish_uses_the_unfiltered_30_day_analysis(self):
        with self.app.app_context():
            out = routes.publish_public_risk(self.db, self.app.config, now=NOW)
        period = an.resolve_period(public_risk.PUBLIC_RANGE, now=NOW)
        expected = public_risk.public_areas(risk.analyze(ROWS, period, BASES), BASES)
        stored = [{k: a[k] for k in ALLOWED} for a in self.db.tables["public_risk_areas"]]
        self.assertEqual(stored, expected)
        self.assertEqual((out["version"], out["areas"], out["period_days"]), (1, len(expected), 30))
        self.assertTrue(self.db.tables["public_risk_feed"][0]["sufficient"])

    def test_republishing_replaces_the_copy_when_the_data_changes(self):
        with self.app.app_context():
            routes.publish_public_risk(self.db, self.app.config, now=NOW)
            before = {a["area_id"]: a["risk_level"] for a in self.db.tables["public_risk_areas"]}
            # Many new critical incidents in area C: the analysis changes, so the public copy must too.
            self.db.tables["emergency_incidents"] += [incident(100 + n, C, at(10, 5, 8), priority=1) for n in range(12)]
            out = routes.publish_public_risk(self.db, self.app.config, now=NOW)
        after = {a["area_id"]: a["risk_level"] for a in self.db.tables["public_risk_areas"]}
        c = risk.area_id(risk.cell_of(*C, 1.0))
        self.assertEqual((before[c], after[c]), ("low", "high"))
        self.assertEqual(out["version"], 2)

    def test_endpoint_needs_a_dispatch_role_and_can_be_switched_off(self):
        self.assertEqual(self.client.post("/api/analytics/risk/publish", json={}).status_code, 401)
        for role in ("citizen", "responder", "hospital"):
            self.login(role)
            self.assertEqual(self.client.post("/api/analytics/risk/publish", json={}).status_code, 403, role)
        self.assertEqual(self.db.tables["public_risk_areas"], [])
        self.login()
        res = self.client.post("/api/analytics/risk/publish", json={})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["published"]["areas"], len(self.db.tables["public_risk_areas"]))
        self.app.config["RISK_PUBLIC_ENABLED"] = False
        self.assertEqual(self.client.post("/api/analytics/risk/publish", json={}).status_code, 409)

    def test_opening_analytics_publishes_once_then_waits_until_stale(self):
        self.login()
        self.assertEqual(self.client.get("/api/analytics/risk", query_string={"range": "7d"}).status_code, 200)
        self.assertEqual(self.db.tables["public_risk_feed"][0]["version"], 1)
        self.client.get("/api/analytics/risk", query_string={"range": "7d", "priority": 1})
        self.assertEqual(self.db.tables["public_risk_feed"][0]["version"], 1)        # fresh: not republished
        self.db.tables["public_risk_feed"][0]["published_at"] = "2026-10-05T01:00:00+00:00"   # hours ago
        self.client.get("/api/analytics/risk", query_string={"range": "7d"})
        self.assertEqual(self.db.tables["public_risk_feed"][0]["version"], 2)

    def test_a_missing_migration_never_breaks_the_analytics_tab(self):
        self.login()
        plain = FakeIncidentsDB(ROWS, [], hospitals=BASES)       # no public tables, no rpc
        with mock.patch("app.routes.analytics._db", return_value=plain):
            res = self.client.get("/api/analytics/risk", query_string={"range": "7d"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["areas"])


if __name__ == "__main__":
    unittest.main()
