from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from supabase_auth.errors import AuthError

from app import recovery
from app.supabase_client import get_supabase, get_supabase_admin

bp = Blueprint("admin", __name__)

ROLE_FILTERS = ("citizen", "responder", "organization", "admin")
PER_PAGE = 50
BAN_FOREVER = "876000h"  # ~100 years; Supabase has no permanent ban flag
DELETE_CONFIRMATION = "DELETE"


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = session.get("user")
        if not user:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("auth.login", role="admin"))
        if user.get("role") != "admin":
            flash("You do not have access to the admin portal.", "error")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)

    return wrapped


def _user_role(u):
    if (u.app_metadata or {}).get("role") == "admin":
        return "admin"
    return (u.user_metadata or {}).get("role")


def _row(u):
    meta = u.user_metadata or {}
    return {
        "id": u.id,
        "email": u.email,
        "name": meta.get("full_name") or meta.get("agency_name") or "—",
        "role": _user_role(u) or "unknown",
        "agency": meta.get("agency_name") or meta.get("station") or "",
        "created_at": str(u.created_at)[:10] if u.created_at else "",
        "last_sign_in": str(u.last_sign_in_at)[:16].replace("T", " ") if u.last_sign_in_at else "Never",
        "confirmed": bool(u.email_confirmed_at),
        "banned": bool(getattr(u, "banned_until", None)),
    }


@bp.route("/")
@admin_required
def index():
    page = max(request.args.get("page", 1, type=int), 1)
    role = request.args.get("role")
    q = request.args.get("q", "").strip().lower()

    try:
        users = get_supabase_admin().auth.admin.list_users(page=page, per_page=PER_PAGE)
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
        users = []

    rows = [_row(u) for u in users]
    reset_requests = _reset_requests()
    stats = {r: sum(1 for x in rows if x["role"] == r) for r in ROLE_FILTERS}
    stats["total"] = len(rows)
    stats["banned"] = sum(1 for x in rows if x["banned"])

    if role in ROLE_FILTERS:
        rows = [x for x in rows if x["role"] == role]
    if q:
        rows = [x for x in rows if q in (x["email"] or "").lower() or q in x["name"].lower()]

    return render_template(
        "admin/index.html",
        rows=rows,
        stats=stats,
        role=role,
        q=q,
        page=page,
        has_next=len(users) == PER_PAGE,
        roles=ROLE_FILTERS,
        reset_requests=reset_requests,
    )


def _reset_requests():
    """Pending MFA reset requests with the account each one belongs to."""
    try:
        db = get_supabase_admin()
        items = recovery.pending_requests(db)
        for item in items:
            user = db.auth.admin.get_user_by_id(item["user_id"]).user
            item["email"] = user.email
            item["name"] = (user.user_metadata or {}).get("full_name") or user.email
            item["created"] = item["created_at"][:16].replace("T", " ")
        return items
    except Exception:
        # Migration 006 not applied yet, or the service key is missing: the section is simply empty.
        return []


@bp.route("/mfa-resets/<uuid:request_id>/approve", methods=["POST"])
@admin_required
def approve_mfa_reset(request_id):
    """Remove a locked-out user's authenticators.

    Requires a pending request that the user created after passing their password check,
    the reference code that user was shown, a note on how their identity was verified and
    the typed confirmation. Everything is written to the audit log.
    """
    if request.form.get("confirm") != "RESET":
        flash("Type RESET to confirm removing the user's authenticators.", "error")
        return _back()
    try:
        recovery.approve_reset(get_supabase_admin(), str(request_id), session["user"]["id"],
                               request.form.get("code", ""), request.form.get("identity_note", ""))
    except recovery.ResetRefused as exc:
        flash(str(exc), "error")
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
    except Exception as exc:
        flash(f"The reset could not be completed: {getattr(exc, 'message', None) or exc}", "error")
    else:
        flash("MFA reset. The user can sign in with their password and has been alerted; ask them to set MFA up again.", "success")
    return _back()


@bp.route("/mfa-resets/<uuid:request_id>/reject", methods=["POST"])
@admin_required
def reject_mfa_reset(request_id):
    try:
        done = recovery.reject_reset(get_supabase_admin(), str(request_id), session["user"]["id"])
    except Exception as exc:
        flash(f"The request could not be rejected: {getattr(exc, 'message', None) or exc}", "error")
    else:
        flash("Request rejected." if done else "That request is no longer pending.", "success" if done else "error")
    return _back()


def _update(user_id, attrs, message):
    if user_id == session["user"]["id"]:
        flash("You cannot change your own admin account here.", "error")
        return
    try:
        get_supabase_admin().auth.admin.update_user_by_id(user_id, attrs)
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
    else:
        flash(message, "success")


def _back():
    return redirect(request.referrer or url_for("admin.index"))


@bp.route("/users/<user_id>/suspend", methods=["POST"])
@admin_required
def suspend(user_id):
    _update(user_id, {"ban_duration": BAN_FOREVER}, "Account suspended.")
    return _back()


@bp.route("/users/<user_id>/reinstate", methods=["POST"])
@admin_required
def reinstate(user_id):
    _update(user_id, {"ban_duration": "none"}, "Account reinstated.")
    return _back()


@bp.route("/users/<user_id>/promote", methods=["POST"])
@admin_required
def promote(user_id):
    _update(user_id, {"app_metadata": {"role": "admin"}}, "User granted admin access.")
    return _back()


@bp.route("/users/<user_id>/demote", methods=["POST"])
@admin_required
def demote(user_id):
    _update(user_id, {"app_metadata": {"role": None}}, "Admin access removed.")
    return _back()


# Same minimums the registration forms enforce.
MIN_PASSWORD = {"organization": 16, "responder": 12, "admin": 12}


@bp.route("/users/<user_id>/password", methods=["POST"])
@admin_required
def set_password(user_id):
    password = request.form.get("password", "")
    try:
        user = get_supabase_admin().auth.admin.get_user_by_id(user_id).user
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
        return _back()

    minimum = MIN_PASSWORD.get(_user_role(user), 8)
    if len(password) < minimum:
        flash(f"Password must be at least {minimum} characters for this account.", "error")
    elif password != request.form.get("confirm_password"):
        flash("Passwords do not match.", "error")
    else:
        _update(user_id, {"password": password}, f"Password changed for {user.email}. Share it with them securely.")
    return _back()


@bp.route("/users/<user_id>/send-reset", methods=["POST"])
@admin_required
def send_reset(user_id):
    try:
        user = get_supabase_admin().auth.admin.get_user_by_id(user_id).user
        get_supabase().auth.reset_password_for_email(
            user.email, {"redirect_to": url_for("auth.login", _external=True)}
        )
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
    else:
        flash(f"Password reset email sent to {user.email}.", "success")
    return _back()


@bp.route("/users/<user_id>/delete", methods=["POST"])
@admin_required
def delete(user_id):
    if user_id == session["user"]["id"]:
        flash("You cannot delete your own account.", "error")
        return _back()
    # Enforced here too, so the check can't be skipped by posting directly.
    if request.form.get("confirm") != DELETE_CONFIRMATION:
        flash(f"Type {DELETE_CONFIRMATION} to confirm deleting an account.", "error")
        return _back()
    try:
        get_supabase_admin().auth.admin.delete_user(user_id)
    except (AuthError, RuntimeError) as exc:
        flash(getattr(exc, "message", None) or str(exc), "error")
    else:
        flash("Account deleted.", "success")
    return _back()
