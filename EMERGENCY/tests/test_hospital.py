"""Offline tests for Hospital Signal: rules, the dispatcher and hospital APIs, and who may see what.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import create_app
from app import hospital as hs
from app.dispatch import ValidationError
from tests.fake_incidents import FakeIncidentsDB

NOW = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)   # 14:30 PHT

DISPATCHER = "00000000-0000-0000-0000-0000000000d1"
OTHER_DISPATCHER = "00000000-0000-0000-0000-0000000000d2"
NURSE_A = "00000000-0000-0000-0000-0000000000a1"
NURSE_B = "00000000-0000-0000-0000-0000000000b1"
CITIZEN = "00000000-0000-0000-0000-0000000000c1"
HOSPITAL_A = "10000000-0000-0000-0000-00000000000a"
HOSPITAL_B = "10000000-0000-0000-0000-00000000000b"
HOSPITAL_OFF = "10000000-0000-0000-0000-00000000000f"
INCIDENT = "20000000-0000-0000-0000-000000000001"
INCIDENT_NO_GPS = "20000000-0000-0000-0000-000000000002"


def make_db():
    return FakeIncidentsDB(
        incidents=[
            {"id": INCIDENT, "incident_number": 21, "emergency_type": "Medical & EMS: Unconscious person", "status": "en_route",
             "priority": 1, "latitude": 7.0731, "longitude": 125.6128, "created_at": "2026-10-05T05:00:00+00:00", "archived_at": None,
             "citizen_name": "Reporter Name", "contact_number": "+63 900 000 0000"},
            {"id": INCIDENT_NO_GPS, "incident_number": 22, "emergency_type": "Contacts & Roads: Road accident", "status": "resolved",
             "priority": None, "latitude": None, "longitude": None, "created_at": "2026-10-04T05:00:00+00:00",
             "archived_at": "2026-10-04T08:00:00+00:00"},
        ],
        hospitals=[
            {"id": HOSPITAL_A, "name": "Alpha General Hospital", "address": "1 Test St", "phone_number": "+63 82 000 0001",
             "latitude": 7.0800, "longitude": 125.6200, "availability": "ready", "availability_updated_at": None, "is_active": True},
            {"id": HOSPITAL_B, "name": "Bravo Medical Center", "address": None, "phone_number": None,
             "latitude": 7.2000, "longitude": 125.7000, "availability": "limited", "availability_updated_at": None, "is_active": True},
            {"id": HOSPITAL_OFF, "name": "Offline Clinic", "address": None, "phone_number": None,
             "latitude": None, "longitude": None, "availability": "offline", "availability_updated_at": None, "is_active": True},
        ],
        hospital_staff=[{"hospital_id": HOSPITAL_A, "user_id": NURSE_A}, {"hospital_id": HOSPITAL_B, "user_id": NURSE_B}],
        hospital_signals=[],
        users=[
            {"id": DISPATCHER, "email": "dispatch@example.com", "full_name": "Dana Dispatcher", "agency_name": None},
            {"id": NURSE_A, "email": "nurse.a@example.com", "full_name": "Nurse Alpha", "agency_name": None},
            {"id": NURSE_B, "email": "nurse.b@example.com", "full_name": "Nurse Bravo", "agency_name": None},
            {"id": CITIZEN, "email": "citizen@example.com", "full_name": "Cee Citizen", "agency_name": None},
        ],
    )


class RuleTests(unittest.TestCase):
    def test_actions_by_side_and_status(self):
        self.assertEqual(hs.allowed_actions("sent", "hospital"), ["acknowledge", "accept", "arrive", "unable"])
        self.assertEqual(hs.allowed_actions("acknowledged", "hospital"), ["accept", "arrive", "unable"])
        self.assertEqual(hs.allowed_actions("accepted", "hospital"), ["arrive", "unable"])
        self.assertEqual(hs.allowed_actions("arrived", "hospital"), ["handoff"])
        self.assertEqual(hs.allowed_actions("handoff", "hospital"), ["complete"])
        self.assertEqual(hs.allowed_actions("accepted", "dispatcher"), ["cancel"])
        self.assertEqual(hs.allowed_actions("arrived", "dispatcher"), [])           # too late to cancel once the patient is there
        for final in hs.FINAL:
            self.assertEqual(hs.allowed_actions(final, "hospital") + hs.allowed_actions(final, "dispatcher"), [], final)

    def test_every_status_is_active_or_final_and_labelled(self):
        self.assertEqual(set(hs.ACTIVE) | hs.FINAL, set(hs.STATUSES))
        self.assertFalse(set(hs.ACTIVE) & hs.FINAL)
        self.assertEqual(set(hs.PORTAL_STATUSES), set(hs.STATUSES))
        self.assertEqual((hs.PORTAL_STATUSES["sent"], hs.PORTAL_STATUSES["accepted"]), ("Incoming", "Preparing"))
        for action, spec in hs.ACTIONS.items():
            self.assertIn(spec[4], hs.EVENTS, action)

    def test_transition_stamps_actor_and_time(self):
        changes = hs.transition({"status": "sent"}, "acknowledge", NURSE_A, "hospital", now=NOW)
        self.assertEqual(changes, {"status": "acknowledged", "acknowledged_by": NURSE_A, "acknowledged_at": NOW.isoformat()})
        skipped = hs.transition({"status": "sent"}, "arrive", NURSE_A, "hospital", now=NOW)
        self.assertEqual((skipped["status"], skipped["arrived_by"], skipped["arrived_at"], skipped["acknowledged_by"]),
                         ("arrived", NURSE_A, NOW.isoformat(), NURSE_A))
        started = hs.transition({"status": "arrived", "acknowledged_at": "x"}, "handoff", NURSE_A, "hospital", now=NOW)
        self.assertEqual(started, {"status": "handoff", "handoff_started_by": NURSE_A, "handoff_started_at": NOW.isoformat()})

    def test_transition_refuses_wrong_side_and_wrong_status(self):
        for row, action, side in [({"status": "sent"}, "cancel", "hospital"), ({"status": "sent"}, "accept", "dispatcher"),
                                  ({"status": "completed"}, "cancel", "dispatcher"), ({"status": "cancelled"}, "acknowledge", "hospital"),
                                  ({"status": "acknowledged"}, "acknowledge", "hospital"), ({"status": "sent"}, "explode", "hospital"),
                                  ({"status": "accepted"}, "handoff", "hospital"), ({"status": "arrived"}, "complete", "hospital"),
                                  ({"status": "completed"}, "arrive", "hospital"), ({"status": "received"}, "handoff", "hospital")]:
            with self.assertRaises(hs.TransitionError, msg=(row, action, side)):
                hs.transition(row, action, NURSE_A, side, {"receiving_staff": "Dr X"})
        with self.assertRaises(hs.TransitionError) as cm:
            hs.transition({"status": "completed"}, "arrive", NURSE_A, "hospital")
        self.assertIn("read-only", str(cm.exception))

    def test_unable_requires_a_reason(self):
        for data in (None, {}, {"reason": ""}, {"reason": "   \n "}):
            with self.assertRaises(ValidationError) as cm:
                hs.transition({"status": "sent"}, "unable", NURSE_A, "hospital", data)
            self.assertIn("reason", cm.exception.errors)
        with self.assertRaises(ValidationError):
            hs.transition({"status": "sent"}, "unable", NURSE_A, "hospital", {"reason": "x" * 301})
        ok = hs.transition({"status": "acknowledged", "acknowledged_at": "x"}, "unable", NURSE_A, "hospital", {"reason": "  No available   bed "}, now=NOW)
        self.assertEqual((ok["status"], ok["rejection_reason"], ok["rejected_by"]), ("unable", "No available bed", NURSE_A))
        self.assertNotIn("acknowledged_at", ok)

    def test_complete_requires_who_received_the_patient(self):
        row = {"status": "handoff", "acknowledged_at": "x"}
        for data in (None, {"receiving_staff": "  "}, {"receiving_staff": "Dr Reyes", "handoff_notes": "x" * 1001}):
            with self.assertRaises(ValidationError):
                hs.transition(row, "complete", NURSE_A, "hospital", data)
        ok = hs.transition(row, "complete", NURSE_A, "hospital", {"receiving_staff": " Dr  Reyes ", "handoff_notes": " stable on arrival "}, now=NOW)
        self.assertEqual(ok, {"status": "completed", "completed_by": NURSE_A, "completed_at": NOW.isoformat(),
                              "receiving_staff": "Dr Reyes", "handoff_notes": "stable on arrival"})

    def test_validate_signal(self):
        row = hs.validate_signal({"hospital_id": HOSPITAL_A, "signal_type": "incoming_patient", "patient_condition": "serious",
                                  "patient_name": "  Juan  ", "patient_age": "45", "patient_gender": "male",
                                  "eta_time": "14:45", "eta_date": "2026-10-05", "additional_notes": " BP 90/60 ",
                                  "medic_unit": " Medic 02 ", "transport_type": "ground_als", "triage_priority": "1",
                                  "chief_complaint": "Smoke inhalation", "heart_rate": "128", "bp_systolic": "90", "bp_diastolic": "60",
                                  "spo2": "88", "temperature": "37.25", "respiratory_rate": "28"}, now=NOW)
        self.assertEqual((row["patient_name"], row["patient_age"], row["additional_notes"]), ("Juan", 45, "BP 90/60"))
        self.assertEqual(row["eta"], "2026-10-05T06:45:00+00:00")            # 14:45 PHT stored in UTC
        self.assertEqual((row["medic_unit"], row["transport_type"], row["triage_priority"], row["chief_complaint"]),
                         ("Medic 02", "ground_als", 1, "Smoke inhalation"))
        self.assertEqual((row["heart_rate"], row["bp_systolic"], row["bp_diastolic"], row["spo2"], row["temperature"], row["respiratory_rate"]),
                         (128, 90, 60, 88, 37.2, 28))
        minimal = hs.validate_signal({"hospital_id": HOSPITAL_A, "signal_type": "bed_request", "patient_condition": "stable"}, now=NOW)
        self.assertEqual((minimal["patient_name"], minimal["patient_age"], minimal["patient_gender"], minimal["eta"], minimal["incident_id"],
                          minimal["medic_unit"], minimal["triage_priority"], minimal["heart_rate"]), (None,) * 8)

    def test_validate_signal_errors(self):
        with self.assertRaises(ValidationError) as cm:
            hs.validate_signal({"signal_type": "teleport", "patient_condition": "", "patient_age": "200", "patient_gender": "robot",
                                "additional_notes": "x" * 501, "eta_time": "09:00", "eta_date": "2026-10-05", "transport_type": "rocket",
                                "triage_priority": "7", "spo2": "140", "heart_rate": "fast", "bp_systolic": "80", "bp_diastolic": "120"}, now=NOW)
        self.assertEqual(set(cm.exception.errors), {"hospital_id", "signal_type", "patient_condition", "patient_age", "patient_gender",
                                                    "additional_notes", "eta_time", "transport_type", "triage_priority", "spo2",
                                                    "heart_rate", "bp_diastolic"})
        for age in ("-1", "12.5", "abc", "131"):
            with self.assertRaises(ValidationError):
                hs.validate_signal({"hospital_id": HOSPITAL_A, "signal_type": "other", "patient_condition": "other", "patient_age": age}, now=NOW)
        for age, expected in (("0", 0), (130, 130), ("", None)):
            ok = hs.validate_signal({"hospital_id": HOSPITAL_A, "signal_type": "other", "patient_condition": "other", "patient_age": age}, now=NOW)
            self.assertEqual(ok["patient_age"], expected)

    def test_eta(self):
        self.assertIsNone(hs.parse_eta("", "", NOW))
        self.assertEqual(hs.parse_eta("", "15:00", NOW), datetime(2026, 10, 5, 15, 0, tzinfo=hs.PHT))   # date defaults to today (PHT)
        self.assertEqual(hs.parse_eta("2026-10-05", "14:25", NOW).hour, 14)                              # a few minutes ago is tolerated
        for date_text, time_text, field in [("2026-10-05", "13:00", "eta_time"), ("2026-10-09", "10:00", "eta_date"), ("2026-10-05", "25:99", "eta_time")]:
            with self.assertRaises(ValidationError) as cm:
                hs.parse_eta(date_text, time_text, NOW)
            self.assertIn(field, cm.exception.errors)

    def test_validate_hospital(self):
        row = hs.validate_hospital({"name": "  City   Hospital ", "phone_number": "+63 (82) 224-1234", "latitude": "7.07", "longitude": "125.61"})
        self.assertEqual((row["name"], row["latitude"], row["availability"], row["is_active"]), ("City Hospital", 7.07, "ready", True))
        with self.assertRaises(ValidationError) as cm:
            hs.validate_hospital({"name": "", "phone_number": "call us", "latitude": "7.07", "availability": "plenty"})
        self.assertEqual(set(cm.exception.errors), {"name", "phone_number", "latitude", "availability"})

    def test_distance(self):
        self.assertAlmostEqual(hs.distance_km(7.0731, 125.6128, 7.0800, 125.6200), 1.1, places=1)
        self.assertIsNone(hs.distance_km(None, None, 7.08, 125.62))
        self.assertIsNone(hs.distance_km(0, 0, 7.08, 125.62))            # (0, 0) is a missing GPS fix, not a place

    def test_critical_and_sorting(self):
        self.assertTrue(hs.is_critical({"triage_priority": 1, "patient_condition": "stable"}))
        self.assertTrue(hs.is_critical({"patient_condition": "unconscious"}))
        self.assertTrue(hs.is_critical({"signal_type": "critical_patient", "patient_condition": "other"}))
        self.assertFalse(hs.is_critical({"triage_priority": 2, "patient_condition": "serious"}))
        rows = [{"id": "late", "patient_condition": "stable", "eta": "2026-10-05T07:30:00+00:00", "sent_at": "1"},
                {"id": "soon", "patient_condition": "stable", "eta": "2026-10-05T06:40:00+00:00", "sent_at": "2"},
                {"id": "no-eta", "patient_condition": "stable", "eta": None, "sent_at": "0"},
                {"id": "p1", "triage_priority": 1, "patient_condition": "serious", "eta": "2026-10-05T09:00:00+00:00", "sent_at": "3"}]
        self.assertEqual([r["id"] for r in hs.sort_incoming(rows)], ["p1", "soon", "late", "no-eta"])

    def test_bay_state_and_stats(self):
        self.assertEqual(hs.bay_state({"status": "ready"}), "ready")
        self.assertEqual(hs.bay_state({"status": "cleaning"}), "cleaning")
        self.assertEqual(hs.bay_state({"status": "ready"}, {"status": "accepted"}), "preparing")
        self.assertEqual(hs.bay_state({"status": "ready"}, {"status": "arrived"}), "occupied")
        self.assertEqual(hs.bay_state({"status": "ready"}, {"status": "completed"}), "ready")
        rows = [{"status": "sent", "triage_priority": 1}, {"status": "acknowledged", "patient_condition": "stable"},
                {"status": "accepted", "patient_condition": "critical"}, {"status": "arrived", "patient_condition": "stable"},
                {"status": "handoff", "patient_condition": "stable"}]
        self.assertEqual(hs.dashboard_stats(rows, 4), {"incoming": 2, "critical": 2, "preparing": 1, "arrived": 1,
                                                       "pending_handoffs": 2, "responding": 0, "on_scene": 0, "completed_today": 4})

    def test_codes(self):
        self.assertEqual(hs.signal_code({"signal_number": 7}), "HS-000007")


class HospitalApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.db = make_db()
        patcher = mock.patch("app.routes.hospital._db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Pages look up the signed-in user's display size (Settings -> Display); keep that offline too.
        display = mock.patch("app.routes.account._load_display_row", return_value=None)
        display.start()
        self.addCleanup(display.stop)
        self.dispatcher = self.client_for(DISPATCHER, "organization")
        self.nurse_a = self.client_for(NURSE_A, "responder")
        self.nurse_b = self.client_for(NURSE_B, "responder")

    def client_for(self, user_id, role):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["user"] = {"id": user_id, "email": f"{user_id[-2:]}@example.com", "role": role, "name": "Test"}
        return client

    def payload(self, **changes):
        eta = datetime.now(timezone.utc).astimezone(hs.PHT) + timedelta(minutes=20)
        body = {"hospital_id": HOSPITAL_A, "incident_id": INCIDENT, "signal_type": "incoming_patient", "patient_condition": "serious",
                "patient_name": "Juan Dela Cruz", "patient_age": "45", "patient_gender": "male",
                "eta_time": eta.strftime("%H:%M"), "eta_date": eta.strftime("%Y-%m-%d"), "additional_notes": "BP 90/60, conscious"}
        body.update(changes)
        return body

    def send(self, **changes):
        res = self.dispatcher.post("/api/hospital-signals/signals", json=self.payload(**changes))
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        return res.get_json()["item"]

    def status_seen_by_dispatcher(self, signal_id):
        return self.dispatcher.get(f"/api/hospital-signals/signals/{signal_id}").get_json()["item"]["status"]

    # ---------------------------------------------------------------- the whole workflow

    def test_full_workflow_dispatcher_to_hospital(self):
        # 1-3. The dispatcher opens the tab: hospitals come from the database, nearest to the incident first.
        hospitals = self.dispatcher.get(f"/api/hospital-signals/hospitals?incident_id={INCIDENT}").get_json()
        self.assertEqual([h["name"] for h in hospitals["items"]], ["Alpha General Hospital", "Bravo Medical Center", "Offline Clinic"])
        self.assertEqual((hospitals["items"][0]["distance_km"], hospitals["items"][0]["staff_count"], hospitals["items"][2]["distance_km"]), (1.1, 1, None))
        incidents = self.dispatcher.get("/api/hospital-signals/incidents?q=INC-000021").get_json()["items"]
        self.assertEqual([(i["code"], i["status_label"]) for i in incidents], [("INC-000021", "Units En Route")])

        # 4-6. Send: stored once, status Sent, sender and time recorded, visible in Recent Signals.
        res = self.dispatcher.post("/api/hospital-signals/signals", json=self.payload())
        self.assertEqual(res.status_code, 201)
        sent = res.get_json()
        item = sent["item"]
        self.assertEqual((item["code"], item["status"], item["status_label"], item["hospital_name"], item["incident"]["code"]),
                         ("HS-000001", "sent", "Sent", "Alpha General Hospital", "INC-000021"))
        self.assertEqual((item["sent_by_name"], sent["staff_notified"], item["actions"]), ("Dana Dispatcher", 1, ["cancel"]))
        stored = self.db.tables["hospital_signals"][0]
        self.assertEqual((stored["sent_by"], stored["hospital_id"], stored["incident_id"]), (DISPATCHER, HOSPITAL_A, INCIDENT))
        self.assertTrue(stored["sent_at"])
        recent = self.dispatcher.get("/api/hospital-signals/signals").get_json()
        self.assertEqual([(s["code"], s["status"]) for s in recent["items"]], [("HS-000001", "sent")])
        self.assertEqual(len(self.db.tables["emergency_incidents"]), 2)          # the incident was linked, not copied

        # 7. The hospital receives it, with everything it needs to prepare.
        self.assertEqual(self.nurse_a.get("/api/hospital/overview").get_json()["hospital"]["name"], "Alpha General Hospital")
        got = self.nurse_a.get("/api/hospital/reports?view=signals").get_json()["items"][0]
        self.assertEqual((got["code"], got["signal_type_label"], got["condition_label"], got["patient_name"], got["patient_age"],
                          got["additional_notes"], got["sent_by_name"], got["incident"]["code"]),
                         ("HS-000001", "Incoming Patient", "Serious", "Juan Dela Cruz", 45, "BP 90/60, conscious", "Dana Dispatcher", "INC-000021"))
        self.assertTrue(got["eta"] and got["sent_at"])
        self.assertEqual(got["actions"], ["acknowledge", "accept", "arrive", "unable"])
        self.assertNotIn("contact_number", got["incident"])                       # the reporting citizen's details stay with dispatch

        # 8-13. Acknowledge -> Accepted -> Received, each seen by the dispatcher with who and when.
        url = f"/api/hospital/reports/{item['id']}"
        steps = (("acknowledge", "acknowledged"), ("accept", "accepted"), ("arrive", "arrived"), ("handoff", "handoff"), ("complete", "completed"))
        for action, status in steps:
            res = self.nurse_a.post(f"{url}/{action}", json={"receiving_staff": "Dr Reyes"})
            self.assertEqual((res.status_code, res.get_json()["item"]["status"]), (200, status), action)
            self.assertEqual(self.status_seen_by_dispatcher(item["id"]), status)
        final = self.dispatcher.get(f"/api/hospital-signals/signals/{item['id']}").get_json()["item"]
        self.assertEqual((final["acknowledged_by_name"], final["accepted_by_name"], final["completed_by_name"]), ("Nurse Alpha",) * 3)
        self.assertTrue(final["acknowledged_at"] and final["accepted_at"] and final["arrived_at"] and final["completed_at"])
        self.assertEqual((final["status_label"], final["actions"], final["receiving_staff"]), ("Completed", [], "Dr Reyes"))
        self.assertEqual([e["event"] for e in final["timeline"] if e["event"] != "INCIDENT_REPORTED"],
                         ["REPORT_RECEIVED", "REPORT_ACKNOWLEDGED", "BAY_PREPARATION_STARTED", "PATIENT_ARRIVED", "HANDOFF_STARTED",
                          "HANDOFF_COMPLETED", "REPORT_ARCHIVED"])
        self.assertEqual(self.db.tables["emergency_incidents"][0]["status"], "en_route")   # incident status is untouched
        self.assertEqual(self.nurse_a.get("/api/hospital/reports?view=incoming").get_json()["items"], [])
        self.assertEqual(len(self.nurse_a.get("/api/hospital/reports?view=completed").get_json()["items"]), 1)

    def test_unable_to_receive_needs_a_reason(self):
        item = self.send()
        url = f"/api/hospital/reports/{item['id']}/unable"
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            res = self.nurse_a.post(url, json=body)
            self.assertEqual(res.status_code, 422, body)
            self.assertIn("reason", res.get_json()["fields"])
        self.assertEqual(self.status_seen_by_dispatcher(item["id"]), "sent")      # nothing changed without a reason
        res = self.nurse_a.post(url, json={"reason": "No available bed: ICU is full"})
        self.assertEqual(res.status_code, 200)
        seen = self.dispatcher.get(f"/api/hospital-signals/signals/{item['id']}").get_json()["item"]
        self.assertEqual((seen["status"], seen["status_label"], seen["rejection_reason"], seen["rejected_by_name"]),
                         ("unable", "Unable to Receive", "No available bed: ICU is full", "Nurse Alpha"))
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{item['id']}/accept", json={}).status_code, 409)   # final

    def test_dispatcher_cancel(self):
        item = self.send()
        self.nurse_a.post(f"/api/hospital/reports/{item['id']}/acknowledge", json={})
        res = self.dispatcher.post(f"/api/hospital-signals/signals/{item['id']}/cancel", json={})
        self.assertEqual((res.status_code, res.get_json()["item"]["status"]), (200, "cancelled"))
        self.assertEqual(self.db.tables["hospital_signals"][0]["cancelled_by"], DISPATCHER)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{item['id']}/arrive", json={}).status_code, 409)
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{item['id']}/cancel", json={}).status_code, 409)
        done = self.send()
        self.nurse_a.post(f"/api/hospital/reports/{done['id']}/arrive", json={})
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{done['id']}/cancel", json={}).status_code, 409)

    def test_two_people_acting_at_once(self):
        item = self.send()
        stale = dict(self.db.tables["hospital_signals"][0])
        self.nurse_a.post(f"/api/hospital/reports/{item['id']}/acknowledge", json={})
        with mock.patch("app.routes.hospital._load_signal", return_value=stale):   # this request read the row a moment earlier
            res = self.dispatcher.post(f"/api/hospital-signals/signals/{item['id']}/cancel", json={})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.status_seen_by_dispatcher(item["id"]), "acknowledged")

    # ---------------------------------------------------------------- sending: validation and duplicates

    def test_duplicate_submission_stores_one_signal(self):
        body = self.payload(client_request_id="3f0e9a52-6b1c-4d5e-8f7a-0123456789ab")
        first = self.dispatcher.post("/api/hospital-signals/signals", json=body)
        second = self.dispatcher.post("/api/hospital-signals/signals", json=body)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertTrue(second.get_json()["duplicate"])
        self.assertEqual(first.get_json()["item"]["id"], second.get_json()["item"]["id"])
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)

    def test_duplicate_caught_by_the_unique_index(self):
        body = self.payload(client_request_id="3f0e9a52-6b1c-4d5e-8f7a-0123456789ab")
        self.dispatcher.post("/api/hospital-signals/signals", json=body)
        real_table = self.db.table
        calls = {"n": 0}

        def racing(name):   # the "already sent?" check runs before the other request's insert landed
            query = real_table(name)
            if name == "hospital_signals":
                calls["n"] += 1
                if calls["n"] == 1:
                    query.rows = []
            return query
        with mock.patch.object(self.db, "table", side_effect=racing):
            res = self.dispatcher.post("/api/hospital-signals/signals", json=body)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)

    def test_send_validation(self):
        cases = [
            ({"hospital_id": ""}, 422, "hospital_id"), ({"patient_condition": ""}, 422, "patient_condition"),
            ({"signal_type": "nope"}, 422, "signal_type"), ({"patient_age": "999"}, 422, "patient_age"),
            ({"hospital_id": "not-a-uuid"}, 422, "hospital_id"), ({"incident_id": "not-a-uuid"}, 422, "incident_id"),
            ({"incident_id": "20000000-0000-0000-0000-00000000dead"}, 422, "incident_id"),
            ({"hospital_id": "10000000-0000-0000-0000-00000000dead"}, 409, "hospital_id"),
            ({"hospital_id": HOSPITAL_OFF}, 409, "hospital_id"),
        ]
        for changes, status, field in cases:
            res = self.dispatcher.post("/api/hospital-signals/signals", json=self.payload(**changes))
            self.assertEqual(res.status_code, status, changes)
            self.assertIn(field, res.get_json()["fields"], changes)
        self.assertIn("Offline", self.dispatcher.post("/api/hospital-signals/signals", json=self.payload(hospital_id=HOSPITAL_OFF)).get_json()["error"])
        self.assertEqual(self.db.tables["hospital_signals"], [])
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/signals", data="x=1").status_code, 415)

    def test_optional_fields_and_no_incident(self):
        item = self.send(patient_name="", patient_age="", patient_gender="", incident_id="", eta_time="", eta_date="", additional_notes="")
        self.assertEqual((item["patient_name"], item["patient_age"], item["gender_label"], item["incident"], item["eta"]), (None,) * 5)

    def test_inactive_hospital_cannot_be_signalled(self):
        self.db.tables["hospitals"][0]["is_active"] = False
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/signals", json=self.payload()).status_code, 409)
        names = [h["name"] for h in self.dispatcher.get("/api/hospital-signals/hospitals").get_json()["items"]]
        self.assertNotIn("Alpha General Hospital", names)
        self.assertEqual(len(self.dispatcher.get("/api/hospital-signals/hospitals?all=1").get_json()["items"]), 3)

    def test_database_failure_is_reported_without_internals(self):
        self.db.fail = RuntimeError('relation "secret_table" violates constraint xyz at 10.0.0.5')
        for res in (self.dispatcher.post("/api/hospital-signals/signals", json=self.payload()),
                    self.dispatcher.get("/api/hospital-signals/signals"), self.dispatcher.get("/api/hospital-signals/hospitals"),
                    self.nurse_a.get("/api/hospital/overview")):
            self.assertEqual(res.status_code, 502)
            self.assertNotIn("secret_table", res.get_data(as_text=True))
            self.assertNotIn("10.0.0.5", res.get_data(as_text=True))

    def test_missing_migration_is_explained(self):
        error = RuntimeError("Could not find the table 'public.hospitals' in the schema cache")
        error.code = "PGRST205"
        self.db.fail = error
        res = self.dispatcher.get("/api/hospital-signals/hospitals")
        self.assertEqual(res.status_code, 503)
        self.assertIn("007_hospital_signals.sql", res.get_json()["error"])
        self.assertIn("008_hospital_portal.sql", res.get_json()["error"])

    # ---------------------------------------------------------------- permissions

    def test_only_dispatch_roles_use_the_dispatcher_api(self):
        anonymous = self.app.test_client()
        gets = ["/api/hospital-signals/hospitals", "/api/hospital-signals/incidents", "/api/hospital-signals/signals"]
        for path in gets:
            self.assertEqual(anonymous.get(path).status_code, 401, path)
        self.assertEqual(anonymous.post("/api/hospital-signals/signals", json=self.payload()).status_code, 401)
        item = self.send()
        for role_client in (self.client_for(CITIZEN, "citizen"), self.nurse_a):   # hospital staff are not dispatchers
            for path in gets + [f"/api/hospital-signals/signals/{item['id']}"]:
                self.assertEqual(role_client.get(path).status_code, 403, path)
            self.assertEqual(role_client.post("/api/hospital-signals/signals", json=self.payload()).status_code, 403)
            self.assertEqual(role_client.post(f"/api/hospital-signals/signals/{item['id']}/cancel", json={}).status_code, 403)
            self.assertEqual(role_client.post("/api/hospital-signals/hospitals", json={"name": "X"}).status_code, 403)
        self.assertEqual(self.client_for("00000000-0000-0000-0000-0000000000ad", "admin").get("/api/hospital-signals/signals").get_json()["total"], 1)
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)

    def test_hospital_staff_see_and_act_on_their_own_hospital_only(self):
        to_a = self.send()
        to_b = self.send(hospital_id=HOSPITAL_B, patient_name="Maria Santos")
        for view in ("incoming", "signals", "completed", "history"):
            res = self.nurse_a.get(f"/api/hospital/reports?view={view}")
            self.assertNotIn("Maria Santos", res.get_data(as_text=True), view)
        self.assertEqual([s["code"] for s in self.nurse_a.get("/api/hospital/reports?view=signals").get_json()["items"]], [to_a["code"]])
        self.assertEqual(self.nurse_a.get(f"/api/hospital/reports/{to_b['id']}").status_code, 404)
        for action in ("acknowledge", "accept", "arrive", "handoff", "complete", "unable"):
            res = self.nurse_a.post(f"/api/hospital/reports/{to_b['id']}/{action}", json={"reason": "x", "receiving_staff": "x"})
            self.assertEqual(res.status_code, 404, action)                          # as if it did not exist
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{to_b['id']}/bay", json={"bay_id": HOSPITAL_A}).status_code, 404)
        self.assertEqual(self.status_seen_by_dispatcher(to_b["id"]), "sent")
        self.assertEqual(self.nurse_b.post(f"/api/hospital/reports/{to_b['id']}/acknowledge", json={}).status_code, 200)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{to_a['id']}/cancel", json={}).status_code, 404)   # not a hospital action

    def test_accounts_without_a_hospital_get_nothing(self):
        self.send()
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/hospital/reports").status_code, 401)
        for client in (self.client_for(CITIZEN, "citizen"), self.dispatcher):
            res = client.get("/api/hospital/reports")
            self.assertEqual(res.status_code, 403)
            self.assertNotIn("Juan", res.get_data(as_text=True))
        signal_id = self.db.tables["hospital_signals"][0]["id"]
        self.assertEqual(self.client_for(CITIZEN, "citizen").post(f"/api/hospital/reports/{signal_id}/acknowledge", json={}).status_code, 403)

    def test_removed_staff_lose_access_immediately(self):
        item = self.send()
        self.assertEqual(self.dispatcher.delete(f"/api/hospital-signals/hospitals/{HOSPITAL_A}/staff/{NURSE_A}").status_code, 200)
        self.assertEqual(self.nurse_a.get("/api/hospital/reports").status_code, 403)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{item['id']}/acknowledge", json={}).status_code, 403)

    # ---------------------------------------------------------------- search, filters, pagination

    def seed_many(self):
        base = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
        statuses = ["sent", "acknowledged", "accepted", "completed", "unable", "cancelled"]
        for n in range(1, 24):
            self.db.tables["hospital_signals"].append({
                **self.db.defaults["hospital_signals"], "id": f"30000000-0000-0000-0000-{n:012d}", "signal_number": n,
                "hospital_id": HOSPITAL_A if n % 2 else HOSPITAL_B, "incident_id": INCIDENT if n % 3 == 0 else None,
                "signal_type": "incoming_patient", "patient_condition": "stable", "patient_name": "Maria Santos" if n == 5 else None,
                "status": statuses[n % 6], "rejection_reason": "No available bed" if statuses[n % 6] == "unable" else None,
                "sent_by": DISPATCHER, "sent_at": (base + timedelta(hours=n * 5)).isoformat(),
            })

    def codes(self, **params):
        res = self.dispatcher.get("/api/hospital-signals/signals", query_string={"per_page": 50, **params})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return [s["code"] for s in res.get_json()["items"]]

    def test_pagination(self):
        self.seed_many()
        page1 = self.dispatcher.get("/api/hospital-signals/signals").get_json()
        self.assertEqual((len(page1["items"]), page1["total"], page1["pages"], page1["items"][0]["code"]), (10, 23, 3, "HS-000023"))
        seen = [s["id"] for n in (1, 2, 3) for s in self.dispatcher.get(f"/api/hospital-signals/signals?page={n}").get_json()["items"]]
        self.assertEqual((len(seen), len(set(seen))), (23, 23))
        past = self.dispatcher.get("/api/hospital-signals/signals?page=9").get_json()
        self.assertEqual((past["page"], len(past["items"])), (1, 10))
        self.assertEqual(self.dispatcher.get("/api/hospital-signals/signals?per_page=25").get_json()["pages"], 1)

    def test_filters(self):
        self.seed_many()
        self.assertEqual(len(self.codes(status="unable")), 4)
        self.assertEqual(len(self.codes(hospital_id=HOSPITAL_B)), 11)
        self.assertEqual(len(self.codes(status="sent", hospital_id=HOSPITAL_B)), 3)       # n = 6, 12, 18
        self.assertEqual(self.codes(status="nonsense"), self.codes())
        day = self.codes(**{"from": "2026-10-02", "to": "2026-10-02"})                      # PHT day
        self.assertEqual(day, ["HS-000007", "HS-000006", "HS-000005", "HS-000004", "HS-000003"])
        self.assertEqual(len(self.codes(**{"from": "2026-10-05"})), 7)                      # n = 17..23
        self.assertEqual(self.dispatcher.get("/api/hospital-signals/signals?from=2026-10-05&to=2026-10-01").status_code, 422)

    def test_search(self):
        self.seed_many()
        self.assertEqual(self.codes(q="HS-000007"), ["HS-000007"])
        self.assertEqual(self.codes(q="maria"), ["HS-000005"])
        self.assertEqual(len(self.codes(q="bravo")), 11)                                    # by hospital name
        self.assertEqual(len(self.codes(q="INC-000021")), 7)                                # by linked incident
        self.assertEqual(self.codes(q="zzzz"), [])
        self.assertEqual(self.codes(q="%"), self.codes())

    def test_recent_signals_archive_filters(self):
        self.seed_many()
        self.assertEqual(self.codes(filter="completed"), ["HS-000021", "HS-000015", "HS-000009", "HS-000003"])
        self.assertEqual(len(self.codes(filter="acknowledged")), 4)
        self.assertEqual(len(self.codes(filter="cancelled")), 4)
        self.assertEqual((len(self.codes(filter="incoming_patients")), self.codes(filter="hospital_alerts"), self.codes(filter="critical")),
                         (23, [], []))
        rows = {r["signal_number"]: r for r in self.db.tables["hospital_signals"]}
        rows[2]["signal_type"] = "bed_request"
        rows[4]["triage_priority"] = 1
        rows[6]["patient_condition"] = "unconscious"
        rows[8]["signal_type"] = "critical_patient"
        self.assertEqual(self.codes(filter="critical"), ["HS-000008", "HS-000006", "HS-000004"])
        self.assertEqual(self.codes(filter="hospital_alerts"), ["HS-000002"])
        self.assertEqual(len(self.codes(filter="incoming_patients")), 21)
        # A quick filter and a search narrow together.
        self.assertEqual(self.codes(filter="critical", q="bravo"), ["HS-000008", "HS-000006", "HS-000004"])
        self.assertEqual(self.codes(filter="critical", q="alpha"), [])
        self.assertEqual(self.codes(filter="nonsense"), self.codes())
        # Search by the signal id as written in the archive, and by signal type.
        self.assertEqual(self.codes(q="SIG-000007"), ["HS-000007"])
        self.assertEqual(self.codes(q="bed request"), ["HS-000002"])
        # Every signal of one incident, each its own record.
        self.assertEqual(len(self.codes(incident_id=INCIDENT)), 7)
        self.assertEqual(self.codes(incident_id="not-a-uuid"), self.codes())

    def test_archive_moves_a_finished_signal_to_signal_history(self):
        first, second = self.send(), self.send(signal_type="critical_patient")
        live = lambda: self.codes(archived="0")
        history = lambda: self.codes(archived="1")
        self.assertEqual((live(), history()), ([second["code"], first["code"]], []))
        self.assertIsNone(first["archived_at"])
        # An open signal stays in Hospital Signal: the hospital is still working on it.
        res = self.dispatcher.post(f"/api/hospital-signals/signals/{first['id']}/archive", json={})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.nurse_a.post(f"/api/hospital/reports/{first['id']}/acknowledge", json={}).status_code, 200)
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{second['id']}/cancel", json={}).status_code, 200)
        self.assertEqual(live(), [second["code"], first["code"]])                 # finishing alone does not archive
        # Archive: the same row is stamped and moves lists.
        res = self.dispatcher.post(f"/api/hospital-signals/signals/{second['id']}/archive", json={})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertTrue(res.get_json()["item"]["archived_at"])
        self.assertEqual((live(), history()), ([first["code"]], [second["code"]]))
        self.assertEqual(self.codes(), [second["code"], first["code"]])           # "View All" still shows both
        self.assertEqual(len(self.db.tables["hospital_signals"]), 2)             # nothing copied, nothing deleted
        stored = next(r for r in self.db.tables["hospital_signals"] if r["id"] == second["id"])
        self.assertEqual(stored["archived_by"], DISPATCHER)
        # Archiving twice changes nothing.
        again = self.dispatcher.post(f"/api/hospital-signals/signals/{second['id']}/archive", json={}).get_json()["item"]
        self.assertEqual(again["archived_at"], stored["archived_at"])
        detail = self.dispatcher.get(f"/api/hospital-signals/signals/{second['id']}").get_json()["item"]
        self.assertEqual((detail["hospital_id"], detail["incident"]["id"], detail["incident"]["code"]), (HOSPITAL_A, INCIDENT, "INC-000021"))
        self.assertIn("latitude", detail["incident"])
        self.assertEqual([e["event"] for e in detail["timeline"]].count("SIGNAL_ARCHIVED"), 1)
        # History filters and the incident link work on archived signals.
        self.assertEqual(self.codes(archived="1", filter="cancelled", incident_id=INCIDENT), [second["code"]])
        self.assertEqual(self.codes(archived="1", filter="completed"), [])
        # Restore puts it back.
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{second['id']}/restore", json={}).status_code, 200)
        self.assertEqual((live(), history()), ([second["code"], first["code"]], []))
        # Only dispatchers archive: hospital staff and signed-out visitors cannot.
        self.assertEqual(self.nurse_a.post(f"/api/hospital-signals/signals/{second['id']}/archive", json={}).status_code, 403)
        self.assertEqual(self.app.test_client().post(f"/api/hospital-signals/signals/{second['id']}/archive", json={}).status_code, 401)
        self.assertEqual(self.nurse_b.get("/api/hospital-signals/signals").status_code, 403)
        missing = "30000000-0000-0000-0000-00000000dead"
        self.assertEqual(self.dispatcher.post(f"/api/hospital-signals/signals/{missing}/archive", json={}).status_code, 404)

    def test_incident_picker(self):
        items = self.dispatcher.get("/api/hospital-signals/incidents").get_json()["items"]
        self.assertEqual([i["code"] for i in items], ["INC-000021", "INC-000022"])           # newest first, archived included
        self.assertEqual((items[1]["archived"], items[1]["has_location"]), (True, False))
        self.assertEqual([i["code"] for i in self.dispatcher.get("/api/hospital-signals/incidents?q=road").get_json()["items"]], ["INC-000022"])
        self.assertFalse({"citizen_name", "contact_number", "latitude"} & set(items[0]))

    # ---------------------------------------------------------------- hospitals and staff

    def test_hospital_management(self):
        res = self.dispatcher.post("/api/hospital-signals/hospitals", json={"name": "Charlie Hospital", "latitude": "7.1", "longitude": "125.6"})
        self.assertEqual(res.status_code, 201)
        new_id = res.get_json()["item"]["id"]
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/hospitals", json={"name": "charlie hospital"}).status_code, 422)
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/hospitals", json={"name": ""}).status_code, 422)
        res = self.dispatcher.put(f"/api/hospital-signals/hospitals/{new_id}", json={"name": "Charlie Hospital", "availability": "full"})
        self.assertEqual((res.status_code, res.get_json()["item"]["availability_label"]), (200, "Full"))
        self.assertEqual(self.dispatcher.delete(f"/api/hospital-signals/hospitals/{new_id}").status_code, 200)
        self.send()
        res = self.dispatcher.delete(f"/api/hospital-signals/hospitals/{HOSPITAL_A}")
        self.assertEqual(res.status_code, 409)                                              # has history: deactivate instead
        self.assertIn("inactive", res.get_json()["error"])

    def test_staff_management(self):
        url = f"/api/hospital-signals/hospitals/{HOSPITAL_OFF}/staff"
        self.assertEqual(self.dispatcher.get(url).get_json()["items"], [])
        self.assertEqual(self.dispatcher.post(url, json={"email": "nobody@example.com"}).status_code, 422)
        self.assertEqual(self.dispatcher.post(url, json={"email": "not-an-email"}).status_code, 422)
        self.assertEqual(self.dispatcher.post(url, json={"email": "Nurse.A@Example.com"}).status_code, 201)
        self.assertEqual(self.dispatcher.post(url, json={"email": "nurse.a@example.com"}).status_code, 201)   # adding twice is harmless
        self.assertEqual([m["email"] for m in self.dispatcher.get(url).get_json()["items"]], ["nurse.a@example.com"])
        self.assertEqual(self.nurse_a.get("/api/hospital/overview").get_json()["me"]["other_hospitals"], 1)

    def test_hospital_staff_set_their_own_availability(self):
        # The hospital is taken from the account, so a hospital id in the request is simply ignored.
        res = self.nurse_a.post("/api/hospital/availability", json={"availability": "full", "hospital_id": HOSPITAL_B})
        self.assertEqual((res.status_code, res.get_json()["item"]["availability"], res.get_json()["item"]["id"]), (200, "full", HOSPITAL_A))
        self.assertEqual(self.db.tables["hospitals"][0]["availability_updated_by"], NURSE_A)
        self.assertEqual(self.db.tables["hospitals"][1]["availability"], "limited")          # Bravo's own value, untouched
        self.assertEqual(self.nurse_a.post("/api/hospital/availability", json={"availability": "great"}).status_code, 422)
        seen = self.dispatcher.get("/api/hospital-signals/hospitals").get_json()["items"][0]
        self.assertEqual(seen["availability_label"], "Full")

    # ---------------------------------------------------------------- pages

    def test_pages(self):
        self.app.config["ANNOUNCEMENT_MANAGER_ROLES"] = set()
        html = self.dispatcher.get("/dashboard").get_data(as_text=True)
        for needle in ('data-tab="hospital"', "Hospital Signal", 'id="hospital-signal"', "/api/hospital-signals/signals",
                       'data-tab="dispatch"', 'data-tab="analytics"', 'data-tab="reports"', 'data-tab="announcements"', 'id="analytics"'):
            self.assertIn(needle, html)
        self.assertLess(html.index('data-tab="dispatch"'), html.index('data-tab="hospital"'))
        self.assertLess(html.index('data-tab="hospital"'), html.index('data-tab="analytics"'))
        self.assertTrue(self.app.test_client().get("/hospital").headers["Location"].endswith("/hospital/login"))
        page = self.nurse_a.get("/hospital")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="hospital-portal"', page.get_data(as_text=True))
        with mock.patch("app.routes.main.is_hospital_staff", return_value=True):
            self.assertTrue(self.nurse_a.get("/dashboard").headers["Location"].endswith("/hospital"))
        with mock.patch("app.routes.main.is_hospital_staff", return_value=False):
            self.assertEqual(self.client_for(CITIZEN, "responder").get("/dashboard").status_code, 200)


if __name__ == "__main__":
    unittest.main()
