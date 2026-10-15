"""Dispatcher duty status (Console Status) and incident assignment rules.

The two database functions from supabase/migrations/014_dispatcher_duty.sql are simulated with the same
rules (On Duty check, compare-and-swap on the current assignee) so the Flask enforcement is tested end to
end against the in-memory FakeDB. Run: set PYTHONPATH=tests;. and python -m unittest tests.test_duty
"""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from fake_db import FakeDB

from app import create_app
from app import duty

ME = "00000000-0000-0000-0000-000000000001"
PEER = "00000000-0000-0000-0000-000000000002"
OUTSIDER = "00000000-0000-0000-0000-000000000009"   # citizen account
INC = "00000000-0000-0000-0000-0000000000a1"
INC2 = "00000000-0000-0000-0000-0000000000a2"


def incident(iid, number, **over):
    return {"id": iid, "incident_number": number, "emergency_type": "Fire", "description": None, "latitude": None,
            "longitude": None, "location_accuracy_m": None, "location_verified": False, "status": "new", "priority": 2,
            "citizen_name": "C", "contact_number": "0917", "acknowledged_at": None, "acknowledged_by": None,
            "resolved_at": None, "archived_at": None, "archived_by": None, "updated_by": None, "assigned_to": None,
            "assigned_at": None, "created_at": f"2026-10-15T00:0{number}:00+00:00", "updated_at": None, **over}


class SimulatedDB(FakeDB):
    """FakeDB plus the migration's set_dispatcher_duty / assign_incident, serialized like row locks."""

    def __init__(self, **tables):
        super().__init__(**tables)
        self.lock = threading.Lock()
        self.tables.setdefault("dispatcher_duty", [])
        self.tables.setdefault("dispatcher_duty_events", [])
        self.tables.setdefault("incident_events", [])
        self.rpc = mock.MagicMock(side_effect=self._rpc)

    def _rpc(self, name, args):
        fn = {"set_dispatcher_duty": self._set_duty, "assign_incident": self._assign}[name]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=fn(**args)))

    def _set_duty(self, p_user, p_status, p_actor):
        with self.lock:
            if p_status not in duty.STATUSES:
                raise RuntimeError("ESRMS_INVALID_DUTY_STATUS")
            row = next((r for r in self.tables["dispatcher_duty"] if r["user_id"] == p_user), None)
            if row is None:
                row = {"user_id": p_user, "status": "off_duty", "changed_at": "t0"}
                self.tables["dispatcher_duty"].append(row)
            old = row["status"]
            active = sum(1 for i in self.tables["emergency_incidents"]
                         if i["assigned_to"] == p_user and not i["archived_at"] and i["status"] not in ("resolved", "cancelled"))
            if old != p_status:
                row.update(status=p_status, changed_at="t1")
                self.tables["dispatcher_duty_events"].append(
                    {"user_id": p_user, "from_status": old, "to_status": p_status, "actor_id": p_actor, "active_incidents": active})
            return [{"status": row["status"], "changed_at": row["changed_at"], "previous": old, "active_incidents": active}]

    def _assign(self, p_incident, p_assignee, p_actor, p_expected):
        with self.lock:
            if not any(r["user_id"] == p_assignee and r["status"] == "on_duty" for r in self.tables["dispatcher_duty"]):
                raise RuntimeError("ESRMS_NOT_ON_DUTY")
            row = next((i for i in self.tables["emergency_incidents"] if i["id"] == p_incident), None)
            if not row or row["archived_at"] or row["status"] in ("resolved", "cancelled") or row["assigned_to"] != p_expected:
                raise RuntimeError("ESRMS_ASSIGN_CONFLICT")
            old = row["assigned_to"]
            row.update(assigned_to=p_assignee, assigned_at="now", updated_by=p_actor)
            self.tables["incident_events"].append({"incident_id": p_incident, "event": "reassigned" if old else "assigned",
                                                   "from_assignee": old, "to_assignee": p_assignee, "actor_id": p_actor})
            return [dict(row)]

    def duty_of(self, uid):
        return next((r["status"] for r in self.tables["dispatcher_duty"] if r["user_id"] == uid), "off_duty")


class DutyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test", SUPABASE_URL="https://p.supabase.co", MEDIA_BUCKET="m")
        self.db = SimulatedDB(emergency_incidents=[incident(INC, 1), incident(INC2, 2)])
        directory = {ME: {"id": ME, "email": "me@x", "role": "organization", "banned": False},
                     PEER: {"id": PEER, "email": "peer@x", "role": "organization", "banned": False}}
        for p in (mock.patch("app.routes.dispatch.get_supabase_admin", return_value=self.db),
                  mock.patch("app.routes.dispatch._names", return_value={}),
                  mock.patch("app.routes.dispatch._dispatcher_directory", return_value=directory)):
            p.start()
            self.addCleanup(p.stop)
        self.client = self.app.test_client()
        self.login(ME)

    def login(self, uid, role="organization", client=None):
        with (client or self.client).session_transaction() as s:
            s["user"] = {"id": uid, "email": f"{uid}@x", "role": role, "name": "D"}

    def set_duty(self, status, confirm=False, client=None):
        return (client or self.client).post("/api/dispatch/duty", json={"status": status, "confirm": confirm})

    def listed(self):
        res = self.client.get("/api/dispatch/incidents")
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def ack(self, iid=INC, client=None):
        return (client or self.client).post(f"/api/dispatch/incidents/{iid}/status", json={"status": "acknowledged"})

    # 1. On Duty receives and can take new reports.
    def test_on_duty_receives_and_takes_new_reports(self):
        self.assertEqual(self.set_duty("on_duty").status_code, 200)
        data = self.listed()
        self.assertEqual(data["duty"], "on_duty")
        self.assertEqual({i["id"] for i in data["items"]}, {INC, INC2})
        res = self.ack()
        self.assertEqual(res.status_code, 200, res.get_json())
        row = self.db.tables["emergency_incidents"][0]
        self.assertEqual((row["assigned_to"], row["status"], row["acknowledged_by"]), (ME, "acknowledged", ME))

    # 2 + 3. On Break / Off Duty: nothing new listed, opened, or taken; direct API calls are refused too.
    def test_break_and_off_duty_receive_nothing_new(self):
        for status in ("on_break", "off_duty"):
            self.assertEqual(self.set_duty(status, confirm=True).status_code, 200)
            data = self.listed()
            self.assertEqual((data["duty"], data["items"], data["counts"]["new"]), (status, [], 0), status)
            self.assertEqual(self.client.get(f"/api/dispatch/incidents/{INC}").status_code, 403, status)
            self.assertEqual(self.ack().status_code, 409, status)
            res = self.client.post(f"/api/dispatch/incidents/{INC}/assign", json={"assignee_id": ME})
            self.assertEqual(res.status_code, 409, status)
            self.assertIsNone(self.db.tables["emergency_incidents"][0]["assigned_to"], status)

    def test_new_accounts_default_to_off_duty(self):
        self.assertEqual(self.client.get("/api/dispatch/duty").get_json()["status"], "off_duty")
        self.assertEqual(self.listed()["items"], [])
        self.assertEqual(self.ack().status_code, 409)

    # 4 + 5. Returning to On Duty restores eligibility.
    def test_returning_on_duty_restores_eligibility(self):
        for away in ("on_break", "off_duty"):
            self.set_duty(away, confirm=True)
            self.assertEqual(self.listed()["items"], [])
            self.set_duty("on_duty")
            self.assertEqual(len(self.listed()["items"]), 2, away)
        self.assertEqual(self.ack().status_code, 200)

    # 6. Status belongs to the signed-in account; another account cannot be changed.
    def test_cannot_change_another_dispatchers_status(self):
        self.set_duty("on_duty")
        res = self.client.post("/api/dispatch/duty", json={"status": "off_duty", "user_id": PEER, "p_user": PEER})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.db.duty_of(ME), "off_duty")        # only the caller's own row changed
        self.assertEqual(self.db.duty_of(PEER), "off_duty")      # PEER never had a row; still default
        self.assertNotIn(PEER, {e["user_id"] for e in self.db.tables["dispatcher_duty_events"]})

    def test_non_dispatchers_cannot_use_duty_endpoints(self):
        self.login(OUTSIDER, role="citizen")
        self.assertEqual(self.client.get("/api/dispatch/duty").status_code, 403)
        self.assertEqual(self.set_duty("on_duty").status_code, 403)
        self.client.post("/logout")
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.set_duty("on_duty").status_code, 401)
        self.assertEqual(self.db.tables["dispatcher_duty"], [])

    def test_invalid_status_rejected(self):
        for bad in ("busy", "", None, "ON_DUTY"):
            self.assertEqual(self.set_duty(bad).status_code, 422, bad)
        self.assertEqual(self.db.tables["dispatcher_duty"], [])

    # 7. A direct API request cannot assign to an unavailable dispatcher.
    def test_cannot_assign_to_unavailable_dispatcher(self):
        self.set_duty("on_duty")
        for peer_status in (None, "on_break", "off_duty"):
            if peer_status:
                self.db._set_duty(PEER, peer_status, PEER)
            res = self.client.post(f"/api/dispatch/incidents/{INC}/assign", json={"assignee_id": PEER})
            self.assertEqual((res.status_code, res.get_json().get("code")), (409, "not_on_duty"), peer_status)
        self.assertIsNone(self.db.tables["emergency_incidents"][0]["assigned_to"])
        self.assertEqual(self.client.post(f"/api/dispatch/incidents/{INC}/assign",
                                          json={"assignee_id": OUTSIDER}).status_code, 422)

    def test_stale_page_cannot_assign_after_peer_goes_on_break(self):
        self.set_duty("on_duty")
        self.db._set_duty(PEER, "on_duty", PEER)
        avail = self.client.get("/api/dispatch/dispatchers/available").get_json()["items"]
        self.assertEqual({d["id"] for d in avail}, {ME, PEER})
        self.db._set_duty(PEER, "on_break", PEER)               # after the page loaded its list
        res = self.client.post(f"/api/dispatch/incidents/{INC}/assign", json={"assignee_id": PEER})
        self.assertEqual(res.status_code, 409)
        self.assertIsNone(self.db.tables["emergency_incidents"][0]["assigned_to"])
        self.assertEqual({d["id"] for d in self.client.get("/api/dispatch/dispatchers/available").get_json()["items"]}, {ME})

    # 8. Concurrent claims: exactly one wins, no double assignment.
    def test_concurrent_claims_only_one_wins(self):
        self.set_duty("on_duty")
        peer = self.app.test_client()
        self.login(PEER, client=peer)
        self.set_duty("on_duty", client=peer)
        results, start = [], threading.Barrier(2)

        def claim(client):
            start.wait()
            results.append(self.ack(client=client).status_code)

        threads = [threading.Thread(target=claim, args=(c,)) for c in (self.client, peer)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [200, 409])
        assigned = [e for e in self.db.tables["incident_events"] if e["incident_id"] == INC]
        self.assertEqual(len(assigned), 1)
        self.assertIn(self.db.tables["emergency_incidents"][0]["assigned_to"], {ME, PEER})

    def test_reassign_with_stale_expected_assignee_is_refused(self):
        self.set_duty("on_duty")
        self.db._set_duty(PEER, "on_duty", PEER)
        self.ack()                                                # ME now owns INC
        res = self.client.post(f"/api/dispatch/incidents/{INC}/assign",
                               json={"assignee_id": PEER, "expected_assignee_id": None})   # page thought it was unassigned
        self.assertEqual((res.status_code, res.get_json()["code"]), (409, "conflict"))
        self.assertEqual(self.db.tables["emergency_incidents"][0]["assigned_to"], ME)

    # 9. Going on break with active incidents: warned, then nothing is deleted, resolved or moved.
    def test_active_incidents_warn_and_stay_intact(self):
        self.set_duty("on_duty")
        self.ack()
        before = dict(self.db.tables["emergency_incidents"][0])
        res = self.set_duty("on_break")
        self.assertEqual((res.status_code, res.get_json()["code"]), (409, "active_incidents"))
        self.assertEqual([i["code"] for i in res.get_json()["active"]], ["INC-000001"])
        self.assertEqual(self.db.duty_of(ME), "on_duty")          # nothing changed without confirmation
        self.assertEqual(self.set_duty("on_break", confirm=True).status_code, 200)
        self.assertEqual(self.db.tables["emergency_incidents"][0], before)
        self.assertEqual(self.db.tables["dispatcher_duty_events"][-1]["active_incidents"], 1)
        # Still visible and workable by its assignee, but nothing new.
        self.assertEqual([i["id"] for i in self.listed()["items"]], [INC])
        self.assertEqual(self.client.get(f"/api/dispatch/incidents/{INC}").status_code, 200)
        self.assertEqual(self.ack(INC2).status_code, 409)

    def test_handover_to_on_duty_peer_is_recorded(self):
        self.set_duty("on_duty")
        self.ack()
        self.set_duty("on_break", confirm=True)
        self.db._set_duty(PEER, "on_duty", PEER)
        res = self.client.post(f"/api/dispatch/incidents/{INC}/assign", json={"assignee_id": PEER, "expected_assignee_id": ME})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.db.tables["emergency_incidents"][0]["assigned_to"], PEER)
        events = [e for e in self.db.tables["incident_events"] if e["incident_id"] == INC]
        self.assertEqual([(e["event"], e["from_assignee"], e["to_assignee"], e["actor_id"]) for e in events],
                         [("assigned", None, ME, ME), ("reassigned", ME, PEER, ME)])

    def test_status_changes_are_audited(self):
        self.set_duty("on_duty")
        self.set_duty("on_break")
        self.set_duty("on_break")                                 # no-op: no duplicate audit row
        self.set_duty("off_duty")
        self.assertEqual([(e["from_status"], e["to_status"]) for e in self.db.tables["dispatcher_duty_events"]],
                         [("off_duty", "on_duty"), ("on_duty", "on_break"), ("on_break", "off_duty")])

    def test_missing_migration_is_reported_not_bypassed(self):
        def broken(*_):
            raise RuntimeError('relation "public.dispatcher_duty" does not exist (42P01)')
        with mock.patch("app.routes.dispatch.duty.status_of", side_effect=broken):
            res = self.client.get("/api/dispatch/incidents")
        self.assertEqual(res.status_code, 503)
        self.assertIn("014_dispatcher_duty.sql", res.get_json()["error"])


class DutyHelperTests(unittest.TestCase):
    def test_can_see(self):
        self.assertTrue(duty.can_see("on_duty", ME, {"assigned_to": None}))
        self.assertTrue(duty.can_see("on_break", ME, {"assigned_to": ME}))
        self.assertFalse(duty.can_see("on_break", ME, {"assigned_to": None}))
        self.assertFalse(duty.can_see("off_duty", ME, {"assigned_to": PEER}))

    def test_rpc_failure(self):
        self.assertEqual(duty.rpc_failure(RuntimeError("ESRMS_NOT_ON_DUTY")), "not_on_duty")
        self.assertEqual(duty.rpc_failure(RuntimeError("ESRMS_ASSIGN_CONFLICT")), "conflict")
        self.assertEqual(duty.rpc_failure(RuntimeError("Could not find the function public.assign_incident (PGRST202)")), "missing")
        self.assertIsNone(duty.rpc_failure(RuntimeError("timeout")))


if __name__ == "__main__":
    unittest.main()
