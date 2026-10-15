"""MFA recovery: single-use recovery codes and the administrator-assisted reset.

Both paths end the same way: the lost authenticators are removed through Supabase's admin
MFA API, every other session of the account is ended, and the change is written to the
audit log and to the account's security activity (which emails/pushes a security alert).

Neither path works on an email address alone:
  * a recovery code is only accepted after the account's password was verified, and
  * an administrator can only approve a request that the password holder created, by
    entering the reference code that person was shown.
"""
import logging
from datetime import datetime, timezone

from app import account as acct

log = logging.getLogger(__name__)

CODES = "mfa_recovery_codes"
REQUESTS = "mfa_reset_requests"
AUDIT = "mfa_recovery_audit"
SECURITY_EVENTS = "account_security_events"
MAX_RESET_CODE_ATTEMPTS = 5


def _now():
    return datetime.now(timezone.utc).isoformat()


def audit(db, action, user_id, actor_id=None, detail=None):
    """Audit trail. A failure here is logged loudly but does not undo the security action."""
    try:
        db.table(AUDIT).insert({"action": action, "user_id": user_id, "actor_id": actor_id,
                                "detail": (detail or None) and detail[:500]}).execute()
    except Exception:
        log.error("MFA recovery audit write failed: %s for %s by %s", action, user_id, actor_id, exc_info=True)


def record_security_event(db, user_id, event, user_agent=None):
    try:
        rows = db.table(SECURITY_EVENTS).insert({
            "user_id": user_id, "event": event, "user_agent": (user_agent or "")[:300] or None}).execute().data
        return rows[0]["id"] if rows else None
    except Exception:
        log.warning("Could not record security event %s", event, exc_info=True)
        return None


def verified_factors(db, user_id):
    factors = db.auth.admin.mfa.list_factors({"user_id": user_id}).factors
    return [f for f in factors if f.status == "verified"]


def remove_all_factors(db, user_id):
    """Delete every MFA factor of the account. Returns how many verified ones were removed."""
    factors = db.auth.admin.mfa.list_factors({"user_id": user_id}).factors
    for f in factors:
        db.auth.admin.mfa.delete_factor({"user_id": user_id, "id": f.id})
    return sum(1 for f in factors if f.status == "verified")


def clear_recovery_codes(db, user_id):
    db.table(CODES).delete().eq("user_id", user_id).execute()


def replace_recovery_codes(db, user_id):
    """New set of codes; the old set stops working. Returns the plain codes to show once."""
    codes = acct.generate_recovery_codes()
    clear_recovery_codes(db, user_id)
    db.table(CODES).insert([{"user_id": user_id, "code_hash": acct.hash_code(user_id, c)} for c in codes]).execute()
    audit(db, "codes_generated", user_id, user_id)
    return codes


def recovery_code_status(db, user_id):
    rows = db.table(CODES).select("used_at, created_at").eq("user_id", user_id).execute().data
    return {
        "remaining": sum(1 for r in rows if not r["used_at"]),
        "generated_at": min((r["created_at"] for r in rows), default=None),
    }


def use_recovery_code(db, user_id, code, user_agent=None):
    """Spend one recovery code. True only if this call was the one that used it."""
    if len(acct.normalize_code(code)) != 16:
        return False
    spent = (db.table(CODES).update({"used_at": _now()})
             .eq("user_id", user_id).eq("code_hash", acct.hash_code(user_id, code)).is_("used_at", "null")
             .execute().data)
    if not spent:
        audit(db, "code_rejected", user_id, user_id, user_agent)
        return False
    audit(db, "code_used", user_id, user_id, user_agent)
    return True


def recover_with_code(db, user_id, code, user_agent=None):
    """Recovery-code sign-in: on a valid code the lost authenticators are removed."""
    if not use_recovery_code(db, user_id, code, user_agent):
        return False
    remove_all_factors(db, user_id)
    clear_recovery_codes(db, user_id)   # the rest belonged to the removed authenticator setup
    record_security_event(db, user_id, "recovery_code_used", user_agent)
    return True


def create_reset_request(db, user_id, user_agent=None):
    """Called after the password check. Returns the reference code to show the user once."""
    code = acct.new_code(2)
    db.table(REQUESTS).update({"status": "expired"}).eq("user_id", user_id).eq("status", "pending").execute()
    db.table(REQUESTS).insert({"user_id": user_id, "code_hash": acct.hash_code(user_id, code),
                               "user_agent": (user_agent or "")[:300] or None}).execute()
    audit(db, "reset_requested", user_id, user_id, user_agent)
    record_security_event(db, user_id, "mfa_reset_requested", user_agent)
    return code


def pending_requests(db):
    return (db.table(REQUESTS).select("id, user_id, user_agent, attempts, created_at, expires_at")
            .eq("status", "pending").gt("expires_at", _now()).order("created_at").execute().data)


class ResetRefused(Exception):
    pass


def approve_reset(db, request_id, admin_id, code, identity_note):
    """Administrator approval. Raises ResetRefused with the reason unless everything checks out."""
    note = " ".join((identity_note or "").split())
    if len(note) < 10:
        raise ResetRefused("Describe how you verified the person's identity (at least 10 characters).")
    rows = db.table(REQUESTS).select("*").eq("id", request_id).limit(1).execute().data
    if not rows or rows[0]["status"] != "pending" or rows[0]["expires_at"] <= _now():
        raise ResetRefused("That request is no longer pending.")
    req = rows[0]
    if req["user_id"] == admin_id:
        raise ResetRefused("Another administrator has to approve a reset of your own account.")
    if req["attempts"] >= MAX_RESET_CODE_ATTEMPTS:
        db.table(REQUESTS).update({"status": "rejected", "decided_by": admin_id, "decided_at": _now()}).eq("id", request_id).execute()
        raise ResetRefused("Too many wrong reference codes. The request was closed; the user has to start a new one.")
    if acct.hash_code(req["user_id"], code) != req["code_hash"]:
        db.table(REQUESTS).update({"attempts": req["attempts"] + 1}).eq("id", request_id).execute()
        audit(db, "reset_code_mismatch", req["user_id"], admin_id)
        raise ResetRefused("That reference code does not match the request. Ask the user to read it out again.")
    # Claim the request first so two administrators can't both act on it.
    claimed = (db.table(REQUESTS).update({"status": "approved", "decided_by": admin_id, "decided_at": _now(),
                                          "identity_note": note[:500]})
               .eq("id", request_id).eq("status", "pending").execute().data)
    if not claimed:
        raise ResetRefused("That request was just handled by someone else.")
    remove_all_factors(db, req["user_id"])
    clear_recovery_codes(db, req["user_id"])
    audit(db, "reset_approved", req["user_id"], admin_id, note)
    record_security_event(db, req["user_id"], "mfa_reset_by_admin")
    return req["user_id"]


def reject_reset(db, request_id, admin_id):
    done = (db.table(REQUESTS).update({"status": "rejected", "decided_by": admin_id, "decided_at": _now()})
            .eq("id", request_id).eq("status", "pending").execute().data)
    if done:
        audit(db, "reset_rejected", done[0]["user_id"], admin_id)
    return bool(done)
