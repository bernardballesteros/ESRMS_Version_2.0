"""Hospital Signal (dispatch side) and the Hospital Portal API (receiving side).

One patient report is one hospital_signals row for its whole life. Two sets of routes work on it:
  * /api/hospital-signals/...  Dispatch roles (config DISPATCH_ROLES): hospitals, staff links,
                               incident picker, sending, listing and cancelling reports.
  * /hospital, /api/hospital/... Accounts listed in hospital_staff: their own hospital's reports,
                               acknowledge / bay / arrival / handoff, history, bays, staff, profile.

The hospital a request acts for always comes from the signed-in account's membership row,
never from the browser. Patient information lives in tables the browser cannot reach (RLS,
no policies); everything goes through the service-role client on the server after the role or
membership check, and database errors are logged here and never shown to the user.
"""
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from app import dispatch as dsp
from app import hospital as hs
from app import organizations as orgs
from app.routes.dispatch import EVENTS as INCIDENT_EVENTS
from app.routes.dispatch import INCIDENTS, can_dispatch, dispatcher_api
from app.supabase_client import get_supabase_admin

bp = Blueprint("hospital", __name__)
log = logging.getLogger(__name__)

HOSPITALS = "hospitals"
STAFF = "hospital_staff"
SIGNALS = "hospital_signals"
BAYS = "hospital_bays"
SIGNAL_EVENTS = "hospital_signal_events"
PAGE_SIZES = (10, 25, 50)
ACTIVE_LIMIT = 200
INCIDENT_COLUMNS = "id, incident_number, emergency_type, status, priority, latitude, longitude, created_at, archived_at"
# The hospital also reads the incident's description and position; never the reporting citizen's name or number.
REPORT_INCIDENT_COLUMNS = INCIDENT_COLUMNS + ", description, location_verified"
PEOPLE = ("sent_by", "acknowledged_by", "accepted_by", "arrived_by", "handoff_started_by", "completed_by",
          "responding_by", "on_scene_by",
          "received_by", "rejected_by", "cancelled_by")
NOT_SET_UP = ("The Hospital Portal is not fully set up yet. An administrator needs to run "
              "supabase/migrations/007_hospital_signals.sql, 008_hospital_portal.sql, 009_signal_archive.sql and "
              "010_organizations.sql "
              "in the Supabase SQL Editor.")


# ---------------------------------------------------------------- helpers

def _db():
    """One service-role client per request."""
    if "hospital_db" not in g:
        g.hospital_db = get_supabase_admin()
    return g.hospital_db


def _user_id():
    return session["user"]["id"]


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _utc(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(exc, action):
    """Log the real error; tell the user what did not happen, without database detail."""
    log.exception("Hospital: could not %s", action)
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '') or exc}"
    if any(mark in text for mark in ("PGRST205", "PGRST204", "42P01", "42703", "schema cache")):
        return jsonify(error=NOT_SET_UP, code="not_set_up"), 503
    if isinstance(exc, RuntimeError) and "SERVICE_ROLE" in str(exc):
        return jsonify(error="The server is not connected to the database. Contact your administrator."), 503
    return jsonify(error=f"Could not {action}. Check your connection and try again; nothing was changed."), 502


def invalid(exc):
    return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422


def _like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _quoted(value):
    """A PostgREST or() operand, quoted so commas, colons and parentheses in it are literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def memberships(db, user_id):
    """The hospitals this account belongs to, oldest membership first."""
    rows = db.table(STAFF).select("*").eq("user_id", user_id).execute().data
    return sorted(rows, key=lambda r: r.get("created_at") or "")


def staff_hospital_ids(db, user_id):
    return [m["hospital_id"] for m in memberships(db, user_id)]


def _staff_guard(admin_only):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = session.get("user")
            if not user:
                return jsonify(error="Sign in to the Hospital Portal to continue."), 401
            if request.method in ("POST", "PUT", "PATCH") and not request.is_json:
                return jsonify(error="Expected a JSON request."), 415
            try:
                mine = memberships(_db(), user["id"])
            except Exception as exc:
                return fail(exc, "check your hospital access")
            if not mine:
                return jsonify(error="Your account is not assigned to a hospital."), 403
            # The hospital is taken from the account's own membership, never from the request.
            g.hospital_ids = [m["hospital_id"] for m in mine]
            g.hospital_id = mine[0]["hospital_id"]
            g.staff_role = mine[0].get("staff_role") or "reception"
            if admin_only and g.staff_role != "admin":
                return jsonify(error="Only a Hospital Administrator can do this."), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


staff_api = _staff_guard(admin_only=False)
hospital_admin_api = _staff_guard(admin_only=True)


def _names(db, ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    try:
        rows = db.table("users").select("id, email, full_name, agency_name").in_("id", list(ids)).execute().data
    except Exception:
        log.warning("Could not load user names", exc_info=True)
        return {}
    return {u["id"]: u.get("full_name") or u.get("agency_name") or u.get("email") for u in rows}


def _hospital_json(row, staff_count=None, origin=None):
    out = {k: row.get(k) for k in ("id", "name", "address", "city", "province", "country", "phone_number", "official_email",
                                   "ed_contact", "hospital_type", "license_number", "latitude", "longitude", "availability",
                                   "availability_updated_at", "is_active", "created_at")}
    out["verified"] = row.get("verified") is not False
    kind = row.get("org_type") or "hospital"
    out.update(org_type=kind, org_type_label=orgs.ORG_TYPES.get(kind, kind), base_label=orgs.BASE_LABELS.get(kind),
               base_name=row.get("base_name"), status_label=orgs.AVAILABILITY.get(row.get("availability")))
    out["availability_label"] = hs.AVAILABILITY.get(row.get("availability"), row.get("availability"))
    out["type_label"] = hs.HOSPITAL_TYPES.get(row.get("hospital_type"))
    out["has_location"] = dsp.valid_coordinates(row.get("latitude"), row.get("longitude"))
    if staff_count is not None:
        out["staff_count"] = staff_count
    out["distance_km"] = hs.distance_km(origin[0], origin[1], row.get("latitude"), row.get("longitude")) if origin else None
    return out


def _incident_json(row, full=False):
    located = dsp.valid_coordinates(row.get("latitude"), row.get("longitude"))
    out = {
        "id": row["id"], "code": dsp.incident_code(row), "emergency_type": row["emergency_type"],
        "status": row["status"], "status_label": dsp.STATUSES.get(row["status"], row["status"]),
        "priority": row.get("priority"), "created_at": row["created_at"], "archived": bool(row.get("archived_at")),
        "has_location": located,
    }
    if full:
        out.update(description=row.get("description"), latitude=row.get("latitude") if located else None,
                   longitude=row.get("longitude") if located else None)
    return out


SIGNAL_FIELDS = (
    "id", "hospital_id", "incident_id", "signal_type", "patient_name", "patient_age", "patient_gender", "patient_condition",
    "eta", "additional_notes", "status", "medic_unit", "transport_type", "triage_priority", "chief_complaint", "bay_id",
    "receiving_team", "receiving_staff", "handoff_notes", "rejection_reason", "sent_at", "acknowledged_at", "accepted_at",
    "arrived_at", "handoff_started_at", "completed_at", "received_at", "rejected_at", "cancelled_at", "updated_at",
    "archived_at", "responding_at", "on_scene_at",
    *hs.VITALS,
)


def _signals_json(db, rows, side, full=False):
    """Reports with their hospital, incident, bay and people resolved. `side` decides labels and offered actions."""
    if not rows:
        return []
    labels = hs.PORTAL_STATUSES if side == "hospital" else hs.STATUSES
    hospital_ids = list({r["hospital_id"] for r in rows})
    hospitals = {h["id"]: h for h in db.table(HOSPITALS).select("*").in_("id", hospital_ids).execute().data}
    incident_ids = list({r["incident_id"] for r in rows if r.get("incident_id")})
    columns = REPORT_INCIDENT_COLUMNS if full else INCIDENT_COLUMNS
    incidents = ({i["id"]: i for i in db.table(INCIDENTS).select(columns).in_("id", incident_ids).execute().data}
                 if incident_ids else {})
    bay_ids = list({r["bay_id"] for r in rows if r.get("bay_id")})
    bays = {b["id"]: b for b in db.table(BAYS).select("id, name, kind").in_("id", bay_ids).execute().data} if bay_ids else {}
    names = _names(db, [r.get(k) for r in rows for k in PEOPLE])
    out = []
    for r in rows:
        item = {k: r.get(k) for k in SIGNAL_FIELDS}
        hospital = hospitals.get(r["hospital_id"]) or {}
        incident = incidents.get(r.get("incident_id"))
        bay = bays.get(r.get("bay_id"))
        item.update(
            code=hs.signal_code(r),
            signal_type_label=hs.ALL_SIGNAL_TYPES.get(r["signal_type"], r["signal_type"]),
            kind="response" if hs.is_response(r) else "patient",
            org_type=hospital.get("org_type") or "hospital",
            # Incident location -> this organization's fixed base. Never a person's position.
            distance_km=(hs.distance_km(incident.get("latitude"), incident.get("longitude"),
                                        hospital.get("latitude"), hospital.get("longitude")) if incident else None),
            condition_label=hs.CONDITIONS.get(r["patient_condition"], r["patient_condition"]),
            gender_label=hs.GENDERS.get(r.get("patient_gender")),
            transport_label=hs.TRANSPORT_TYPES.get(r.get("transport_type")),
            triage_label=hs.TRIAGE.get(r.get("triage_priority")),
            status_label="Accepted" if r["status"] == "accepted" and hs.is_response(r) else labels.get(r["status"], r["status"]),
            critical=hs.is_critical(r),
            read_only=r["status"] in hs.FINAL,
            hospital_name=hospital.get("name") or "Unknown hospital",
            hospital_phone=hospital.get("phone_number"),
            hospital_ed_contact=hospital.get("ed_contact"),
            hospital_address=hospital.get("address"),
            bay_name=bay["name"] if bay else None,
            incident=_incident_json(incident, full) if incident else None,
            actions=hs.allowed_actions(r["status"], side, r),
            **{f"{k}_name": names.get(r.get(k)) for k in PEOPLE},
        )
        out.append(item)
    return out


def _timeline(db, row, item):
    """Everything that happened to this report, oldest first: the incident's start, then the report's own events."""
    entries = []
    incident = item.get("incident")
    if incident:
        entries.append({"at": incident["created_at"], "event": "INCIDENT_REPORTED", "label": f"Emergency reported · {incident['code']}"})
        moves = (db.table(INCIDENT_EVENTS).select("to_status, created_at").eq("incident_id", incident["id"])
                 .eq("event", "status").order("created_at").execute().data)
        first = next((m for m in moves if m.get("to_status") in ("dispatched", "en_route")), None)
        if first:
            entries.append({"at": first["created_at"], "event": "MEDIC_DISPATCHED", "label": "Responders dispatched"})
    events = db.table(SIGNAL_EVENTS).select("*").eq("signal_id", row["id"]).order("id").execute().data
    names = _names(db, [e.get("actor_id") for e in events])
    labels = {**hs.EVENTS, **hs.RESPONSE_EVENTS} if hs.is_response(row) else hs.EVENTS
    for e in events:
        entries.append({"at": e["created_at"], "event": e["event"], "label": labels.get(e["event"], e["event"]),
                        "detail": e.get("detail"), "actor": names.get(e.get("actor_id"))})
    return sorted(entries, key=lambda e: hs.parse_ts(e["at"]) or datetime.min.replace(tzinfo=timezone.utc))


def _event(db, signal, event, detail=None):
    """Record one step of a report's history. A report is never blocked because its log entry failed."""
    try:
        db.table(SIGNAL_EVENTS).insert({"signal_id": signal["id"], "hospital_id": signal["hospital_id"], "event": event,
                                        "detail": detail, "actor_id": _user_id()}).execute()
    except Exception:
        log.exception("Hospital: could not record %s for report %s", event, signal.get("id"))


def _load_signal(db, signal_id):
    rows = db.table(SIGNALS).select("*").eq("id", str(signal_id)).limit(1).execute().data
    return rows[0] if rows else None


def _apply(db, existing, changes):
    """Write a change only if nobody moved the report meanwhile. Returns the stored row or None."""
    updated = db.table(SIGNALS).update(changes).eq("id", existing["id"]).eq("status", existing["status"]).execute().data
    return updated[0] if updated else None


STALE = "This report was updated by someone else a moment ago. The latest version is now shown."


def _act(signal_id, action, side, hospital_id=None):
    """Shared by the hospital's actions and the dispatcher's cancel."""
    db = _db()
    try:
        existing = _load_signal(db, signal_id)
    except Exception as exc:
        return fail(exc, "load the report")
    # A report for another hospital is answered exactly like one that does not exist.
    if not existing or (hospital_id is not None and existing["hospital_id"] != hospital_id):
        return jsonify(error="That report does not exist or is not addressed to your hospital."), 404
    try:
        changes = hs.transition(existing, action, _user_id(), side, request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return invalid(exc)
    except hs.TransitionError as exc:
        return jsonify(error=str(exc)), 409
    try:
        stored = _apply(db, existing, changes)
        if not stored:
            return jsonify(error=STALE), 409
        event = hs.actions_for(existing)[action][4]
        _event(db, stored, event, stored.get("rejection_reason") if action == "unable"
               else f"Received by {stored['receiving_staff']}" if action == "complete" and not hs.is_response(stored) else None)
        if action == "complete" and not hs.is_response(stored):
            _release_bay(db, stored, "cleaning")
            _event(db, stored, "REPORT_ARCHIVED")
        return jsonify(item=_signals_json(db, [stored], side, full=side == "hospital")[0])
    except Exception as exc:
        return fail(exc, "update the report")


def _release_bay(db, signal, bay_status):
    """After a completed handoff the bay needs cleaning before it is Ready again."""
    if not signal.get("bay_id"):
        return
    released = db.table(BAYS).update({"status": bay_status, "updated_by": _user_id()}).eq("id", signal["bay_id"]).execute().data
    if released:
        _event(db, signal, "TRAUMA_BAY_RELEASED", f"{released[0]['name']} marked {hs.BAY_STORED[bay_status].lower()}")


# ---------------------------------------------------------------- dispatcher: hospitals

@bp.route("/api/hospital-signals/hospitals")
@dispatcher_api
def list_hospitals():
    """Hospitals with how many staff accounts can receive a report, and the distance from an incident if known."""
    try:
        db = _db()
        query = db.table(HOSPITALS).select("*").order("name")
        if request.args.get("all") != "1":
            query = query.eq("is_active", True)
        if request.args.get("type") in orgs.ORG_TYPES:
            query = query.eq("org_type", request.args["type"])
        rows = query.execute().data
        counts = {}
        for s in db.table(STAFF).select("hospital_id").execute().data:
            counts[s["hospital_id"]] = counts.get(s["hospital_id"], 0) + 1
        origin = None
        incident_id = _uuid(request.args.get("incident_id"))
        if incident_id:
            found = db.table(INCIDENTS).select("latitude, longitude").eq("id", incident_id).limit(1).execute().data
            if found and dsp.valid_coordinates(found[0]["latitude"], found[0]["longitude"]):
                origin = (found[0]["latitude"], found[0]["longitude"])
    except Exception as exc:
        return fail(exc, "load hospitals")
    items = [_hospital_json(r, counts.get(r["id"], 0), origin) for r in rows]
    if origin:
        items.sort(key=lambda h: (h["distance_km"] is None, h["distance_km"] or 0, h["name"].lower()))
    return jsonify(items=items, distance_from_incident=bool(origin))


def _save_hospital(hospital_id=None):
    payload = request.get_json(silent=True) or {}
    try:
        row = hs.validate_hospital(payload)
    except dsp.ValidationError as exc:
        return invalid(exc)
    row["updated_by"] = _user_id()
    try:
        db = _db()
        clash = db.table(HOSPITALS).select("id").ilike("name", _like(row["name"])).execute().data
        if any(c["id"] != hospital_id for c in clash):
            return invalid(dsp.ValidationError({"name": "A hospital with this name already exists."}))
        if hospital_id:
            current = db.table(HOSPITALS).select("*").eq("id", hospital_id).limit(1).execute().data
            if not current:
                return jsonify(error="That hospital no longer exists."), 404
            # Verifying a self-registered hospital is what allows patient reports to be sent to it.
            if isinstance(payload.get("verified"), bool) and payload["verified"] != (current[0].get("verified") is not False):
                row.update(verified=payload["verified"], verified_at=_now() if payload["verified"] else None,
                           verified_by=_user_id() if payload["verified"] else None)
            saved = db.table(HOSPITALS).update(row).eq("id", hospital_id).execute().data
        else:
            row.update(created_by=_user_id(), verified=True, verified_at=_now(), verified_by=_user_id())
            saved = db.table(HOSPITALS).insert(row).execute().data
        if not saved:
            return jsonify(error="The hospital was not saved. Try again."), 502
    except Exception as exc:
        return fail(exc, "save the hospital")
    return jsonify(item=_hospital_json(saved[0])), 200 if hospital_id else 201


@bp.route("/api/hospital-signals/hospitals", methods=["POST"])
@dispatcher_api
def create_hospital():
    return _save_hospital()


@bp.route("/api/hospital-signals/hospitals/<uuid:hospital_id>", methods=["PUT"])
@dispatcher_api
def update_hospital(hospital_id):
    return _save_hospital(str(hospital_id))


@bp.route("/api/hospital-signals/hospitals/<uuid:hospital_id>", methods=["DELETE"])
@dispatcher_api
def delete_hospital(hospital_id):
    """Remove a hospital that never received a report. One with history can only be deactivated."""
    try:
        db = _db()
        if db.table(SIGNALS).select("id").eq("hospital_id", str(hospital_id)).limit(1).execute().data:
            return jsonify(error="This hospital has report history, so it cannot be deleted. Mark it inactive instead."), 409
        deleted = db.table(HOSPITALS).delete().eq("id", str(hospital_id)).execute().data
    except Exception as exc:
        return fail(exc, "delete the hospital")
    if not deleted:
        return jsonify(error="That hospital no longer exists."), 404
    return jsonify(ok=True)


def _staff_json(db, hospital_id):
    members = db.table(STAFF).select("*").eq("hospital_id", hospital_id).execute().data
    members.sort(key=lambda m: m.get("created_at") or "")
    users = ({u["id"]: u for u in db.table("users").select("id, email, full_name")
              .in_("id", [m["user_id"] for m in members]).execute().data} if members else {})
    return [{"user_id": m["user_id"], "email": (users.get(m["user_id"]) or {}).get("email"),
             "name": (users.get(m["user_id"]) or {}).get("full_name"), "position": m.get("position"),
             "staff_role": m.get("staff_role") or "reception",
             "role_label": hs.STAFF_ROLES.get(m.get("staff_role") or "reception"), "added_at": m.get("created_at")} for m in members]


@bp.route("/api/hospital-signals/hospitals/<uuid:hospital_id>/staff")
@dispatcher_api
def list_staff(hospital_id):
    try:
        return jsonify(items=_staff_json(_db(), str(hospital_id)))
    except Exception as exc:
        return fail(exc, "load hospital staff")


@bp.route("/api/hospital-signals/hospitals/<uuid:hospital_id>/staff", methods=["POST"])
@dispatcher_api
def add_staff(hospital_id):
    """Give an existing ESRMS account access to this hospital's reports."""
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return invalid(dsp.ValidationError({"email": "Enter the email address of the staff member's ESRMS account."}))
    try:
        db = _db()
        if not db.table(HOSPITALS).select("id").eq("id", str(hospital_id)).limit(1).execute().data:
            return jsonify(error="That hospital no longer exists."), 404
        found = db.table("users").select("id, email, full_name").ilike("email", _like(email)).limit(1).execute().data
        if not found:
            return invalid(dsp.ValidationError({"email": "No ESRMS account uses this email. The person has to register and sign in once first."}))
        db.table(STAFF).upsert({"hospital_id": str(hospital_id), "user_id": found[0]["id"], "added_by": _user_id()},
                               on_conflict="hospital_id,user_id", ignore_duplicates=True).execute()
    except Exception as exc:
        return fail(exc, "add the staff account")
    return jsonify(item={"user_id": found[0]["id"], "email": found[0]["email"], "name": found[0].get("full_name")}), 201


@bp.route("/api/hospital-signals/hospitals/<uuid:hospital_id>/staff/<uuid:user_id>", methods=["DELETE"])
@dispatcher_api
def remove_staff(hospital_id, user_id):
    try:
        removed = (_db().table(STAFF).delete().eq("hospital_id", str(hospital_id)).eq("user_id", str(user_id)).execute().data)
    except Exception as exc:
        return fail(exc, "remove the staff account")
    if not removed:
        return jsonify(error="That account is not on this hospital's staff list."), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------- dispatcher: incident picker

@bp.route("/api/hospital-signals/incidents")
@dispatcher_api
def search_incidents():
    """Existing incidents to attach a report to: newest first, live and archived, searchable by INC number or type."""
    query = _db().table(INCIDENTS).select(INCIDENT_COLUMNS)
    q = "".join(ch for ch in request.args.get("q", "") if ch.isalnum() or ch in " -_&").strip()[:60]
    if q:
        conditions = [f"emergency_type.ilike.{_quoted('*' + _like(q) + '*')}"]
        number = q.upper().removeprefix("INC-").removeprefix("INC").strip().lstrip("0")
        if number.isdigit() and len(number) < 18:
            conditions.append(f"incident_number.eq.{number}")
        query = query.or_(",".join(conditions))
    try:
        rows = query.order("created_at", desc=True).limit(20).execute().data
    except Exception as exc:
        return fail(exc, "search incidents")
    return jsonify(items=[_incident_json(r) for r in rows])


# ---------------------------------------------------------------- dispatcher: reports

def _day_bound(text, end=False):
    try:
        day = datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return _utc(datetime.combine(day + timedelta(days=1 if end else 0), time.min, hs.PHT))


def _search_conditions(db, q, hospital_names=True):
    """or() conditions matching a report by its number, patient, complaint, hospital or linked incident."""
    text = _like(q)
    conditions = [f"patient_name.ilike.{_quoted('*' + text + '*')}", f"chief_complaint.ilike.{_quoted('*' + text + '*')}"]
    upper = q.upper()
    hs_number = (upper.removeprefix("SIG-").removeprefix("SIG").removeprefix("HS-").removeprefix("HS")
                 .strip().lstrip("0"))
    if hs_number.isdigit() and len(hs_number) < 18 and not upper.startswith("INC"):
        conditions.append(f"signal_number.eq.{hs_number}")
    types = [key for key, label in hs.ALL_SIGNAL_TYPES.items() if q.lower() in label.lower()]
    if types:
        conditions.append(f"signal_type.in.({','.join(types)})")
    if hospital_names:
        hospitals = db.table(HOSPITALS).select("id").ilike("name", f"%{text}%").execute().data
        if hospitals:
            conditions.append(f"hospital_id.in.({','.join(h['id'] for h in hospitals)})")
    incident_filters = [f"emergency_type.ilike.{_quoted('*' + text + '*')}"]
    inc_number = upper.removeprefix("INC-").removeprefix("INC").strip().lstrip("0")
    if inc_number.isdigit() and len(inc_number) < 18 and not upper.startswith(("HS", "SIG")):
        incident_filters.append(f"incident_number.eq.{inc_number}")
    incidents = db.table(INCIDENTS).select("id").or_(",".join(incident_filters)).limit(200).execute().data
    if incidents:
        conditions.append(f"incident_id.in.({','.join(i['id'] for i in incidents)})")
    return conditions


def _paged(build, page, per_page):
    """Run a paged query; fall back to page 1 if the page no longer exists."""
    def run(number):
        first = (number - 1) * per_page
        return build().range(first, first + per_page - 1).execute()
    try:
        return run(page), page
    except Exception as exc:
        if getattr(exc, "code", None) != "PGRST103" or page == 1:
            raise
        return run(1), 1


def _page_args():
    per_page = request.args.get("per_page", type=int)
    return max(1, request.args.get("page", default=1, type=int)), per_page if per_page in PAGE_SIZES else PAGE_SIZES[0]


def _search_text():
    return "".join(ch for ch in request.args.get("q", "") if ch.isalnum() or ch in " -_&.'").strip()[:60]


@bp.route("/api/hospital-signals/signals")
@dispatcher_api
def list_signals():
    """Reports, newest first, filtered and paged in the database."""
    args = request.args
    page, per_page = _page_args()
    start, end = _day_bound(args.get("from")), _day_bound(args.get("to"), end=True)
    if args.get("from") and args.get("to") and start and end and start >= end:
        return jsonify(error="The start date cannot be later than the end date."), 422
    q = _search_text()
    try:
        db = _db()
        conditions = _search_conditions(db, q) if q else []

        def build():
            query = db.table(SIGNALS).select("*", count="exact")
            if args.get("status") in hs.STATUSES:
                query = query.eq("status", args["status"])
            if _uuid(args.get("hospital_id")):
                query = query.eq("hospital_id", _uuid(args["hospital_id"]))
            if _uuid(args.get("incident_id")):
                query = query.eq("incident_id", _uuid(args["incident_id"]))
            # archived=1: Signal History. archived=0: the live Hospital Signal list. Absent: both.
            if args.get("archived") == "1":
                query = query.not_.is_("archived_at", "null")
            elif args.get("archived") == "0":
                query = query.is_("archived_at", "null")
            # Signal History quick filters.
            quick = args.get("filter")
            if quick == "critical":      # the same rule as hs.is_critical
                query = query.or_("triage_priority.eq.1,patient_condition.in.(critical,unconscious),"
                                  "signal_type.eq.critical_patient")
            elif quick == "hospital_alerts":
                query = query.in_("signal_type", list(hs.ALERT_TYPES))
            elif quick == "incoming_patients":
                query = query.eq("signal_type", "incoming_patient")
            elif quick == "acknowledged":
                query = query.eq("status", "acknowledged")
            elif quick == "completed":
                query = query.in_("status", list(hs.DONE))
            elif quick == "cancelled":
                query = query.eq("status", "cancelled")
            if start:
                query = query.gte("sent_at", start)
            if end:
                query = query.lt("sent_at", end)
            if conditions:
                query = query.or_(",".join(conditions))
            return query.order("sent_at", desc=True).order("signal_number", desc=True)

        res, page = _paged(build, page, per_page)
        items = _signals_json(db, res.data, "dispatcher")
    except Exception as exc:
        return fail(exc, "load hospital signals")
    total = res.count or 0
    return jsonify(items=items, total=total, page=page, per_page=per_page, pages=max(1, -(-total // per_page)))


@bp.route("/api/hospital-signals/signals/<uuid:signal_id>")
@dispatcher_api
def get_signal(signal_id):
    try:
        db = _db()
        row = _load_signal(db, signal_id)
        item = _signals_json(db, [row], "dispatcher", full=True)[0] if row else None
        if item:
            item["timeline"] = _timeline(db, row, item)
    except Exception as exc:
        return fail(exc, "load the signal")
    if not item:
        return jsonify(error="That signal no longer exists."), 404
    return jsonify(item=item)


@bp.route("/api/hospital-signals/signals", methods=["POST"])
@dispatcher_api
def send_signal():
    """Store one report for one hospital. The database trigger queues the hospital staff notifications."""
    try:
        row = hs.validate_signal(request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return invalid(exc)
    hospital_id, incident_id = _uuid(row["hospital_id"]), _uuid(row["incident_id"]) if row["incident_id"] else None
    if not hospital_id:
        return invalid(dsp.ValidationError({"hospital_id": "Select the receiving hospital."}))
    if row["incident_id"] and not incident_id:
        return invalid(dsp.ValidationError({"incident_id": "Choose an incident from the list."}))
    row.update(hospital_id=hospital_id, incident_id=incident_id, status="sent", sent_by=_user_id())
    db = _db()
    try:
        # A repeated click or a retry after a lost response carries the same request id: return the stored report.
        if row["client_request_id"]:
            again = db.table(SIGNALS).select("*").eq("client_request_id", row["client_request_id"]).limit(1).execute().data
            if again:
                return jsonify(item=_signals_json(db, again, "dispatcher")[0], duplicate=True)
        hospital = db.table(HOSPITALS).select("*").eq("id", hospital_id).limit(1).execute().data
        if not hospital or not hospital[0]["is_active"]:
            return jsonify(error="That hospital is no longer available in ESRMS. Choose another hospital.",
                           fields={"hospital_id": "Choose another hospital."}), 409
        if hospital[0].get("verified") is False:
            return jsonify(error=f"{hospital[0]['name']} registered itself and has not been verified yet. Verify it "
                                 "(edit the hospital) before sending patient information.",
                           fields={"hospital_id": "This hospital is not verified yet."}), 409
        if hospital[0]["availability"] == "offline":
            return jsonify(error=f"{hospital[0]['name']} is marked Offline and cannot receive signals. "
                                 "Choose another hospital or contact it by phone.",
                           fields={"hospital_id": "This hospital is offline."}), 409
        if incident_id and not db.table(INCIDENTS).select("id").eq("id", incident_id).limit(1).execute().data:
            return invalid(dsp.ValidationError({"incident_id": "That incident no longer exists. Choose another."}))
        try:
            created = db.table(SIGNALS).insert(row).execute().data
            fresh = True
        except Exception as exc:
            if getattr(exc, "code", None) != "23505" or not row["client_request_id"]:
                raise
            created = db.table(SIGNALS).select("*").eq("client_request_id", row["client_request_id"]).limit(1).execute().data
            fresh = False
        if not created:
            return jsonify(error="The database did not confirm the signal. It was not sent; try again."), 502
        if fresh:
            _event(db, created[0], "REPORT_RECEIVED", f"Sent to {hospital[0]['name']}")
        staff = len(db.table(STAFF).select("user_id").eq("hospital_id", hospital_id).execute().data)
        item = _signals_json(db, created, "dispatcher")[0]
    except Exception as exc:
        return fail(exc, "send the signal")
    return jsonify(item=item, staff_notified=staff), 201


@bp.route("/api/hospital-signals/signals/<uuid:signal_id>/cancel", methods=["POST"])
@dispatcher_api
def cancel_signal(signal_id):
    return _act(signal_id, "cancel", "dispatcher")


def _set_archived(signal_id, archived):
    """Move a finished signal into Signal History, or back. The row is stamped, never copied or deleted."""
    db = _db()
    try:
        existing = _load_signal(db, signal_id)
        if not existing:
            return jsonify(error="That signal no longer exists."), 404
        if archived and existing["status"] not in hs.FINAL:
            return jsonify(error="This signal is still open at the hospital. It can be archived once it is completed, "
                                 "cancelled or marked unable to receive."), 409
        if bool(existing.get("archived_at")) != archived:
            changes = {"archived_at": _now() if archived else None, "archived_by": _user_id() if archived else None}
            stored = db.table(SIGNALS).update(changes).eq("id", existing["id"]).execute().data
            if not stored:
                return jsonify(error=STALE), 409
            existing = stored[0]
            _event(db, existing, "SIGNAL_ARCHIVED" if archived else "SIGNAL_RESTORED")
        return jsonify(item=_signals_json(db, [existing], "dispatcher")[0])
    except Exception as exc:
        return fail(exc, "archive the signal" if archived else "restore the signal")


@bp.route("/api/hospital-signals/signals/<uuid:signal_id>/archive", methods=["POST"])
@dispatcher_api
def archive_signal(signal_id):
    return _set_archived(signal_id, True)


@bp.route("/api/hospital-signals/signals/<uuid:signal_id>/restore", methods=["POST"])
@dispatcher_api
def restore_signal(signal_id):
    return _set_archived(signal_id, False)


# ---------------------------------------------------------------- dispatcher: organization bases

def _brief(signal):
    return signal and {"id": signal["id"], "code": hs.signal_code(signal), "status": signal["status"],
                       "status_label": ("Accepted" if signal["status"] == "accepted" and hs.is_response(signal)
                                        else hs.STATUSES.get(signal["status"], signal["status"])),
                       "active": signal["status"] not in hs.FINAL, "sent_at": signal.get("sent_at"),
                       "signal_type_label": hs.ALL_SIGNAL_TYPES.get(signal["signal_type"], signal["signal_type"])}


@bp.route("/api/organizations/nearby")
@dispatcher_api
def nearby_organizations():
    """Organization bases ranked for one incident: suitability and availability first, then distance to the base."""
    incident_id = _uuid(request.args.get("incident_id"))
    if not incident_id:
        return jsonify(error="Choose an incident."), 422
    try:
        db = _db()
        found = db.table(INCIDENTS).select(INCIDENT_COLUMNS).eq("id", incident_id).limit(1).execute().data
        if not found:
            return jsonify(error="That incident no longer exists."), 404
        rows = db.table(HOSPITALS).select("*").eq("is_active", True).execute().data
        sent = (db.table(SIGNALS).select("id, signal_number, signal_type, status, hospital_id, sent_at")
                .eq("incident_id", incident_id).order("sent_at", desc=True).limit(ACTIVE_LIMIT).execute().data)
    except Exception as exc:
        return fail(exc, "load nearby organizations")
    latest = {}
    for signal in sent:
        latest.setdefault(signal["hospital_id"], signal)
    result = orgs.recommend(found[0], rows)

    def entry(e):
        return {**{k: v for k, v in e.items() if k != "org"}, "org": _hospital_json(e["org"]), "signal": _brief(latest.get(e["org"]["id"]))}

    return jsonify(incident=_incident_json(found[0]), state=result["state"], category=result["category"], located=result["located"],
                   primary=[orgs.ORG_TYPES[t] for t in result["primary"]], support=[orgs.ORG_TYPES[t] for t in result["support"]],
                   items=[entry(e) for e in result["items"]], others=[entry(e) for e in result["others"]],
                   signals_sent=len(sent))


@bp.route("/api/organizations/<uuid:org_id>/signals", methods=["POST"])
@dispatcher_api
def send_request(org_id):
    """Send a response request for an existing incident to one organization. One row per request; the incident is linked."""
    payload = request.get_json(silent=True) or {}
    incident_id = _uuid(payload.get("incident_id"))
    if not incident_id:
        return invalid(dsp.ValidationError({"incident_id": "Choose the incident this signal is for."}))
    db = _db()
    try:
        incident = db.table(INCIDENTS).select(INCIDENT_COLUMNS).eq("id", incident_id).limit(1).execute().data
        if not incident:
            return invalid(dsp.ValidationError({"incident_id": "That incident no longer exists."}))
        try:
            priority, message, request_id = orgs.validate_request(payload, incident[0].get("priority"))
        except dsp.ValidationError as exc:
            return invalid(exc)
        if request_id:
            again = db.table(SIGNALS).select("*").eq("client_request_id", request_id).limit(1).execute().data
            if again:
                return jsonify(item=_signals_json(db, again, "dispatcher")[0], duplicate=True)
        org = db.table(HOSPITALS).select("*").eq("id", str(org_id)).limit(1).execute().data
        if not org or not org[0]["is_active"]:
            return jsonify(error="That organization is no longer available in ESRMS. Choose another."), 409
        org = org[0]
        kind = org.get("org_type") or "hospital"
        if kind == "hospital":
            return jsonify(error="Hospitals receive a patient report. Use the Hospital Signal tab for this hospital.",
                           code="use_hospital_signal"), 409
        if org.get("verified") is False:
            return jsonify(error=f"{org['name']} has not been verified yet. Verify it before sending a signal."), 409
        if org["availability"] == "offline":
            return jsonify(error=f"{org['name']} is Offline and cannot receive signals. Choose another organization "
                                 "or contact it by phone."), 409
        row = {"hospital_id": org["id"], "incident_id": incident_id, "signal_type": orgs.SIGNAL_FOR.get(kind, "response_request"),
               "patient_condition": "other", "triage_priority": priority, "additional_notes": message, "status": "sent",
               "sent_by": _user_id(), "client_request_id": request_id}
        try:
            created = db.table(SIGNALS).insert(row).execute().data
            fresh = True
        except Exception as exc:
            if getattr(exc, "code", None) != "23505" or not request_id:
                raise
            created = db.table(SIGNALS).select("*").eq("client_request_id", request_id).limit(1).execute().data
            fresh = False
        if not created:
            return jsonify(error="The database did not confirm the signal. It was not sent; try again."), 502
        if fresh:
            _event(db, created[0], "REPORT_RECEIVED", f"Sent to {org['name']}")
        staff = len(db.table(STAFF).select("user_id").eq("hospital_id", org["id"]).execute().data)
        item = _signals_json(db, created, "dispatcher")[0]
    except Exception as exc:
        return fail(exc, "send the signal")
    return jsonify(item=item, staff_notified=staff), 201


# ---------------------------------------------------------------- hospital portal: dashboard

@bp.route("/hospital")
def dashboard_page():
    """Hospital Medical Reception Dashboard. The API decides what the signed-in account may see."""
    user = session.get("user")
    if not user:
        return redirect(url_for("hospital_auth.login"))
    return render_template("hospital/dashboard.html", options=client_options(), can_dispatch=can_dispatch(user))


def _bays_json(db, hospital_id, active=None):
    bays = db.table(BAYS).select("*").eq("hospital_id", hospital_id).execute().data
    if active is None:
        active = db.table(SIGNALS).select("id, signal_number, status, bay_id").eq("hospital_id", hospital_id) \
            .in_("status", list(hs.ACTIVE)).execute().data
    holder = {r["bay_id"]: r for r in active if r.get("bay_id")}
    out = []
    for bay in sorted(bays, key=lambda b: (b.get("kind") != "trauma", b["name"].lower())):
        report = holder.get(bay["id"])
        state = hs.bay_state(bay, report)
        out.append({"id": bay["id"], "name": bay["name"], "kind": bay["kind"], "kind_label": hs.BAY_KINDS.get(bay["kind"]),
                    "status": state, "status_label": hs.BAY_STATUSES[state], "stored_status": bay.get("status"),
                    "report_id": report["id"] if report else None, "report_code": hs.signal_code(report) if report else None,
                    "assignable": state == "ready"})
    return out


@bp.route("/api/hospital/overview")
@staff_api
def overview():
    """This account's hospital, its role there, the dashboard counters and the bay board."""
    try:
        db = _db()
        hospital = db.table(HOSPITALS).select("*").eq("id", g.hospital_id).limit(1).execute().data
        if not hospital:
            return jsonify(error="Your hospital record no longer exists. Contact ESRMS dispatch."), 404
        active = (db.table(SIGNALS).select("id, signal_number, status, bay_id, triage_priority, patient_condition, signal_type")
                  .eq("hospital_id", g.hospital_id).in_("status", list(hs.ACTIVE)).limit(ACTIVE_LIMIT).execute().data)
        done_today = (db.table(SIGNALS).select("id", count="exact").eq("hospital_id", g.hospital_id)
                      .in_("status", list(hs.DONE)).gte("completed_at", _utc(hs.day_start())).limit(1).execute().count or 0)
        bays = _bays_json(db, g.hospital_id, active)
    except Exception as exc:
        return fail(exc, "load the dashboard")
    user = session["user"]
    return jsonify(
        hospital=_hospital_json(hospital[0]),
        me={"name": user.get("name"), "email": user.get("email"), "staff_role": g.staff_role,
            "role_label": ("Organization Administrator" if g.staff_role == "admin" and (hospital[0].get("org_type") or "hospital") != "hospital"
                           else hs.STAFF_ROLES.get(g.staff_role)), "is_admin": g.staff_role == "admin",
            "other_hospitals": len(g.hospital_ids) - 1},
        stats=hs.dashboard_stats(active, done_today), bays=bays,
    )


# ---------------------------------------------------------------- hospital portal: reports

PERIODS = {"today": 0, "week": 6, "month": 29}


@bp.route("/api/hospital/reports")
@staff_api
def list_reports():
    """One hospital's reports. view = signals (new, not yet acknowledged), incoming (acknowledged and active), completed, or history."""
    args = request.args
    view = args.get("view") if args.get("view") in ("incoming", "signals", "completed", "history") else "incoming"
    page, per_page = _page_args()
    try:
        db = _db()
        base = lambda count=None: db.table(SIGNALS).select("*", count=count).eq("hospital_id", g.hospital_id)  # noqa: E731
        if view in ("incoming", "signals"):
            rows = base().in_("status", ["sent"] if view == "signals" else list(hs.INCOMING)).limit(ACTIVE_LIMIT).execute().data
            level = args.get("filter")
            if level == "critical":
                rows = [r for r in rows if hs.is_critical(r)]
            elif level == "stable":
                rows = [r for r in rows if not hs.is_critical(r)]
            items = _signals_json(db, hs.sort_incoming(rows), "hospital")
            return jsonify(items=items, total=len(items), page=1, pages=1, per_page=len(items) or 1)

        q = _search_text()
        conditions = _search_conditions(db, q, hospital_names=False) if q else []
        scope = args.get("filter")

        def build():
            query = base("exact")
            if view == "completed" or scope == "completed":
                query = query.in_("status", list(hs.DONE))
            else:
                query = query.in_("status", list(hs.FINAL))
            if scope in PERIODS:
                query = query.gte("sent_at", _utc(hs.day_start(days_back=PERIODS[scope])))
            if scope == "critical":
                query = query.or_("triage_priority.eq.1,patient_condition.in.(critical,unconscious),signal_type.eq.critical_patient")
            if conditions:
                query = query.or_(",".join(conditions))
            return query.order("updated_at", desc=True).order("signal_number", desc=True)

        res, page = _paged(build, page, per_page)
        items = _signals_json(db, res.data, "hospital")
    except Exception as exc:
        return fail(exc, "load patient reports")
    total = res.count or 0
    return jsonify(items=items, total=total, page=page, per_page=per_page, pages=max(1, -(-total // per_page)))


@bp.route("/api/hospital/reports/<uuid:signal_id>")
@staff_api
def get_report(signal_id):
    """The complete report with its timeline. Another hospital's report does not exist as far as this caller can tell."""
    try:
        db = _db()
        row = _load_signal(db, signal_id)
        if not row or row["hospital_id"] != g.hospital_id:
            return jsonify(error="That report does not exist or is not addressed to your hospital."), 404
        item = _signals_json(db, [row], "hospital", full=True)[0]
        item["timeline"] = _timeline(db, row, item)
    except Exception as exc:
        return fail(exc, "load the report")
    return jsonify(item=item)


@bp.route("/api/hospital/reports/<uuid:signal_id>/<any(acknowledge, accept, arrive, handoff, respond, on_scene, complete, unable):action>",
          methods=["POST"])
@staff_api
def respond(signal_id, action):
    return _act(signal_id, action, "hospital", g.hospital_id)


@bp.route("/api/hospital/reports/<uuid:signal_id>/bay", methods=["POST"])
@staff_api
def assign_bay(signal_id):
    """Assign (or change) the bay for an active report. A bay holds one active patient at a time."""
    payload = request.get_json(silent=True) or {}
    bay_id = _uuid(payload.get("bay_id"))
    team = payload.get("receiving_team")
    team = " ".join(team.split())[:120] if isinstance(team, str) else None
    if not bay_id:
        return invalid(dsp.ValidationError({"bay_id": "Choose a bay."}))
    db = _db()
    try:
        existing = _load_signal(db, signal_id)
        if not existing or existing["hospital_id"] != g.hospital_id:
            return jsonify(error="That report does not exist or is not addressed to your hospital."), 404
        if existing["status"] in hs.FINAL:
            return jsonify(error="This report is archived and read-only."), 409
        bay = db.table(BAYS).select("*").eq("id", bay_id).eq("hospital_id", g.hospital_id).limit(1).execute().data
        if not bay:
            return invalid(dsp.ValidationError({"bay_id": "That bay does not belong to your hospital."}))
        if existing.get("bay_id") == bay_id and (team is None or team == (existing.get("receiving_team") or "")):
            return jsonify(item=_signals_json(db, [existing], "hospital", full=True)[0])
        if existing.get("bay_id") != bay_id:
            taken = (db.table(SIGNALS).select("id, signal_number").eq("bay_id", bay_id).in_("status", list(hs.ACTIVE))
                     .limit(1).execute().data)
            if taken:
                return jsonify(error=f"{bay[0]['name']} is already assigned to {hs.signal_code(taken[0])}. Choose another bay.",
                               fields={"bay_id": "Already in use."}), 409
            if bay[0].get("status") != "ready":
                return jsonify(error=f"{bay[0]['name']} is marked {hs.BAY_STORED.get(bay[0].get('status'), 'unavailable')}. "
                                     "Mark it Ready first or choose another bay.", fields={"bay_id": "Not ready."}), 409
        changes = {"bay_id": bay_id}
        if team is not None:
            changes["receiving_team"] = team or None
        # Assigning a bay is the start of preparation.
        if existing["status"] in ("sent", "acknowledged"):
            changes.update(hs.transition(existing, "accept", _user_id(), "hospital"))
        try:
            stored = _apply(db, existing, changes)
        except Exception as exc:
            if getattr(exc, "code", None) != "23505":
                raise
            return jsonify(error=f"{bay[0]['name']} was assigned to another patient a moment ago. Choose another bay.",
                           fields={"bay_id": "Already in use."}), 409
        if not stored:
            return jsonify(error=STALE), 409
        if existing.get("bay_id") != bay_id:
            _event(db, stored, "TRAUMA_BAY_ASSIGNED", bay[0]["name"])
        if "status" in changes:
            _event(db, stored, "BAY_PREPARATION_STARTED")
        return jsonify(item=_signals_json(db, [stored], "hospital", full=True)[0])
    except Exception as exc:
        return fail(exc, "assign the bay")


# ---------------------------------------------------------------- hospital portal: bays

@bp.route("/api/hospital/bays", methods=["POST"])
@hospital_admin_api
def create_bay():
    return _save_bay()


@bp.route("/api/hospital/bays/<uuid:bay_id>", methods=["PUT"])
@hospital_admin_api
def update_bay(bay_id):
    return _save_bay(str(bay_id))


def _save_bay(bay_id=None):
    try:
        row = hs.validate_bay(request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return invalid(exc)
    try:
        db = _db()
        clash = db.table(BAYS).select("id").eq("hospital_id", g.hospital_id).ilike("name", _like(row["name"])).execute().data
        if any(c["id"] != bay_id for c in clash):
            return invalid(dsp.ValidationError({"name": "Your hospital already has a bay with this name."}))
        row["updated_by"] = _user_id()
        if bay_id:
            saved = db.table(BAYS).update(row).eq("id", bay_id).eq("hospital_id", g.hospital_id).execute().data
        else:
            saved = db.table(BAYS).insert({**row, "hospital_id": g.hospital_id, "status": "ready"}).execute().data
        if not saved:
            return jsonify(error="That bay does not exist in your hospital."), 404
        return jsonify(items=_bays_json(db, g.hospital_id)), 200 if bay_id else 201
    except Exception as exc:
        return fail(exc, "save the bay")


@bp.route("/api/hospital/bays/<uuid:bay_id>", methods=["DELETE"])
@hospital_admin_api
def delete_bay(bay_id):
    try:
        db = _db()
        if db.table(SIGNALS).select("id").eq("bay_id", str(bay_id)).in_("status", list(hs.ACTIVE)).limit(1).execute().data:
            return jsonify(error="A patient is assigned to this bay. Complete or reassign that report first."), 409
        deleted = db.table(BAYS).delete().eq("id", str(bay_id)).eq("hospital_id", g.hospital_id).execute().data
        if not deleted:
            return jsonify(error="That bay does not exist in your hospital."), 404
        return jsonify(items=_bays_json(db, g.hospital_id))
    except Exception as exc:
        return fail(exc, "delete the bay")


@bp.route("/api/hospital/bays/<uuid:bay_id>/status", methods=["POST"])
@staff_api
def set_bay_status(bay_id):
    """Any staff member can mark an empty bay Ready or Cleaning. Preparing and Occupied follow the assigned report."""
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in hs.BAY_STORED:
        return invalid(dsp.ValidationError({"status": "Choose Ready or Cleaning."}))
    try:
        db = _db()
        if db.table(SIGNALS).select("id").eq("bay_id", str(bay_id)).in_("status", list(hs.ACTIVE)).limit(1).execute().data:
            return jsonify(error="This bay has a patient assigned, so its status follows that report."), 409
        saved = (db.table(BAYS).update({"status": status, "updated_by": _user_id()}).eq("id", str(bay_id))
                 .eq("hospital_id", g.hospital_id).execute().data)
        if not saved:
            return jsonify(error="That bay does not exist in your hospital."), 404
        return jsonify(items=_bays_json(db, g.hospital_id))
    except Exception as exc:
        return fail(exc, "update the bay")


# ---------------------------------------------------------------- hospital portal: settings

@bp.route("/api/hospital/availability", methods=["POST"])
@staff_api
def set_availability():
    """Hospital staff declare their own readiness; dispatchers see it in Available Hospitals."""
    value = (request.get_json(silent=True) or {}).get("availability")
    if value not in hs.AVAILABILITY:
        return invalid(dsp.ValidationError({"availability": "Choose an availability status."}))
    try:
        saved = (_db().table(HOSPITALS).update({
            "availability": value, "availability_updated_at": _now(),
            "availability_updated_by": _user_id(), "updated_by": _user_id()}).eq("id", g.hospital_id).execute().data)
    except Exception as exc:
        return fail(exc, "update availability")
    if not saved:
        return jsonify(error="Your hospital record no longer exists."), 404
    return jsonify(item=_hospital_json(saved[0]))


@bp.route("/api/hospital/profile", methods=["PUT"])
@hospital_admin_api
def update_profile():
    try:
        row = hs.validate_profile(request.get_json(silent=True) or {})
    except dsp.ValidationError as exc:
        return invalid(exc)
    try:
        db = _db()
        if any(c["id"] != g.hospital_id for c in db.table(HOSPITALS).select("id").ilike("name", _like(row["name"])).execute().data):
            return invalid(dsp.ValidationError({"hospital_name": "Another hospital already uses this name."}))
        if any(c["id"] != g.hospital_id for c in db.table(HOSPITALS).select("id").ilike("official_email", _like(row["official_email"])).execute().data):
            return invalid(dsp.ValidationError({"official_email": "Another hospital already uses this email."}))
        row["updated_by"] = _user_id()
        saved = db.table(HOSPITALS).update(row).eq("id", g.hospital_id).execute().data
    except Exception as exc:
        return fail(exc, "save the hospital profile")
    if not saved:
        return jsonify(error="Your hospital record no longer exists."), 404
    return jsonify(item=_hospital_json(saved[0]))


@bp.route("/api/hospital/staff")
@staff_api
def portal_staff():
    try:
        return jsonify(items=_staff_json(_db(), g.hospital_id), can_manage=g.staff_role == "admin")
    except Exception as exc:
        return fail(exc, "load hospital staff")


@bp.route("/api/hospital/staff", methods=["POST"])
@hospital_admin_api
def portal_add_staff():
    """Create a staff account for this hospital, or attach an existing ESRMS account by email."""
    payload = request.get_json(silent=True) or {}
    db = _db()
    try:
        email = (payload.get("email") or "").strip().lower()
        existing = db.table("users").select("id, email, full_name").ilike("email", _like(email)).limit(1).execute().data if email else []
        row = hs.validate_staff(payload, new_account=not existing)
    except dsp.ValidationError as exc:
        return invalid(exc)
    except Exception as exc:
        return fail(exc, "check the staff account")
    try:
        if existing:
            user_id = existing[0]["id"]
            if db.table(STAFF).select("user_id").eq("hospital_id", g.hospital_id).eq("user_id", user_id).execute().data:
                return invalid(dsp.ValidationError({"email": "This person is already on your staff list."}))
        else:
            try:
                created = db.auth.admin.create_user({
                    "email": row["email"], "password": row["password"], "email_confirm": True,
                    "user_metadata": {"role": "hospital", "full_name": row["full_name"], "position": row["position"]}})
            except Exception as exc:
                log.warning("Hospital: staff account not created: %s", getattr(exc, "message", exc))
                return invalid(dsp.ValidationError({"email": "This account could not be created. The email may already be registered."}))
            user_id = created.user.id
        db.table(STAFF).insert({"hospital_id": g.hospital_id, "user_id": user_id, "staff_role": row["staff_role"],
                                "position": row["position"], "added_by": _user_id()}).execute()
        return jsonify(items=_staff_json(db, g.hospital_id), created=not existing), 201
    except Exception as exc:
        return fail(exc, "add the staff account")


def _other_admins(db, user_id):
    return [m for m in db.table(STAFF).select("*").eq("hospital_id", g.hospital_id).execute().data
            if (m.get("staff_role") or "reception") == "admin" and m["user_id"] != user_id]


@bp.route("/api/hospital/staff/<uuid:user_id>", methods=["PUT"])
@hospital_admin_api
def portal_update_staff(user_id):
    payload = request.get_json(silent=True) or {}
    role = payload.get("staff_role")
    if role not in hs.STAFF_ROLES:
        return invalid(dsp.ValidationError({"staff_role": "Choose a role."}))
    position = payload.get("position")
    position = " ".join(position.split())[:80] or None if isinstance(position, str) else None
    try:
        db = _db()
        if role != "admin" and not _other_admins(db, str(user_id)):
            return jsonify(error="A hospital needs at least one Hospital Administrator. Make someone else an administrator first."), 409
        saved = (db.table(STAFF).update({"staff_role": role, "position": position}).eq("hospital_id", g.hospital_id)
                 .eq("user_id", str(user_id)).execute().data)
        if not saved:
            return jsonify(error="That account is not on your staff list."), 404
        return jsonify(items=_staff_json(db, g.hospital_id))
    except Exception as exc:
        return fail(exc, "update the staff account")


@bp.route("/api/hospital/staff/<uuid:user_id>", methods=["DELETE"])
@hospital_admin_api
def portal_remove_staff(user_id):
    """Take an account off this hospital. The account itself stays; it simply sees no reports any more."""
    if str(user_id) == _user_id():
        return jsonify(error="You cannot remove yourself. Ask another Hospital Administrator."), 409
    try:
        db = _db()
        removed = db.table(STAFF).delete().eq("hospital_id", g.hospital_id).eq("user_id", str(user_id)).execute().data
        if not removed:
            return jsonify(error="That account is not on your staff list."), 404
        return jsonify(items=_staff_json(db, g.hospital_id))
    except Exception as exc:
        return fail(exc, "remove the staff account")


# ---------------------------------------------------------------- shared

def is_hospital_staff(user_id):
    """For page routing only; False when the lookup is not possible."""
    try:
        return bool(staff_hospital_ids(_db(), user_id))
    except Exception:
        return False


def client_options():
    """Labels and rules the Hospital Signal and Hospital Portal scripts need, defined once on the server."""
    return {
        "signalTypes": hs.SIGNAL_TYPES, "genders": hs.GENDERS, "conditions": hs.CONDITIONS, "statuses": hs.STATUSES,
        "portalStatuses": hs.PORTAL_STATUSES, "orgTypes": orgs.ORG_TYPES, "baseLabels": orgs.BASE_LABELS,
        "orgAvailability": orgs.AVAILABILITY, "messageLimit": orgs.MESSAGE_LIMIT, "archiveFilters": hs.ARCHIVE_FILTERS, "transportTypes": hs.TRANSPORT_TYPES,
        "triage": {str(k): v for k, v in hs.TRIAGE.items()}, "final": sorted(hs.FINAL), "availability": hs.AVAILABILITY,
        "staffRoles": hs.STAFF_ROLES, "bayKinds": hs.BAY_KINDS, "bayStored": hs.BAY_STORED,
        "rejectionReasons": list(hs.REJECTION_REASONS), "notesLimit": hs.NOTES_LIMIT, "reasonLimit": hs.REASON_LIMIT,
        "handoffNotesLimit": hs.HANDOFF_NOTES_LIMIT, "minPassword": hs.MIN_PASSWORD, "pageSizes": list(PAGE_SIZES),
        "today": datetime.now(timezone.utc).astimezone(hs.PHT).date().isoformat(),
        "realtime": {"url": current_app.config["SUPABASE_URL"], "key": current_app.config["SUPABASE_KEY"],
                     "table": "hospital_signal_feed"},
    }
