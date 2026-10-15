"""Offline tests for the Hospital Portal: registration, sign-in, the receiving workflow, bays,
history, staff and isolation between hospitals.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from supabase_auth.errors import AuthApiError

from app import create_app
from app import hospital as hs
from app.dispatch import ValidationError
from app.routes import hospital_auth
from tests.fake_incidents import FakeIncidentsDB

DISPATCHER = "00000000-0000-0000-0000-0000000000d1"
ADMIN_A = "00000000-0000-0000-0000-0000000000a0"
NURSE_A = "00000000-0000-0000-0000-0000000000a1"
ADMIN_B = "00000000-0000-0000-0000-0000000000b0"
OUTSIDER = "00000000-0000-0000-0000-0000000000c1"
NEW_USER = "00000000-0000-0000-0000-0000000000e1"
HOSPITAL_A = "10000000-0000-0000-0000-00000000000a"
HOSPITAL_B = "10000000-0000-0000-0000-00000000000b"
BAY_A1 = "40000000-0000-0000-0000-0000000000a1"
BAY_A2 = "40000000-0000-0000-0000-0000000000a2"
BAY_B1 = "40000000-0000-0000-0000-0000000000b1"
INCIDENT = "20000000-0000-0000-0000-000000000001"

REGISTRATION = {
    "hospital_name": "Charlie Medical Center", "hospital_type": "private", "address": "12 Mabini St", "city": "Davao City",
    "province": "Davao del Sur", "country": "Philippines", "contact_number": "+63 82 555 0100", "official_email": "info@charlie.example",
    "ed_contact": "ER desk +63 82 555 0111", "license_number": "DOH-2026-0042", "admin_name": "Carla Admin", "position": "ER Head Nurse",
    "email": "carla@charlie.example", "password": "Str0ngPassword!", "confirm_password": "Str0ngPassword!", "agree": "1",
}


def hospital(hospital_id, name, **extra):
    return {"id": hospital_id, "name": name, "address": "1 Test St", "city": "Davao City", "province": "Davao del Sur",
            "country": "Philippines", "phone_number": "+63 82 000 0001", "official_email": f"{name.split()[0].lower()}@example.com",
            "ed_contact": "ER desk", "hospital_type": "private", "license_number": None, "latitude": None, "longitude": None,
            "availability": "ready", "availability_updated_at": None, "is_active": True, "verified": True,
            "created_at": "2026-10-01T00:00:00+00:00", **extra}


def make_db():
    return FakeIncidentsDB(
        incidents=[{"id": INCIDENT, "incident_number": 8842, "emergency_type": "Fire & Hazmat: Building fire", "status": "en_route",
                    "priority": 1, "latitude": 7.0731, "longitude": 125.6128, "location_verified": True, "description": "Two-storey house fire",
                    "created_at": "2026-10-01T05:00:00+00:00", "archived_at": None, "citizen_name": "Reporter Name", "contact_number": "+63 900 000 0000"}],
        events=[{"id": 1, "incident_id": INCIDENT, "event": "status", "to_status": "dispatched", "created_at": "2026-10-01T05:01:00+00:00"}],
        hospitals=[hospital(HOSPITAL_A, "Alpha General Hospital"), hospital(HOSPITAL_B, "Bravo Medical Center")],
        hospital_staff=[
            {"hospital_id": HOSPITAL_A, "user_id": ADMIN_A, "staff_role": "admin", "position": "ER Chief", "created_at": "2026-10-01T00:00:00+00:00"},
            {"hospital_id": HOSPITAL_A, "user_id": NURSE_A, "staff_role": "nurse", "position": None, "created_at": "2026-10-02T00:00:00+00:00"},
            {"hospital_id": HOSPITAL_B, "user_id": ADMIN_B, "staff_role": "admin", "position": None, "created_at": "2026-10-01T00:00:00+00:00"},
        ],
        hospital_bays=[
            {"id": BAY_A1, "hospital_id": HOSPITAL_A, "name": "Trauma Bay 1", "kind": "trauma", "status": "ready"},
            {"id": BAY_A2, "hospital_id": HOSPITAL_A, "name": "Trauma Bay 2", "kind": "trauma", "status": "ready"},
            {"id": BAY_B1, "hospital_id": HOSPITAL_B, "name": "Trauma Bay 1", "kind": "trauma", "status": "ready"},
        ],
        hospital_signals=[], hospital_signal_events=[],
        users=[
            {"id": DISPATCHER, "email": "dispatch@example.com", "full_name": "Dana Dispatcher", "agency_name": None},
            {"id": ADMIN_A, "email": "admin.a@example.com", "full_name": "Alma Admin", "agency_name": None},
            {"id": NURSE_A, "email": "nurse.a@example.com", "full_name": "Nurse Alpha", "agency_name": None},
            {"id": ADMIN_B, "email": "admin.b@example.com", "full_name": "Bea Admin", "agency_name": None},
            {"id": OUTSIDER, "email": "outsider@example.com", "full_name": "Otto Outsider", "agency_name": None},
        ],
    )


class RegistrationRuleTests(unittest.TestCase):
    def test_valid_registration(self):
        row, account = hs.validate_registration({**REGISTRATION, "hospital_name": "  Charlie   Medical Center ", "email": " Carla@Charlie.Example "})
        self.assertEqual((row["name"], row["hospital_type"], row["official_email"], row["phone_number"], row["license_number"]),
                         ("Charlie Medical Center", "private", "info@charlie.example", "+63 82 555 0100", "DOH-2026-0042"))
        self.assertEqual((account["email"], account["full_name"], account["position"]), ("carla@charlie.example", "Carla Admin", "ER Head Nurse"))

    def test_every_required_field_is_reported(self):
        with self.assertRaises(ValidationError) as cm:
            hs.validate_registration({})
        self.assertEqual(set(cm.exception.errors), {"hospital_name", "hospital_type", "address", "city", "province", "country", "contact_number",
                                                    "official_email", "ed_contact", "admin_name", "position", "email", "password", "agree"})

    def test_invalid_values(self):
        cases = [({"email": "not-an-email"}, "email"), ({"official_email": "info@"}, "official_email"), ({"contact_number": "call us"}, "contact_number"),
                 ({"hospital_type": "spaceship"}, "hospital_type"), ({"confirm_password": "Different123!"}, "confirm_password"),
                 ({"password": "short1A", "confirm_password": "short1A"}, "password"), ({"password": "alllowercase123", "confirm_password": "alllowercase123"}, "password"),
                 ({"password": "NoDigitsHereAtAll", "confirm_password": "NoDigitsHereAtAll"}, "password"), ({"agree": ""}, "agree"),
                 ({"hospital_name": "x" * 121}, "hospital_name")]
        for change, field in cases:
            with self.assertRaises(ValidationError) as cm:
                hs.validate_registration({**REGISTRATION, **change})
            self.assertEqual(list(cm.exception.errors), [field], change)
        self.assertIsNone(hs.validate_registration({**REGISTRATION, "license_number": ""})[0]["license_number"])   # optional


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.db = make_db()
        for target in ("app.routes.hospital._db", "app.routes.hospital_auth.get_supabase_admin"):
            patcher = mock.patch(target, return_value=self.db)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Pages look up the signed-in user's display size (Settings -> Display); keep that offline too.
        display = mock.patch("app.routes.account._load_display_row", return_value=None)
        display.start()
        self.addCleanup(display.stop)
        self.dispatcher = self.client_for(DISPATCHER, "organization", "Dana Dispatcher")
        self.admin_a = self.client_for(ADMIN_A, "hospital", "Alma Admin")
        self.nurse_a = self.client_for(NURSE_A, "hospital", "Nurse Alpha")
        self.admin_b = self.client_for(ADMIN_B, "hospital", "Bea Admin")

    def client_for(self, user_id, role, name="Test"):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["user"] = {"id": user_id, "email": f"{name.split()[0].lower()}@example.com", "role": role, "name": name}
        return client

    def send(self, hospital_id=HOSPITAL_A, **changes):
        eta = datetime.now(timezone.utc).astimezone(hs.PHT) + timedelta(minutes=4)
        body = {"hospital_id": hospital_id, "incident_id": INCIDENT, "signal_type": "critical_patient", "patient_condition": "critical",
                "patient_age": "45", "patient_gender": "male", "eta_time": eta.strftime("%H:%M"), "eta_date": eta.strftime("%Y-%m-%d"),
                "medic_unit": "Medic 02", "transport_type": "ground_als", "triage_priority": "1",
                "chief_complaint": "Severe smoke inhalation and thermal burns", "heart_rate": "128", "bp_systolic": "92", "bp_diastolic": "58",
                "spo2": "88", "temperature": "37.4", "respiratory_rate": "30", "additional_notes": "Intubation likely. 20% TBSA burns."}
        body.update(changes)
        res = self.dispatcher.post("/api/hospital-signals/signals", json=body)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()["item"]

    def report(self, client, report_id):
        res = client.get(f"/api/hospital/reports/{report_id}")
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()["item"]

    def codes(self, client, **params):
        res = client.get("/api/hospital/reports", query_string=params)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return [i["code"] for i in res.get_json()["items"]]


class RegistrationAndSignInTests(PortalTestCase):
    def signup(self, session=None, identities=("email",)):
        auth = mock.MagicMock()
        auth.sign_up.return_value = SimpleNamespace(user=SimpleNamespace(id=NEW_USER, identities=list(identities)), session=session)
        patcher = mock.patch("app.routes.hospital_auth.get_supabase", return_value=SimpleNamespace(auth=auth))
        patcher.start()
        self.addCleanup(patcher.stop)
        return auth

    def test_portal_pages(self):
        anonymous = self.app.test_client()
        self.assertTrue(anonymous.get("/hospital/portal").headers["Location"].endswith("/hospital/login"))
        login = anonymous.get("/hospital/login").get_data(as_text=True)
        # The same sign-in page as the other ESRMS portals, wired to the hospital routes.
        for text in ("Welcome Back", "Hospital Portal sign-in for receiving hospitals.", "CHANGE PORTAL", "Forgot Password?", "Register for free",
                     'action="/hospital/login"', 'formaction="/hospital/forgot-password"', 'href="/hospital/register"'):
            self.assertIn(text, login)
        dispatcher_login = anonymous.get("/login?role=organization").get_data(as_text=True)   # the other portals are unchanged
        for text in ("Organization / 911 Dispatcher console sign-in.", 'action="/login?role=organization"', 'href="/register?role=organization"'):
            self.assertIn(text, dispatcher_login)
        self.assertNotIn("/hospital/", dispatcher_login)
        form = anonymous.get("/hospital/register").get_data(as_text=True)
        for text in ("Register Your Hospital — Free", "Hospital Name", "Emergency Department Contact", "Administrator Full Name",
                     "Create Hospital Account — Free", "I agree to the ERMS Hospital Portal Terms and Privacy Policy", "Already have an account?"):
            self.assertIn(text, form)
        self.assertIn('href="/hospital/login"', anonymous.get("/").get_data(as_text=True))    # linked from the ESRMS gateway
        self.assertTrue(self.admin_a.get("/hospital/portal").headers["Location"].endswith("/hospital"))

    def test_registration_creates_account_hospital_admin_and_bays(self):
        auth = self.signup()
        res = self.app.test_client().post("/hospital/register", data=REGISTRATION)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].endswith("/hospital/login"))                   # then: Hospital Sign In
        sent = auth.sign_up.call_args.args[0]
        self.assertEqual((sent["email"], sent["password"], sent["options"]["data"]["role"], sent["options"]["data"]["full_name"]),
                         ("carla@charlie.example", "Str0ngPassword!", "hospital", "Carla Admin"))
        created = [h for h in self.db.tables["hospitals"] if h["name"] == "Charlie Medical Center"]
        self.assertEqual(len(created), 1)
        row = created[0]
        self.assertEqual((row["city"], row["official_email"], row["ed_contact"], row["hospital_type"], row["registered_by"], row["is_active"]),
                         ("Davao City", "info@charlie.example", "ER desk +63 82 555 0111", "private", NEW_USER, True))
        self.assertIs(row["verified"], False)                                                   # dispatch has to verify it first
        staff = [s for s in self.db.tables["hospital_staff"] if s["hospital_id"] == row["id"]]
        self.assertEqual([(s["user_id"], s["staff_role"], s["position"]) for s in staff], [(NEW_USER, "admin", "ER Head Nurse")])
        bays = sorted(b["name"] for b in self.db.tables["hospital_bays"] if b["hospital_id"] == row["id"])
        self.assertEqual(bays, ["Emergency Room 1", "Emergency Room 2", "Trauma Bay 1", "Trauma Bay 2", "Trauma Bay 3"])
        with self.app.test_client() as client:
            client.post("/hospital/register", data={**REGISTRATION, "hospital_name": "Delta Hospital", "official_email": "d@delta.example", "email": "d2@delta.example"})
            with client.session_transaction() as s:
                self.assertNotIn("user", s)                                                     # registering does not sign anyone in
                self.assertIn("Hospital account created successfully.", " ".join(m for _, m in s.get("_flashes", [])))

    def test_registration_validation_creates_nothing(self):
        auth = self.signup()
        before = len(self.db.tables["hospitals"])
        res = self.app.test_client().post("/hospital/register", data={**REGISTRATION, "email": "bad", "confirm_password": "nope", "agree": ""})
        html = res.get_data(as_text=True)
        self.assertEqual(res.status_code, 200)
        for message in ("Enter a valid email address.", "The passwords do not match.", "You must agree to the Hospital Portal Terms"):
            self.assertIn(message, html)
        self.assertIn('value="Charlie Medical Center"', html)                                   # what was typed is kept
        self.assertNotIn("Str0ngPassword!", html)                                               # passwords are never echoed back
        auth.sign_up.assert_not_called()
        self.assertEqual(len(self.db.tables["hospitals"]), before)

    def test_duplicates_are_refused_before_anything_is_created(self):
        auth = self.signup()
        cases = [({"hospital_name": "alpha general hospital"}, "A hospital with this name is already registered"),
                 ({"official_email": "ALPHA@example.com"}, "already registered with this official email"),
                 ({"email": "Nurse.A@example.com"}, "An ESRMS account already uses this email")]
        for change, message in cases:
            res = self.app.test_client().post("/hospital/register", data={**REGISTRATION, **change})
            self.assertIn(message, res.get_data(as_text=True), change)
        auth.sign_up.assert_not_called()
        self.assertEqual(len(self.db.tables["hospitals"]), 2)

    def test_existing_auth_account_reported_by_supabase(self):
        auth = self.signup()
        auth.sign_up.side_effect = AuthApiError("User already registered", 422, "user_already_exists")
        self.assertIn("User already registered", self.app.test_client().post("/hospital/register", data=REGISTRATION).get_data(as_text=True))
        auth.sign_up.side_effect = None
        auth.sign_up.return_value = SimpleNamespace(user=SimpleNamespace(id=NEW_USER, identities=[]), session=None)   # decoy user
        self.assertIn("An ESRMS account already uses this email", self.app.test_client().post("/hospital/register", data=REGISTRATION).get_data(as_text=True))
        self.assertEqual(len(self.db.tables["hospitals"]), 2)

    def test_failed_registration_is_rolled_back(self):
        self.signup()
        real_table = self.db.table

        def failing(name):
            if name == "hospital_bays":
                raise RuntimeError("database went away")
            return real_table(name)
        with mock.patch.object(self.db, "table", side_effect=failing):
            res = self.app.test_client().post("/hospital/register", data=REGISTRATION)
        self.assertIn("Registration is temporarily unavailable", res.get_data(as_text=True))
        self.assertNotIn("database went away", res.get_data(as_text=True))
        self.assertEqual([h["name"] for h in self.db.tables["hospitals"]], ["Alpha General Hospital", "Bravo Medical Center"])
        self.db.auth.admin.delete_user.assert_called_once_with(NEW_USER)                         # no orphan sign-in is left behind

    def sign_in(self, user_id, role):
        auth = mock.MagicMock()
        auth.sign_in_with_password.return_value = SimpleNamespace(
            user=SimpleNamespace(id=user_id, email="who@example.com", user_metadata={"role": role, "full_name": "Who"}, app_metadata={}, factors=[]),
            session=SimpleNamespace(access_token="a.b.c", refresh_token="r", expires_at=int(datetime.now(timezone.utc).timestamp()) + 3600))
        return mock.patch("app.routes.hospital_auth.get_supabase", return_value=SimpleNamespace(auth=auth)), auth

    def test_sign_in_uses_supabase_auth_and_lands_on_the_dashboard(self):
        patcher, auth = self.sign_in(ADMIN_A, "hospital")
        with patcher, self.app.test_client() as client:
            res = client.post("/hospital/login", data={"email": "admin.a@example.com", "password": "Str0ngPassword!"})
            self.assertTrue(res.headers["Location"].endswith("/hospital"))
            auth.sign_in_with_password.assert_called_once_with({"email": "admin.a@example.com", "password": "Str0ngPassword!"})
            with client.session_transaction() as s:
                self.assertEqual((s["user"]["id"], s["user"]["role"]), (ADMIN_A, "hospital"))
            self.assertEqual(client.get("/hospital").status_code, 200)
            self.assertEqual(client.get("/api/hospital/overview").get_json()["hospital"]["name"], "Alpha General Hospital")

    def test_sign_in_failures(self):
        patcher, auth = self.sign_in(ADMIN_A, "hospital")
        auth.sign_in_with_password.side_effect = AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        with patcher, self.app.test_client() as client:
            res = client.post("/hospital/login", data={"email": "admin.a@example.com", "password": "wrong"})
            self.assertIn("Invalid login credentials", res.get_data(as_text=True))
            with client.session_transaction() as s:
                self.assertNotIn("user", s)
            self.assertIn("Enter your hospital email and password.", client.post("/hospital/login", data={"email": ""}).get_data(as_text=True))

    def test_non_hospital_account_is_sent_to_its_own_dashboard(self):
        patcher, _ = self.sign_in(OUTSIDER, "citizen")
        with patcher, self.app.test_client() as client:
            self.assertTrue(client.post("/hospital/login", data={"email": "o@example.com", "password": "x"}).headers["Location"].endswith("/dashboard"))

    def test_forgot_password(self):
        auth = mock.MagicMock()
        with mock.patch("app.routes.hospital_auth.get_supabase", return_value=SimpleNamespace(auth=auth)), self.app.test_client() as client:
            self.assertTrue(client.post("/hospital/forgot-password", data={"email": "admin.a@example.com"}).headers["Location"].endswith("/hospital/login"))
            self.assertEqual(auth.reset_password_for_email.call_args.args[0], "admin.a@example.com")
            client.post("/hospital/forgot-password", data={"email": ""})
            self.assertEqual(auth.reset_password_for_email.call_count, 1)

    def test_unverified_hospital_cannot_be_sent_reports_until_dispatch_verifies_it(self):
        self.db.tables["hospitals"][0]["verified"] = False
        res = self.dispatcher.post("/api/hospital-signals/signals", json={"hospital_id": HOSPITAL_A, "signal_type": "other", "patient_condition": "stable"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("has not been verified", res.get_json()["error"])
        self.assertFalse(self.admin_a.get("/api/hospital/overview").get_json()["hospital"]["verified"])
        listed = self.dispatcher.get("/api/hospital-signals/hospitals").get_json()["items"][0]
        self.assertEqual((listed["name"], listed["verified"]), ("Alpha General Hospital", False))
        body = {"name": "Alpha General Hospital", "address": "1 Test St", "verified": True}
        self.assertEqual(self.dispatcher.put(f"/api/hospital-signals/hospitals/{HOSPITAL_A}", json=body).status_code, 200)
        self.assertEqual((self.db.tables["hospitals"][0]["verified"], self.db.tables["hospitals"][0]["verified_by"]), (True, DISPATCHER))
        self.send()


class ReceivingWorkflowTests(PortalTestCase):
    def test_the_whole_scenario_keeps_one_report(self):
        # 7-9. Dispatch sends a report; it reaches the hospital as a Hospital Signal only. It is not an
        # Incoming Report until someone acknowledges it.
        sent = self.send()
        code, rid = sent["code"], sent["id"]
        self.assertEqual(self.codes(self.nurse_a, view="incoming"), [])
        signals = self.nurse_a.get("/api/hospital/reports?view=signals").get_json()["items"]
        self.assertEqual([(i["id"], i["code"], i["status"], i["status_label"]) for i in signals], [(rid, code, "sent", "Incoming")])
        card = signals[0]
        self.assertEqual((card["medic_unit"], card["triage_label"], card["chief_complaint"], card["patient_age"], card["gender_label"], card["critical"]),
                         ("Medic 02", "P1 · Critical", "Severe smoke inhalation and thermal burns", 45, "Male", True))
        self.assertEqual(self.nurse_a.get("/api/hospital/overview").get_json()["stats"],
                         {"incoming": 0, "critical": 1, "preparing": 0, "arrived": 0, "pending_handoffs": 0, "responding": 0, "on_scene": 0,
                          "completed_today": 0})

        # 10-11. The full report.
        full = self.report(self.nurse_a, rid)
        self.assertEqual((full["incident"]["code"], full["incident"]["emergency_type"], full["incident"]["description"], full["incident"]["latitude"]),
                         ("INC-008842", "Fire & Hazmat: Building fire", "Two-storey house fire", 7.0731))
        self.assertEqual((full["transport_label"], full["heart_rate"], full["bp_systolic"], full["bp_diastolic"], full["spo2"], full["temperature"],
                          full["respiratory_rate"], full["additional_notes"], full["hospital_name"], full["sent_by_name"]),
                         ("Ground ambulance (ALS)", 128, 92, 58, 88, 37.4, 30, "Intubation likely. 20% TBSA burns.", "Alpha General Hospital", "Dana Dispatcher"))
        self.assertEqual([e["event"] for e in full["timeline"]], ["INCIDENT_REPORTED", "MEDIC_DISPATCHED", "REPORT_RECEIVED"])
        self.assertNotIn("Reporter Name", str(full))                                            # the citizen's identity stays with dispatch
        self.assertNotIn("contact_number", full["incident"])
        self.assertFalse(full["read_only"])

        # 12-14. Acknowledge; a fresh request (a browser refresh) still shows it.
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/acknowledge", json={})
        self.assertEqual((res.status_code, res.get_json()["item"]["status_label"]), (200, "Acknowledged"))
        stored = self.db.tables["hospital_signals"][0]
        self.assertEqual((stored["status"], stored["acknowledged_by"], stored["hospital_id"]), ("acknowledged", NURSE_A, HOSPITAL_A))
        self.assertTrue(stored["acknowledged_at"])
        again = self.report(self.client_for(NURSE_A, "hospital", "Nurse Alpha"), rid)
        self.assertEqual((again["status"], again["acknowledged_by_name"]), ("acknowledged", "Nurse Alpha"))
        self.assertEqual(self.codes(self.nurse_a, view="signals"), [])                           # acknowledging moved it out of Hospital Signal...
        self.assertEqual(self.codes(self.nurse_a, view="incoming"), [code])                      # ...into Incoming Reports: same report, same id
        self.assertEqual(self.nurse_a.get("/api/hospital/overview").get_json()["stats"]["incoming"], 1)

        # 15-17. Trauma Bay 1, kept after a refresh.
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1, "receiving_team": "Trauma Team A"})
        self.assertEqual((res.status_code, res.get_json()["item"]["bay_name"], res.get_json()["item"]["status_label"]), (200, "Trauma Bay 1", "Preparing"))
        again = self.report(self.client_for(ADMIN_A, "hospital", "Alma Admin"), rid)
        self.assertEqual((again["bay_name"], again["receiving_team"], again["status"]), ("Trauma Bay 1", "Trauma Team A", "accepted"))
        bays = {b["name"]: b for b in self.nurse_a.get("/api/hospital/overview").get_json()["bays"]}
        self.assertEqual((bays["Trauma Bay 1"]["status_label"], bays["Trauma Bay 1"]["report_code"], bays["Trauma Bay 2"]["status_label"]),
                         ("Preparing", code, "Ready"))

        # 18-20. Arrived -> handoff -> completed.
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/arrive", json={})
        self.assertEqual((res.get_json()["item"]["status_label"], res.get_json()["item"]["actions"]), ("Arrived", ["handoff"]))
        self.assertEqual((self.db.tables["hospital_signals"][0]["arrived_by"], bool(self.db.tables["hospital_signals"][0]["arrived_at"])), (NURSE_A, True))
        self.assertEqual({b["name"]: b["status_label"] for b in self.nurse_a.get("/api/hospital/overview").get_json()["bays"]}["Trauma Bay 1"], "Occupied")
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/handoff", json={})
        self.assertEqual((res.get_json()["item"]["status_label"], res.get_json()["item"]["actions"]), ("Handoff", ["complete"]))
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/complete", json={}).status_code, 422)   # who received the patient?
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/complete", json={"receiving_staff": "Dr. Reyes", "handoff_notes": "Airway secured in bay."})
        done = res.get_json()["item"]
        self.assertEqual((res.status_code, done["status"], done["status_label"], done["read_only"], done["actions"]), (200, "completed", "Completed", True, []))
        stored = self.db.tables["hospital_signals"][0]
        self.assertEqual((stored["completed_by"], stored["receiving_staff"], stored["handoff_notes"]), (NURSE_A, "Dr. Reyes", "Airway secured in bay."))
        self.assertTrue(stored["completed_at"])

        # 21-22. It moved: out of Incoming, into Completed Handoffs and History. Still the same id and code.
        self.assertEqual(self.codes(self.nurse_a, view="incoming"), [])
        self.assertEqual(self.codes(self.nurse_a, view="completed"), [code])
        self.assertEqual(self.codes(self.nurse_a, view="history"), [code])
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)
        self.assertEqual(self.nurse_a.get("/api/hospital/overview").get_json()["stats"]["completed_today"], 1)
        bays = {b["name"]: b for b in self.nurse_a.get("/api/hospital/overview").get_json()["bays"]}
        self.assertEqual((bays["Trauma Bay 1"]["status_label"], bays["Trauma Bay 1"]["report_code"]), ("Cleaning", None))

        # 23-27. Open it from History: complete, archived, read-only.
        archived = self.report(self.nurse_a, rid)
        self.assertEqual((archived["id"], archived["code"], archived["read_only"], archived["bay_name"], archived["receiving_staff"]),
                         (rid, code, True, "Trauma Bay 1", "Dr. Reyes"))
        self.assertEqual([e["event"] for e in archived["timeline"]],
                         ["INCIDENT_REPORTED", "MEDIC_DISPATCHED", "REPORT_RECEIVED", "REPORT_ACKNOWLEDGED", "TRAUMA_BAY_ASSIGNED",
                          "BAY_PREPARATION_STARTED", "PATIENT_ARRIVED", "HANDOFF_STARTED", "HANDOFF_COMPLETED", "TRAUMA_BAY_RELEASED", "REPORT_ARCHIVED"])
        self.assertEqual({e["event"]: e.get("detail") for e in archived["timeline"]}["TRAUMA_BAY_ASSIGNED"], "Trauma Bay 1")
        self.assertTrue(all(e["actor"] == "Nurse Alpha" for e in archived["timeline"] if e["event"].startswith(("REPORT_A", "PATIENT", "HANDOFF"))))
        for action in ("acknowledge", "accept", "arrive", "handoff", "complete", "unable"):
            res = self.nurse_a.post(f"/api/hospital/reports/{rid}/{action}", json={"reason": "x", "receiving_staff": "x"})
            self.assertEqual(res.status_code, 409, action)
            self.assertIn("read-only", res.get_json()["error"])
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A2}).status_code, 409)
        self.assertEqual(self.db.tables["hospital_signals"][0]["bay_id"], BAY_A1)
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{rid}/cancel", json={}).status_code, 409)
        seen = self.dispatcher.get(f"/api/hospital-signals/signals/{rid}").get_json()["item"]
        self.assertEqual((seen["code"], seen["status_label"], seen["bay_name"]), (code, "Completed", "Trauma Bay 1"))

    def test_any_first_action_moves_a_signal_to_incoming(self):
        # A bay assigned or a patient arriving before anyone pressed Acknowledge still counts as acknowledged.
        by_bay, by_arrival, untouched = self.send()["id"], self.send()["id"], self.send()
        self.nurse_a.post(f"/api/hospital/reports/{by_bay}/bay", json={"bay_id": BAY_A1})
        self.nurse_a.post(f"/api/hospital/reports/{by_arrival}/arrive", json={})
        self.assertEqual(self.codes(self.nurse_a, view="signals"), [untouched["code"]])
        incoming = self.nurse_a.get("/api/hospital/reports?view=incoming").get_json()["items"]
        self.assertEqual({i["id"] for i in incoming}, {by_bay, by_arrival})
        self.assertTrue(all(i["acknowledged_at"] for i in incoming))

    def test_steps_cannot_be_skipped_at_the_end(self):
        rid = self.send()["id"]
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/handoff", json={}).status_code, 409)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/complete", json={"receiving_staff": "Dr X"}).status_code, 409)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/arrive", json={}).status_code, 200)       # arriving unannounced is fine
        self.assertTrue(self.db.tables["hospital_signals"][0]["acknowledged_at"])
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/complete", json={"receiving_staff": "Dr X"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("Start the handoff", res.get_json()["error"])

    def test_incoming_filters_and_order(self):
        stable = self.send(patient_condition="stable", signal_type="incoming_patient", triage_priority="2", chief_complaint="Suspected acute stroke", medic_unit="Medic 14")
        critical = self.send()
        self.assertEqual(self.codes(self.nurse_a, view="signals"), [critical["code"], stable["code"]])             # critical first, in both lists
        for item in (stable, critical):
            self.nurse_a.post(f"/api/hospital/reports/{item['id']}/acknowledge", json={})
        self.assertEqual(self.codes(self.nurse_a, view="incoming"), [critical["code"], stable["code"]])
        self.assertEqual(self.codes(self.nurse_a, view="incoming", filter="critical"), [critical["code"]])
        self.assertEqual(self.codes(self.nurse_a, view="incoming", filter="stable"), [stable["code"]])

    def test_unable_to_receive_goes_to_history_with_its_reason(self):
        rid = self.send()["id"]
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/unable", json={}).status_code, 422)
        self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1})
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/unable", json={"reason": "No available bed"}).status_code, 200)
        self.assertEqual(self.codes(self.nurse_a, view="completed"), [])
        item = self.nurse_a.get("/api/hospital/reports?view=history").get_json()["items"][0]
        self.assertEqual((item["status_label"], item["rejection_reason"], item["read_only"]), ("Unable to Receive", "No available bed", True))
        bay = next(b for b in self.nurse_a.get("/api/hospital/overview").get_json()["bays"] if b["id"] == BAY_A1)
        self.assertEqual((bay["status_label"], bay["assignable"]), ("Ready", True))                               # nobody used it; it is free again


class BayTests(PortalTestCase):
    def test_one_active_patient_per_bay(self):
        first, second = self.send()["id"], self.send()["id"]
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{first}/bay", json={"bay_id": BAY_A1}).status_code, 200)
        res = self.nurse_a.post(f"/api/hospital/reports/{second}/bay", json={"bay_id": BAY_A1})
        self.assertEqual(res.status_code, 409)
        self.assertIn("already assigned to HS-", res.get_json()["error"])
        self.assertIsNone(self.db.tables["hospital_signals"][1]["bay_id"])
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{second}/bay", json={"bay_id": BAY_A2}).status_code, 200)
        # Moving the first patient to a free bay releases Bay 1 for someone else.
        self.db.tables["hospital_bays"].append({"id": "40000000-0000-0000-0000-0000000000a3", "hospital_id": HOSPITAL_A, "name": "Emergency Room 1", "kind": "er", "status": "ready"})
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{first}/bay", json={"bay_id": "40000000-0000-0000-0000-0000000000a3"}).status_code, 200)
        third = self.send()["id"]
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{third}/bay", json={"bay_id": BAY_A1}).status_code, 200)

    def test_two_assignments_at_the_same_moment(self):
        first, second = self.send()["id"], self.send()["id"]
        self.nurse_a.post(f"/api/hospital/reports/{first}/bay", json={"bay_id": BAY_A1})
        real_table = self.db.table

        def racing(name):   # the "is it taken?" check ran before the other assignment landed
            query = real_table(name)
            if name == "hospital_signals" and getattr(racing, "blind", True):
                original = query.in_
                query.in_ = lambda col, values: original(col, []) if col == "status" else original(col, values)
            return query
        with mock.patch.object(self.db, "table", side_effect=racing):
            res = self.nurse_a.post(f"/api/hospital/reports/{second}/bay", json={"bay_id": BAY_A1})
        self.assertEqual(res.status_code, 409)                                                                      # the unique index decides
        self.assertIn("another patient a moment ago", res.get_json()["error"])
        self.assertIsNone(self.db.tables["hospital_signals"][1]["bay_id"])

    def test_bay_must_be_ready_and_belong_to_the_hospital(self):
        rid = self.send()["id"]
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_B1}).status_code, 422)   # another hospital's bay
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={}).status_code, 422)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/bays/{BAY_A1}/status", json={"status": "cleaning"}).status_code, 200)
        res = self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1})
        self.assertEqual(res.status_code, 409)
        self.assertIn("Cleaning", res.get_json()["error"])
        self.nurse_a.post(f"/api/hospital/bays/{BAY_A1}/status", json={"status": "ready"})
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1}).status_code, 200)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/bays/{BAY_A1}/status", json={"status": "cleaning"}).status_code, 409)   # a patient is in it
        self.assertEqual(self.nurse_a.post(f"/api/hospital/bays/{BAY_B1}/status", json={"status": "cleaning"}).status_code, 404)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/bays/{BAY_A2}/status", json={"status": "occupied"}).status_code, 422)

    def test_bay_management_is_for_hospital_administrators(self):
        self.assertEqual(self.nurse_a.post("/api/hospital/bays", json={"name": "Trauma Bay 9"}).status_code, 403)
        res = self.admin_a.post("/api/hospital/bays", json={"name": "Resus Room", "kind": "er"})
        self.assertEqual(res.status_code, 201)
        self.assertIn("Resus Room", [b["name"] for b in res.get_json()["items"]])
        self.assertEqual(self.admin_a.post("/api/hospital/bays", json={"name": "trauma bay 1"}).status_code, 422)   # duplicate name
        self.assertEqual(self.admin_a.post("/api/hospital/bays", json={"name": ""}).status_code, 422)
        self.assertEqual(self.admin_a.put(f"/api/hospital/bays/{BAY_A2}", json={"name": "Trauma Bay B", "kind": "trauma"}).status_code, 200)
        self.assertEqual(self.admin_a.put(f"/api/hospital/bays/{BAY_B1}", json={"name": "Stolen", "kind": "trauma"}).status_code, 404)
        self.assertEqual(self.admin_a.delete(f"/api/hospital/bays/{BAY_B1}").status_code, 404)
        self.assertEqual(self.db.tables["hospital_bays"][2]["name"], "Trauma Bay 1")
        rid = self.send()["id"]
        self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1})
        self.assertEqual(self.admin_a.delete(f"/api/hospital/bays/{BAY_A1}").status_code, 409)
        self.assertEqual(self.admin_a.delete(f"/api/hospital/bays/{BAY_A2}").status_code, 200)


class HistoryTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc)
        specs = [(1, "completed", 0, "Juan Dela Cruz", 1, "Chest pain"), (2, "completed", 3, "Maria Santos", 3, "Fractured wrist"),
                 (3, "unable", 10, None, 2, "Suspected stroke"), (4, "cancelled", 40, None, None, None), (5, "received", 2, "Legacy Patient", None, None)]
        for n, status, days, name, triage, complaint in specs:
            when = (now - timedelta(days=days, hours=1)).isoformat()
            self.db.tables["hospital_signals"].append({
                **self.db.defaults["hospital_signals"], "id": f"30000000-0000-0000-0000-{n:012d}", "signal_number": n, "hospital_id": HOSPITAL_A,
                "incident_id": INCIDENT if n == 1 else None, "signal_type": "incoming_patient", "patient_condition": "stable", "patient_name": name,
                "triage_priority": triage, "chief_complaint": complaint, "status": status, "sent_by": DISPATCHER, "sent_at": when, "updated_at": when,
                "completed_at": when if status == "completed" else None, "rejection_reason": "No available bed" if status == "unable" else None})
        for n in range(6, 18):
            when = (now - timedelta(days=50 + n)).isoformat()
            self.db.tables["hospital_signals"].append({
                **self.db.defaults["hospital_signals"], "id": f"30000000-0000-0000-0000-{n:012d}", "signal_number": n, "hospital_id": HOSPITAL_A,
                "signal_type": "bed_request", "patient_condition": "stable", "status": "completed", "sent_by": DISPATCHER, "sent_at": when,
                "updated_at": when, "completed_at": when})
        self.db.tables["hospital_signals"].append({
            **self.db.defaults["hospital_signals"], "id": "30000000-0000-0000-0000-00000000b001", "signal_number": 99, "hospital_id": HOSPITAL_B,
            "signal_type": "incoming_patient", "patient_condition": "critical", "patient_name": "Other Hospital Patient", "status": "completed",
            "sent_by": DISPATCHER, "sent_at": now.isoformat(), "updated_at": now.isoformat(), "completed_at": now.isoformat()})

    def test_history_filters(self):
        self.assertEqual(self.nurse_a.get("/api/hospital/reports?view=history").get_json()["total"], 17)
        self.assertEqual(self.codes(self.nurse_a, view="history", filter="today"), ["HS-000001"])
        self.assertEqual(self.codes(self.nurse_a, view="history", filter="week"), ["HS-000001", "HS-000005", "HS-000002"])
        self.assertEqual(self.codes(self.nurse_a, view="history", filter="month"), ["HS-000001", "HS-000005", "HS-000002", "HS-000003"])
        self.assertEqual(self.codes(self.nurse_a, view="history", filter="critical"), ["HS-000001"])
        self.assertEqual(self.nurse_a.get("/api/hospital/reports?view=history&filter=completed").get_json()["total"], 15)   # completed + legacy received
        self.assertEqual(self.nurse_a.get("/api/hospital/reports?view=completed").get_json()["total"], 15)

    def test_history_search(self):
        self.assertEqual(self.codes(self.nurse_a, view="history", q="maria"), ["HS-000002"])
        self.assertEqual(self.codes(self.nurse_a, view="history", q="HS-000003"), ["HS-000003"])
        self.assertEqual(self.codes(self.nurse_a, view="history", q="INC-008842"), ["HS-000001"])                  # by CAD / incident number
        self.assertEqual(self.codes(self.nurse_a, view="history", q="building fire"), ["HS-000001"])                # by incident
        self.assertEqual(self.codes(self.nurse_a, view="history", q="stroke"), ["HS-000003"])                       # by chief complaint
        self.assertEqual(self.codes(self.nurse_a, view="history", q="zzzz"), [])
        self.assertEqual(self.codes(self.nurse_a, view="history", q="Other Hospital"), [])                          # never another hospital's patient

    def test_history_pagination_and_isolation(self):
        seen = []
        for page in (1, 2):
            data = self.nurse_a.get(f"/api/hospital/reports?view=history&page={page}").get_json()
            self.assertEqual((data["total"], data["pages"]), (17, 2))
            seen += [i["id"] for i in data["items"]]
        self.assertEqual((len(seen), len(set(seen))), (17, 17))
        self.assertEqual(self.nurse_a.get("/api/hospital/reports?view=history&page=9").get_json()["page"], 1)
        self.assertNotIn("Other Hospital Patient", self.nurse_a.get("/api/hospital/reports?view=history&per_page=50").get_data(as_text=True))
        self.assertEqual(self.codes(self.admin_b, view="history"), ["HS-000099"])

    def test_archived_reports_are_read_only(self):
        for n in (1, 3, 4, 5):
            rid = f"30000000-0000-0000-0000-{n:012d}"
            item = self.report(self.nurse_a, rid)
            self.assertEqual((item["read_only"], item["actions"]), (True, []), n)
            for action in ("acknowledge", "arrive", "handoff", "complete", "unable"):
                self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/{action}", json={"reason": "x", "receiving_staff": "x"}).status_code, 409)
            self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_A1}).status_code, 409)
        self.assertEqual(self.report(self.nurse_a, "30000000-0000-0000-0000-000000000005")["status_label"], "Completed")
        for method in ("put", "patch", "delete"):                                                                    # there is no edit route at all
            self.assertEqual(getattr(self.nurse_a, method)("/api/hospital/reports/30000000-0000-0000-0000-000000000001", json={}).status_code, 405)


class IsolationAndSettingsTests(PortalTestCase):
    def test_hospital_b_never_sees_hospital_a(self):
        rid = self.send(patient_name="Alpha Patient")["id"]
        for view in ("incoming", "signals", "completed", "history"):
            self.assertNotIn("Alpha Patient", self.admin_b.get(f"/api/hospital/reports?view={view}").get_data(as_text=True), view)
        self.assertEqual(self.admin_b.get(f"/api/hospital/reports/{rid}").status_code, 404)
        for action in ("acknowledge", "accept", "arrive", "handoff", "complete", "unable"):
            self.assertEqual(self.admin_b.post(f"/api/hospital/reports/{rid}/{action}", json={"reason": "x", "receiving_staff": "x"}).status_code, 404, action)
        self.assertEqual(self.admin_b.post(f"/api/hospital/reports/{rid}/bay", json={"bay_id": BAY_B1}).status_code, 404)
        self.assertEqual(self.db.tables["hospital_signals"][0]["status"], "sent")
        self.assertEqual(self.admin_b.get("/api/hospital/overview").get_json()["stats"]["incoming"], 0)
        self.assertEqual([b["id"] for b in self.admin_b.get("/api/hospital/overview").get_json()["bays"]], [BAY_B1])

    def test_a_hospital_id_from_the_browser_is_ignored(self):
        self.send(patient_name="Alpha Patient")
        for url in (f"/api/hospital/reports?hospital_id={HOSPITAL_A}", f"/api/hospital/overview?hospital_id={HOSPITAL_A}"):
            self.assertNotIn("Alpha", self.admin_b.get(url).get_data(as_text=True))
        res = self.admin_b.put("/api/hospital/profile", json={
            "hospital_id": HOSPITAL_A, "hospital_name": "Bravo Renamed", "address": "9 New St", "city": "Tagum", "province": "Davao del Norte",
            "country": "Philippines", "contact_number": "+63 84 000 0002", "official_email": "bravo@example.com", "ed_contact": "ER"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([h["name"] for h in self.db.tables["hospitals"]], ["Alpha General Hospital", "Bravo Renamed"])

    def test_everything_needs_sign_in_and_membership(self):
        rid = self.send()["id"]
        anonymous, outsider = self.app.test_client(), self.client_for(OUTSIDER, "hospital", "Otto Outsider")
        gets = ["/api/hospital/overview", "/api/hospital/reports", f"/api/hospital/reports/{rid}", "/api/hospital/staff"]
        posts = [f"/api/hospital/reports/{rid}/acknowledge", f"/api/hospital/reports/{rid}/bay", "/api/hospital/availability", "/api/hospital/bays", "/api/hospital/staff"]
        for path in gets:
            self.assertEqual((anonymous.get(path).status_code, outsider.get(path).status_code, self.dispatcher.get(path).status_code), (401, 403, 403), path)
        for path in posts:
            self.assertEqual((anonymous.post(path, json={}).status_code, outsider.post(path, json={}).status_code), (401, 403), path)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{rid}/acknowledge", data="x=1").status_code, 415)
        self.assertEqual(self.db.tables["hospital_signals"][0]["status"], "sent")

    def test_profile_is_edited_by_the_hospital_administrator(self):
        body = {"hospital_name": "Alpha General Hospital", "address": "2 Updated Ave", "city": "Davao City", "province": "Davao del Sur",
                "country": "Philippines", "contact_number": "+63 82 111 2222", "official_email": "er@alpha.example", "ed_contact": "ER desk 24/7"}
        self.assertEqual(self.nurse_a.put("/api/hospital/profile", json=body).status_code, 403)
        res = self.admin_a.put("/api/hospital/profile", json=body)
        self.assertEqual((res.status_code, res.get_json()["item"]["address"], res.get_json()["item"]["ed_contact"]), (200, "2 Updated Ave", "ER desk 24/7"))
        self.assertEqual(self.admin_a.put("/api/hospital/profile", json={**body, "official_email": "nope", "city": ""}).status_code, 422)
        self.assertEqual(self.admin_a.put("/api/hospital/profile", json={**body, "hospital_name": "bravo medical center"}).status_code, 422)
        self.assertIs(self.db.tables["hospitals"][0]["verified"], True)                         # a hospital cannot verify itself

    def test_staff_management(self):
        listing = self.nurse_a.get("/api/hospital/staff").get_json()
        self.assertEqual(([m["name"] for m in listing["items"]], listing["can_manage"]), (["Alma Admin", "Nurse Alpha"], False))
        new = {"email": "dr.cruz@alpha.example", "full_name": "Dr Cruz", "staff_role": "physician", "position": "ER Consultant", "password": "TempPassw0rd123"}
        self.assertEqual(self.nurse_a.post("/api/hospital/staff", json=new).status_code, 403)
        self.db.auth.admin.create_user.return_value = SimpleNamespace(user=SimpleNamespace(id=NEW_USER))
        res = self.admin_a.post("/api/hospital/staff", json=new)
        self.assertEqual((res.status_code, res.get_json()["created"]), (201, True))
        sent = self.db.auth.admin.create_user.call_args.args[0]
        self.assertEqual((sent["email"], sent["user_metadata"]["role"], sent["email_confirm"]), ("dr.cruz@alpha.example", "hospital", True))
        member = [s for s in self.db.tables["hospital_staff"] if s["user_id"] == NEW_USER][0]
        self.assertEqual((member["hospital_id"], member["staff_role"], member["position"]), (HOSPITAL_A, "physician", "ER Consultant"))
        for bad in ({"password": "weak"}, {"staff_role": "king"}, {"email": "nope"}, {"full_name": ""}):
            self.assertEqual(self.admin_a.post("/api/hospital/staff", json={**new, "email": "x@alpha.example", **bad}).status_code, 422, bad)
        # An existing ESRMS account is attached, not recreated; the same person cannot be added twice.
        res = self.admin_a.post("/api/hospital/staff", json={"email": "outsider@example.com", "staff_role": "reception"})
        self.assertEqual((res.status_code, res.get_json()["created"]), (201, False))
        self.assertEqual(self.admin_a.post("/api/hospital/staff", json={"email": "nurse.a@example.com", "staff_role": "nurse"}).status_code, 422)
        self.assertEqual(self.db.auth.admin.create_user.call_count, 1)

    def test_staff_roles_and_removal(self):
        self.assertEqual(self.admin_a.put(f"/api/hospital/staff/{NURSE_A}", json={"staff_role": "physician", "position": "Resident"}).status_code, 200)
        self.assertEqual(self.db.tables["hospital_staff"][1]["staff_role"], "physician")
        self.assertEqual(self.admin_a.put(f"/api/hospital/staff/{ADMIN_A}", json={"staff_role": "nurse"}).status_code, 409)      # the only administrator
        self.assertEqual(self.admin_a.delete(f"/api/hospital/staff/{ADMIN_A}").status_code, 409)                                  # cannot remove yourself
        self.assertEqual(self.admin_a.put(f"/api/hospital/staff/{ADMIN_B}", json={"staff_role": "nurse"}).status_code, 404)      # not on this hospital's list
        self.assertEqual(self.admin_a.delete(f"/api/hospital/staff/{ADMIN_B}").status_code, 404)
        self.assertEqual(self.db.tables["hospital_staff"][2]["staff_role"], "admin")
        self.assertEqual(self.nurse_a.delete(f"/api/hospital/staff/{ADMIN_A}").status_code, 403)
        self.assertEqual(self.admin_a.delete(f"/api/hospital/staff/{NURSE_A}").status_code, 200)
        self.assertEqual(self.nurse_a.get("/api/hospital/reports").status_code, 403)                                             # access ends at once

    def test_dashboard_page(self):
        html = self.nurse_a.get("/hospital").get_data(as_text=True)
        for text in ("Hospital Medical Reception Dashboard", "Incoming Reports", "Hospital Signal", "Completed Handoffs", "History", "Settings",
                     "/api/hospital", "hospital-portal.js"):
            self.assertIn(text, html)
        self.assertTrue(self.app.test_client().get("/hospital").headers["Location"].endswith("/hospital/login"))
        self.assertTrue(self.nurse_a.get("/dashboard").headers["Location"].endswith("/hospital"))


if __name__ == "__main__":
    unittest.main()
