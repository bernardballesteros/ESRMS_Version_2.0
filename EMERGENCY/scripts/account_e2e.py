"""Live end-to-end check of the account settings features against your Supabase project.

    .venv\\Scripts\\python scripts\\account_e2e.py

Creates temporary accounts (e2e-account-...@example.com), drives the real Flask routes with
them and deletes them at the end (their rows, preferences, codes and events go with them).
No email or push is sent to anyone: the notification checks use the temporary account only
and stub the providers.

Needs supabase/migrations/005 for profile/agency/preferences and 006 for recovery codes,
administrator MFA reset, the session list, agency personnel and the notification outbox.
Parts whose migration is missing are reported as SKIP, never as PASS.
The MFA parts wait for fresh authenticator codes, so a full run takes a few minutes.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import sys
import time
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account as acct  # noqa: E402
from app import create_app, delivery  # noqa: E402
from app.supabase_client import get_supabase, get_supabase_admin  # noqa: E402

app = create_app()
app.config.update(TESTING=True)
results = []

TAG = secrets.token_hex(4)
EMAIL = f"e2e-account-{TAG}@example.com"
RESPONDER_EMAIL = f"e2e-responder-{TAG}@example.com"
OLD_PASSWORD = "E2e!" + secrets.token_urlsafe(18) + "aA1"
NEW_PASSWORD = "E2e#" + secrets.token_urlsafe(18) + "bB2"
THIRD_PASSWORD = "E2e%" + secrets.token_urlsafe(18) + "cC3"
BROWSER = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 E2E/{TAG}"
FAKE_ADMIN = {"id": "00000000-0000-0000-0000-0000000e2ead", "email": "e2e-admin@example.com", "role": "admin", "name": "E2E Admin"}


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def skip(name, why):
    print(f"[SKIP] {name} -- {why}")


def totp(secret, at=None):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", int((at or time.time()) // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


USED = {}


def fresh_code(secret):
    """A code Supabase has not seen yet: wait for the next 30-second window if needed."""
    used = USED.setdefault(secret, set())
    while totp(secret) in used:
        time.sleep(1)
    time.sleep(1)
    code = totp(secret)
    used.add(code)
    return code


def wrong_code(secret):
    return f"{(int(totp(secret)) + 1) % 1000000:06d}"


def browser():
    c = app.test_client()
    c.environ_base["HTTP_USER_AGENT"] = BROWSER
    return c


def sign_in(client, password, email=None):
    return client.post("/login?role=organization", data={"email": email or EMAIL, "password": password})


def exists(db, table, columns):
    try:
        db.table(table).select(columns).limit(1).execute()
        return True
    except Exception:
        return False


def enroll(client, name="Authenticator"):
    """Enroll and verify an authenticator; returns its secret."""
    res = client.post("/api/account/mfa/enroll", json={"name": name})
    if res.status_code != 200:
        return None, res
    data = res.get_json()
    res = client.post("/api/account/mfa/verify", json={"factor_id": data["factor_id"], "code": fresh_code(data["secret"])})
    return (data["secret"] if res.status_code == 200 else None), res


def mfa_state(client):
    return client.get("/api/account/security").get_json()["mfa"]


def main():
    with app.app_context():
        db = get_supabase_admin()
        user = db.auth.admin.create_user({
            "email": EMAIL, "password": OLD_PASSWORD, "email_confirm": True,
            "user_metadata": {"role": "organization", "full_name": "[E2E] Account Check", "agency_name": "[E2E] Agency",
                              "agency_type": "psap-primary", "ori_code": "E2E-0001", "jurisdiction": "E2E", "psap_phone": "+63 82 000 0000"},
        }).user
        uid = user.id
        responder = db.auth.admin.create_user({
            "email": RESPONDER_EMAIL, "password": OLD_PASSWORD, "email_confirm": True,
            "user_metadata": {"role": "responder", "full_name": "[E2E] Responder"},
        }).user
        try:
            m005 = exists(db, "notification_preferences", "user_id") and exists(db, "users", "phone, agency_address")
            m006 = (exists(db, "mfa_recovery_codes", "id") and exists(db, "notification_events", "id")
                    and exists(db, "users", "agency_id") and exists(db, "notification_preferences", "email_enabled"))
            need006 = "run supabase/migrations/006_delivery_recovery_sessions.sql first"

            c = browser()
            res = sign_in(c, OLD_PASSWORD)
            check("Sign-in without MFA goes straight to the dashboard", res.status_code == 302 and res.headers["Location"].endswith("/dashboard"))
            for section in ("profile", "agency", "security", "notifications"):
                check(f"/account/{section} opens", c.get(f"/account/{section}").status_code == 200)

            # ---------------------------------------------------- sessions created before tokens were kept
            legacy = browser()
            with legacy.session_transaction() as s:
                s["user"] = {"id": uid, "email": EMAIL, "role": "organization", "name": "E2E"}
            data = legacy.get("/api/account/security").get_json()
            check("An older session can manage security without signing out", data["can_manage"] is True)
            check("...and it can then act as the user (sign out other sessions)", legacy.post("/api/account/sessions/revoke", json={}).status_code == 200)
            res = sign_in(c, OLD_PASSWORD)   # the line above ended c's session; sign in again
            check("Signing in again works after being signed out remotely", res.status_code == 302)

            # ---------------------------------------------------- profile, agency, notifications
            if not m005:
                for name in ("Profile persistence", "Agency settings persistence", "Notification preference persistence"):
                    skip(name, "run supabase/migrations/005_account_settings.sql first")
            else:
                res = c.get("/api/account/profile")
                check("Profile loads from the database", res.status_code == 200 and res.get_json()["profile"]["full_name"] == "[E2E] Account Check", res.get_data(as_text=True)[:200])
                res = c.put("/api/account/profile", json={"full_name": "[E2E] Renamed", "email": EMAIL, "phone": "+63 912 000 0000", "role": "admin"})
                check("Profile saves", res.status_code == 200, res.get_data(as_text=True)[:200])
                check("Profile change is there after reloading", c.get("/api/account/profile").get_json()["profile"]["full_name"] == "[E2E] Renamed")
                row = db.table("users").select("full_name, phone, role").eq("id", uid).execute().data[0]
                check("Profile is stored in public.users", row["full_name"] == "[E2E] Renamed" and row["phone"] == "+63 912 000 0000")
                check("Role cannot be changed through the profile", row["role"] == "organization")
                meta = db.auth.admin.get_user_by_id(uid).user.user_metadata
                check("Sign-in profile (Auth metadata) kept in step", meta.get("full_name") == "[E2E] Renamed" and meta.get("role") == "organization")

                agency = {"agency_name": "[E2E] Agency Renamed", "agency_type": "psap-secondary", "jurisdiction": "E2E District",
                          "psap_phone": "+63 82 111 1111", "agency_email": "e2e-agency@example.com", "agency_address": "1 E2E St.", "agency_description": ""}
                res = c.put("/api/account/agency", json={**agency, "ori_code": "CHANGED-1", "status": "verified"})
                check("Agency settings save for the agency administrator", res.status_code == 200, res.get_data(as_text=True)[:200])
                got = c.get("/api/account/agency").get_json()["agency"]
                check("Agency settings persisted and reloaded", got["agency_name"] == agency["agency_name"] and got["agency_address"] == "1 E2E St.")
                check("Agency administrator cannot change the code or verification status", got["ori_code"] == "E2E-0001" and got["status"] != "verified")

                outsider = browser()
                sign_in(outsider, OLD_PASSWORD, RESPONDER_EMAIL)
                check("An unassigned responder cannot view or change the agency",
                      outsider.get("/api/account/agency").status_code == 403 and outsider.put("/api/account/agency", json=agency).status_code == 403)
                check("...including by naming the agency explicitly",
                      outsider.put(f"/api/account/agency?agency={uid}", json=agency).status_code == 403)

                first = c.get("/api/account/notifications").get_json()
                check("Preferences start at the defaults, unsaved", first["saved"] is False and first["preferences"] == acct.DEFAULT_PREFERENCES, str(first.get("preferences")))
                if m006:
                    wanted = {**acct.DEFAULT_PREFERENCES, "new_reports": False, "status_updates": False}
                    check("Preferences save", c.put("/api/account/notifications", json=wanted).status_code == 200)
                    again = browser()
                    sign_in(again, OLD_PASSWORD)
                    check("Preferences persist across a new sign-in", again.get("/api/account/notifications").get_json()["preferences"] == wanted)
                    again.get("/logout")
                else:
                    skip("Preference save and persistence", need006)

            # ---------------------------------------------------- agency personnel + system administrator (006)
            if m005 and m006:
                res = c.post("/api/account/agency/personnel", json={"email": RESPONDER_EMAIL})
                check("Agency administrator assigns a responder", res.status_code == 200, res.get_data(as_text=True)[:200])
                view = outsider.get("/api/account/agency")
                check("Assigned responder can view the agency", view.status_code == 200 and view.get_json()["can_edit"] is False)
                check("...but still cannot change it", outsider.put("/api/account/agency", json=agency).status_code == 403)
                check("Agency administrator cannot add a non-responder", c.post("/api/account/agency/personnel", json={"email": EMAIL}).status_code == 404)

                admin = browser()
                with admin.session_transaction() as s:
                    s["user"] = dict(FAKE_ADMIN)
                check("System administrator needs no agency of their own", admin.get("/api/account/agency").status_code == 400)
                listed = admin.get("/api/account/agencies").get_json()["items"]
                check("System administrator's selector lists the real agencies", any(a["id"] == uid for a in listed))
                res = admin.put(f"/api/account/agency?agency={uid}", json={**agency, "ori_code": "E2E-0002", "status": "verified"})
                got = c.get("/api/account/agency").get_json()["agency"]
                check("System administrator can set an agency's code and status", res.status_code == 200 and got["ori_code"] == "E2E-0002" and got["status"] == "verified")
                check("A responder account is not an agency", admin.get(f"/api/account/agency?agency={responder.id}").status_code == 404)

                # The public-key update path must not let a user change their own protected columns.
                anon = get_supabase()
                anon.auth.sign_in_with_password({"email": RESPONDER_EMAIL, "password": OLD_PASSWORD})
                try:
                    anon.table("users").update({"role": "organization", "status": "verified", "agency_id": None, "full_name": "[E2E] Self edit"}).eq("id", responder.id).execute()
                except Exception:
                    pass
                row = db.table("users").select("role, status, agency_id, full_name").eq("id", responder.id).execute().data[0]
                check("RLS: a user cannot change their own role, status or agency with the public key",
                      row["role"] == "responder" and row["status"] != "verified" and row["agency_id"] == uid, str(row))
                try:
                    stolen = anon.table("users").update({"agency_name": "[E2E] hijacked"}).eq("id", uid).execute().data
                except Exception:
                    stolen = []
                check("RLS: a user cannot update another agency's row", not stolen and c.get("/api/account/agency").get_json()["agency"]["agency_name"] == agency["agency_name"])
                check("Responder removed from the agency loses access",
                      c.delete(f"/api/account/agency/personnel/{responder.id}").status_code == 200 and outsider.get("/api/account/agency").status_code == 403)
            else:
                skip("Agency personnel, system administrator access and column protection", need006)

            # ---------------------------------------------------- password
            res = c.post("/api/account/password", json={"current_password": "not-the-password", "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
            check("Wrong current password is rejected", res.status_code == 422 and "current_password" in res.get_json()["fields"], res.get_data(as_text=True)[:200])
            res = c.post("/api/account/password", json={"current_password": OLD_PASSWORD, "new_password": "weak", "confirm_password": "weak"})
            check("Weak new password is rejected", res.status_code == 422)
            res = c.post("/api/account/password", json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
            check("Password changes with the correct current password", res.status_code == 200, res.get_data(as_text=True)[:300])
            try:
                get_supabase().auth.sign_in_with_password({"email": EMAIL, "password": OLD_PASSWORD})
                check("Old password no longer works", False)
            except Exception:
                check("Old password no longer works", True)
            check("This browser stays signed in after the change", c.get("/api/account/security").status_code == 200)
            providers = c.get("/api/account/security").get_json()
            check("Sign-in provider is reported from Supabase", providers["has_password"] is True and providers["providers"][0]["id"] == "email")

            # ---------------------------------------------------- MFA: enroll, sign-in challenge, backup authenticator
            res = c.post("/api/account/mfa/enroll", json={})
            if res.status_code != 200:
                check("MFA enrollment starts (TOTP must be enabled in Supabase Auth)", False, res.get_data(as_text=True)[:300])
                raise SystemExit
            first_factor = res.get_json()
            secret = first_factor["secret"]
            check("MFA enrollment returns a QR code and setup key", first_factor["qr_code"].startswith("data:image/svg+xml") and len(secret) >= 16)
            check("MFA is NOT reported enabled before verification", mfa_state(c)["enabled"] is False)
            res = c.post("/api/account/mfa/verify", json={"factor_id": first_factor["factor_id"], "code": wrong_code(secret)})
            check("A wrong code does not enable MFA", res.status_code == 422 and mfa_state(c)["enabled"] is False)
            res = c.post("/api/account/mfa/verify", json={"factor_id": first_factor["factor_id"], "code": fresh_code(secret)})
            check("A correct code enables MFA", res.status_code == 200, res.get_data(as_text=True)[:300])
            check("MFA is reported enabled after verification", mfa_state(c)["enabled"] is True)

            login = browser()
            res = sign_in(login, NEW_PASSWORD)
            check("Sign-in now stops at the authenticator step", res.headers.get("Location", "").endswith("/login/mfa"))
            check("No access with the password alone", login.get("/api/account/profile").status_code == 401 and login.get("/dashboard").status_code == 302)
            login.post("/login/mfa", data={"code": wrong_code(secret)})
            check("A wrong code does not sign in", login.get("/api/account/profile").status_code == 401)
            res = login.post("/login/mfa", data={"code": fresh_code(secret)})
            check("The correct code completes sign-in", res.status_code == 302 and login.get("/api/account/security").status_code == 200, res.get_data(as_text=True)[:200])

            legacy2 = browser()
            with legacy2.session_transaction() as s:
                s["user"] = {"id": uid, "email": EMAIL, "role": "organization", "name": "E2E"}
            check("An older session of an MFA account is NOT upgraded past the authenticator",
                  legacy2.get("/api/account/security").get_json()["can_manage"] is False
                  and legacy2.post("/api/account/mfa/disable", json={"code": fresh_code(secret)}).status_code == 409)

            res = login.post("/api/account/password", json={"current_password": NEW_PASSWORD, "new_password": THIRD_PASSWORD, "confirm_password": THIRD_PASSWORD})
            check("Password changes while MFA is on (MFA-verified session)", res.status_code == 200, res.get_data(as_text=True)[:300])

            backup, res = enroll(login, "Backup authenticator")
            check("A backup authenticator can be added", bool(backup), res.get_data(as_text=True)[:300])
            if backup:
                check("Both authenticators are listed", len(mfa_state(login)["factors"]) == 2)
                second = browser()
                sign_in(second, THIRD_PASSWORD)
                res = second.post("/login/mfa", data={"code": fresh_code(backup)})
                check("Sign-in accepts a code from the backup authenticator", res.status_code == 302 and second.get("/api/account/security").status_code == 200)
                backup_id = next(f["id"] for f in mfa_state(login)["factors"] if f["id"] != first_factor["factor_id"])
                res = login.delete(f"/api/account/mfa/factors/{backup_id}", json={"code": wrong_code(secret)})
                check("An authenticator cannot be removed with a wrong code", res.status_code == 422 and len(mfa_state(login)["factors"]) == 2)
                res = login.delete(f"/api/account/mfa/factors/{backup_id}", json={"code": fresh_code(secret)})
                state = mfa_state(login)
                check("Removing the backup keeps MFA on", res.status_code == 200 and state["enabled"] and len(state["factors"]) == 1, res.get_data(as_text=True)[:300])

            # ---------------------------------------------------- sessions (006)
            if m006:
                listing = login.get("/api/account/security").get_json()["sessions"]
                mine = [s for s in listing["items"] if s["current"]]
                check("Session list comes from Supabase and marks this device", listing["available"] and len(mine) == 1, str(listing)[:300])
                check("Sessions show the browser, not the ESRMS server", bool(mine) and f"E2E/{TAG}" in (mine[0]["user_agent"] or ""), str(mine)[:200])
                victim = browser()
                sign_in(victim, THIRD_PASSWORD)
                victim.post("/login/mfa", data={"code": fresh_code(secret)})
                with victim.session_transaction() as s:
                    victim_sid = s["sb"]["sid"]
                check("The current device cannot be revoked as 'another device'",
                      login.post(f"/api/account/sessions/{mine[0]['id']}/revoke", json={}).status_code == 409)
                res = login.post(f"/api/account/sessions/{victim_sid}/revoke", json={})
                check("One other device can be signed out", res.status_code == 200, res.get_data(as_text=True)[:300])
                with victim.session_transaction() as s:
                    s["sb"] = {**s["sb"], "chk": 0}   # as if a minute had passed
                check("The signed-out device loses access", victim.get("/api/account/profile").status_code == 401)
                check("This device is still signed in", login.get("/api/account/security").status_code == 200)
                stranger = "99999999-9999-9999-9999-999999999999"
                check("A session that is not yours cannot be revoked", login.post(f"/api/account/sessions/{stranger}/revoke", json={}).status_code == 404)
            else:
                skip("Session list and single-device sign-out", need006)

            # ---------------------------------------------------- MFA recovery (006)
            if m006:
                res = login.post("/api/account/mfa/recovery-codes", json={"code": wrong_code(secret)})
                check("Recovery codes need a valid authenticator code", res.status_code == 422)
                res = login.post("/api/account/mfa/recovery-codes", json={"code": fresh_code(secret)})
                codes = res.get_json().get("codes", [])
                check("Ten recovery codes are generated", res.status_code == 200 and len(codes) == 10, res.get_data(as_text=True)[:200])
                stored = str(db.table("mfa_recovery_codes").select("*").eq("user_id", uid).execute().data)
                check("Only hashes are stored", bool(codes) and not any(code in stored or acct.normalize_code(code) in stored for code in codes))

                # Administrator-assisted reset: the user has no authenticator and no codes.
                locked = browser()
                sign_in(locked, THIRD_PASSWORD)
                page = locked.post("/login/mfa?mode=reset").get_data(as_text=True)
                found = re.search(r"select-all\">([A-Z0-9]{4}-[A-Z0-9]{4})<", page)
                check("A locked-out user (password verified) gets a reference code", bool(found))
                check("Requesting a reset does not sign in or remove MFA", locked.get("/api/account/profile").status_code == 401 and mfa_state(login)["enabled"])
                admin = browser()
                with admin.session_transaction() as s:
                    s["user"] = dict(FAKE_ADMIN)
                request_id = db.table("mfa_reset_requests").select("id").eq("user_id", uid).eq("status", "pending").execute().data[0]["id"]
                form = {"identity_note": "E2E: verified in person with agency ID", "confirm": "RESET"}
                not_admin = c.post(f"/admin/mfa-resets/{request_id}/approve", data={**form, "code": found.group(1)})
                check("A non-administrator cannot approve a reset", not_admin.status_code == 302 and mfa_state(login)["enabled"])
                admin.post(f"/admin/mfa-resets/{request_id}/approve", data={**form, "code": "AAAA-AAAA"})
                check("An administrator cannot reset MFA without the user's reference code", mfa_state(login)["enabled"])
                admin.post(f"/admin/mfa-resets/{request_id}/approve", data={"code": found.group(1), "confirm": "RESET", "identity_note": ""})
                check("...or without recording how identity was verified", mfa_state(login)["enabled"])
                admin.post(f"/admin/mfa-resets/{request_id}/approve", data={**form, "code": found.group(1)})
                after = db.auth.admin.get_user_by_id(uid).user
                check("With the code, a note and confirmation the administrator reset removes MFA", not [f for f in (after.factors or []) if f.status == "verified"])
                audit = [a["action"] for a in db.table("mfa_recovery_audit").select("action").eq("user_id", uid).order("id").execute().data]
                check("The reset is in the audit log", "reset_requested" in audit and "reset_code_mismatch" in audit and "reset_approved" in audit, str(audit))
                check("Recovery codes were invalidated by the reset", not db.table("mfa_recovery_codes").select("id").eq("user_id", uid).execute().data)
                plain = browser()
                check("After the reset, sign-in needs only the password", sign_in(plain, THIRD_PASSWORD).headers.get("Location", "").endswith("/dashboard"))

                # Recovery code: set MFA up again, generate codes, then sign in with one.
                secret, res = enroll(plain)
                check("MFA can be set up again after a reset", bool(secret), res.get_data(as_text=True)[:300])
                codes = plain.post("/api/account/mfa/recovery-codes", json={"code": fresh_code(secret)}).get_json()["codes"]
                rec = browser()
                sign_in(rec, THIRD_PASSWORD)
                rec.post("/login/mfa?mode=recovery", data={"recovery_code": "AAAA-BBBB-CCCC-DDDD"})
                check("A wrong recovery code does not sign in", rec.get("/api/account/profile").status_code == 401)
                other_user = browser()
                sign_in(other_user, OLD_PASSWORD, RESPONDER_EMAIL)
                check("A recovery code is useless without that account's password",
                      other_user.post("/login/mfa?mode=recovery", data={"recovery_code": codes[0]}).status_code == 302
                      and mfa_state(plain)["enabled"])
                res = rec.post("/login/mfa?mode=recovery", data={"recovery_code": codes[0]})
                check("A valid recovery code signs in", res.status_code == 302 and rec.get("/api/account/profile").status_code == 200)
                check("...and removes the lost authenticator", mfa_state(rec)["enabled"] is False)
                rec2 = browser()
                check("The used code and the rest of its set no longer do anything",
                      sign_in(rec2, THIRD_PASSWORD).headers.get("Location", "").endswith("/dashboard")
                      and not db.table("mfa_recovery_codes").select("id").eq("user_id", uid).execute().data)
                login = rec
            else:
                skip("Recovery codes and administrator MFA reset", need006)
                res = login.post("/api/account/mfa/disable", json={"code": wrong_code(secret)})
                check("MFA cannot be disabled with a wrong code", res.status_code == 422 and mfa_state(login)["enabled"] is True)
                res = login.post("/api/account/mfa/disable", json={"code": fresh_code(secret)})
                check("MFA is disabled with a correct code", res.status_code == 200, res.get_data(as_text=True)[:300])
                check("MFA is reported disabled afterwards", mfa_state(login)["enabled"] is False)
                plain = browser()
                check("Sign-in no longer asks for a code", sign_in(plain, THIRD_PASSWORD).headers.get("Location", "").endswith("/dashboard"))

            # ---------------------------------------------------- notification outbox (006). Providers are stubbed.
            if m006:
                events = db.table("notification_events").select("*").eq("target_user_id", uid).order("id").execute().data
                check("Real security changes were queued by the database trigger",
                      len(events) >= 3 and all(e["category"] == "security_alerts" for e in events), str(len(events)))
                keys = [e["event_key"] for e in events]
                check("Each event is queued once", len(keys) == len(set(keys)))

                cfg = {**app.config, "RESEND_API_KEY": "stub", "EMAIL_FROM": "ESRMS <stub@example.com>",
                       "VAPID_PUBLIC_KEY": "stub", "VAPID_PRIVATE_KEY": "stub", "VAPID_SUBJECT": "mailto:stub@example.com"}
                now = datetime.now(timezone.utc)
                event = {**events[-1], "available_at": now.isoformat()}
                me = [delivery.directory_entry(db.auth.admin.get_user_by_id(uid).user.model_dump(), set())]
                db.table("push_subscriptions").insert({"user_id": uid, "endpoint": f"https://push.example.com/e2e/{TAG}",
                                                       "p256dh": "B" * 87, "auth": "a" * 22}).execute()

                def deliveries():
                    return db.table("notification_deliveries").select("*").eq("event_id", event["id"]).order("id").execute().data

                with mock.patch("app.delivery.load_directory", return_value=me):
                    db.table("notification_preferences").upsert({"user_id": uid, **acct.DEFAULT_PREFERENCES, "security_alerts": False, "email_enabled": True}, on_conflict="user_id").execute()
                    delivery.fan_out(db, cfg, event, now)
                    check("A switched-off category queues no email and no push", deliveries() == [])
                    db.table("notification_preferences").update({"security_alerts": True}).eq("user_id", uid).execute()
                    delivery.fan_out(db, cfg, event, now)
                    delivery.fan_out(db, cfg, event, now)
                    rows = deliveries()
                    check("An enabled category queues exactly one email and one push", sorted(r["channel"] for r in rows) == ["email", "push"], str(rows)[:300])
                    check("The email goes to the account's verified address", [r["target"] for r in rows if r["channel"] == "email"] == [EMAIL])

                def claim(row):
                    return db.table("notification_deliveries").update({"status": "sending", "attempts": row["attempts"] + 1}).eq("id", row["id"]).execute().data[0]

                email_row = next(r for r in rows if r["channel"] == "email")
                with mock.patch("app.delivery.send_email", side_effect=delivery.DeliveryError("stubbed provider outage")):
                    outcome = delivery.deliver(db, cfg, claim(email_row), event, now)
                failed = next(r for r in deliveries() if r["channel"] == "email")
                check("A provider failure is recorded as failed, not sent", outcome == "failed" and failed["status"] == "failed" and failed["sent_at"] is None)
                with mock.patch("app.delivery.send_email", return_value="stub-message-id") as send:
                    delivery.deliver(db, cfg, claim(failed), event, now)
                sent = next(r for r in deliveries() if r["channel"] == "email")
                check("The retry reuses the same delivery and is marked sent only after acceptance",
                      sent["id"] == email_row["id"] and sent["status"] == "sent" and sent["provider_id"] == "stub-message-id"
                      and send.call_args.kwargs["idempotency_key"] == f"esrms-delivery-{email_row['id']}")
                push_row = next(r for r in rows if r["channel"] == "push")
                with mock.patch("app.delivery.send_push", side_effect=delivery.SubscriptionGone("stubbed 410")):
                    delivery.deliver(db, cfg, claim(push_row), event, now)
                check("An expired push subscription is removed", not db.table("push_subscriptions").select("id").eq("user_id", uid).execute().data)
                log = login.get("/api/account/notifications").get_json()["recent"]
                check("The Notifications page shows the real delivery outcomes", {(r["channel"], r["status"]) for r in log} >= {("email", "sent"), ("push", "dead")}, str(log)[:300])
            else:
                skip("Notification outbox, preferences and retry behaviour", need006)

            real_email = delivery.email_configured(app.config)
            real_push = delivery.push_configured(app.config)
            if not real_email:
                skip("Sending a real email", "RESEND_API_KEY and EMAIL_FROM are not set in .env")
            if not real_push:
                skip("Sending a real push", "VAPID keys are not set in .env (scripts/generate_vapid_keys.py)")
        except SystemExit:
            pass
        finally:
            for account in (uid, responder.id):
                db.auth.admin.delete_user(account)
            print(f"Temporary accounts {EMAIL} and {RESPONDER_EMAIL} deleted.")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
