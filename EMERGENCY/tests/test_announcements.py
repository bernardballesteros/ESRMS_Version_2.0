"""Offline tests for announcement rules, authorization and error handling.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
No database needed; Supabase calls are mocked. For the live end-to-end
workflow against your Supabase project, run scripts/announcements_e2e.py.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import announcements as ann
from app import create_app

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def row(**kw):
    base = {
        "id": "a1", "title": "T", "message": "M", "category": "general", "priority": "medium",
        "target_audience": "all_citizens", "barangay": None, "target_groups": [],
        "status": "active", "is_pinned": False,
        "published_at": iso(NOW - timedelta(hours=1)), "expires_at": None,
    }
    base.update(kw)
    return base


CITIZEN = ann.viewer_context({"role": "citizen"})
ANON = ann.viewer_context(None)


class EffectiveStatusTests(unittest.TestCase):
    def test_active(self):
        self.assertEqual(ann.effective_status(row(), NOW), "active")

    def test_future_publication_is_scheduled_even_if_stored_active(self):
        self.assertEqual(ann.effective_status(row(published_at=iso(NOW + timedelta(minutes=1))), NOW), "scheduled")

    def test_scheduled_goes_live_when_time_arrives(self):
        r = row(status="scheduled", published_at=iso(NOW - timedelta(seconds=1)))
        self.assertEqual(ann.effective_status(r, NOW), "active")

    def test_expired(self):
        self.assertEqual(ann.effective_status(row(expires_at=iso(NOW)), NOW), "expired")

    def test_counts_consistent(self):
        rows = [row(), row(status="draft"), row(status="archived"), row(expires_at=iso(NOW - timedelta(1))),
                row(status="scheduled", published_at=iso(NOW + timedelta(days=1)))]
        c = ann.status_counts(rows, NOW)
        self.assertEqual((c["active"], c["scheduled"], c["archived"], c["draft"], c["expired"]), (1, 1, 2, 1, 1))


class PublicVisibilityTests(unittest.TestCase):
    # Test 7
    def test_draft_never_public(self):
        self.assertFalse(ann.is_publicly_visible(row(status="draft"), CITIZEN, NOW))

    def test_future_scheduled_not_public(self):
        r = row(status="scheduled", published_at=iso(NOW + timedelta(hours=2)))
        self.assertFalse(ann.is_publicly_visible(r, CITIZEN, NOW))

    # Test 6
    def test_archived_not_public(self):
        self.assertFalse(ann.is_publicly_visible(row(status="archived"), CITIZEN, NOW))

    def test_expired_not_public(self):
        self.assertFalse(ann.is_publicly_visible(row(expires_at=iso(NOW - timedelta(minutes=1))), CITIZEN, NOW))

    def test_all_citizens_visible_to_anonymous(self):
        self.assertTrue(ann.is_publicly_visible(row(), ANON, NOW))

    # Test 9
    def test_barangay_only_for_that_barangay(self):
        r = row(target_audience="barangay", barangay="Poblacion")
        self.assertFalse(ann.is_publicly_visible(r, ANON, NOW))
        self.assertFalse(ann.is_publicly_visible(r, ann.viewer_context(None, "Talomo"), NOW))
        self.assertTrue(ann.is_publicly_visible(r, ann.viewer_context(None, " poblacion "), NOW))

    def test_account_barangay_overrides_query(self):
        viewer = ann.viewer_context({"role": "citizen", "barangay": "Talomo"}, "Poblacion")
        self.assertFalse(ann.is_publicly_visible(row(target_audience="barangay", barangay="Poblacion"), viewer, NOW))

    def test_personnel_only(self):
        r = row(target_audience="emergency_personnel")
        self.assertFalse(ann.is_publicly_visible(r, CITIZEN, NOW))
        self.assertFalse(ann.is_publicly_visible(r, ANON, NOW))
        self.assertTrue(ann.is_publicly_visible(r, ann.viewer_context({"role": "responder"}), NOW))

    def test_selected_groups(self):
        r = row(target_audience="selected_groups", target_groups=["fire"])
        self.assertTrue(ann.is_publicly_visible(r, ann.viewer_context({"role": "responder", "branch": "fire"}), NOW))
        self.assertFalse(ann.is_publicly_visible(r, ann.viewer_context({"role": "responder", "branch": "ems"}), NOW))
        self.assertFalse(ann.is_publicly_visible(r, CITIZEN, NOW))


class ValidationTests(unittest.TestCase):
    def payload(self, **kw):
        p = {"title": "Emergency Weather Advisory", "message": "Stay indoors.", "category": "emergency",
             "priority": "critical", "target_audience": "all_citizens", "status": "active", "action": "publish"}
        p.update(kw)
        return p

    def test_publish_now(self):
        r = ann.validate(self.payload(), NOW)
        self.assertEqual(r["status"], "active")
        self.assertEqual(r["published_at"], iso(NOW))

    def test_future_time_becomes_scheduled(self):
        r = ann.validate(self.payload(published_at=iso(NOW + timedelta(hours=3))), NOW)
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual(r["scheduled_at"], r["published_at"])

    def test_draft_button_forces_draft(self):
        self.assertEqual(ann.validate(self.payload(action="draft"), NOW)["status"], "draft")

    def test_required_fields(self):
        with self.assertRaises(ann.ValidationError) as cm:
            ann.validate({"action": "publish"}, NOW)
        self.assertTrue({"title", "message", "category", "priority", "target_audience"} <= set(cm.exception.errors))

    def test_barangay_required(self):
        with self.assertRaises(ann.ValidationError) as cm:
            ann.validate(self.payload(target_audience="barangay"), NOW)
        self.assertIn("barangay", cm.exception.errors)

    def test_scheduled_in_past_rejected(self):
        with self.assertRaises(ann.ValidationError):
            ann.validate(self.payload(status="scheduled", published_at=iso(NOW - timedelta(hours=1))), NOW)

    def test_expiry_before_publication_rejected(self):
        with self.assertRaises(ann.ValidationError):
            ann.validate(self.payload(published_at=iso(NOW), expires_at=iso(NOW - timedelta(minutes=5))), NOW)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ann.ValidationError):
            ann.validate(self.payload(published_at="2026-10-03T10:00"), NOW)

    def test_search_sanitized(self):
        self.assertEqual(ann.clean_search("a,b)or(id.eq.1"), "aborideq1")


class ApiAuthAndErrorTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()

    def login(self, role):
        with self.client.session_transaction() as s:
            s["user"] = {"id": "00000000-0000-0000-0000-000000000001", "email": "x@example.com", "role": role, "name": "X"}

    def create(self):
        return self.client.post("/api/announcements", json={
            "title": "Emergency Weather Advisory", "message": "Stay indoors.", "category": "emergency",
            "priority": "critical", "target_audience": "all_citizens", "status": "active", "action": "publish"})

    # Test 8
    def test_anonymous_cannot_write(self):
        with mock.patch("app.routes.announcements.get_supabase_admin") as db:
            self.assertEqual(self.create().status_code, 401)
            self.assertEqual(self.client.delete("/api/announcements/00000000-0000-0000-0000-000000000009").status_code, 401)
            db.assert_not_called()

    def test_citizen_and_responder_cannot_write(self):
        for role in ("citizen", "responder", None):
            self.login(role)
            with mock.patch("app.routes.announcements.get_supabase_admin") as db:
                self.assertEqual(self.create().status_code, 403, role)
                self.assertEqual(self.client.post("/api/announcements/00000000-0000-0000-0000-000000000009/archive", json={}).status_code, 403)
                self.assertEqual(self.client.get("/api/announcements").status_code, 403)
                db.assert_not_called()

    def test_form_post_rejected(self):
        self.login("admin")
        with mock.patch("app.routes.announcements.get_supabase_admin") as db:
            res = self.client.post("/api/announcements", data={"title": "x"})
            self.assertEqual(res.status_code, 415)
            db.assert_not_called()

    def test_manage_page_requires_manager(self):
        self.login("citizen")
        self.assertEqual(self.client.get("/announcements/manage").status_code, 302)

    # Test 10
    def test_database_error_is_not_success(self):
        self.login("admin")
        with mock.patch("app.routes.announcements.get_supabase_admin", side_effect=RuntimeError("connection refused")):
            res = self.create()
        self.assertEqual(res.status_code, 502)
        self.assertIn("connection refused", res.get_json()["error"])
        self.assertNotIn("item", res.get_json())

    def test_empty_insert_response_is_not_success(self):
        self.login("admin")
        fake = mock.MagicMock()
        fake.table.return_value.insert.return_value.execute.return_value.data = []
        with mock.patch("app.routes.announcements.get_supabase_admin", return_value=fake):
            self.assertEqual(self.create().status_code, 502)

    def test_validation_error_returns_fields(self):
        self.login("organization")
        with mock.patch("app.routes.announcements.get_supabase_admin") as db:
            res = self.client.post("/api/announcements", json={"action": "publish"})
            db.assert_not_called()
        self.assertEqual(res.status_code, 422)
        self.assertIn("title", res.get_json()["fields"])

    def test_public_api_error(self):
        with mock.patch("app.routes.announcements.get_supabase_admin", side_effect=RuntimeError("down")):
            res = self.client.get("/api/announcements/public")
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("items", res.get_json())


if __name__ == "__main__":
    unittest.main()
