"""Offline tests for the Dispatcher Dashboard rules, authorization and error handling.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
Live database checks: scripts/dispatch_e2e.py.
"""
import unittest
from unittest import mock

from app import create_app
from app import dispatch as dsp

INC = "00000000-0000-0000-0000-0000000000a1"


class CoordinateTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(dsp.valid_coordinates(7.0731, 125.6128))
        self.assertTrue(dsp.valid_coordinates("7.1", "125.5"))

    def test_invalid(self):
        for lat, lng in [(None, None), (7.0, None), ("abc", 125), (91, 0), (0, 181), (0, 0),
                         (float("nan"), 125), (float("inf"), 125)]:
            self.assertFalse(dsp.valid_coordinates(lat, lng), (lat, lng))


class SortAndCountTests(unittest.TestCase):
    rows = [
        {"id": "old-open", "status": "acknowledged", "priority": 3, "created_at": "2026-10-01T00:00:00+00:00"},
        {"id": "resolved", "status": "resolved", "priority": 1, "created_at": "2026-10-03T00:00:00+00:00"},
        {"id": "new-low", "status": "new", "priority": 4, "created_at": "2026-10-03T01:00:00+00:00"},
        {"id": "new-p1", "status": "new", "priority": 1, "created_at": "2026-10-02T00:00:00+00:00"},
        {"id": "new-none", "status": "new", "priority": None, "created_at": "2026-10-03T02:00:00+00:00"},
        {"id": "enroute", "status": "en_route", "priority": 1, "created_at": "2026-10-02T05:00:00+00:00"},
    ]

    def test_new_first_then_open_then_closed(self):
        order = [r["id"] for r in dsp.sort_incidents(self.rows)]
        self.assertEqual(order, ["new-p1", "new-low", "new-none", "enroute", "old-open", "resolved"])

    def test_older_unresolved_not_hidden(self):
        self.assertIn("old-open", [r["id"] for r in dsp.sort_incidents(self.rows)])

    def test_counts(self):
        c = dsp.counts(self.rows)
        self.assertEqual(c, {"active": 5, "critical": 2, "en_route": 1, "queue": 4, "new": 3})


class HotlineValidationTests(unittest.TestCase):
    def test_valid(self):
        row = dsp.validate_hotline({"organization_name": " Central 911 ", "phone_number": "+63 (82) 224-1234", "category": "police"})
        self.assertEqual(row["organization_name"], "Central 911")
        self.assertIsNone(row["description"])

    def test_invalid(self):
        with self.assertRaises(dsp.ValidationError) as cm:
            dsp.validate_hotline({"organization_name": "", "phone_number": "call me", "category": "x"})
        self.assertEqual(set(cm.exception.errors), {"organization_name", "phone_number", "category"})

    def test_tel_href(self):
        self.assertEqual(dsp.tel_href("+63 (82) 224-1234"), "tel:+63822241234")
        self.assertEqual(dsp.tel_href("911"), "tel:911")


class PhotoTests(unittest.TestCase):
    media = "https://proj.supabase.co/storage/v1/object/public/esrms-media/incidents/"
    url = media + "0b1c2d3e-0000-4000-8000-000000000001/9f8e7d6c5b4a39281706f5e4d3c2b1a0.jpg"

    def test_photo_line_becomes_a_photo(self):
        text, photos = dsp.split_photos(f"Car on fire\n[Photo attached by citizen: {self.url}]\n[OFFLINE REPORT: x]", self.media)
        self.assertEqual(photos, [self.url])
        self.assertEqual(text, "Car on fire\n[OFFLINE REPORT: x]")

    def test_photo_only_report_has_no_description(self):
        self.assertEqual(dsp.split_photos(f"[Photo attached by citizen: {self.url}]", self.media), (None, [self.url]))

    def test_no_photo(self):
        self.assertEqual(dsp.split_photos("Car on fire", self.media), ("Car on fire", []))
        self.assertEqual(dsp.split_photos(None, self.media), (None, []))

    def test_urls_outside_the_incident_folder_are_never_photos(self):
        for url in ("https://evil.example/x.jpg", "javascript:alert(1)", self.media + "../avatars/x.png",
                    self.media + "a/b.svg", self.media + "a/b.jpg?x=1", self.url.replace("https://proj.", "https://other.")):
            line = f"[Photo attached by citizen: {url}]"
            self.assertEqual(dsp.split_photos(line, self.media), (line, []), url)


class DispatchApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()

    def login(self, role):
        with self.client.session_transaction() as s:
            s["user"] = {"id": "00000000-0000-0000-0000-000000000001", "email": "d@example.com", "role": role, "name": "D"}

    def writes(self):
        return [
            self.client.post(f"/api/dispatch/incidents/{INC}/status", json={"status": "acknowledged"}),
            self.client.post(f"/api/dispatch/incidents/{INC}/archive", json={}),
            self.client.post("/api/dispatch/hotlines", json={"organization_name": "X", "phone_number": "911", "category": "police"}),
            self.client.put(f"/api/dispatch/hotlines/{INC}", json={"organization_name": "X", "phone_number": "911", "category": "police"}),
            self.client.delete(f"/api/dispatch/hotlines/{INC}"),
        ]

    # Test H
    def test_anonymous_blocked(self):
        with mock.patch("app.routes.dispatch.get_supabase_admin") as db:
            self.assertTrue(all(r.status_code == 401 for r in self.writes()))
            self.assertEqual(self.client.get("/api/dispatch/incidents").status_code, 401)
            db.assert_not_called()

    def test_citizen_and_responder_blocked(self):
        for role in ("citizen", "responder", None):
            self.login(role)
            with mock.patch("app.routes.dispatch.get_supabase_admin") as db:
                self.assertTrue(all(r.status_code == 403 for r in self.writes()), role)
                self.assertEqual(self.client.get(f"/api/dispatch/incidents/{INC}").status_code, 403)
                db.assert_not_called()

    def test_dispatch_page_requires_role(self):
        self.login("citizen")
        self.assertEqual(self.client.get("/dispatch").status_code, 302)

    def test_db_error_is_not_success(self):
        self.login("organization")
        with mock.patch("app.routes.dispatch.get_supabase_admin", side_effect=RuntimeError("timeout")):
            res = self.client.post("/api/dispatch/hotlines", json={"organization_name": "X", "phone_number": "911", "category": "police"})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("item", res.get_json())

    def test_invalid_status_rejected(self):
        self.login("organization")
        with mock.patch("app.routes.dispatch.get_supabase_admin") as db:
            res = self.client.post(f"/api/dispatch/incidents/{INC}/status", json={"status": "teleported"})
            db.assert_not_called()
        self.assertEqual(res.status_code, 422)

    def _fake_db(self, existing, updated):
        fake = mock.MagicMock()
        table = fake.table.return_value
        table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [existing] if existing else []
        table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = updated
        return fake

    def test_cannot_archive_open_incident(self):
        self.login("organization")
        fake = self._fake_db({"id": INC, "status": "en_route", "archived_at": None}, [])
        with mock.patch("app.routes.dispatch.get_supabase_admin", return_value=fake):
            res = self.client.post(f"/api/dispatch/incidents/{INC}/archive", json={})
        self.assertEqual(res.status_code, 409)
        fake.table.return_value.update.assert_not_called()

    ME = "00000000-0000-0000-0000-000000000001"

    def test_concurrent_change_is_not_success(self):
        self.login("organization")
        fake = self._fake_db({"id": INC, "status": "new", "archived_at": None, "acknowledged_at": None, "assigned_to": self.ME}, [])
        with mock.patch("app.routes.dispatch.get_supabase_admin", return_value=fake), \
                mock.patch("app.routes.dispatch._my_duty", return_value="on_duty"):
            res = self.client.post(f"/api/dispatch/incidents/{INC}/status", json={"status": "acknowledged"})
        self.assertEqual(res.status_code, 409)

    def test_acknowledge_records_who_and_when(self):
        self.login("organization")
        existing = {"id": INC, "status": "new", "archived_at": None, "acknowledged_at": None, "incident_number": 7}
        fake = self._fake_db(existing, [{**existing, "status": "acknowledged", "contact_number": "0917"}])
        # Acknowledging an unassigned incident first takes it (assign_incident), then updates the status.
        fake.rpc.return_value.execute.return_value.data = [{**existing, "assigned_to": self.ME}]
        with mock.patch("app.routes.dispatch.get_supabase_admin", return_value=fake), \
                mock.patch("app.routes.dispatch._my_duty", return_value="on_duty"):
            res = self.client.post(f"/api/dispatch/incidents/{INC}/status", json={"status": "acknowledged"})
        self.assertEqual(res.status_code, 200)
        changes = fake.table.return_value.update.call_args[0][0]
        self.assertEqual(changes["acknowledged_by"], "00000000-0000-0000-0000-000000000001")
        self.assertIn("acknowledged_at", changes)
        self.assertNotIn("contact_number", res.get_json()["item"])
        self.assertEqual(res.get_json()["item"]["code"], "INC-000007")

    def test_incident_carries_its_photos(self):
        self.login("organization")
        self.app.config.update(SUPABASE_URL="https://proj.supabase.co", MEDIA_BUCKET="esrms-media")
        row = {"id": INC, "status": "new", "incident_number": 7,
               "description": f"Car on fire\n[Photo attached by citizen: {PhotoTests.url}]"}
        fake = self._fake_db(row, [])
        fake.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value.data = []
        with mock.patch("app.routes.dispatch.get_supabase_admin", return_value=fake), \
                mock.patch("app.routes.dispatch._names", return_value={}), \
                mock.patch("app.routes.dispatch._my_duty", return_value="on_duty"):
            item = self.client.get(f"/api/dispatch/incidents/{INC}").get_json()["item"]
        self.assertEqual(item["photos"], [PhotoTests.url])
        self.assertEqual(item["description"], "Car on fire")


if __name__ == "__main__":
    unittest.main()
