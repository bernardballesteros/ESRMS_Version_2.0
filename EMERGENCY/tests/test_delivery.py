"""Offline tests for email/push delivery, MFA recovery, sessions and agency access.

Run: .venv\\Scripts\\python -m unittest discover -s tests -v
Live checks against Supabase: scripts/account_e2e.py.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from supabase_auth.errors import AuthApiError

from app import account as acct
from app import create_app, delivery, recovery
from fake_db import FakeDB

NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat()
DISPATCHER, ADMIN, CITIZEN, OTHER = "u-dispatcher", "u-admin", "u-citizen", "u-other"
CFG = {"RESEND_API_KEY": "re_test", "EMAIL_FROM": "ESRMS <alerts@example.com>", "APP_BASE_URL": "https://esrms.example.com",
       "VAPID_PUBLIC_KEY": "pub", "VAPID_PRIVATE_KEY": "priv", "VAPID_SUBJECT": "mailto:ops@example.com",
       "ADMIN_EMAILS": set(), "DISPATCH_ROLES": {"admin", "organization"}}


def person(uid, role, verified=True, **extra):
    return {"id": uid, "email": f"{uid}@example.com", "verified": verified, "banned": False, "role": role,
            "branch": None, "barangay": None, **extra}


USERS = [person(DISPATCHER, "organization"), person(ADMIN, "admin"), person(CITIZEN, "citizen"), person(OTHER, "organization")]


def event(category="new_reports", **extra):
    return {"id": 1, "event_key": "incident:1:new", "category": category, "title": "New emergency report INC-000001",
            "body": "Fire", "code": "new", "url": "/dispatch", "subject_id": None, "target_user_id": None, "actor_id": None,
            "available_at": ISO, "created_at": ISO, "status": "pending", "claimed_at": None, **extra}


def prefs(uid, **over):
    return {"user_id": uid, **acct.DEFAULT_PREFERENCES, "email_enabled": True, **over}


class RecipientTests(unittest.TestCase):
    def test_incidents_go_to_dispatch_roles_only(self):
        on_duty = {DISPATCHER, ADMIN, OTHER, CITIZEN}
        got = {u["id"] for u in delivery.recipients_for(event(), USERS, CFG["DISPATCH_ROLES"], on_duty=on_duty)}
        self.assertEqual(got, {DISPATCHER, ADMIN, OTHER})

    def test_incidents_only_reach_on_duty_dispatchers(self):
        # ADMIN is on break / off duty (not in the set): no incident notification of any kind.
        for category in ("emergency_incidents", "new_reports", "dispatch_updates", "status_updates"):
            got = {u["id"] for u in delivery.recipients_for(event(category), USERS, CFG["DISPATCH_ROLES"],
                                                            on_duty={DISPATCHER, OTHER})}
            self.assertEqual(got, {DISPATCHER, OTHER}, category)
        # Nobody On Duty (or no duty data): nobody is notified, never everyone.
        self.assertEqual(delivery.recipients_for(event(), USERS, CFG["DISPATCH_ROLES"], on_duty=set()), [])
        self.assertEqual(delivery.recipients_for(event(), USERS, CFG["DISPATCH_ROLES"]), [])

    def test_returning_on_duty_restores_notifications(self):
        before = delivery.recipients_for(event(), USERS, CFG["DISPATCH_ROLES"], on_duty={DISPATCHER})
        after = delivery.recipients_for(event(), USERS, CFG["DISPATCH_ROLES"], on_duty={DISPATCHER, ADMIN})
        self.assertNotIn(ADMIN, {u["id"] for u in before})
        self.assertIn(ADMIN, {u["id"] for u in after})

    def test_the_actor_and_banned_users_are_skipped(self):
        users = [*USERS[:3], person(OTHER, "organization", banned=True)]
        got = {u["id"] for u in delivery.recipients_for(event(actor_id=DISPATCHER), users, CFG["DISPATCH_ROLES"],
                                                        on_duty={DISPATCHER, ADMIN, OTHER})}
        self.assertEqual(got, {ADMIN})

    def test_security_alerts_only_reach_their_own_account(self):
        e = event("security_alerts", target_user_id=CITIZEN, code="password_changed")
        self.assertEqual([u["id"] for u in delivery.recipients_for(e, USERS, CFG["DISPATCH_ROLES"])], [CITIZEN])
        self.assertEqual(delivery.recipients_for(event("security_alerts"), USERS, CFG["DISPATCH_ROLES"]), [])
        self.assertIn("Password changed", delivery.message_for(e)[1])

    def test_announcements_follow_the_audience_rules(self):
        live = {"status": "active", "published_at": (NOW - timedelta(minutes=1)).isoformat(), "expires_at": None,
                "target_audience": "emergency_personnel", "target_groups": [], "barangay": None}
        e = event("system_announcements")
        got = {u["id"] for u in delivery.recipients_for(e, USERS, CFG["DISPATCH_ROLES"], live, NOW)}
        self.assertEqual(got, {DISPATCHER, ADMIN, OTHER})
        archived = {**live, "status": "archived"}
        self.assertEqual(delivery.recipients_for(e, USERS, CFG["DISPATCH_ROLES"], archived, NOW), [])
        self.assertEqual(delivery.recipients_for(e, USERS, CFG["DISPATCH_ROLES"], None, NOW), [])


class PlanTests(unittest.TestCase):
    subs = {DISPATCHER: [{"id": "sub-1"}, {"id": "sub-2"}]}

    def plan(self, p, email_on=True, push_on=True, users=None):
        return delivery.plan_deliveries(event(), users or [USERS[0]], {DISPATCHER: p} if p else {}, self.subs, email_on, push_on)

    def test_one_row_per_channel_and_device(self):
        rows = self.plan(prefs(DISPATCHER))
        self.assertEqual([(r["channel"], r["target"]) for r in rows],
                         [("email", f"{DISPATCHER}@example.com"), ("push", "sub-1"), ("push", "sub-2")])

    def test_disabled_category_sends_nothing_on_any_channel(self):
        self.assertEqual(self.plan(prefs(DISPATCHER, new_reports=False)), [])

    def test_email_needs_opt_in_and_a_verified_address(self):
        self.assertEqual([r["channel"] for r in self.plan(None)], ["push", "push"])          # default: email off
        unverified = [person(DISPATCHER, "organization", verified=False)]
        self.assertEqual([r["channel"] for r in self.plan(prefs(DISPATCHER), users=unverified)], ["push", "push"])

    def test_unconfigured_channels_are_never_planned(self):
        self.assertEqual(self.plan(prefs(DISPATCHER), email_on=False, push_on=False), [])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB(
            notification_events=[event()],
            notification_preferences=[prefs(DISPATCHER), prefs(OTHER, new_reports=False)],
            push_subscriptions=[{"id": "sub-1", "user_id": DISPATCHER, "endpoint": "https://push.example/1", "p256dh": "k", "auth": "a"}],
            dispatcher_duty=[{"user_id": DISPATCHER, "status": "on_duty"}, {"user_id": OTHER, "status": "on_duty"}],
        )
        self.patches = [
            mock.patch("app.delivery.load_directory", return_value=[USERS[0], USERS[3]]),
            mock.patch("app.delivery.send_email", return_value="email-id-1"),
            mock.patch("app.delivery.send_push", return_value="201"),
        ]
        _, self.send_email, self.send_push = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def deliveries(self):
        return sorted(self.db.tables.get("notification_deliveries", []), key=lambda d: d["channel"])

    def test_off_duty_dispatcher_gets_no_incident_delivery(self):
        self.db.tables["dispatcher_duty"][0]["status"] = "on_break"
        stats = delivery.process_once(self.db, CFG, NOW)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(self.deliveries(), [])
        self.send_email.assert_not_called()
        self.send_push.assert_not_called()

    def test_event_is_delivered_once_per_channel(self):
        stats = delivery.process_once(self.db, CFG, NOW)
        self.assertEqual((stats["events"], stats["queued"], stats["sent"]), (1, 2, 2))
        self.assertEqual([(d["channel"], d["status"], d["user_id"]) for d in self.deliveries()],
                         [("email", "sent", DISPATCHER), ("push", "sent", DISPATCHER)])
        self.assertEqual(self.deliveries()[0]["provider_id"], "email-id-1")
        # Running again (or a second worker) sends nothing more.
        again = delivery.process_once(self.db, CFG, NOW + timedelta(seconds=5))
        self.assertEqual((again["events"], again["sent"]), (0, 0))
        self.assertEqual(self.send_email.call_count, 1)
        self.assertEqual(self.send_push.call_count, 1)

    def test_a_requeued_event_cannot_create_duplicate_deliveries(self):
        delivery.process_once(self.db, CFG, NOW)
        self.db.tables["notification_events"][0]["status"] = "pending"     # e.g. a worker crashed before marking it done
        delivery.process_once(self.db, CFG, NOW + timedelta(seconds=5))
        self.assertEqual(len(self.deliveries()), 2)
        self.assertEqual(self.send_email.call_count, 1)

    def test_failure_is_recorded_then_retried_with_the_same_idempotency_key(self):
        self.send_email.side_effect = delivery.DeliveryError("Email provider answered 503")
        stats = delivery.process_once(self.db, CFG, NOW)
        email = self.deliveries()[0]
        self.assertEqual((stats["failed"], email["status"], email["attempts"], email["sent_at"]), (1, "failed", 1, None))
        self.assertIn("503", email["last_error"])
        # Not due yet: nothing is resent.
        delivery.process_once(self.db, CFG, NOW + timedelta(seconds=10))
        self.assertEqual(self.send_email.call_count, 1)
        self.send_email.side_effect = None
        delivery.process_once(self.db, CFG, NOW + timedelta(seconds=61))
        self.assertEqual(self.deliveries()[0]["status"], "sent")
        keys = [c.kwargs["idempotency_key"] for c in self.send_email.call_args_list]
        self.assertEqual(len(set(keys)), 1)

    def test_permanent_failure_is_not_retried_or_reported_as_sent(self):
        self.send_email.side_effect = delivery.DeliveryError("Email provider answered 422", retryable=False)
        delivery.process_once(self.db, CFG, NOW)
        delivery.process_once(self.db, CFG, NOW + timedelta(hours=1))
        self.assertEqual(self.deliveries()[0]["status"], "dead")
        self.assertEqual(self.send_email.call_count, 1)

    def test_expired_push_subscription_is_removed(self):
        self.send_push.side_effect = delivery.SubscriptionGone("gone")
        delivery.process_once(self.db, CFG, NOW)
        self.assertEqual(self.db.tables["push_subscriptions"], [])
        self.assertEqual(self.deliveries()[1]["status"], "dead")

    def test_nothing_is_sent_without_provider_configuration(self):
        stats = delivery.process_once(self.db, {**CFG, "RESEND_API_KEY": "", "VAPID_PRIVATE_KEY": ""}, NOW)
        self.assertEqual((stats["events"], stats["queued"]), (1, 0))
        self.send_email.assert_not_called()
        self.send_push.assert_not_called()

    def test_scheduled_and_stale_events_wait_or_are_dropped(self):
        self.db.tables["notification_events"] = [
            event(id=2, event_key="a", available_at=(NOW + timedelta(hours=1)).isoformat()),
            event(id=3, event_key="b", available_at=(NOW - timedelta(hours=5)).isoformat()),
        ]
        stats = delivery.process_once(self.db, CFG, NOW)
        self.assertEqual((stats["events"], stats["queued"]), (1, 0))       # only the stale one was closed, unsent
        self.assertEqual(self.db.tables["notification_events"][0]["status"], "pending")


class EmailSenderTests(unittest.TestCase):
    def send(self, response):
        with mock.patch("app.delivery.httpx.post", return_value=response) as post:
            result = delivery.send_email(CFG, "d@example.com", "Title <b>", "Body & more", "new_reports", "/dispatch", NOW, "key-1")
        return result, post

    def test_accepted_only_with_a_provider_id(self):
        result, post = self.send(SimpleNamespace(status_code=200, json=lambda: {"id": "re_123"}, text=""))
        self.assertEqual(result, "re_123")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "key-1")
        self.assertEqual(kwargs["json"]["to"], ["d@example.com"])
        self.assertIn("Title &lt;b&gt;", kwargs["json"]["html"])            # user text is escaped
        self.assertIn("https://esrms.example.com/dispatch", kwargs["json"]["html"])
        self.assertIn("ESRMS", kwargs["json"]["subject"])
        with self.assertRaises(delivery.DeliveryError):
            self.send(SimpleNamespace(status_code=200, json=lambda: {}, text=""))

    def test_provider_errors(self):
        with self.assertRaises(delivery.DeliveryError) as cm:
            self.send(SimpleNamespace(status_code=422, json=lambda: {"message": "Invalid `to`"}, text=""))
        self.assertFalse(cm.exception.retryable)
        with self.assertRaises(delivery.DeliveryError) as cm:
            self.send(SimpleNamespace(status_code=503, json=lambda: {}, text="down"))
        self.assertTrue(cm.exception.retryable)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        factor = SimpleNamespace(id="f1", status="verified")
        self.db.auth.admin.mfa.list_factors.return_value = SimpleNamespace(factors=[factor])

    def test_only_hashes_are_stored(self):
        codes = recovery.replace_recovery_codes(self.db, "user-1")
        self.assertEqual(len(set(codes)), acct.RECOVERY_CODE_COUNT)
        stored = str(self.db.tables["mfa_recovery_codes"])
        self.assertFalse(any(c in stored or acct.normalize_code(c) in stored for c in codes))

    def test_code_is_single_use_and_bound_to_its_account(self):
        codes = recovery.replace_recovery_codes(self.db, "user-1")
        self.assertFalse(recovery.recover_with_code(self.db, "user-2", codes[0]))      # someone else's account
        self.assertFalse(recovery.recover_with_code(self.db, "user-1", "AAAA-AAAA-AAAA-AAAA"))
        self.db.auth.admin.mfa.delete_factor.assert_not_called()
        self.assertTrue(recovery.recover_with_code(self.db, "user-1", codes[0].lower().replace("-", " ")))
        self.db.auth.admin.mfa.delete_factor.assert_called_once_with({"user_id": "user-1", "id": "f1"})
        self.assertFalse(recovery.recover_with_code(self.db, "user-1", codes[0]))      # already spent
        self.assertFalse(recovery.recover_with_code(self.db, "user-1", codes[1]))      # the set died with the factors
        actions = [a["action"] for a in self.db.tables["mfa_recovery_audit"]]
        self.assertIn("code_used", actions)
        self.assertIn("code_rejected", actions)
        self.assertEqual([e["event"] for e in self.db.tables["account_security_events"]], ["recovery_code_used"])

    def test_new_codes_replace_old_ones(self):
        old = recovery.replace_recovery_codes(self.db, "user-1")
        recovery.replace_recovery_codes(self.db, "user-1")
        self.assertFalse(recovery.use_recovery_code(self.db, "user-1", old[0]))

    def approve(self, request_id, admin="admin-1", code="", note="Called the number on file and confirmed badge"):
        return recovery.approve_reset(self.db, request_id, admin, code, note)

    def test_admin_reset_needs_the_users_code_a_note_and_another_admin(self):
        code = recovery.create_reset_request(self.db, "user-1")
        request_id = self.db.tables["mfa_reset_requests"][0]["id"]
        self.assertNotIn(code, str(self.db.tables["mfa_reset_requests"]))
        for kwargs in ({"code": "WRNG-CODE"}, {"code": code, "note": "ok"}, {"code": code, "admin": "user-1"}):
            with self.assertRaises(recovery.ResetRefused):
                self.approve(request_id, **kwargs)
        self.db.auth.admin.mfa.delete_factor.assert_not_called()
        self.assertEqual(self.approve(request_id, code=code), "user-1")
        self.db.auth.admin.mfa.delete_factor.assert_called_once()
        row = self.db.tables["mfa_reset_requests"][0]
        self.assertEqual((row["status"], row["decided_by"]), ("approved", "admin-1"))
        audit = [(a["action"], a["actor_id"]) for a in self.db.tables["mfa_recovery_audit"]]
        self.assertIn(("reset_code_mismatch", "admin-1"), audit)
        self.assertIn(("reset_approved", "admin-1"), audit)
        with self.assertRaises(recovery.ResetRefused):                                # cannot be replayed
            self.approve(request_id, code=code)

    def test_admin_cannot_reset_without_a_request_from_the_user(self):
        with self.assertRaises(recovery.ResetRefused):
            self.approve("no-such-request", code="AAAA-AAAA")
        self.db.auth.admin.mfa.delete_factor.assert_not_called()

    def test_guessing_the_reference_code_closes_the_request(self):
        code = recovery.create_reset_request(self.db, "user-1")
        request_id = self.db.tables["mfa_reset_requests"][0]["id"]
        for _ in range(recovery.MAX_RESET_CODE_ATTEMPTS):
            with self.assertRaises(recovery.ResetRefused):
                self.approve(request_id, code="WRNG-CODE")
        with self.assertRaises(recovery.ResetRefused):
            self.approve(request_id, code=code)                                       # even the right code is too late
        self.assertEqual(self.db.tables["mfa_reset_requests"][0]["status"], "rejected")
        self.db.auth.admin.mfa.delete_factor.assert_not_called()


UID = "00000000-0000-0000-0000-000000000001"
ORG = "00000000-0000-0000-0000-0000000000a1"
ORG2 = "00000000-0000-0000-0000-0000000000a2"
AGENCY = {"agency_name": "Davao Emergency Communication", "agency_type": "psap-primary", "jurisdiction": "Davao City",
          "psap_phone": "+63 82 221 911", "agency_email": "ems@example.gov.ph", "agency_address": "Rizal St."}


def auth_user(uid=UID, role="organization", **over):
    base = dict(id=uid, email=f"{role}@example.com", new_email=None, user_metadata={"role": role, "agency_name": "Agency"},
                app_metadata={"providers": ["email"]}, factors=None, created_at=NOW, last_sign_in_at=NOW,
                email_confirmed_at=NOW, banned_until=None)
    return SimpleNamespace(**{**base, **over})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test", **{k: v for k, v in CFG.items() if k.isupper() and k != "ADMIN_EMAILS"})
        self.client = self.app.test_client()
        patcher = mock.patch.dict("app.routes.account._name_column", {"exists": False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = FakeDB(users=[
            {"id": ORG, "email": "org@example.com", "role": "organization", "agency_name": "Agency One", "ori_code": "ONE-001", "status": "pending"},
            {"id": ORG2, "email": "org2@example.com", "role": "organization", "agency_name": "Agency Two", "ori_code": "TWO-002", "status": "pending"},
            {"id": UID, "email": "resp@example.com", "role": "responder", "agency_id": None},
        ])
        accounts = {ORG: auth_user(ORG), ORG2: auth_user(ORG2), UID: auth_user(UID, "responder")}

        def get_user(uid):
            if uid not in accounts:
                raise AuthApiError("User not found", 404, "user_not_found")
            return SimpleNamespace(user=accounts[uid])
        self.db.auth.admin.get_user_by_id.side_effect = get_user
        self.db.auth.admin.list_users.return_value = list(accounts.values())
        for target in ("app.routes.account.get_supabase_admin", "app.supabase_client.get_supabase_admin"):
            patcher = mock.patch(target, return_value=self.db)
            patcher.start()
            self.addCleanup(patcher.stop)

    def login(self, uid=UID, role="responder", tokens=True):
        with self.client.session_transaction() as s:
            s["user"] = {"id": uid, "email": f"{role}@example.com", "role": role, "name": "T"}
            if tokens:
                s["sb"] = {"at": "access", "rt": "refresh", "exp": 9999999999, "sid": "11111111-1111-1111-1111-111111111111"}

    # ---- agency access

    def test_agency_admin_manages_only_their_own_agency(self):
        self.login(ORG, "organization")
        self.assertEqual(self.client.put("/api/account/agency", json={**AGENCY, "ori_code": "HACK-1", "status": "verified"}).status_code, 200)
        own = self.db.tables["users"][0]
        self.assertEqual((own["agency_name"], own["ori_code"], own["status"]), (AGENCY["agency_name"], "ONE-001", "pending"))
        self.assertEqual(self.client.get(f"/api/account/agency?agency={ORG2}").status_code, 403)
        self.assertEqual(self.client.put(f"/api/account/agency?agency={ORG2}", json=AGENCY).status_code, 403)
        self.assertEqual(self.db.tables["users"][1]["agency_name"], "Agency Two")
        self.assertEqual(self.client.get("/api/account/agencies").status_code, 403)

    def test_unassigned_users_have_no_agency_access(self):
        for role in ("responder", "citizen"):
            self.login(UID, role)
            self.assertEqual(self.client.get("/api/account/agency").status_code, 403, role)
            self.assertEqual(self.client.put("/api/account/agency", json=AGENCY).status_code, 403, role)

    def test_assigned_personnel_can_view_but_not_change(self):
        self.db.tables["users"][2]["agency_id"] = ORG
        self.login(UID, "responder")
        res = self.client.get("/api/account/agency")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.get_json()["agency"]["agency_name"], res.get_json()["can_edit"]), ("Agency One", False))
        self.assertEqual(self.client.put("/api/account/agency", json=AGENCY).status_code, 403)
        self.assertEqual(self.client.post("/api/account/agency/personnel", json={"email": "x@example.com"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/account/agency?agency={ORG2}").status_code, 403)
        self.assertEqual(self.db.tables["users"][0]["agency_name"], "Agency One")

    def test_system_admin_selects_real_agencies(self):
        self.login("00000000-0000-0000-0000-00000000ad01", "admin")
        self.assertEqual(self.client.get("/api/account/agency").status_code, 400)             # no personal agency needed
        names = [a["agency_name"] for a in self.client.get("/api/account/agencies").get_json()["items"]]
        self.assertEqual(names, ["Agency", "Agency"])                                          # the two organization accounts
        self.assertEqual(self.client.get(f"/api/account/agency?agency={UID}").status_code, 404)  # a responder is not an agency
        self.assertEqual(self.client.get("/api/account/agency?agency=not-a-uuid").status_code, 404)
        res = self.client.put(f"/api/account/agency?agency={ORG2}", json={**AGENCY, "ori_code": "new-222", "status": "verified"})
        self.assertEqual(res.status_code, 200)
        two = self.db.tables["users"][1]
        self.assertEqual((two["ori_code"], two["status"], two["agency_name"]), ("NEW-222", "verified", AGENCY["agency_name"]))
        self.assertEqual(self.db.tables["users"][0]["agency_name"], "Agency One")              # the other agency is untouched

    def test_agency_admin_assigns_responders_only(self):
        self.login(ORG, "organization")
        self.assertEqual(self.client.post("/api/account/agency/personnel", json={"email": "organization@example.com"}).status_code, 404)
        self.assertEqual(self.client.post("/api/account/agency/personnel", json={"email": "nobody@example.com"}).status_code, 404)
        res = self.client.post("/api/account/agency/personnel", json={"email": "responder@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.db.tables["users"][2]["agency_id"], ORG)
        self.login(ORG2, "organization")
        self.assertEqual(self.client.post("/api/account/agency/personnel", json={"email": "responder@example.com"}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/account/agency/personnel/{UID}", json={}).status_code, 404)
        self.assertEqual(self.db.tables["users"][2]["agency_id"], ORG)

    # ---- sessions

    def test_session_list_is_real_or_empty(self):
        self.login()
        self.db.rpc.return_value.execute.return_value.data = [
            {"id": "11111111-1111-1111-1111-111111111111", "user_agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/140", "created_at": ISO, "last_active_at": ISO, "aal": "aal1"},
            {"id": "22222222-2222-2222-2222-222222222222", "user_agent": "Mozilla/5.0 (Android 14) Chrome/140", "created_at": ISO, "last_active_at": ISO, "aal": "aal2"},
        ]
        sessions = self.client.get("/api/account/security").get_json()["sessions"]
        self.assertEqual([(s["current"], s["mfa_verified"]) for s in sessions["items"]], [(True, False), (False, True)])
        self.db.rpc.assert_called_with("account_sessions", {"p_user_id": UID})
        self.db.rpc.return_value.execute.side_effect = RuntimeError("function missing")
        sessions = self.client.get("/api/account/security").get_json()["sessions"]
        self.assertEqual((sessions["available"], sessions["items"]), (False, []))

    def test_revoking_one_session(self):
        self.login()
        self.assertEqual(self.client.post("/api/account/sessions/11111111-1111-1111-1111-111111111111/revoke", json={}).status_code, 409)
        self.db.rpc.return_value.execute.return_value.data = False
        self.assertEqual(self.client.post("/api/account/sessions/33333333-3333-3333-3333-333333333333/revoke", json={}).status_code, 404)
        self.assertEqual(self.db.tables.get("account_security_events", []), [])              # nothing claimed
        self.db.rpc.return_value.execute.return_value.data = True
        res = self.client.post("/api/account/sessions/22222222-2222-2222-2222-222222222222/revoke", json={})
        self.assertEqual(res.status_code, 200)
        self.db.rpc.assert_called_with("revoke_account_session", {"p_user_id": UID, "p_session_id": "22222222-2222-2222-2222-222222222222"})

    def test_revoked_session_loses_access(self):
        self.login()
        fake = FakeDB()
        fake.rpc.return_value.execute.return_value.data = False
        with mock.patch("app.supabase_client.get_supabase_admin", return_value=fake):
            self.assertEqual(self.client.get("/api/account/notifications").status_code, 401)

    # ---- sign-in providers and older sessions

    def test_google_only_account_gets_guidance_not_a_password_form(self):
        self.db.auth.admin.get_user_by_id.side_effect = None
        self.db.auth.admin.get_user_by_id.return_value.user = auth_user(UID, "citizen", app_metadata={"providers": ["google"]})
        self.login(UID, "citizen")
        data = self.client.get("/api/account/security").get_json()
        self.assertFalse(data["has_password"])
        self.assertEqual(data["providers"], [{"id": "google", "label": "Google"}])
        page = self.client.get("/account/security").get_data(as_text=True)
        self.assertIn("https://myaccount.google.com/security", page)

    def test_older_session_is_upgraded_without_a_logout(self):
        self.login(tokens=False)
        minted = SimpleNamespace(access_token="h.e30.s", refresh_token="r", expires_at=9999999999)
        anon = mock.MagicMock()
        anon.auth.verify_otp.return_value = SimpleNamespace(session=minted)
        with mock.patch("app.supabase_client.get_supabase_admin", return_value=self.db), \
                mock.patch("app.supabase_client.get_supabase", return_value=anon):
            data = self.client.get("/api/account/security").get_json()
        self.assertTrue(data["can_manage"])
        self.db.auth.admin.generate_link.assert_called_once_with({"type": "magiclink", "email": "responder@example.com"})
        with self.client.session_transaction() as s:
            self.assertEqual(s["sb"]["at"], "h.e30.s")
            self.assertEqual(s["user"]["id"], UID)

    def test_older_session_with_mfa_must_sign_in_again(self):
        self.login(tokens=False)
        self.db.auth.admin.get_user_by_id.side_effect = None
        self.db.auth.admin.get_user_by_id.return_value.user = auth_user(
            UID, "responder", factors=[SimpleNamespace(id="f", factor_type="totp", status="verified", friendly_name="A", created_at=NOW)])
        with mock.patch("app.supabase_client.get_supabase_admin", return_value=self.db):
            data = self.client.get("/api/account/security").get_json()
            self.assertEqual(self.client.post("/api/account/sessions/revoke", json={}).status_code, 409)
        self.assertFalse(data["can_manage"])
        self.db.auth.admin.generate_link.assert_not_called()

    # ---- push + email settings

    def test_push_subscription_is_validated_and_tied_to_the_user(self):
        self.login()
        good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
        for bad in ({}, {**good, "endpoint": "http://evil.example/x"}, {**good, "keys": {}}):
            self.assertEqual(self.client.post("/api/account/push/subscribe", json={"subscription": bad}).status_code, 422)
        self.assertEqual(self.client.post("/api/account/push/subscribe", json={"subscription": good, "user_id": ORG}).status_code, 200)
        self.assertEqual([(s["user_id"], s["endpoint"]) for s in self.db.tables["push_subscriptions"]], [(UID, good["endpoint"])])
        # Someone else cannot remove it; the owner can.
        self.login(ORG, "organization")
        self.client.post("/api/account/push/unsubscribe", json={"endpoint": good["endpoint"]})
        self.assertEqual(len(self.db.tables["push_subscriptions"]), 1)
        self.login()
        self.client.post("/api/account/push/unsubscribe", json={"endpoint": good["endpoint"]})
        self.assertEqual(self.db.tables["push_subscriptions"], [])

    def test_push_and_email_report_missing_configuration(self):
        self.app.config.update(VAPID_PRIVATE_KEY="", RESEND_API_KEY="")
        self.login()
        sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
        self.assertEqual(self.client.post("/api/account/push/subscribe", json={"subscription": sub}).status_code, 503)
        data = self.client.get("/api/account/notifications").get_json()
        self.assertFalse(data["delivery"]["email"]["available"])
        self.assertFalse(data["delivery"]["push"]["available"])
        self.assertEqual(self.client.put("/api/account/notifications", json={**acct.DEFAULT_PREFERENCES, "email_enabled": True}).status_code, 409)
        self.assertEqual(self.client.post("/api/account/notifications/test", json={"channel": "email"}).status_code, 409)

    def test_test_email_only_claims_what_the_provider_accepted(self):
        self.login()
        with mock.patch("app.delivery.send_email", side_effect=delivery.DeliveryError("Email provider answered 403: domain not verified")):
            res = self.client.post("/api/account/notifications/test", json={"channel": "email"})
        self.assertEqual(res.status_code, 502)
        self.assertIn("not accepted", res.get_json()["error"])
        with self.client.session_transaction() as s:
            s.pop("last_test", None)
        with mock.patch("app.delivery.send_email", return_value="re_1") as send:
            res = self.client.post("/api/account/notifications/test", json={"channel": "email"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(send.call_args.args[1], "responder@example.com")                    # the verified account address
        self.assertIn("No emergency", send.call_args.args[3])

    # ---- MFA recovery at sign-in

    def pending(self):
        with self.client.session_transaction() as s:
            s["mfa_pending"] = {"at": "a", "rt": "r", "role": "responder", "until": 9999999999}

    def test_recovery_needs_the_password_step_first(self):
        res = self.client.post("/login/mfa?mode=recovery", data={"recovery_code": "AAAA-AAAA-AAAA-AAAA"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.post("/login/mfa?mode=reset").status_code, 302)
        self.assertEqual(self.db.tables.get("mfa_reset_requests", []), [])

    def test_wrong_recovery_code_does_not_sign_in_or_remove_mfa(self):
        factor = SimpleNamespace(id="f1", factor_type="totp", status="verified")
        user = auth_user(UID, "responder", factors=[factor])
        codes = recovery.replace_recovery_codes(self.db, UID)
        sb = mock.MagicMock()
        sb.auth.set_session.return_value = SimpleNamespace(user=user, session=SimpleNamespace(access_token="a"))
        self.db.auth.admin.mfa.list_factors.return_value = SimpleNamespace(factors=[factor])
        with mock.patch("app.routes.auth.get_supabase", return_value=sb), \
                mock.patch("app.routes.auth.get_supabase_admin", return_value=self.db):
            self.pending()
            res = self.client.post("/login/mfa?mode=recovery", data={"recovery_code": "AAAA-BBBB-CCCC-DDDD"})
            self.assertEqual(res.status_code, 200)
            with self.client.session_transaction() as s:
                self.assertNotIn("user", s)
            self.db.auth.admin.mfa.delete_factor.assert_not_called()

            sb.auth.get_user.return_value.user = auth_user(UID, "responder", factors=None)
            sb.auth.get_session.return_value = SimpleNamespace(access_token="h.e30.s", refresh_token="r", expires_at=9999999999)
            res = self.client.post("/login/mfa?mode=recovery", data={"recovery_code": codes[3]})
            self.assertTrue(res.headers["Location"].endswith("/account/security"))
            self.db.auth.admin.mfa.delete_factor.assert_called_once_with({"user_id": UID, "id": "f1"})
            sb.auth.admin.sign_out.assert_called_once_with("a", "others")
            with self.client.session_transaction() as s:
                self.assertFalse(s["user"]["mfa_enabled"])

    def test_reset_request_shows_the_code_once_and_does_not_sign_in(self):
        factor = SimpleNamespace(id="f1", factor_type="totp", status="verified")
        sb = mock.MagicMock()
        sb.auth.set_session.return_value = SimpleNamespace(user=auth_user(UID, "responder", factors=[factor]), session=SimpleNamespace(access_token="a"))
        with mock.patch("app.routes.auth.get_supabase", return_value=sb), \
                mock.patch("app.routes.auth.get_supabase_admin", return_value=self.db):
            self.pending()
            page = self.client.post("/login/mfa?mode=reset").get_data(as_text=True)
        self.assertIn("Your reference code", page)
        with self.client.session_transaction() as s:
            self.assertNotIn("user", s)
        self.assertEqual(len(self.db.tables["mfa_reset_requests"]), 1)
        self.db.auth.admin.mfa.delete_factor.assert_not_called()

    def test_admin_reset_route_requires_admin_and_confirmation(self):
        code = recovery.create_reset_request(self.db, UID)
        request_id = "44444444-4444-4444-4444-444444444444"
        self.db.tables["mfa_reset_requests"][0]["id"] = request_id
        self.db.auth.admin.mfa.list_factors.return_value = SimpleNamespace(factors=[SimpleNamespace(id="f1", status="verified")])
        form = {"code": code, "identity_note": "Verified in person with agency ID", "confirm": "RESET"}
        with mock.patch("app.routes.admin.get_supabase_admin", return_value=self.db):
            self.login(ORG, "organization")
            self.client.post(f"/admin/mfa-resets/{request_id}/approve", data=form)
            self.login("00000000-0000-0000-0000-00000000ad01", "admin")
            self.client.post(f"/admin/mfa-resets/{request_id}/approve", data={**form, "confirm": "yes"})
            self.db.auth.admin.mfa.delete_factor.assert_not_called()
            self.client.post(f"/admin/mfa-resets/{request_id}/approve", data=form)
        self.db.auth.admin.mfa.delete_factor.assert_called_once_with({"user_id": UID, "id": "f1"})


if __name__ == "__main__":
    unittest.main()
