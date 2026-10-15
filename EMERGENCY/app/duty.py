"""Dispatcher duty status (Console Status) and who may receive incidents.

Only a dispatcher whose stored status is On Duty may be assigned new incidents, see new reports or be
notified about them. On Break and Off Duty dispatchers keep their session and see only incidents already
assigned to them. The status lives in public.dispatcher_duty (supabase/migrations/014_dispatcher_duty.sql);
no row means Off Duty. The database functions there do the actual changes under row locks.
"""
STATUSES = {"on_duty": "On duty", "on_break": "On break", "off_duty": "Off duty"}
ON_DUTY = "on_duty"
DEFAULT = "off_duty"
TABLE = "dispatcher_duty"
MIGRATION_HINT = "The database needs updating: run supabase/migrations/014_dispatcher_duty.sql in Supabase."


def status_of(db, user_id):
    rows = db.table(TABLE).select("status").eq("user_id", user_id).limit(1).execute().data
    return rows[0]["status"] if rows and rows[0].get("status") in STATUSES else DEFAULT


def on_duty_ids(db):
    return {r["user_id"] for r in db.table(TABLE).select("user_id").eq("status", ON_DUTY).execute().data}


def can_see(duty, user_id, incident):
    """Open incidents are for On Duty dispatchers; others only see what is already assigned to them."""
    return duty == ON_DUTY or incident.get("assigned_to") == user_id


def rpc_failure(exc):
    """Map a database function error to 'not_on_duty', 'conflict', 'missing' or None."""
    text = " ".join(str(part) for part in (getattr(exc, "message", None), exc) if part)
    if "ESRMS_NOT_ON_DUTY" in text:
        return "not_on_duty"
    if "ESRMS_ASSIGN_CONFLICT" in text:
        return "conflict"
    if any(s in text for s in ("dispatcher_duty", "assign_incident", "set_dispatcher_duty", "assigned_to")) and \
            any(s in text for s in ("does not exist", "Could not find", "PGRST202", "PGRST205", "42P01", "42703")):
        return "missing"
    return None
