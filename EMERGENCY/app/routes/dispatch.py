"""Dispatcher Dashboard API: incoming emergency incidents and responder hotlines.

Incidents are written by the separate Citizen Emergency App straight into
public.emergency_incidents. This module only reads them and applies dispatcher
updates. Everything goes through the service-role client on the server after a
role check; the browser never touches these tables or sees the service key.
"""
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, session

from app import delivery
from app import dispatch as dsp
from app import duty
from app.supabase_client import get_supabase_admin

bp = Blueprint("dispatch", __name__, url_prefix="/api/dispatch")
log = logging.getLogger(__name__)

INCIDENTS = "emergency_incidents"
EVENTS = "incident_events"
HOTLINES = "responder_hotlines"
LIST_LIMIT = 300
# Contact numbers are left out of lists and only sent when one incident is opened.
LIST_COLUMNS = ("id, incident_number, emergency_type, description, latitude, longitude, location_accuracy_m, "
                "location_verified, status, priority, citizen_name, acknowledged_at, resolved_at, archived_at, "
                "assigned_to, assigned_at, created_at, updated_at")


def can_dispatch(user):
    return bool(user) and user.get("role") in current_app.config["DISPATCH_ROLES"]


def dispatcher_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = session.get("user")
        if not user:
            return jsonify(error="Sign in to use the dispatch console."), 401
        if not can_dispatch(user):
            return jsonify(error="Only dispatchers and administrators can do this."), 403
        # JSON-only writes: cross-site forms can't send JSON without a CORS preflight.
        if request.method in ("POST", "PUT", "PATCH") and not request.is_json:
            return jsonify(error="Expected a JSON request."), 415
        return view(*args, **kwargs)
    return wrapped


def db_error(exc, action):
    log.exception("Dispatch %s failed", action)
    detail = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    return jsonify(error=f"Could not {action}: {detail}"), 502


def _db():
    return get_supabase_admin()


def _user_id():
    return session["user"]["id"]


def duty_error(exc, action):
    """Database function errors as user-facing answers; anything unknown is a 502."""
    kind = duty.rpc_failure(exc)
    if kind == "not_on_duty":
        return jsonify(error="That dispatcher is not On Duty, so they cannot take new incidents.", code="not_on_duty"), 409
    if kind == "conflict":
        return jsonify(error="Another dispatcher took or reassigned this incident first. The list has been refreshed.",
                       code="conflict"), 409
    if kind == "missing":
        log.error("Dispatcher duty schema missing: %s", exc)
        return jsonify(error=duty.MIGRATION_HINT), 503
    return db_error(exc, action)


def _my_duty():
    """The signed-in dispatcher's stored status, read fresh on every request (never trusted from the browser)."""
    return duty.status_of(_db(), _user_id())


def _incident_json(row):
    out = dict(row)
    out["code"] = dsp.incident_code(row)
    out["has_location"] = dsp.valid_coordinates(row.get("latitude"), row.get("longitude"))
    # Photos live in the public media bucket (supabase/migrations/013_incident_photos.sql).
    media = f"{current_app.config['SUPABASE_URL'].rstrip('/')}/storage/v1/object/public/{current_app.config['MEDIA_BUCKET']}/incidents/"
    out["description"], out["photos"] = dsp.split_photos(row.get("description"), media)
    return out


def _names(ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    try:
        res = _db().table("users").select("id, email, full_name, agency_name").in_("id", list(ids)).execute()
    except Exception:
        log.warning("Could not load user names", exc_info=True)
        return {}
    return {u["id"]: u.get("full_name") or u.get("agency_name") or u.get("email") for u in res.data}


# ---------------------------------------------------------------- incidents

@bp.route("/incidents")
@dispatcher_api
def list_incidents():
    """Incoming panel: everything not yet archived, plus the counters."""
    try:
        db = _db()
        my_duty = _my_duty()
        rows_q = db.table(INCIDENTS).select(LIST_COLUMNS).is_("archived_at", "null")
        stats_q = db.table(INCIDENTS).select("status, priority").is_("archived_at", "null")
        if my_duty != duty.ON_DUTY:
            # On Break / Off Duty: no new reports, only incidents they are already handling.
            rows_q, stats_q = rows_q.eq("assigned_to", _user_id()), stats_q.eq("assigned_to", _user_id())
        rows = rows_q.order("created_at", desc=True).limit(LIST_LIMIT).execute().data
        stats = stats_q.execute().data
    except Exception as exc:
        return duty_error(exc, "load incidents")
    names = _names(r.get("assigned_to") for r in rows)
    items = []
    for r in dsp.sort_incidents(rows):
        item = _incident_json(r)
        item["assigned_to_name"] = names.get(r.get("assigned_to"))
        items.append(item)
    return jsonify(items=items, counts=dsp.counts(stats), duty=my_duty)


@bp.route("/incidents/history")
@dispatcher_api
def incident_history():
    """Incident Report tab: archived (resolved/cancelled) incidents."""
    query = _db().table(INCIDENTS).select(LIST_COLUMNS + ", archived_by").not_.is_("archived_at", "null")
    status = request.args.get("status")
    if status in dsp.CLOSED:
        query = query.eq("status", status)
    q = "".join(ch for ch in request.args.get("q", "") if ch.isalnum() or ch in " -_").strip()[:60]
    if q:
        number = q.upper().removeprefix("INC-").lstrip("0")
        filters = [f"emergency_type.ilike.*{q}*", f"description.ilike.*{q}*", f"citizen_name.ilike.*{q}*"]
        if number.isdigit():
            filters.append(f"incident_number.eq.{number}")
        query = query.or_(",".join(filters))
    try:
        rows = query.order("archived_at", desc=True).limit(200).execute().data
    except Exception as exc:
        return db_error(exc, "load incident history")
    names = _names(r.get("archived_by") for r in rows)
    items = []
    for r in rows:
        item = _incident_json(r)
        item["archived_by_name"] = names.get(r.get("archived_by"))
        items.append(item)
    return jsonify(items=items)


@bp.route("/incidents/<uuid:incident_id>")
@dispatcher_api
def get_incident(incident_id):
    """One incident with the citizen's contact number and its status history."""
    try:
        db = _db()
        rows = db.table(INCIDENTS).select("*").eq("id", str(incident_id)).limit(1).execute().data
        events = (db.table(EVENTS).select("*")
                  .eq("incident_id", str(incident_id)).order("created_at").execute().data) if rows else []
    except Exception as exc:
        return db_error(exc, "load the incident")
    if not rows:
        return jsonify(error="That incident no longer exists."), 404
    row = rows[0]
    if not row.get("archived_at"):
        try:
            my_duty = _my_duty()
        except Exception as exc:
            return duty_error(exc, "check your duty status")
        if not duty.can_see(my_duty, _user_id(), row):
            return jsonify(error=f"You are {duty.STATUSES[my_duty]}. Set yourself On Duty to view new incidents.",
                           code="not_on_duty"), 403
    names = _names([row.get("acknowledged_by"), row.get("archived_by"), row.get("updated_by"), row.get("assigned_to")]
                   + [e.get("actor_id") for e in events]
                   + [e.get(k) for e in events for k in ("from_assignee", "to_assignee")])
    item = _incident_json(row)
    for key in ("acknowledged_by", "archived_by", "updated_by", "assigned_to"):
        item[f"{key}_name"] = names.get(row.get(key))
    for e in events:
        e["actor_name"] = names.get(e.get("actor_id")) or ("Citizen" if e["event"] == "reported" else None)
        e["from_assignee_name"] = names.get(e.get("from_assignee"))
        e["to_assignee_name"] = names.get(e.get("to_assignee"))
    item["events"] = events
    return jsonify(item=item)


def _load(incident_id):
    rows = _db().table(INCIDENTS).select("*").eq("id", incident_id).limit(1).execute().data
    return rows[0] if rows else None


def _conditional_update(existing, changes, action):
    """Update only if nobody changed the status meanwhile; never report success on 0 rows."""
    try:
        updated = (_db().table(INCIDENTS).update(changes)
                   .eq("id", existing["id"]).eq("status", existing["status"]).execute().data)
    except Exception as exc:
        return db_error(exc, action)
    if not updated:
        return jsonify(error="Another dispatcher changed this incident first. The list has been refreshed."), 409
    return jsonify(item=_incident_json({k: v for k, v in updated[0].items() if k != "contact_number"}))


@bp.route("/incidents/<uuid:incident_id>/status", methods=["POST"])
@dispatcher_api
def set_status(incident_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in dsp.STATUSES:
        return jsonify(error="Choose a valid status.", fields={"status": "Choose a valid status."}), 422
    try:
        existing = _load(str(incident_id))
    except Exception as exc:
        return db_error(exc, "load the incident")
    if not existing:
        return jsonify(error="That incident no longer exists."), 404
    if existing.get("archived_at"):
        return jsonify(error="Archived incidents are read-only. Restore it first."), 409
    try:
        my_duty = _my_duty()
    except Exception as exc:
        return duty_error(exc, "check your duty status")
    if not duty.can_see(my_duty, _user_id(), existing):
        return jsonify(error=f"You are {duty.STATUSES[my_duty]}. Set yourself On Duty to take new incidents.",
                       code="not_on_duty"), 409
    if existing["status"] == status and existing.get("assigned_to") not in (None, _user_id()):
        # Someone else got there first; don't let this dispatcher believe they took it.
        return jsonify(error="Another dispatcher already took this incident. The list has been refreshed.", code="conflict"), 409
    if existing["status"] == status:
        return jsonify(item=_incident_json({k: v for k, v in existing.items() if k != "contact_number"}))
    if not existing.get("assigned_to") and status not in dsp.CLOSED:
        # Working an unassigned incident takes it: atomic, and only while this dispatcher is On Duty.
        try:
            claimed = _db().rpc("assign_incident", {"p_incident": existing["id"], "p_assignee": _user_id(),
                                                    "p_actor": _user_id(), "p_expected": None}).execute().data
        except Exception as exc:
            return duty_error(exc, "take the incident")
        if not claimed:
            return jsonify(error="The database did not confirm the assignment.", code="conflict"), 409
        existing = claimed[0]
        if existing["status"] == status:
            return jsonify(item=_incident_json({k: v for k, v in existing.items() if k != "contact_number"}))

    now = datetime.now(timezone.utc).isoformat()
    changes = {"status": status, "updated_by": _user_id()}
    if existing["status"] == "new" and not existing.get("acknowledged_at"):
        changes.update(acknowledged_at=now, acknowledged_by=_user_id())
    if status in dsp.CLOSED:
        changes["resolved_at"] = now
    elif existing["status"] in dsp.CLOSED:
        changes["resolved_at"] = None  # reopened
    return _conditional_update(existing, changes, "update the incident status")


@bp.route("/incidents/<uuid:incident_id>/archive", methods=["POST"])
@dispatcher_api
def archive_incident(incident_id):
    try:
        existing = _load(str(incident_id))
    except Exception as exc:
        return db_error(exc, "load the incident")
    if not existing:
        return jsonify(error="That incident no longer exists."), 404
    if existing["status"] not in dsp.CLOSED:
        return jsonify(error="Only resolved or cancelled incidents can be archived."), 409
    if existing.get("archived_at"):
        return jsonify(error="This incident is already archived."), 409
    changes = {"archived_at": datetime.now(timezone.utc).isoformat(), "archived_by": _user_id(), "updated_by": _user_id()}
    return _conditional_update(existing, changes, "archive the incident")


@bp.route("/incidents/<uuid:incident_id>/restore", methods=["POST"])
@dispatcher_api
def restore_incident(incident_id):
    try:
        existing = _load(str(incident_id))
    except Exception as exc:
        return db_error(exc, "load the incident")
    if not existing:
        return jsonify(error="That incident no longer exists."), 404
    if not existing.get("archived_at"):
        return jsonify(error="This incident is not archived."), 409
    return _conditional_update(existing, {"archived_at": None, "archived_by": None, "updated_by": _user_id()},
                               "restore the incident")


@bp.route("/incidents/<uuid:incident_id>", methods=["DELETE"])
@dispatcher_api
def delete_incident(incident_id):
    """Permanently remove an archived incident; its incident_events go with it (on delete cascade)."""
    try:
        deleted = (_db().table(INCIDENTS).delete().eq("id", str(incident_id))
                   .not_.is_("archived_at", "null").execute().data)
    except Exception as exc:
        return db_error(exc, "delete the incident")
    if not deleted:
        return jsonify(error="Only archived incidents can be deleted, or it no longer exists."), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------- duty status & assignment

def _active_assigned(user_id):
    rows = (_db().table(INCIDENTS).select("id, incident_number, emergency_type, status, priority")
            .eq("assigned_to", user_id).is_("archived_at", "null").execute().data)
    return [{"id": r["id"], "code": dsp.incident_code(r), "emergency_type": r["emergency_type"], "status": r["status"]}
            for r in rows if dsp.is_open(r)]


@bp.route("/duty")
@dispatcher_api
def get_duty():
    try:
        return jsonify(status=_my_duty(), active=_active_assigned(_user_id()))
    except Exception as exc:
        return duty_error(exc, "load your duty status")


@bp.route("/duty", methods=["POST"])
@dispatcher_api
def set_duty():
    """Change the signed-in dispatcher's own status. There is no way to name another account."""
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in duty.STATUSES:
        return jsonify(error="Choose On duty, On break or Off duty."), 422
    try:
        if status != duty.ON_DUTY and not body.get("confirm"):
            active = _active_assigned(_user_id())
            if active:
                return jsonify(error=f"You still have {len(active)} active incident(s) assigned to you. They stay "
                                     "assigned to you and may need reassignment to an On Duty dispatcher.",
                               code="active_incidents", active=active), 409
        res = _db().rpc("set_dispatcher_duty", {"p_user": _user_id(), "p_status": status, "p_actor": _user_id()}).execute().data
    except Exception as exc:
        return duty_error(exc, "update your status")
    row = (res or [None])[0]
    if not row or row.get("status") != status:
        return jsonify(error="The database did not confirm the status change."), 502
    log.info("Dispatcher %s duty %s -> %s", _user_id(), row.get("previous"), status)
    return jsonify(status=row["status"], changed_at=row.get("changed_at"), previous=row.get("previous"),
                   active_incidents=row.get("active_incidents", 0))


def _dispatcher_directory():
    """Every account allowed to dispatch, by id (same role rules as sign-in and notifications)."""
    users = delivery.load_directory(_db(), current_app.config.get("ADMIN_EMAILS", set()))
    roles = current_app.config["DISPATCH_ROLES"]
    return {u["id"]: u for u in users if u["role"] in roles and not u["banned"]}


@bp.route("/dispatchers/available")
@dispatcher_api
def available_dispatchers():
    """On Duty dispatchers who can take an incident right now."""
    try:
        directory = _dispatcher_directory()
        ids = [i for i in duty.on_duty_ids(_db()) if i in directory]
    except Exception as exc:
        return duty_error(exc, "load available dispatchers")
    names = _names(ids)
    items = [{"id": i, "name": names.get(i) or directory[i]["email"], "me": i == _user_id()} for i in ids]
    return jsonify(items=sorted(items, key=lambda d: (not d["me"], (d["name"] or "").lower())))


@bp.route("/incidents/<uuid:incident_id>/assign", methods=["POST"])
@dispatcher_api
def assign(incident_id):
    """Claim or reassign an open incident. The database re-checks On Duty and the current assignee atomically."""
    body = request.get_json(silent=True) or {}
    assignee = str(body.get("assignee_id") or "")
    expected = body.get("expected_assignee_id") or None
    try:
        existing = _load(str(incident_id))
    except Exception as exc:
        return db_error(exc, "load the incident")
    if not existing:
        return jsonify(error="That incident no longer exists."), 404
    if existing.get("archived_at") or existing["status"] in dsp.CLOSED:
        return jsonify(error="Only open incidents can be assigned."), 409
    try:
        my_duty = _my_duty()
        # Dispatchers who are not On Duty may only hand over their own incidents, not take or move others.
        if my_duty != duty.ON_DUTY and existing.get("assigned_to") != _user_id():
            return jsonify(error=f"You are {duty.STATUSES[my_duty]}. Set yourself On Duty to assign incidents.",
                           code="not_on_duty"), 409
        if assignee != _user_id() and assignee not in _dispatcher_directory():
            return jsonify(error="Choose an active dispatcher account."), 422
        if expected != existing.get("assigned_to"):
            return jsonify(error="This incident was reassigned meanwhile. The list has been refreshed.", code="conflict"), 409
        if assignee == existing.get("assigned_to"):
            return jsonify(item=_incident_json({k: v for k, v in existing.items() if k != "contact_number"}))
        updated = _db().rpc("assign_incident", {"p_incident": existing["id"], "p_assignee": assignee,
                                                "p_actor": _user_id(), "p_expected": expected}).execute().data
    except Exception as exc:
        return duty_error(exc, "assign the incident")
    if not updated:
        return jsonify(error="The database did not confirm the assignment.", code="conflict"), 409
    item = _incident_json({k: v for k, v in updated[0].items() if k != "contact_number"})
    item["assigned_to_name"] = _names([assignee]).get(assignee)
    return jsonify(item=item)


# ---------------------------------------------------------------- hotlines

@bp.route("/hotlines")
@dispatcher_api
def list_hotlines():
    try:
        rows = _db().table(HOTLINES).select("*").order("category").order("organization_name").execute().data
    except Exception as exc:
        return db_error(exc, "load hotlines")
    for r in rows:
        r["tel"] = dsp.tel_href(r["phone_number"])
    return jsonify(items=rows)


@bp.route("/hotlines", methods=["POST"])
@dispatcher_api
def create_hotline():
    try:
        row = dsp.validate_hotline(request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422
    row["created_by"] = row["updated_by"] = _user_id()
    try:
        created = _db().table(HOTLINES).insert(row).execute().data
    except Exception as exc:
        return db_error(exc, "save the hotline")
    if not created:
        return jsonify(error="The database did not confirm the save."), 502
    return jsonify(item=created[0]), 201


@bp.route("/hotlines/<uuid:hotline_id>", methods=["PUT"])
@dispatcher_api
def update_hotline(hotline_id):
    try:
        row = dsp.validate_hotline(request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422
    row["updated_by"] = _user_id()
    try:
        updated = _db().table(HOTLINES).update(row).eq("id", str(hotline_id)).execute().data
    except Exception as exc:
        return db_error(exc, "update the hotline")
    if not updated:
        return jsonify(error="That hotline no longer exists."), 404
    return jsonify(item=updated[0])


@bp.route("/hotlines/<uuid:hotline_id>", methods=["DELETE"])
@dispatcher_api
def delete_hotline(hotline_id):
    try:
        deleted = _db().table(HOTLINES).delete().eq("id", str(hotline_id)).execute().data
    except Exception as exc:
        return db_error(exc, "delete the hotline")
    if not deleted:
        return jsonify(error="That hotline no longer exists."), 404
    return jsonify(ok=True)
