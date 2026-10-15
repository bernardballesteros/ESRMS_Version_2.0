"""Account settings: Display, Notifications, Security & MFA, My Profile and Agency Settings.

Table reads and writes use the service-role client on the server and are always
filtered to the signed-in user's id, so one user can never touch another user's
row. Anything Supabase Auth only lets the user do themselves (MFA, email change,
signing out other sessions) uses the user's own Supabase session instead.
"""
import logging
import time
import uuid
from functools import wraps

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from supabase_auth.errors import AuthError

from app import account as acct
from app import delivery, recovery
from app.dispatch import ValidationError
from app.routes.auth import verified_totp_factors
from app.supabase_client import (
    TOKENS_KEY, SignInRequired, current_session_id, get_supabase, get_supabase_admin, get_user_client,
    store_tokens, upgrade_legacy_session,
)

bp = Blueprint("account", __name__)
log = logging.getLogger(__name__)

USERS = "users"
PREFERENCES = "notification_preferences"
DISPLAY = "user_preferences"
DISPLAY_MIGRATION = "supabase/migrations/011_user_preferences.sql"
EVENTS = "account_security_events"
PUSH = "push_subscriptions"
DELIVERIES = "notification_deliveries"
NOTIFY_EVENTS = "notification_events"
MIGRATION = "supabase/migrations/006_delivery_recovery_sessions.sql"

SECTIONS = {
    "display": "Display",
    "notifications": "Notifications",
    "security": "Security & MFA",
    "profile": "My Profile",
    "agency": "Agency Settings",
}
# public.users.role only allows these; administrators are marked in app_metadata instead.
TABLE_ROLES = ("citizen", "responder", "organization")
# Columns copied from the sign-up metadata when a user has no public.users row yet.
BACKFILL = ("full_name", "phone", "call_sign", "badge_number", "station", "agency_name", "jurisdiction",
            "cad_cert", "psap_phone")


# ---------------------------------------------------------------- helpers

def account_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return jsonify(error="Sign in to manage your account."), 401
        # JSON or a custom header: cross-site forms can send neither without a CORS preflight.
        # (DELETE cannot be sent by a form at all, so it needs no body.)
        if request.method in ("POST", "PUT") and not (request.is_json or request.headers.get("X-ESRMS-Request")):
            return jsonify(error="Expected a JSON request."), 415
        return view(*args, **kwargs)
    return wrapped


def can_manage_agency(user):
    return bool(user) and user.get("role") in current_app.config["AGENCY_ADMIN_ROLES"]


def _db():
    return get_supabase_admin()


def _uid():
    return session["user"]["id"]


def _error_text(exc):
    return getattr(exc, "message", None) or str(exc) or exc.__class__.__name__


def db_error(exc, action):
    log.exception("Account %s failed", action)
    text = _error_text(exc)
    code = getattr(exc, "code", "") or ""
    if code in ("PGRST202", "PGRST204", "PGRST205", "42703", "42P01", "42883") or "Bucket not found" in text:
        return jsonify(error=f"Could not {action}: the database is missing part of the account settings schema. "
                             f"Run supabase/migrations/005_account_settings.sql and {MIGRATION} in the Supabase SQL Editor."), 503
    return jsonify(error=f"Could not {action}: {text}"), 502


def _missing_schema(exc):
    """True when the error is "that table/column/function does not exist yet" (a migration was not run)."""
    return (getattr(exc, "code", "") or "") in ("PGRST202", "PGRST204", "PGRST205", "42703", "42P01", "42883")


def invalid(exc):
    return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422


def sign_in_again(exc):
    return jsonify(error=str(exc), sign_in_required=True), 409


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _auth_user(uid):
    return _db().auth.admin.get_user_by_id(uid).user


_name_column = {}


def _users_has_name():
    """The live public.users table predates schema.sql and has a required `name` column there. Checked once."""
    if "exists" not in _name_column:
        try:
            _db().table(USERS).select("name").limit(1).execute()
            _name_column["exists"] = True
        except Exception as exc:
            if (getattr(exc, "code", "") or "") != "42703":
                raise
            _name_column["exists"] = False
    return _name_column["exists"]


def _ensure_row(uid, auth_user=None):
    """The user's public.users row, created from their sign-up details if it does not exist yet."""
    db = _db()
    rows = db.table(USERS).select("*").eq("id", uid).limit(1).execute().data
    if rows:
        return rows[0]
    auth_user = auth_user or _auth_user(uid)
    meta = auth_user.user_metadata or {}
    row = {"id": uid, "email": auth_user.email}
    for key in BACKFILL:
        if isinstance(meta.get(key), str) and meta[key].strip():
            row[key] = meta[key].strip()
    if meta.get("role") in TABLE_ROLES:
        row["role"] = meta["role"]
    if meta.get("agency_type") in acct.AGENCY_TYPES:
        row["agency_type"] = meta["agency_type"]
    if meta.get("branch") in ("ems", "fire", "law", "sar"):
        row["branch"] = meta["branch"]
    if meta.get("mfa_type") in ("fido", "piv", "totp"):
        row["mfa_type"] = meta["mfa_type"]
    if isinstance(meta.get("ori_code"), str):
        row["ori_code"] = meta["ori_code"].strip()[:9] or None
    if _users_has_name():
        row["name"] = row.get("full_name") or row.get("agency_name") or auth_user.email
    created = db.table(USERS).upsert(row, on_conflict="id").execute().data
    return created[0] if created else row


def _sync_metadata(uid, values):
    """Sign-in reads these from the Auth profile, so keep it in step with public.users."""
    _db().auth.admin.update_user_by_id(uid, {"user_metadata": values})


def _update_session(**values):
    session["user"] = {**session["user"], **values}


def _record_event(event):
    """Security activity log (also queues the security alert). Never blocks the action it describes."""
    try:
        return recovery.record_security_event(_db(), _uid(), event, request.user_agent.string)
    except Exception:
        log.warning("Could not record security event %s", event, exc_info=True)
        return None


def _store_image(folder, column, sync_key=None, owner=None):
    """Validate an uploaded image, store it in the media bucket and save its URL on the owner's row."""
    upload = request.files.get("file")
    try:
        mime, ext = acct.validate_image(upload.read(acct.MAX_IMAGE_BYTES + 1) if upload else b"")
    except ValidationError as exc:
        return jsonify(error=exc.errors["file"], fields=exc.errors), 422
    uid = owner or _uid()
    upload.seek(0)
    data = upload.read()
    try:
        _ensure_row(uid)
        bucket = _db().storage.from_(current_app.config["MEDIA_BUCKET"])
        path = f"{folder}/{uid}.{ext}"
        bucket.upload(path, data, {"content-type": mime, "upsert": "true", "cache-control": "3600"})
        stale = [f"{folder}/{uid}.{e}" for e in acct.IMAGE_TYPES.values() if e != ext]
        try:
            bucket.remove(stale)
        except Exception:
            log.info("Could not remove replaced images", exc_info=True)
        # Version the URL so browsers drop the cached previous image.
        url = f"{bucket.get_public_url(path).rstrip('?')}?v={int(time.time())}"
        saved = _db().table(USERS).update({column: url}).eq("id", uid).execute().data
        if not saved:
            return jsonify(error="The database did not confirm the save."), 502
        if sync_key:
            _sync_metadata(uid, {sync_key: url})
            _update_session(**{sync_key: url})
    except Exception as exc:
        return db_error(exc, "save the image")
    return jsonify(url=url)


def _clear_image(folder, column, sync_key=None, owner=None):
    uid = owner or _uid()
    try:
        saved = _db().table(USERS).update({column: None}).eq("id", uid).execute().data
        if not saved:
            return jsonify(error="The database did not confirm the change."), 502
        if sync_key:
            _sync_metadata(uid, {sync_key: None})
            _update_session(**{sync_key: None})
        try:
            _db().storage.from_(current_app.config["MEDIA_BUCKET"]).remove(
                [f"{folder}/{uid}.{e}" for e in acct.IMAGE_TYPES.values()])
        except Exception:
            log.info("Could not delete the stored image", exc_info=True)
    except Exception as exc:
        return db_error(exc, "remove the image")
    return jsonify(ok=True)


# ---------------------------------------------------------------- pages

@bp.route("/account")
def index():
    return redirect(url_for("account.settings", section="profile"))


@bp.route("/account/<section>")
def settings(section):
    user = session.get("user")
    if not user:
        flash("Please sign in to continue.", "error")
        return redirect(url_for("auth.login"))
    if section not in SECTIONS:
        return redirect(url_for("account.settings", section="profile"))
    return render_template(
        "account/settings.html",
        section=section,
        sections=SECTIONS,
        display_scales=acct.DISPLAY_SCALES,
        display_default=acct.DEFAULT_DISPLAY_SCALE,
        account_config={
            "section": section,
            "role": user.get("role"),
            "canManageAgency": can_manage_agency(user),
            "systemAdmin": user.get("role") == "admin",
            "userId": user["id"],
            "agencyStatuses": acct.AGENCY_STATUSES,
            "googleSecurityUrl": "https://myaccount.google.com/security",
            "serviceWorker": url_for("main.service_worker"),
            "agencyTypes": acct.AGENCY_TYPES,
            "passwordRules": acct.password_requirements(user.get("role")),
            "minPassword": acct.min_password(user.get("role")),
            "categories": {k: {"label": v[0], "description": v[1]} for k, v in acct.NOTIFICATION_CATEGORIES.items()},
            "securityEvents": acct.SECURITY_EVENTS,
            "signOutUrl": url_for("auth.logout"),
            "api": {
                "profile": url_for("account.get_profile"),
                "avatar": url_for("account.upload_avatar"),
                "agency": url_for("account.get_agency"),
                "logo": url_for("account.upload_logo"),
                "security": url_for("account.get_security"),
                "password": url_for("account.change_password"),
                "mfaEnroll": url_for("account.mfa_enroll"),
                "mfaVerify": url_for("account.mfa_verify"),
                "mfaDisable": url_for("account.mfa_disable"),
                "mfaFactors": "/api/account/mfa/factors",
                "recoveryCodes": url_for("account.mfa_recovery_codes"),
                "revokeSessions": url_for("account.revoke_sessions"),
                "revokeAllSessions": url_for("account.revoke_all_sessions"),
                "sessions": "/api/account/sessions",
                "signOut": url_for("auth.logout"),
                "agencies": url_for("account.list_agencies"),
                "personnel": url_for("account.add_personnel"),
                "notifications": url_for("account.get_notifications"),
                "pushSubscribe": url_for("account.push_subscribe"),
                "pushUnsubscribe": url_for("account.push_unsubscribe"),
                "notificationTest": url_for("account.test_notification"),
                "display": url_for("account.get_display"),
            },
        },
    )


# ---------------------------------------------------------------- my profile

def _profile_json(row, auth_user):
    user = session["user"]
    name = row.get("full_name") or user.get("name") or ""
    if getattr(auth_user, "banned_until", None):
        status = "Suspended"
    elif auth_user.email_confirmed_at:
        status = "Active"
    else:
        status = "Pending email confirmation"
    return {
        "full_name": name,
        "email": auth_user.email,
        "pending_email": getattr(auth_user, "new_email", None) or None,
        "phone": row.get("phone") or "",
        "avatar_url": row.get("avatar_url"),
        "initials": acct.initials(name or auth_user.email),
        "role": user.get("role"),
        "role_label": acct.ROLE_LABELS.get(user.get("role"), "User"),
        "agency_name": row.get("agency_name") or row.get("station") or None,
        "status": status,
        "member_since": str(auth_user.created_at)[:10] if auth_user.created_at else None,
        "can_change_email": TOKENS_KEY in session,
    }


@bp.route("/api/account/profile")
@account_api
def get_profile():
    uid = _uid()
    try:
        auth_user = _auth_user(uid)
        row = _ensure_row(uid, auth_user)
        # A confirmed email change lands in Auth first; bring the table and session along.
        if auth_user.email and row.get("email") != auth_user.email:
            _db().table(USERS).update({"email": auth_user.email}).eq("id", uid).execute()
            row["email"] = auth_user.email
        if session["user"].get("email") != auth_user.email:
            _update_session(email=auth_user.email)
    except Exception as exc:
        return db_error(exc, "load your profile")
    return _no_store(jsonify(profile=_profile_json(row, auth_user)))


@bp.route("/api/account/profile", methods=["PUT"])
@account_api
def update_profile():
    try:
        values = acct.validate_profile(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return invalid(exc)
    uid = _uid()
    try:
        auth_user = _auth_user(uid)
        _ensure_row(uid, auth_user)
    except Exception as exc:
        return db_error(exc, "load your profile")

    email_pending = False
    new_email = values.pop("email")
    if new_email != (auth_user.email or "").lower():
        # Supabase Auth owns the address: it checks it is free and emails a confirmation link.
        try:
            res = get_user_client().auth.update_user(
                {"email": new_email}, {"email_redirect_to": url_for("auth.callback", _external=True)})
        except SignInRequired as exc:
            return sign_in_again(exc)
        except AuthError as exc:
            return jsonify(error="Please fix the highlighted fields.", fields={"email": _error_text(exc)}), 422
        _record_event("email_change_requested")
        if (res.user.email or "").lower() == new_email:
            values["email"] = new_email
        else:
            email_pending = True

    try:
        _sync_metadata(uid, {"full_name": values["full_name"], "phone": values["phone"]})
        if _users_has_name():
            values["name"] = values["full_name"]
        saved = _db().table(USERS).update(values).eq("id", uid).execute().data
        if not saved:
            return jsonify(error="The database did not confirm the save."), 502
        auth_user = _auth_user(uid)
    except Exception as exc:
        return db_error(exc, "save your profile")

    _update_session(name=values["full_name"], email=auth_user.email)
    message = "Profile saved."
    if email_pending:
        message = f"Profile saved. Confirm the link sent to {new_email} to finish changing your email address."
    return jsonify(profile=_profile_json(saved[0], auth_user), message=message)


@bp.route("/api/account/profile/avatar", methods=["POST"])
@account_api
def upload_avatar():
    return _store_image("avatars", "avatar_url", sync_key="avatar_url")


@bp.route("/api/account/profile/avatar", methods=["DELETE"])
@account_api
def delete_avatar():
    return _clear_image("avatars", "avatar_url", sync_key="avatar_url")


# ---------------------------------------------------------------- agency settings
#
# An agency is the organization account that registered it (its public.users row).
#   * that account (AGENCY_ADMIN_ROLES) edits its own agency and nothing else;
#   * personnel assigned to it (users.agency_id) may view it, read-only;
#   * system administrators pick any agency and may also set its code and verification status.

AGENCY_COLUMNS = ("id", "agency_name", "ori_code", "agency_type", "jurisdiction", "psap_phone", "cad_cert",
                  "agency_email", "agency_address", "agency_description", "agency_logo_url", "status", "updated_at")


def _agency_json(row):
    out = {k: row.get(k) for k in AGENCY_COLUMNS}
    out["agency_type_label"] = acct.AGENCY_TYPES.get(row.get("agency_type"))
    return out


def _is_system_admin(user):
    return user.get("role") == "admin"


def _organization_user(agency_id):
    """The Auth account behind an agency id, or None if that id is not an organization account."""
    try:
        agency_id = str(uuid.UUID(str(agency_id)))
    except ValueError:
        return None
    try:
        user = _auth_user(agency_id)
    except AuthError:
        return None
    return user if user and (user.user_metadata or {}).get("role") == "organization" else None


class AgencyDenied(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def agency_scope():
    """Which agency this request is about and what the caller may do with it.

    Returns {"id", "can_edit", "system_admin", "auth_user"}; raises AgencyDenied otherwise.
    """
    user = session["user"]
    requested = request.args.get("agency")
    if _is_system_admin(user):
        if not requested:
            raise AgencyDenied("Choose an agency to manage.", 400)
        org = _organization_user(requested)
        if not org:
            raise AgencyDenied("That agency does not exist.", 404)
        return {"id": org.id, "can_edit": True, "system_admin": True, "auth_user": org}
    if requested and requested != user["id"]:
        raise AgencyDenied("You can only access your own agency.")
    if can_manage_agency(user):
        return {"id": user["id"], "can_edit": True, "system_admin": False, "auth_user": None}
    try:
        rows = _db().table(USERS).select("agency_id").eq("id", user["id"]).limit(1).execute().data
    except Exception as exc:
        if not _missing_schema(exc):
            raise
        rows = []   # agency membership arrives with migration 006; until then nobody is assigned
    if rows and rows[0].get("agency_id"):
        return {"id": rows[0]["agency_id"], "can_edit": False, "system_admin": False, "auth_user": None}
    raise AgencyDenied("Your account is not assigned to an agency. Ask your agency's administrator to add you.")


def agency_api(edit=False):
    def decorator(view):
        @wraps(view)
        @account_api
        def wrapped(*args, **kwargs):
            try:
                scope = agency_scope()
            except AgencyDenied as exc:
                return jsonify(error=str(exc)), exc.status
            except Exception as exc:
                return db_error(exc, "check your agency access")
            if edit and not scope["can_edit"]:
                return jsonify(error="Only your agency's administrator can change agency settings."), 403
            return view(scope, *args, **kwargs)
        return wrapped
    return decorator


@bp.route("/api/account/agencies")
@account_api
def list_agencies():
    """Agency selector for system administrators: the real organization accounts."""
    if not _is_system_admin(session["user"]):
        return jsonify(error="Only system administrators can list agencies."), 403
    try:
        items, page = [], 1
        while True:
            batch = _db().auth.admin.list_users(page=page, per_page=200)
            for u in batch:
                meta = u.user_metadata or {}
                if meta.get("role") == "organization":
                    items.append({"id": u.id, "agency_name": meta.get("agency_name") or u.email,
                                  "ori_code": meta.get("ori_code"), "email": u.email})
            if len(batch) < 200:
                break
            page += 1
    except Exception as exc:
        return db_error(exc, "load the agencies")
    return _no_store(jsonify(items=sorted(items, key=lambda a: a["agency_name"].lower())))


def _personnel(agency_id):
    return (_db().table(USERS).select("id, full_name, email, branch, station").eq("agency_id", agency_id)
            .order("full_name").execute().data)


@bp.route("/api/account/agency")
@agency_api()
def get_agency(scope):
    try:
        row = _ensure_row(scope["id"], scope["auth_user"])
        personnel, personnel_ready = [], True
        if scope["can_edit"]:
            try:
                personnel = _personnel(scope["id"])
            except Exception as exc:
                if not _missing_schema(exc):
                    raise
                personnel_ready = False   # migration 006 not applied yet; the settings themselves still work
    except Exception as exc:
        return db_error(exc, "load the agency settings")
    return _no_store(jsonify(agency=_agency_json(row), can_edit=scope["can_edit"], system_admin=scope["system_admin"],
                             personnel=personnel, personnel_ready=personnel_ready))


@bp.route("/api/account/agency", methods=["PUT"])
@agency_api(edit=True)
def update_agency(scope):
    payload = request.get_json(silent=True) or {}
    try:
        values = acct.validate_agency(payload)
        if scope["system_admin"]:
            values.update(acct.validate_agency_admin_fields(payload))
    except ValidationError as exc:
        return invalid(exc)
    agency_id = scope["id"]
    synced = {k: values[k] for k in ("agency_name", "agency_type", "jurisdiction", "psap_phone")}
    if scope["system_admin"]:
        synced["ori_code"] = values["ori_code"]
    try:
        _ensure_row(agency_id, scope["auth_user"])
        _sync_metadata(agency_id, synced)
        saved = _db().table(USERS).update(values).eq("id", agency_id).execute().data
    except Exception as exc:
        return db_error(exc, "save the agency settings")
    if not saved:
        return jsonify(error="The database did not confirm the save."), 502
    if agency_id == _uid():
        _update_session(agency_name=values["agency_name"], agency_type=values["agency_type"],
                        jurisdiction=values["jurisdiction"])
    return jsonify(agency=_agency_json(saved[0]), message="Agency settings saved.")


@bp.route("/api/account/agency/logo", methods=["POST"])
@agency_api(edit=True)
def upload_logo(scope):
    return _store_image("logos", "agency_logo_url", owner=scope["id"])


@bp.route("/api/account/agency/logo", methods=["DELETE"])
@agency_api(edit=True)
def delete_logo(scope):
    return _clear_image("logos", "agency_logo_url", owner=scope["id"])


@bp.route("/api/account/agency/personnel", methods=["POST"])
@agency_api(edit=True)
def add_personnel(scope):
    """Assign a responder account to this agency so they can view its information."""
    email = (request.get_json(silent=True) or {}).get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if not acct.EMAIL_RE.match(email):
        return jsonify(error="Enter the responder's account email.", fields={"email": "Enter a valid email address."}), 422
    try:
        match, page = None, 1
        while match is None:
            batch = _db().auth.admin.list_users(page=page, per_page=200)
            match = next((u for u in batch if (u.email or "").lower() == email), None)
            if match or len(batch) < 200:
                break
            page += 1
        # The same answer whether the account is missing or not a responder: no account probing.
        if not match or (match.user_metadata or {}).get("role") != "responder" or (match.app_metadata or {}).get("role") == "admin":
            return jsonify(error="No responder account with that email was found.",
                           fields={"email": "No responder account with that email."}), 404
        row = _ensure_row(match.id, match)
        if row.get("agency_id") and row["agency_id"] != scope["id"]:
            return jsonify(error="That responder is already assigned to another agency."), 409
        _db().table(USERS).update({"agency_id": scope["id"]}).eq("id", match.id).execute()
        personnel = _personnel(scope["id"])
    except Exception as exc:
        return db_error(exc, "add the responder")
    return jsonify(personnel=personnel, message="Responder added. They can now view this agency's information.")


@bp.route("/api/account/agency/personnel/<uuid:user_id>", methods=["DELETE"])
@agency_api(edit=True)
def remove_personnel(scope, user_id):
    try:
        removed = (_db().table(USERS).update({"agency_id": None})
                   .eq("id", str(user_id)).eq("agency_id", scope["id"]).execute().data)
        personnel = _personnel(scope["id"])
    except Exception as exc:
        return db_error(exc, "remove the responder")
    if not removed:
        return jsonify(error="That responder is not assigned to this agency."), 404
    return jsonify(personnel=personnel, message="Responder removed from the agency.")


# ---------------------------------------------------------------- security & MFA

PROVIDER_LABELS = {"email": "Email and password", "google": "Google"}


def _providers(auth_user):
    return list((auth_user.app_metadata or {}).get("providers") or [])


def _totp_factors(user):
    return [f for f in (user.factors or []) if f.factor_type == "totp" and f.status == "verified"]


def _factor_json(f):
    return {"id": f.id, "name": f.friendly_name or "Authenticator",
            "created_at": f.created_at.isoformat() if f.created_at else None}


def _sessions(uid):
    """Real sessions from Supabase Auth (migration 006). Never invented: unavailable means an empty list and a reason."""
    try:
        rows = _db().rpc("account_sessions", {"p_user_id": uid}).execute().data or []
    except Exception as exc:
        log.info("Session list unavailable", exc_info=True)
        missing = (getattr(exc, "code", "") or "") in ("PGRST202", "42883")
        return {"available": False, "items": [],
                "reason": (f"Run {MIGRATION} to list your sessions." if missing
                           else "The session list could not be loaded right now.")}
    current = current_session_id()
    return {"available": True, "reason": None, "items": [{
        "id": r["id"], "user_agent": r.get("user_agent"), "created_at": r.get("created_at"),
        "last_active_at": r.get("last_active_at"), "mfa_verified": r.get("aal") == "aal2", "current": r["id"] == current,
    } for r in rows]}


@bp.route("/api/account/security")
@account_api
def get_security():
    uid = _uid()
    if TOKENS_KEY not in session:
        # Signed in before ESRMS kept the Supabase session: get one now instead of forcing a re-login.
        try:
            upgrade_legacy_session()
        except SignInRequired:
            pass
    try:
        auth_user = _auth_user(uid)
    except Exception as exc:
        return db_error(exc, "load your security settings")
    factors = _totp_factors(auth_user)
    if session["user"].get("mfa_enabled") != bool(factors):
        _update_session(mfa_enabled=bool(factors))
    events, events_error = [], None
    try:
        events = (_db().table(EVENTS).select("id, event, user_agent, created_at").eq("user_id", uid)
                  .order("id", desc=True).limit(8).execute().data)
    except Exception as exc:
        log.warning("Could not load security events", exc_info=True)
        events_error = _error_text(exc)
    try:
        codes = recovery.recovery_code_status(_db(), uid)
        codes["available"] = True
    except Exception:
        codes = {"available": False, "remaining": 0, "generated_at": None}
    providers = _providers(auth_user)
    return _no_store(jsonify(
        has_password="email" in providers,
        providers=[{"id": p, "label": PROVIDER_LABELS.get(p, p.capitalize())} for p in providers],
        can_manage=TOKENS_KEY in session,
        mfa={
            "enabled": bool(factors),
            "enrolled_at": factors[0].created_at.isoformat() if factors else None,
            "factors": [_factor_json(f) for f in factors],
            "max_factors": acct.MAX_FACTORS,
            "recovery_codes": codes,
        },
        sessions=_sessions(uid),
        current_session={
            "id": current_session_id(),
            "user_agent": request.user_agent.string,
            "last_sign_in_at": auth_user.last_sign_in_at.isoformat() if auth_user.last_sign_in_at else None,
        },
        events=events,
        events_error=events_error,
    ))


@bp.route("/api/account/security/events")
@account_api
def security_events():
    """Security events newer than the last one this browser has shown (Security alerts notification)."""
    after = request.args.get("after", 0, type=int)
    try:
        rows = (_db().table(EVENTS).select("id, event, created_at").eq("user_id", _uid())
                .gt("id", after).order("id", desc=True).limit(5).execute().data)
    except Exception as exc:
        return db_error(exc, "load security alerts")
    return _no_store(jsonify(items=[{**r, "label": acct.SECURITY_EVENTS.get(r["event"], r["event"])} for r in rows]))


@bp.route("/api/account/password", methods=["POST"])
@account_api
def change_password():
    user = session["user"]
    try:
        current, new = acct.validate_password_change(request.get_json(silent=True) or {}, user.get("role"))
    except ValidationError as exc:
        return invalid(exc)

    # Prove the current password with a real sign-in; nothing is compared or stored here.
    verifier = get_supabase()
    try:
        signed_in = verifier.auth.sign_in_with_password({"email": user["email"], "password": current})
    except AuthError as exc:
        text = _error_text(exc)
        if "invalid login credentials" in text.lower():
            text = "Current password is incorrect."
        return jsonify(error="Please fix the highlighted fields.", fields={"current_password": text}), 422

    try:
        if verified_totp_factors(signed_in.user):
            # With MFA on, Supabase only accepts a password change from the MFA-verified session.
            updater = get_user_client()
            verifier.auth.admin.sign_out(signed_in.session.access_token, "local")
        else:
            updater = verifier
        updater.auth.update_user({"password": new, "current_password": current})
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error="Please fix the highlighted fields.", fields={"new_password": _error_text(exc)}), 422

    current_session = updater.auth.get_session()
    warning = None
    if current_session:
        store_tokens(current_session)
        try:
            updater.auth.admin.sign_out(current_session.access_token, "others")
        except Exception:
            log.warning("Could not sign out other sessions after a password change", exc_info=True)
            warning = "Other devices could not be signed out automatically. Use “Sign out other sessions”."
    event_id = _record_event("password_changed")
    return jsonify(ok=True, event_id=event_id, warning=warning,
                   message="Password changed. Other devices have been signed out." if not warning else "Password changed.")


def _verify_any_factor(client, factors, code):
    """Accept the code if any of the account's authenticators produced it. Raises AuthError otherwise."""
    last = None
    for f in factors:
        try:
            client.auth.mfa.challenge_and_verify({"factor_id": f.id, "code": code})
            return f
        except AuthError as exc:
            last = exc
    raise last or AuthError("No authenticator is enrolled.", None)


BAD_CODE = {"code": "That code was not accepted. Try the current code."}


@bp.route("/api/account/mfa/enroll", methods=["POST"])
@account_api
def mfa_enroll():
    """Start enrolling an authenticator (the first one, or a backup). Nothing is on until /mfa/verify accepts a code."""
    name = acct.clean_factor_name((request.get_json(silent=True) or {}).get("name"))
    try:
        client = get_user_client()
        factors = client.auth.get_user().user.factors or []
        verified = [f for f in factors if f.factor_type == "totp" and f.status == "verified"]
        if len(verified) >= acct.MAX_FACTORS:
            return jsonify(error=f"You can have up to {acct.MAX_FACTORS} authenticators. Remove one first."), 409
        # Abandoned earlier attempts would block a new enrollment.
        for f in factors:
            if f.factor_type == "totp" and f.status != "verified":
                client.auth.mfa.unenroll({"factor_id": f.id})
        if any((f.friendly_name or "") == name for f in verified):
            name = f"{name} {len(verified) + 1}"
        enrolled = client.auth.mfa.enroll({"factor_type": "totp", "issuer": "ESRMS", "friendly_name": name})
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        text = _error_text(exc)
        if "mfa" in text.lower() and "disabled" in text.lower():
            text = ("Authenticator-app MFA is turned off for this Supabase project. Enable TOTP under "
                    "Authentication > Sign In / Providers > Multi-Factor in the Supabase dashboard.")
        elif "aal2" in text.lower():
            text = "Sign out and sign in again with your authenticator code to add another authenticator."
        return jsonify(error=text), 400
    # The secret is shown once so the user can add it to their authenticator; it is never stored or logged here.
    return _no_store(jsonify(factor_id=enrolled.id, qr_code=enrolled.totp.qr_code, secret=enrolled.totp.secret))


@bp.route("/api/account/mfa/verify", methods=["POST"])
@account_api
def mfa_verify():
    body = request.get_json(silent=True) or {}
    factor_id, code = body.get("factor_id"), str(body.get("code", "")).replace(" ", "")
    if not isinstance(factor_id, str) or not factor_id:
        return jsonify(error="Start the MFA setup again."), 422
    if not acct.valid_totp_code(code):
        return jsonify(error="Enter the 6-digit code from your authenticator app.",
                       fields={"code": "Enter the 6-digit code."}), 422
    try:
        client = get_user_client()
        had_mfa = bool(_totp_factors(client.auth.get_user().user))
        client.auth.mfa.challenge_and_verify({"factor_id": factor_id, "code": code})
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc), fields=BAD_CODE), 422
    store_tokens(client.auth.get_session())  # now an MFA-verified session
    _update_session(mfa_enabled=True)
    event_id = _record_event("mfa_factor_added" if had_mfa else "mfa_enabled")
    message = ("Backup authenticator added." if had_mfa else
               "Multi-factor authentication is on. You will be asked for a code each time you sign in. "
               "Generate recovery codes now in case you lose this device.")
    return jsonify(ok=True, event_id=event_id, message=message)


def _after_factor_removal(client, remaining):
    refreshed = client.auth.refresh_session().session
    if refreshed:
        store_tokens(refreshed)
    if not remaining:
        _update_session(mfa_enabled=False)
        try:
            recovery.clear_recovery_codes(_db(), _uid())
        except Exception:
            log.warning("Could not clear recovery codes", exc_info=True)


@bp.route("/api/account/mfa/factors/<factor_id>", methods=["DELETE"])
@account_api
def mfa_remove_factor(factor_id):
    """Remove one authenticator. Needs a current code from any authenticator on the account."""
    code = str((request.get_json(silent=True) or {}).get("code", "")).replace(" ", "")
    if not acct.valid_totp_code(code):
        return jsonify(error="Enter a current 6-digit authenticator code.", fields={"code": "Enter the 6-digit code."}), 422
    try:
        client = get_user_client()
        factors = _totp_factors(client.auth.get_user().user)
        if not any(f.id == factor_id for f in factors):
            return jsonify(error="That authenticator is not on your account."), 404
        try:
            _verify_any_factor(client, factors, code)
        except AuthError as exc:
            return jsonify(error=_error_text(exc), fields=BAD_CODE), 422
        client.auth.mfa.unenroll({"factor_id": factor_id})
        _after_factor_removal(client, remaining=len(factors) - 1)
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc)), 400
    last = len(factors) == 1
    event_id = _record_event("mfa_disabled" if last else "mfa_factor_removed")
    return jsonify(ok=True, event_id=event_id,
                   message="Multi-factor authentication has been turned off." if last else "Authenticator removed.")


@bp.route("/api/account/mfa/disable", methods=["POST"])
@account_api
def mfa_disable():
    code = str((request.get_json(silent=True) or {}).get("code", "")).replace(" ", "")
    if not acct.valid_totp_code(code):
        return jsonify(error="Enter the 6-digit code from your authenticator app to turn MFA off.",
                       fields={"code": "Enter the 6-digit code."}), 422
    try:
        client = get_user_client()
        factors = _totp_factors(client.auth.get_user().user)
        if not factors:
            _update_session(mfa_enabled=False)
            return jsonify(error="Multi-factor authentication is not enabled."), 409
        try:
            # A fresh code proves the person asking still holds an authenticator.
            _verify_any_factor(client, factors, code)
        except AuthError as exc:
            return jsonify(error=_error_text(exc), fields=BAD_CODE), 422
        for f in factors:
            client.auth.mfa.unenroll({"factor_id": f.id})
        _after_factor_removal(client, remaining=0)
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc)), 400
    event_id = _record_event("mfa_disabled")
    return jsonify(ok=True, event_id=event_id, message="Multi-factor authentication has been turned off.")


@bp.route("/api/account/mfa/recovery-codes", methods=["POST"])
@account_api
def mfa_recovery_codes():
    """Generate a new set of single-use recovery codes. They are returned once and only their hashes are kept."""
    code = str((request.get_json(silent=True) or {}).get("code", "")).replace(" ", "")
    if not acct.valid_totp_code(code):
        return jsonify(error="Enter a current 6-digit authenticator code.", fields={"code": "Enter the 6-digit code."}), 422
    try:
        client = get_user_client()
        factors = _totp_factors(client.auth.get_user().user)
        if not factors:
            return jsonify(error="Turn on multi-factor authentication before generating recovery codes."), 409
        try:
            _verify_any_factor(client, factors, code)
        except AuthError as exc:
            return jsonify(error=_error_text(exc), fields=BAD_CODE), 422
        store_tokens(client.auth.get_session())
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc)), 400
    try:
        codes = recovery.replace_recovery_codes(_db(), _uid())
    except Exception as exc:
        return db_error(exc, "generate recovery codes")
    event_id = _record_event("recovery_codes_generated")
    return _no_store(jsonify(codes=codes, event_id=event_id,
                             message="New recovery codes generated. Any earlier codes no longer work."))


@bp.route("/api/account/sessions/revoke", methods=["POST"])
@account_api
def revoke_sessions():
    """Sign out every other session of this account; this browser stays signed in."""
    try:
        client = get_user_client()
        client.auth.admin.sign_out(client.auth.get_session().access_token, "others")
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc)), 400
    event_id = _record_event("sessions_revoked")
    return jsonify(ok=True, event_id=event_id, message="Every other session was signed out.")


@bp.route("/api/account/sessions/<uuid:session_id>/revoke", methods=["POST"])
@account_api
def revoke_session(session_id):
    """Sign out one other device. Only sessions that belong to the signed-in user can be removed."""
    if str(session_id) == current_session_id():
        return jsonify(error="That is this device. Use “Sign out this device” instead."), 409
    try:
        removed = _db().rpc("revoke_account_session", {"p_user_id": _uid(), "p_session_id": str(session_id)}).execute().data
    except Exception as exc:
        return db_error(exc, "sign out that device")
    if not removed:
        return jsonify(error="That session no longer exists."), 404
    event_id = _record_event("session_revoked")
    return jsonify(ok=True, event_id=event_id, message="That device was signed out.")


@bp.route("/api/account/sessions/revoke-all", methods=["POST"])
@account_api
def revoke_all_sessions():
    """Sign out everywhere, including this browser."""
    try:
        client = get_user_client()
        client.auth.admin.sign_out(client.auth.get_session().access_token, "global")
    except SignInRequired as exc:
        return sign_in_again(exc)
    except AuthError as exc:
        return jsonify(error=_error_text(exc)), 400
    _record_event("all_sessions_revoked")
    session.clear()
    return jsonify(ok=True, redirect=url_for("auth.login"), message="All devices were signed out.")


# ---------------------------------------------------------------- display

def _load_display_row(uid):
    rows = _db().table(DISPLAY).select("display_scale, updated_at").eq("user_id", uid).limit(1).execute().data
    return rows[0] if rows else None


def _remember_display(scale, retry_at=None):
    """Keep the size in the session so every page is drawn at it straight away, without a query."""
    session["display"] = {"uid": _uid(), "scale": scale, **({"retry": retry_at} if retry_at else {})}


def display_scale_for(user):
    """The signed-in user's display size for the page being rendered; None when nobody is signed in.

    Read from the database once per sign-in (and again after a failed read), then from the session.
    Keyed by user id, so another account signing in on this browser never inherits it.
    """
    if not user:
        return None
    cached = session.get("display") or {}
    if cached.get("uid") == user["id"] and cached.get("retry", float("inf")) > time.time():
        return acct.display_scale_from_row({"display_scale": cached.get("scale")})
    try:
        scale = acct.display_scale_from_row(_load_display_row(user["id"]))
        _remember_display(scale)
    except Exception:
        # Migration 011 not applied, or the database is unreachable: use the default and look again later.
        log.warning("Display size unavailable; using the default", exc_info=True)
        scale = acct.DEFAULT_DISPLAY_SCALE
        _remember_display(scale, retry_at=int(time.time()) + 300)
    return scale


def _display_error(exc, action):
    if _missing_schema(exc):
        log.exception("Account %s failed", action)
        return jsonify(error=f"Could not {action}: the database does not have the display settings table yet. "
                             f"Run {DISPLAY_MIGRATION} in the Supabase SQL Editor."), 503
    return db_error(exc, action)


def _display_json(row):
    return {"display_scale": acct.display_scale_from_row(row), "saved": bool(row), "updated_at": (row or {}).get("updated_at"),
            "scales": list(acct.DISPLAY_SCALES), "default": acct.DEFAULT_DISPLAY_SCALE}


@bp.route("/api/account/display")
@account_api
def get_display():
    try:
        row = _load_display_row(_uid())
    except Exception as exc:
        return _display_error(exc, "load your display settings")
    _remember_display(acct.display_scale_from_row(row))
    return _no_store(jsonify(_display_json(row)))


@bp.route("/api/account/display", methods=["PUT"])
@account_api
def update_display():
    try:
        scale = acct.validate_display_scale(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify(error=exc.errors["display_scale"], fields=exc.errors), 422
    try:
        saved = _db().table(DISPLAY).upsert({"user_id": _uid(), "display_scale": scale}, on_conflict="user_id").execute().data
    except Exception as exc:
        return _display_error(exc, "save your display size")
    if not saved:
        return jsonify(error="The database did not confirm the save."), 502
    _remember_display(scale)
    return jsonify(**_display_json(saved[0]), message=f"Display size set to {scale}%.")


# ---------------------------------------------------------------- notifications

def _delivery_status(auth_user=None):
    cfg = current_app.config
    email_ok, push_ok = delivery.email_configured(cfg), delivery.push_configured(cfg)
    email = {"available": email_ok, "reason": None}
    if not email_ok:
        email["reason"] = "Email delivery is not configured on this server yet (RESEND_API_KEY and EMAIL_FROM)."
    elif auth_user is not None and not auth_user.email_confirmed_at:
        email = {"available": False, "reason": "Confirm your email address first; alerts are only sent to verified addresses."}
    push = {"available": push_ok, "public_key": cfg["VAPID_PUBLIC_KEY"] if push_ok else None, "reason": None}
    if not push_ok:
        push["reason"] = "Push is not configured on this server yet (VAPID keys)."
    return {"email": email, "push": push}


def _notifications_json(row, auth_user=None, extra=True):
    uid = _uid()
    out = {
        "preferences": acct.preferences_from_row(row),
        "saved": bool(row),
        "updated_at": (row or {}).get("updated_at"),
        "delivery": _delivery_status(auth_user),
        "email": auth_user.email if auth_user is not None else session["user"].get("email"),
        "devices": [],
        "recent": [],
    }
    if not extra:
        return out
    try:
        db = _db()
        out["devices"] = [
            {"endpoint": s["endpoint"], "user_agent": s.get("user_agent"), "created_at": s["created_at"],
             "last_success_at": s.get("last_success_at")}
            for s in db.table(PUSH).select("endpoint, user_agent, created_at, last_success_at").eq("user_id", uid)
            .order("created_at", desc=True).execute().data]
        recent = (db.table(DELIVERIES).select("id, event_id, channel, status, attempts, last_error, sent_at, created_at")
                  .eq("user_id", uid).order("id", desc=True).limit(8).execute().data)
        titles = {}
        if recent:
            for e in (db.table(NOTIFY_EVENTS).select("id, title, body, category, code")
                      .in_("id", list({r["event_id"] for r in recent})).execute().data):
                titles[e["id"]] = delivery.message_for(e)[0] if e["category"] == "security_alerts" else e["title"]
        out["recent"] = [{**r, "title": titles.get(r["event_id"], "Notification")} for r in recent]
    except Exception as exc:
        # Preferences still work; email and push stay off until their tables can be read.
        log.warning("Delivery details unavailable", exc_info=True)
        missing = (getattr(exc, "code", "") or "") in ("PGRST204", "PGRST205", "42703", "42P01")
        reason = (f"Run {MIGRATION} in the Supabase SQL Editor to enable this." if missing
                  else "Delivery details could not be loaded right now.")
        out["delivery"]["email"] = {"available": False, "reason": reason}
        out["delivery"]["push"] = {"available": False, "public_key": None, "reason": reason}
    return out


def load_preferences(uid):
    rows = _db().table(PREFERENCES).select("*").eq("user_id", uid).limit(1).execute().data
    return rows[0] if rows else None


@bp.route("/api/account/notifications")
@account_api
def get_notifications():
    try:
        row = load_preferences(_uid())
        auth_user = _auth_user(_uid())
    except Exception as exc:
        return db_error(exc, "load your notification preferences")
    return _no_store(jsonify(_notifications_json(row, auth_user)))


@bp.route("/api/account/notifications", methods=["PUT"])
@account_api
def update_notifications():
    try:
        values = acct.validate_preferences(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify(error="Send every preference as on or off.", fields=exc.errors), 422
    try:
        auth_user = _auth_user(_uid())
        if values["email_enabled"] and not _delivery_status(auth_user)["email"]["available"]:
            return jsonify(error=_delivery_status(auth_user)["email"]["reason"]), 409
        try:
            saved = _db().table(PREFERENCES).upsert({"user_id": _uid(), **values}, on_conflict="user_id").execute().data
        except Exception as exc:
            if not _missing_schema(exc):
                raise
            # Migration 006 (email_enabled) not applied yet: the category preferences can still be saved.
            older = {k: v for k, v in values.items() if k != "email_enabled"}
            saved = _db().table(PREFERENCES).upsert({"user_id": _uid(), **older}, on_conflict="user_id").execute().data
    except Exception as exc:
        return db_error(exc, "save your notification preferences")
    if not saved:
        return jsonify(error="The database did not confirm the save."), 502
    return jsonify(**_notifications_json(saved[0], auth_user), message="Notification preferences saved.")


@bp.route("/api/account/push/subscribe", methods=["POST"])
@account_api
def push_subscribe():
    """Save this browser's push subscription for the signed-in user."""
    if not delivery.push_configured(current_app.config):
        return jsonify(error="Push is not configured on this server yet (VAPID keys)."), 503
    try:
        sub = acct.validate_push_subscription((request.get_json(silent=True) or {}).get("subscription") or {})
    except ValidationError as exc:
        return jsonify(error=exc.errors["subscription"]), 422
    row = {**sub, "user_id": _uid(), "user_agent": (request.user_agent.string or "")[:300] or None, "failure_count": 0}
    try:
        # One row per browser: if someone else was signed in on it before, it now belongs to this user.
        saved = _db().table(PUSH).upsert(row, on_conflict="endpoint").execute().data
    except Exception as exc:
        return db_error(exc, "save this device")
    if not saved:
        return jsonify(error="The database did not confirm the save."), 502
    return jsonify(ok=True, message="Push notifications are on for this device.")


@bp.route("/api/account/push/unsubscribe", methods=["POST"])
@account_api
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return jsonify(error="No device subscription was given."), 422
    try:
        _db().table(PUSH).delete().eq("endpoint", endpoint).eq("user_id", _uid()).execute()
    except Exception as exc:
        return db_error(exc, "remove this device")
    return jsonify(ok=True, message="Push notifications are off for this device.")


@bp.route("/api/account/notifications/test", methods=["POST"])
@account_api
def test_notification():
    """Send a clearly labelled test now. Reports only what the provider actually accepted."""
    body = request.get_json(silent=True) or {}
    channel = body.get("channel")
    if channel not in ("email", "push"):
        return jsonify(error="Choose email or push."), 422
    now = time.time()
    if now - session.get("last_test", 0) < 20:
        return jsonify(error="Wait a few seconds before sending another test."), 429
    try:
        auth_user = _auth_user(_uid())
        status = _delivery_status(auth_user)[channel]
        if not status["available"]:
            return jsonify(error=status["reason"]), 409
        session["last_test"] = int(now)
        endpoint = body.get("endpoint") if isinstance(body.get("endpoint"), str) else None
        result = delivery.send_test(_db(), current_app.config, _uid(), auth_user.email, channel, endpoint)
    except delivery.DeliveryError as exc:
        return jsonify(error=f"The test was not accepted: {exc}"), 502
    except Exception as exc:
        return db_error(exc, "send the test")
    if not result["accepted"]:
        detail = "; ".join(result.get("errors") or []) or "No subscribed device was found. Switch push on for this device first."
        return jsonify(error=f"The test was not accepted: {detail}"), 502
    if channel == "email":
        message = f"The email provider accepted a test message for {auth_user.email}. Delivery to the inbox can take a minute."
    else:
        message = "The push service accepted the test. It appears when the device is online and notifications are allowed."
    return jsonify(ok=True, message=message)
