"""Offline tests for the account settings rules, authorization and error handling.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from supabase_auth.errors import AuthApiError

from app import account as acct
from app import create_app
from app.dispatch import ValidationError

UID = "00000000-0000-0000-0000-000000000001"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def factor(status="verified"):
    return SimpleNamespace(id="factor-1", factor_type="totp", status=status, created_at=NOW, friendly_name="Authenticator")


def auth_user(**over):
    base = dict(id=UID, email="d@example.com", new_email=None, user_metadata={"role": "organization", "full_name": "D"},
                app_metadata={"providers": ["email"]}, factors=None, created_at=NOW, last_sign_in_at=NOW,
                email_confirmed_at=NOW, banned_until=None)
    return SimpleNamespace(**{**base, **over})


class ProfileRuleTests(unittest.TestCase):
    def test_valid(self):
        out = acct.validate_profile({"full_name": "  Eijkim   G. Maulit ", "email": "EIJKIM@psap911.gov", "phone": "+63 912 345 6789"})
        self.assertEqual(out, {"full_name": "Eijkim G. Maulit", "email": "eijkim@psap911.gov", "phone": "+63 912 345 6789"})

    def test_phone_is_optional(self):
        self.assertIsNone(acct.validate_profile({"full_name": "A", "email": "a@b.co", "phone": ""})["phone"])

    def test_invalid(self):
        with self.assertRaises(ValidationError) as cm:
            acct.validate_profile({"full_name": " ", "email": "not-an-email", "phone": "call me"})
        self.assertEqual(set(cm.exception.errors), {"full_name", "email", "phone"})

    def test_protected_fields_are_ignored(self):
        out = acct.validate_profile({"full_name": "A", "email": "a@b.co", "role": "admin", "status": "verified", "id": "x"})
        self.assertEqual(set(out), {"full_name", "email", "phone"})


class AgencyRuleTests(unittest.TestCase):
    good = {"agency_name": "Davao Emergency Communication", "agency_type": "psap-primary", "jurisdiction": "Davao City",
            "psap_phone": "+63 82 221 911", "agency_email": "ems@example.gov.ph", "agency_address": "Rizal St.\nDavao City"}

    def test_valid_keeps_address_lines_and_drops_code(self):
        out = acct.validate_agency({**self.good, "ori_code": "HACKED-01"})
        self.assertEqual(out["agency_address"], "Rizal St.\nDavao City")
        self.assertIsNone(out["agency_description"])
        self.assertNotIn("ori_code", out)

    def test_invalid(self):
        with self.assertRaises(ValidationError) as cm:
            acct.validate_agency({"agency_type": "made-up", "psap_phone": "12", "agency_email": "x"})
        self.assertEqual(set(cm.exception.errors),
                         {"agency_name", "agency_type", "jurisdiction", "psap_phone", "agency_email", "agency_address"})


class PasswordRuleTests(unittest.TestCase):
    def test_role_minimums(self):
        self.assertEqual(acct.password_problems("Abcdef1!", "citizen"), [])
        self.assertEqual(acct.password_problems("Abcdef1!", "organization"), ["length"])
        self.assertEqual(acct.password_problems("Abcdefghijklmn1!", "organization"), [])

    def test_character_classes(self):
        self.assertEqual(acct.password_problems("abcdefgh", "citizen"), ["upper", "digit", "special"])

    def test_change_validation(self):
        with self.assertRaises(ValidationError) as cm:
            acct.validate_password_change({"current_password": "", "new_password": "Abcdef1!", "confirm_password": "nope"}, "citizen")
        self.assertEqual(set(cm.exception.errors), {"current_password", "confirm_password"})
        with self.assertRaises(ValidationError) as cm:
            acct.validate_password_change({"current_password": "Abcdef1!", "new_password": "Abcdef1!", "confirm_password": "Abcdef1!"}, "citizen")
        self.assertIn("new_password", cm.exception.errors)


class PreferenceRuleTests(unittest.TestCase):
    def test_defaults_until_saved(self):
        prefs = acct.preferences_from_row(None)
        self.assertTrue(all(prefs[k] for k in acct.NOTIFICATION_CATEGORIES))
        self.assertFalse(prefs["browser_enabled"])

    def test_partial_or_non_boolean_rejected(self):
        with self.assertRaises(ValidationError):
            acct.validate_preferences({"new_reports": False})
        with self.assertRaises(ValidationError):
            acct.validate_preferences({**acct.DEFAULT_PREFERENCES, "security_alerts": "yes"})

    def test_only_known_fields_kept(self):
        out = acct.validate_preferences({**acct.DEFAULT_PREFERENCES, "new_reports": False, "user_id": "someone-else"})
        self.assertEqual(set(out), set(acct.PREFERENCE_FIELDS))
        self.assertFalse(out["new_reports"])


class DisplayRuleTests(unittest.TestCase):
    def test_default_until_saved(self):
        self.assertEqual(acct.display_scale_from_row(None), 90)
        self.assertEqual(acct.display_scale_from_row({"display_scale": 125}), 125)
        self.assertEqual(acct.display_scale_from_row({"display_scale": 300}), 90)

    def test_only_the_offered_sizes(self):
        for scale in acct.DISPLAY_SCALES:
            self.assertEqual(acct.validate_display_scale({"display_scale": scale}), scale)
        for bad in (95, "110", 110.0, True, None):
            with self.assertRaises(ValidationError):
                acct.validate_display_scale({"display_scale": bad})


class ImageRuleTests(unittest.TestCase):
    def test_type_comes_from_bytes(self):
        self.assertEqual(acct.validate_image(PNG), ("image/png", "png"))
        with self.assertRaises(ValidationError):
            acct.validate_image(b"<svg onload=alert(1)>")
        with self.assertRaises(ValidationError):
            acct.validate_image(PNG + b"\x00" * acct.MAX_IMAGE_BYTES)


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()
        patcher = mock.patch.dict("app.routes.account._name_column", {"exists": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, role="organization", tokens=False):
        with self.client.session_transaction() as s:
            s["user"] = {"id": UID, "email": "d@example.com", "role": role, "name": "D"}
            s["display"] = {"uid": UID, "scale": 90}   # already looked up, so page renders stay offline
            if tokens:
                s["sb"] = {"at": "access", "rt": "refresh", "exp": 9999999999}

    def fake_db(self, row=None, user=None):
        fake = mock.MagicMock()
        table = fake.table.return_value
        table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [row] if row else []
        fake.auth.admin.get_user_by_id.return_value.user = user or auth_user()
        return fake

    # ---- authentication and navigation

    def test_pages_require_sign_in(self):
        for section in ("display", "profile", "agency", "security", "notifications"):
            self.assertEqual(self.client.get(f"/account/{section}").status_code, 302, section)

    def test_api_requires_sign_in(self):
        with mock.patch("app.routes.account.get_supabase_admin") as db:
            for path in ("display", "profile", "agency", "security", "notifications", "security/events"):
                self.assertEqual(self.client.get(f"/api/account/{path}").status_code, 401, path)
            self.assertEqual(self.client.put("/api/account/profile", json={}).status_code, 401)
            self.assertEqual(self.client.post("/api/account/password", json={}).status_code, 401)
            self.assertEqual(self.client.post("/api/account/mfa/disable", json={"code": "123456"}).status_code, 401)
            db.assert_not_called()

    def test_each_page_renders_with_its_menu_item_active(self):
        self.login()
        for section, title in (("display", "Display"), ("profile", "My Profile"), ("agency", "Agency Settings"),
                               ("security", "Security &amp; MFA"), ("notifications", "Notifications")):
            res = self.client.get(f"/account/{section}")
            self.assertEqual(res.status_code, 200, section)
            html = res.get_data(as_text=True)
            self.assertIn(title, html)
            self.assertEqual(html.count('aria-current="page"'), 2, section)  # side nav + account menu
            self.assertIn(f'/account/{section}" aria-current="page"', html)

    def test_unknown_section_redirects(self):
        self.login()
        self.assertEqual(self.client.get("/account/billing").status_code, 302)

    def test_form_posts_rejected(self):
        self.login()
        with mock.patch("app.routes.account.get_supabase_admin") as db:
            res = self.client.post("/api/account/password", data={"current_password": "x"})
            db.assert_not_called()
        self.assertEqual(res.status_code, 415)

    # ---- profile

    def test_profile_update_is_scoped_to_the_signed_in_user(self):
        self.login()
        fake = self.fake_db(row={"id": UID, "email": "d@example.com"})
        table = fake.table.return_value
        table.update.return_value.eq.return_value.execute.return_value.data = [{"id": UID, "full_name": "New Name", "phone": None}]
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            res = self.client.put("/api/account/profile", json={
                "full_name": "New Name", "email": "d@example.com", "phone": "",
                "id": "someone-else", "role": "admin", "status": "verified"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(table.update.call_args[0][0], {"full_name": "New Name", "phone": None})
        table.update.return_value.eq.assert_called_with("id", UID)
        fake.auth.admin.update_user_by_id.assert_called_once_with(UID, {"user_metadata": {"full_name": "New Name", "phone": None}})
        with self.client.session_transaction() as s:
            self.assertEqual(s["user"]["name"], "New Name")
            self.assertEqual(s["user"]["role"], "organization")

    def test_profile_validation_never_reaches_the_database(self):
        self.login()
        with mock.patch("app.routes.account.get_supabase_admin") as db:
            res = self.client.put("/api/account/profile", json={"full_name": "", "email": "bad"})
            db.assert_not_called()
        self.assertEqual(res.status_code, 422)
        self.assertEqual(set(res.get_json()["fields"]), {"full_name", "email"})

    def test_profile_db_error_is_not_success(self):
        self.login()
        with mock.patch("app.routes.account.get_supabase_admin", side_effect=RuntimeError("timeout")):
            res = self.client.put("/api/account/profile", json={"full_name": "A", "email": "d@example.com"})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("profile", res.get_json())

    def test_email_change_needs_the_users_own_session(self):
        self.login(tokens=False)
        fake = self.fake_db(row={"id": UID, "email": "d@example.com"})
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake), \
                mock.patch("app.supabase_client.get_supabase_admin", side_effect=RuntimeError("no service key")):
            res = self.client.put("/api/account/profile", json={"full_name": "A", "email": "new@example.com"})
        self.assertEqual(res.status_code, 409)
        fake.table.return_value.update.assert_not_called()

    def test_avatar_rejects_non_images(self):
        self.login()
        with mock.patch("app.routes.account.get_supabase_admin") as db:
            res = self.client.post("/api/account/profile/avatar", headers={"X-ESRMS-Request": "1"},
                                   data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "x.png")})
            db.assert_not_called()
        self.assertEqual(res.status_code, 422)

    # ---- agency

    def test_agency_blocked_for_unassigned_users(self):
        for role in ("citizen", "responder", None):
            self.login(role)
            fake = self.fake_db(row={"id": UID, "agency_id": None})
            with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
                self.assertEqual(self.client.get("/api/account/agency").status_code, 403, role)
                self.assertEqual(self.client.put("/api/account/agency", json=AgencyRuleTests.good).status_code, 403, role)
                self.assertEqual(self.client.post("/api/account/agency/logo", headers={"X-ESRMS-Request": "1"},
                                                  data={"file": (io.BytesIO(PNG), "l.png")}).status_code, 403, role)
                self.assertEqual(self.client.delete("/api/account/agency/logo", headers={"X-ESRMS-Request": "1"}).status_code, 403, role)
            fake.table.return_value.update.assert_not_called()
            fake.storage.from_.assert_not_called()

    def test_agency_update_by_administrator(self):
        self.login("organization")
        fake = self.fake_db(row={"id": UID, "email": "d@example.com", "ori_code": "DEMS-001"})
        table = fake.table.return_value
        table.update.return_value.eq.return_value.execute.return_value.data = [{**AgencyRuleTests.good, "ori_code": "DEMS-001"}]
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            res = self.client.put("/api/account/agency", json={**AgencyRuleTests.good, "ori_code": "OTHER-999", "status": "verified"})
        self.assertEqual(res.status_code, 200)
        changes = table.update.call_args[0][0]
        self.assertNotIn("ori_code", changes)
        self.assertNotIn("status", changes)
        table.update.return_value.eq.assert_called_with("id", UID)
        self.assertEqual(res.get_json()["agency"]["ori_code"], "DEMS-001")

    def test_missing_migration_is_explained(self):
        self.login("organization")
        fake = mock.MagicMock()
        err = Exception("column users.phone does not exist")
        err.code = "42703"
        fake.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = err
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            res = self.client.get("/api/account/agency")
        self.assertEqual(res.status_code, 503)
        self.assertIn("005_account_settings.sql", res.get_json()["error"])

    # ---- security

    def test_wrong_current_password_changes_nothing(self):
        self.login("citizen")
        verifier = mock.MagicMock()
        verifier.auth.sign_in_with_password.side_effect = AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        with mock.patch("app.routes.account.get_supabase", return_value=verifier), \
                mock.patch("app.routes.account.get_supabase_admin") as db:
            res = self.client.post("/api/account/password", json={
                "current_password": "wrong", "new_password": "Abcdef1!", "confirm_password": "Abcdef1!"})
            db.assert_not_called()
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["fields"]["current_password"], "Current password is incorrect.")
        verifier.auth.update_user.assert_not_called()

    def test_weak_password_never_reaches_supabase(self):
        self.login("organization")
        with mock.patch("app.routes.account.get_supabase") as sb:
            res = self.client.post("/api/account/password", json={
                "current_password": "old", "new_password": "Short1!", "confirm_password": "Short1!"})
            sb.assert_not_called()
        self.assertEqual(res.status_code, 422)

    def test_password_change_verifies_then_updates(self):
        self.login("citizen")
        verifier = mock.MagicMock()
        verifier.auth.sign_in_with_password.return_value = SimpleNamespace(user=auth_user(), session=SimpleNamespace(access_token="fresh"))
        verifier.auth.get_session.return_value = SimpleNamespace(access_token="fresh", refresh_token="r2", expires_at=9999999999)
        admin = mock.MagicMock()
        admin.table.return_value.insert.return_value.execute.return_value.data = [{"id": 7}]
        with mock.patch("app.routes.account.get_supabase", return_value=verifier), \
                mock.patch("app.routes.account.get_supabase_admin", return_value=admin):
            res = self.client.post("/api/account/password", json={
                "current_password": "OldPass1!", "new_password": "Abcdef1!", "confirm_password": "Abcdef1!"})
        self.assertEqual(res.status_code, 200)
        verifier.auth.sign_in_with_password.assert_called_once_with({"email": "d@example.com", "password": "OldPass1!"})
        self.assertEqual(verifier.auth.update_user.call_args[0][0]["password"], "Abcdef1!")
        verifier.auth.admin.sign_out.assert_called_once_with("fresh", "others")
        self.assertNotIn("Abcdef1!", res.get_data(as_text=True))
        self.assertEqual(admin.table.return_value.insert.call_args[0][0]["event"], "password_changed")
        self.assertEqual(res.get_json()["event_id"], 7)

    def test_mfa_is_reported_from_verified_factors_only(self):
        self.login(tokens=True)
        for factors, expected in ((None, False), ([factor("unverified")], False), ([factor()], True)):
            fake = self.fake_db(user=auth_user(factors=factors))
            fake.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = []
            fake.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
            fake.rpc.return_value.execute.return_value.data = []
            with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
                data = self.client.get("/api/account/security").get_json()
            self.assertEqual(data["mfa"]["enabled"], expected)
            self.assertNotIn("secret", str(data))

    def test_mfa_actions_need_the_users_own_session(self):
        self.login(tokens=False)
        with mock.patch("app.supabase_client.get_supabase_admin", side_effect=RuntimeError("no service key")):
            self.assertEqual(self.client.post("/api/account/mfa/enroll", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/account/mfa/verify", json={"factor_id": "f", "code": "123456"}).status_code, 409)
            self.assertEqual(self.client.post("/api/account/mfa/disable", json={"code": "123456"}).status_code, 409)
            self.assertEqual(self.client.post("/api/account/mfa/recovery-codes", json={"code": "123456"}).status_code, 409)
            self.assertEqual(self.client.post("/api/account/sessions/revoke", json={}).status_code, 409)
            self.assertEqual(self.client.post("/api/account/sessions/revoke-all", json={}).status_code, 409)

    def test_mfa_not_enabled_by_a_rejected_code(self):
        self.login(tokens=True)
        client = mock.MagicMock()
        client.auth.mfa.challenge_and_verify.side_effect = AuthApiError("Invalid TOTP code entered", 422, "mfa_verification_failed")
        with mock.patch("app.routes.account.get_user_client", return_value=client), \
                mock.patch("app.routes.account.get_supabase_admin") as db:
            res = self.client.post("/api/account/mfa/verify", json={"factor_id": "factor-1", "code": "000000"})
            db.assert_not_called()   # no "mfa_enabled" event recorded
        self.assertEqual(res.status_code, 422)
        with self.client.session_transaction() as s:
            self.assertFalse(s["user"].get("mfa_enabled"))

    def test_mfa_disable_requires_a_valid_code(self):
        self.login(tokens=True)
        with mock.patch("app.routes.account.get_user_client") as uc:
            res = self.client.post("/api/account/mfa/disable", json={"code": "12"})
            uc.assert_not_called()
        self.assertEqual(res.status_code, 422)

        client = mock.MagicMock()
        client.auth.get_user.return_value.user = auth_user(factors=[factor()])
        client.auth.mfa.challenge_and_verify.side_effect = AuthApiError("Invalid TOTP code entered", 422, "mfa_verification_failed")
        with mock.patch("app.routes.account.get_user_client", return_value=client):
            res = self.client.post("/api/account/mfa/disable", json={"code": "000000"})
        self.assertEqual(res.status_code, 422)
        client.auth.mfa.unenroll.assert_not_called()

    def test_sign_in_with_mfa_waits_for_the_code(self):
        signed_in = SimpleNamespace(
            user=auth_user(factors=[factor()]),
            session=SimpleNamespace(access_token="a", refresh_token="r", expires_at=9999999999))
        sb = mock.MagicMock()
        sb.auth.sign_in_with_password.return_value = signed_in
        with mock.patch("app.routes.auth.get_supabase", return_value=sb):
            res = self.client.post("/login", data={"email": "d@example.com", "password": "pw"})
        self.assertTrue(res.headers["Location"].endswith("/login/mfa"))
        with self.client.session_transaction() as s:
            self.assertNotIn("user", s)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)      # still signed out
        self.assertEqual(self.client.get("/api/account/profile").status_code, 401)

        sb.auth.set_session.return_value = signed_in
        sb.auth.mfa.challenge_and_verify.side_effect = AuthApiError("Invalid TOTP code entered", 422, "mfa_verification_failed")
        with mock.patch("app.routes.auth.get_supabase", return_value=sb):
            self.assertEqual(self.client.post("/login/mfa", data={"code": "000000"}).status_code, 200)
        with self.client.session_transaction() as s:
            self.assertNotIn("user", s)

        sb.auth.mfa.challenge_and_verify.side_effect = None
        sb.auth.mfa.challenge_and_verify.return_value = SimpleNamespace(user=signed_in.user)
        sb.auth.get_session.return_value = SimpleNamespace(access_token="a2", refresh_token="r2", expires_at=9999999999)
        with mock.patch("app.routes.auth.get_supabase", return_value=sb):
            res = self.client.post("/login/mfa", data={"code": "123456"})
        self.assertEqual(res.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertTrue(s["user"]["mfa_enabled"])
            self.assertEqual(s["sb"]["at"], "a2")

    def test_mfa_step_needs_a_password_sign_in_first(self):
        self.assertEqual(self.client.get("/login/mfa").status_code, 302)

    def test_revoked_session_is_signed_out(self):
        self.login(tokens=True)
        with self.client.session_transaction() as s:
            s["sb"] = {**s["sb"], "exp": 1}   # access token expired
        sb = mock.MagicMock()
        sb.auth.refresh_session.side_effect = AuthApiError("Invalid Refresh Token: Refresh Token Not Found", 400, "refresh_token_not_found")
        with mock.patch("app.supabase_client.create_client", return_value=sb):
            self.assertEqual(self.client.get("/api/account/notifications").status_code, 401)

    def test_network_trouble_does_not_sign_out(self):
        self.login(tokens=True)
        with self.client.session_transaction() as s:
            s["sb"] = {**s["sb"], "exp": 1}   # access token expired
        sb = mock.MagicMock()
        sb.auth.refresh_session.side_effect = ConnectionError("offline")
        with mock.patch("app.supabase_client.create_client", return_value=sb):
            self.assertEqual(self.client.get("/account/profile").status_code, 200)

    # ---- display

    def test_display_default_then_persist_for_this_user(self):
        self.login()
        fake = self.fake_db()
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            data = self.client.get("/api/account/display").get_json()
            self.assertEqual((data["display_scale"], data["saved"]), (90, False))
            html = self.client.get("/account/display").get_data(as_text=True)
            self.assertIn('style="font-size: 90%"', html)
            self.assertIn('data-scale="90" aria-pressed="true" aria-label="Set display size to 90%"', html)
            self.assertIn('data-scale="110" aria-pressed="false"', html)

            fake.table.return_value.upsert.return_value.execute.return_value.data = [
                {"user_id": UID, "display_scale": 110, "updated_at": "2026-10-05T00:00:00+00:00"}]
            res = self.client.put("/api/account/display", json={"display_scale": 110, "user_id": "someone-else"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(fake.table.return_value.upsert.call_args[0][0], {"user_id": UID, "display_scale": 110})
            self.assertEqual((res.get_json()["display_scale"], res.get_json()["saved"]), (110, True))
            # Every page is now drawn at the saved size, without another query.
            fake.table.reset_mock()
            self.assertIn('style="font-size: 110%"', self.client.get("/account/profile").get_data(as_text=True))
            fake.table.assert_not_called()

    def test_display_size_is_loaded_at_sign_in_and_not_inherited_by_another_account(self):
        self.login()
        with self.client.session_transaction() as s:
            s["display"] = {"uid": "someone-else", "scale": 125}   # left by another account on this browser
        fake = self.fake_db(row={"display_scale": 80, "updated_at": None})
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            html = self.client.get("/account/profile").get_data(as_text=True)
        self.assertIn('style="font-size: 80%"', html)
        fake.table.return_value.select.return_value.eq.assert_called_with("user_id", UID)

    def test_display_rejects_other_sizes_and_failed_saves(self):
        self.login()
        fake = self.fake_db()
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            self.assertEqual(self.client.put("/api/account/display", json={"display_scale": 95}).status_code, 422)
            fake.table.return_value.upsert.assert_not_called()
        with mock.patch("app.routes.account.get_supabase_admin", side_effect=RuntimeError("timeout")):
            res = self.client.put("/api/account/display", json={"display_scale": 110})
            self.assertEqual(res.status_code, 502)
            self.assertNotIn("display_scale", res.get_json())
            # The database cannot be read either: pages fall back to the default instead of failing.
            self.assertIn('style="font-size: 90%"', self.client.get("/account/display").get_data(as_text=True))

    def test_signed_out_pages_are_not_scaled(self):
        self.assertNotIn("data-display-scale", self.client.get("/login").get_data(as_text=True))

    # ---- notifications

    def test_preferences_default_then_persist_for_this_user(self):
        self.login("citizen")
        fake = self.fake_db()
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            data = self.client.get("/api/account/notifications").get_json()
        self.assertFalse(data["saved"])
        self.assertEqual(data["preferences"], acct.DEFAULT_PREFERENCES)
        self.assertFalse(data["delivery"]["email"]["available"])

        wanted = {**acct.DEFAULT_PREFERENCES, "new_reports": False, "browser_enabled": True}
        fake.table.return_value.upsert.return_value.execute.return_value.data = [{"user_id": UID, **wanted, "updated_at": "2026-10-04T00:00:00+00:00"}]
        with mock.patch("app.routes.account.get_supabase_admin", return_value=fake):
            res = self.client.put("/api/account/notifications", json={**wanted, "user_id": "someone-else"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(fake.table.return_value.upsert.call_args[0][0], {"user_id": UID, **wanted})
        self.assertTrue(res.get_json()["saved"])
        self.assertFalse(res.get_json()["preferences"]["new_reports"])

    def test_preferences_save_failure_is_not_success(self):
        self.login("citizen")
        with mock.patch("app.routes.account.get_supabase_admin", side_effect=RuntimeError("timeout")):
            res = self.client.put("/api/account/notifications", json=acct.DEFAULT_PREFERENCES)
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("preferences", res.get_json())


if __name__ == "__main__":
    unittest.main()
