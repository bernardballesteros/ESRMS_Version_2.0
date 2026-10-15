"""Organization-based dispatch: recommendation rules, organization bases, response requests and the
receiving organization's workflow. In-memory database only (tests/fake_incidents.py)."""
import unittest
from unittest import mock

from app import create_app
from app import hospital as hs
from app import organizations as orgs
from tests.fake_incidents import FakeIncidentsDB

DISPATCHER = "00000000-0000-0000-0000-0000000000d1"
EMS_STAFF = "00000000-0000-0000-0000-0000000000e1"
FIRE_STAFF = "00000000-0000-0000-0000-0000000000f1"
ACCIDENT = "20000000-0000-0000-0000-000000000001"
NO_GPS = "20000000-0000-0000-0000-000000000002"
EMS_NEAR, EMS_FAR, EMS_BUSY, FIRE, POLICE, BARANGAY, HOSPITAL, POLICE_OFF, EMS_UNVERIFIED = (
    f"10000000-0000-0000-0000-00000000000{n}" for n in range(1, 10))


def org(org_id, name, kind, lat, lng, availability="ready", **extra):
    return {"id": org_id, "name": name, "org_type": kind, "base_name": f"{name} Base", "address": "1 Test St", "city": "Davao City",
            "province": "Davao del Sur", "phone_number": "+63 82 000 0001", "latitude": lat, "longitude": lng,
            "availability": availability, "availability_updated_at": None, "is_active": True, "verified": True, **extra}


def make_db():
    return FakeIncidentsDB(
        incidents=[
            {"id": ACCIDENT, "incident_number": 21, "emergency_type": "Accidents & Roads: Road accident", "status": "new", "priority": 2,
             "latitude": 7.0700, "longitude": 125.6100, "created_at": "2026-10-05T05:00:00+00:00", "archived_at": None},
            {"id": NO_GPS, "incident_number": 22, "emergency_type": "Fire & Hazmat: Building fire", "status": "new", "priority": 1,
             "latitude": None, "longitude": None, "created_at": "2026-10-05T05:10:00+00:00", "archived_at": None},
        ],
        hospitals=[
            org(EMS_NEAR, "City EMS", "ems", 7.0710, 125.6110),
            org(EMS_FAR, "North EMS", "ems", 7.1500, 125.6500),
            org(EMS_BUSY, "Closest EMS", "ems", 7.0701, 125.6101, "limited"),
            org(FIRE, "Central Fire Station", "fire", 7.0800, 125.6200),
            org(POLICE, "Police Station 1", "police", 7.0750, 125.6150),
            org(BARANGAY, "Barangay 5 Response", "barangay", 7.0900, 125.6300),
            org(HOSPITAL, "General Hospital", "hospital", 7.0705, 125.6105),
            org(POLICE_OFF, "Police Station 2", "police", 7.0702, 125.6102, "offline"),
            org(EMS_UNVERIFIED, "New EMS", "ems", 7.0703, 125.6103, verified=False),
        ],
        hospital_staff=[{"hospital_id": EMS_NEAR, "user_id": EMS_STAFF, "staff_role": "admin"},
                        {"hospital_id": FIRE, "user_id": FIRE_STAFF, "staff_role": "admin"}],
        hospital_signals=[],
        users=[{"id": DISPATCHER, "email": "dispatch@example.com", "full_name": "Dana Dispatcher", "agency_name": None},
               {"id": EMS_STAFF, "email": "ems@example.com", "full_name": "Ems Lead", "agency_name": None},
               {"id": FIRE_STAFF, "email": "fire@example.com", "full_name": "Fire Lead", "agency_name": None}],
    )


class RuleTests(unittest.TestCase):
    def test_rule_for_each_kind_of_emergency(self):
        kinds = lambda text: orgs.rule_for(text)[2:]  # noqa: E731
        self.assertEqual(kinds("Medical & EMS: Unconscious person"), (("ems",), ("barangay", "police")))
        self.assertEqual(kinds("Fire & Hazmat: Building fire"), (("fire",), ("police", "ems", "barangay")))
        self.assertEqual(kinds("Crime & Security: Robbery"), (("police",), ("barangay", "ems")))
        self.assertEqual(kinds("Accidents & Roads: Road accident"), (("ems", "police"), ("fire", "barangay")))
        self.assertEqual(kinds("Flooding")[0], ("barangay", "ems", "fire", "police", "hospital"))
        # The category decides before the detail does.
        self.assertEqual(kinds("Medical & EMS: Injured in a road accident")[0], ("ems",))
        self.assertEqual(kinds("Something unusual")[0], ("ems", "police", "fire", "barangay"))
        self.assertEqual(kinds(None)[0], ("ems", "police", "fire", "barangay"))

    def test_recommendation_uses_suitability_availability_then_base_distance(self):
        db = make_db()
        result = orgs.recommend(db.tables["emergency_incidents"][0], db.tables["hospitals"])
        names = [e["org"]["name"] for e in result["items"]]
        # Available first (recommended types, nearest base first; then support types), busy ones last.
        self.assertEqual(names, ["City EMS", "Police Station 1", "North EMS", "Central Fire Station", "Barangay 5 Response", "Closest EMS"])
        first = result["items"][0]
        self.assertEqual((first["recommended"], first["reason"], first["distance_km"]), (True, "Nearest suitable EMS organization", 0.2))
        self.assertEqual(first["distance_km"], hs.distance_km(7.07, 125.61, 7.071, 125.611))    # incident -> base
        self.assertEqual([e["recommended"] for e in result["items"]].count(True), 1)
        self.assertEqual(result["items"][-1]["reason"], "Busy: not recommended first")
        others = {e["org"]["name"]: e["note"] for e in result["others"]}
        self.assertEqual(others, {"General Hospital": "Hospital is not a usual responder for this type of emergency",
                                  "Police Station 2": "Offline", "New EMS": "Not verified yet"})
        self.assertEqual(result["state"], "ok")

    def test_states_when_nothing_is_registered_or_available(self):
        incident = make_db().tables["emergency_incidents"][0]
        self.assertEqual(orgs.recommend(incident, [])["state"], "none_registered")
        busy = [org(EMS_BUSY, "Busy EMS", "ems", 7.07, 125.61, "limited"), org(FIRE, "Full Fire", "fire", 7.07, 125.61, "full")]
        result = orgs.recommend(incident, busy)
        self.assertEqual((result["state"], [e["recommended"] for e in result["items"]], len(result["others"])), ("none_available", [False], 1))
        inactive = [org(EMS_NEAR, "Closed EMS", "ems", 7.07, 125.61, is_active=False)]
        self.assertEqual(orgs.recommend(incident, inactive)["state"], "none_registered")

    def test_incident_without_gps_gets_no_invented_distance(self):
        db = make_db()
        result = orgs.recommend(db.tables["emergency_incidents"][1], db.tables["hospitals"])
        self.assertTrue(all(e["distance_km"] is None for e in result["items"] + result["others"]))
        self.assertEqual(result["items"][0]["org"]["org_type"], "fire")
        self.assertIn("distance unknown", result["items"][0]["reason"])

    def test_response_workflow_rules(self):
        row = {"signal_type": "fire_response", "status": "sent"}
        self.assertEqual(hs.allowed_actions("sent", "hospital", row), ["acknowledge", "accept", "respond", "on_scene", "unable"])
        self.assertEqual(hs.allowed_actions("on_scene", "hospital", row), ["complete"])
        self.assertEqual(hs.allowed_actions("sent", "hospital", {"signal_type": "incoming_patient"}), ["acknowledge", "accept", "arrive", "unable"])
        with self.assertRaises(hs.TransitionError):
            hs.transition(row, "complete", EMS_STAFF, "hospital")
        with self.assertRaises(hs.TransitionError):
            hs.transition(row, "arrive", EMS_STAFF, "hospital")                 # a hospital-only step
        changes = hs.transition({**row, "status": "on_scene"}, "complete", EMS_STAFF, "hospital", {})
        self.assertEqual((changes["status"], changes["completed_by"]), ("completed", EMS_STAFF))
        self.assertNotIn("receiving_staff", changes)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.db = make_db()
        patcher = mock.patch("app.routes.hospital._db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dispatcher = self.client_for(DISPATCHER, "organization")
        self.ems = self.client_for(EMS_STAFF, "responder")
        self.fire = self.client_for(FIRE_STAFF, "responder")

    def client_for(self, user_id, role):
        client = self.app.test_client()
        with client.session_transaction() as s:
            s["user"] = {"id": user_id, "email": f"{user_id[-2:]}@example.com", "role": role, "name": "Test"}
        return client

    def send(self, org_id=EMS_NEAR, expect=201, **body):
        res = self.dispatcher.post(f"/api/organizations/{org_id}/signals", json={"incident_id": ACCIDENT, **body})
        self.assertEqual(res.status_code, expect, res.get_data(as_text=True))
        return res.get_json()

    def test_organization_bases_are_managed_in_the_database(self):
        body = {"name": "South Fire Station", "org_type": "fire", "base_name": "South Station", "address": "9 Rizal St", "city": "Davao City",
                "province": "Davao del Sur", "phone_number": "+63 82 555 0101", "latitude": "7.0500", "longitude": "125.6000", "availability": "ready"}
        res = self.dispatcher.post("/api/hospital-signals/hospitals", json=body)
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        item = res.get_json()["item"]
        self.assertEqual((item["org_type_label"], item["base_name"], item["status_label"], item["verified"], item["latitude"]),
                         ("Fire", "South Station", "Available", True, 7.05))
        # Edit: availability and enable/disable.
        res = self.dispatcher.put(f"/api/hospital-signals/hospitals/{item['id']}", json={**body, "availability": "limited", "is_active": False})
        self.assertEqual((res.status_code, res.get_json()["item"]["status_label"], res.get_json()["item"]["is_active"]), (200, "Busy", False))
        listed = self.dispatcher.get("/api/hospital-signals/hospitals").get_json()["items"]
        self.assertNotIn("South Fire Station", [o["name"] for o in listed])                 # disabled ones are hidden by default
        self.assertIn("South Fire Station", [o["name"] for o in self.dispatcher.get("/api/hospital-signals/hospitals?all=1").get_json()["items"]])
        only_hospitals = self.dispatcher.get("/api/hospital-signals/hospitals?type=hospital").get_json()["items"]
        self.assertEqual([o["name"] for o in only_hospitals], ["General Hospital"])
        # Validation and permissions.
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/hospitals", json={**body, "name": "X", "org_type": "army"}).status_code, 422)
        self.assertEqual(self.dispatcher.post("/api/hospital-signals/hospitals", json={**body, "name": "Y", "latitude": "97"}).status_code, 422)
        self.assertEqual(self.ems.post("/api/hospital-signals/hospitals", json={**body, "name": "Z"}).status_code, 403)
        # An organization with no signal history can be deleted.
        self.assertEqual(self.dispatcher.delete(f"/api/hospital-signals/hospitals/{item['id']}").status_code, 200)

    def test_nearby_endpoint(self):
        data = self.dispatcher.get(f"/api/organizations/nearby?incident_id={ACCIDENT}").get_json()
        self.assertEqual((data["state"], data["category"], data["primary"], data["support"]), ("ok", "Traffic accident", ["EMS", "Police"], ["Fire", "Barangay"]))
        first = data["items"][0]
        self.assertEqual((first["org"]["name"], first["recommended"], first["distance_km"], first["signal"]), ("City EMS", True, 0.2, None))
        self.assertEqual(self.dispatcher.get("/api/organizations/nearby").status_code, 422)
        self.assertEqual(self.dispatcher.get(f"/api/organizations/nearby?incident_id={HOSPITAL}").status_code, 404)
        self.assertEqual(self.ems.get(f"/api/organizations/nearby?incident_id={ACCIDENT}").status_code, 403)

    def test_send_signal_and_full_organization_workflow(self):
        sent = self.send(priority=1, message="Traffic accident reported. Medical assistance requested.",
                         client_request_id="11111111-1111-1111-1111-111111111111")
        item = sent["item"]
        self.assertEqual((item["signal_type_label"], item["status"], item["triage_priority"], item["kind"], item["hospital_name"], item["incident"]["code"]),
                         ("Medical Response Request", "sent", 1, "response", "City EMS", "INC-000021"))
        self.assertEqual(sent["staff_notified"], 1)
        stored = self.db.tables["hospital_signals"][0]
        self.assertEqual((stored["hospital_id"], stored["incident_id"], stored["sent_by"], stored["additional_notes"]),
                         (EMS_NEAR, ACCIDENT, DISPATCHER, "Traffic accident reported. Medical assistance requested."))
        # A repeated click does not create a second signal.
        again = self.send(priority=1, client_request_id="11111111-1111-1111-1111-111111111111", expect=200)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)
        # One incident, several organizations: each its own signal, the incident never copied.
        self.send(POLICE)
        self.send(FIRE)
        self.assertEqual(len(self.db.tables["hospital_signals"]), 3)
        self.assertEqual(len(self.db.tables["emergency_incidents"]), 2)
        self.assertEqual({r["incident_id"] for r in self.db.tables["hospital_signals"]}, {ACCIDENT})

        # The EMS base sees its own signal only, with the distance from the incident to its base.
        mine = self.ems.get("/api/hospital/reports?view=signals").get_json()["items"]
        self.assertEqual([(s["code"], s["kind"], s["distance_km"]) for s in mine], [(item["code"], "response", 0.2)])
        self.assertEqual(mine[0]["actions"], ["acknowledge", "accept", "respond", "on_scene", "unable"])
        fire_signals = self.fire.get("/api/hospital/reports?view=signals").get_json()["items"]
        self.assertEqual([s["hospital_name"] for s in fire_signals], ["Central Fire Station"])
        # Another organization's signal does not exist for this account, read or write.
        self.assertEqual(self.fire.get(f"/api/hospital/reports/{item['id']}").status_code, 404)
        self.assertEqual(self.fire.post(f"/api/hospital/reports/{item['id']}/acknowledge", json={}).status_code, 404)

        url = f"/api/hospital/reports/{item['id']}"
        self.assertEqual(self.ems.post(f"{url}/complete", json={}).status_code, 409)       # nothing to complete yet
        for action, status in (("acknowledge", "acknowledged"), ("accept", "accepted"), ("respond", "responding"), ("on_scene", "on_scene")):
            res = self.ems.post(f"{url}/{action}", json={})
            self.assertEqual((res.status_code, res.get_json()["item"]["status"]), (200, status), action)
            self.assertEqual(self.db.tables["hospital_signals"][0]["status"], status)      # stored, not just shown
        incoming = self.ems.get("/api/hospital/reports?view=incoming").get_json()["items"]
        self.assertEqual([s["status_label"] for s in incoming], ["On Scene"])
        # The dispatcher sees the organization's progress on the same incident.
        nearby = self.dispatcher.get(f"/api/organizations/nearby?incident_id={ACCIDENT}").get_json()
        self.assertEqual((nearby["items"][0]["signal"]["status_label"], nearby["items"][0]["signal"]["active"], nearby["signals_sent"]),
                         ("On Scene", True, 3))
        self.assertEqual(self.ems.post(f"{url}/arrive", json={}).status_code, 409)         # hospital-only step
        res = self.ems.post(f"{url}/complete", json={"handoff_notes": "Two patients treated on scene."})
        self.assertEqual((res.status_code, res.get_json()["item"]["status"], res.get_json()["item"]["read_only"]), (200, "completed", True))
        detail = self.dispatcher.get(f"/api/hospital-signals/signals/{item['id']}").get_json()["item"]
        events = [e["event"] for e in detail["timeline"]]
        self.assertEqual(events[-6:], ["REPORT_RECEIVED", "REPORT_ACKNOWLEDGED", "REQUEST_ACCEPTED", "ORG_RESPONDING", "ORG_ON_SCENE", "RESPONSE_COMPLETED"])
        self.assertEqual(self.ems.get("/api/hospital/overview").get_json()["stats"]["completed_today"], 1)
        self.assertEqual(self.ems.post(f"{url}/respond", json={}).status_code, 409)        # final: read-only

    def test_send_is_refused_where_it_must_be(self):
        self.assertEqual(self.send(POLICE_OFF, expect=409)["error"][:16], "Police Station 2")
        self.assertIn("not been verified", self.send(EMS_UNVERIFIED, expect=409)["error"])
        self.assertEqual(self.send(HOSPITAL, expect=409)["code"], "use_hospital_signal")
        self.assertEqual(self.dispatcher.post(f"/api/organizations/{EMS_NEAR}/signals", json={}).status_code, 422)
        self.assertEqual(self.dispatcher.post(f"/api/organizations/{EMS_NEAR}/signals", json={"incident_id": HOSPITAL}).status_code, 422)
        self.assertEqual(self.send(priority=9, expect=422)["fields"]["priority"], "Choose a priority.")
        self.assertEqual(self.ems.post(f"/api/organizations/{EMS_NEAR}/signals", json={"incident_id": ACCIDENT}).status_code, 403)
        self.assertEqual(self.app.test_client().post(f"/api/organizations/{EMS_NEAR}/signals", json={"incident_id": ACCIDENT}).status_code, 401)
        # A busy organization can still be chosen manually; the incident's own priority is the default.
        busy = self.send(EMS_BUSY)["item"]
        self.assertEqual((busy["hospital_name"], busy["triage_priority"]), ("Closest EMS", 2))
        self.assertEqual(len(self.db.tables["hospital_signals"]), 1)

    def test_organization_changes_its_own_status(self):
        res = self.ems.post("/api/hospital/availability", json={"availability": "limited"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        data = self.dispatcher.get(f"/api/organizations/nearby?incident_id={ACCIDENT}").get_json()
        self.assertEqual((data["items"][0]["org"]["name"], data["items"][0]["recommended"]), ("Police Station 1", True))
        busy = next(e for e in data["items"] if e["org"]["name"] == "City EMS")
        self.assertEqual((busy["recommended"], busy["org"]["status_label"]), (False, "Busy"))


if __name__ == "__main__":
    unittest.main()
