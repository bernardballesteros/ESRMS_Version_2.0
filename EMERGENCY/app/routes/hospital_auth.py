"""Hospital Portal entry: free hospital registration, sign-in and password reset.

Uses the same Supabase Auth accounts and Flask session as the rest of ESRMS (app/routes/auth.py).
A hospital account is an ordinary ESRMS account with role 'hospital' plus a hospital_staff row
that ties it to its hospital; registering a hospital creates the account, the hospital and that
first Hospital Administrator membership together.
"""
import logging

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from supabase_auth.errors import AuthError

from app import hospital as hs
from app.dispatch import ValidationError
from app.routes.auth import _complete_sign_in, _error_text
from app.routes.hospital import BAYS, HOSPITALS, STAFF, _like, is_hospital_staff
from app.supabase_client import get_supabase, get_supabase_admin

bp = Blueprint("hospital_auth", __name__, url_prefix="/hospital")
log = logging.getLogger(__name__)

UNAVAILABLE = "Registration is temporarily unavailable. Nothing was created; please try again shortly."


@bp.route("/portal")
def portal():
    if session.get("user") and is_hospital_staff(session["user"]["id"]):
        return redirect(url_for("hospital.dashboard_page"))
    return redirect(url_for("hospital_auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Enter your hospital email and password.", "error")
        else:
            try:
                res = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
            except AuthError as exc:
                flash(_error_text(exc), "error")
            else:
                # An ESRMS account that is not hospital staff is sent to its own dashboard instead.
                staff = bool(res.user) and ((res.user.user_metadata or {}).get("role") == "hospital" or is_hospital_staff(res.user.id))
                return _complete_sign_in(res, "hospital" if staff else None,
                                         landing="hospital.dashboard_page" if staff else "main.dashboard")
    # Same sign-in page as the other ESRMS portals; role only selects its copy and links.
    return render_template("auth/login.html", role="hospital", form=request.form)


@bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = request.form.get("email", "").strip()
    if not email:
        flash("Enter your hospital email above, then choose Forgot Password.", "error")
        return redirect(url_for("hospital_auth.login"))
    try:
        get_supabase().auth.reset_password_for_email(email, {"redirect_to": url_for("hospital_auth.login", _external=True)})
    except AuthError as exc:
        flash(_error_text(exc), "error")
    else:
        flash("If that account exists, a password reset link is on its way.", "success")
    return redirect(url_for("hospital_auth.login"))


def create_hospital_account(db, auth, hospital, account):
    """Create the auth account, the hospital and its first administrator. Returns (ok, errors, needs_confirmation).

    Duplicates are refused before anything is written. If a later step fails, what was already
    created is removed again so a half-registered hospital can never be left behind.
    """
    errors = {}
    if db.table(HOSPITALS).select("id").ilike("name", _like(hospital["name"])).limit(1).execute().data:
        errors["hospital_name"] = "A hospital with this name is already registered. Sign in, or contact ESRMS dispatch."
    if db.table(HOSPITALS).select("id").ilike("official_email", _like(hospital["official_email"])).limit(1).execute().data:
        errors["official_email"] = "A hospital is already registered with this official email."
    if db.table("users").select("id").ilike("email", _like(account["email"])).limit(1).execute().data:
        errors["email"] = "An ESRMS account already uses this email. Sign in instead, or use another email."
    if errors:
        return False, errors, False

    try:
        res = auth.sign_up({
            "email": account["email"], "password": account["password"],
            "options": {"data": {"role": "hospital", "full_name": account["full_name"], "position": account["position"],
                                 "agency_name": hospital["name"]},
                        "email_redirect_to": url_for("hospital_auth.login", _external=True)},
        })
    except AuthError as exc:
        text = _error_text(exc)
        field = "password" if "password" in text.lower() else "email"
        return False, {field: text}, False
    user = res.user
    # With email confirmation on, Supabase answers a sign-up for an existing address with a decoy user.
    if not user or (getattr(user, "identities", None) is not None and not user.identities):
        return False, {"email": "An ESRMS account already uses this email. Sign in instead, or use another email."}, False

    created_hospital = None
    try:
        created_hospital = db.table(HOSPITALS).insert({
            **hospital, "availability": "ready", "is_active": True, "verified": False,
            "registered_by": user.id, "created_by": user.id, "updated_by": user.id}).execute().data[0]
        db.table(STAFF).insert({"hospital_id": created_hospital["id"], "user_id": user.id, "staff_role": "admin",
                                "position": account["position"], "added_by": user.id}).execute()
        db.table(BAYS).insert([{"hospital_id": created_hospital["id"], "name": name, "kind": kind, "status": "ready"}
                               for name, kind in hs.DEFAULT_BAYS]).execute()
    except Exception:
        log.exception("Hospital registration could not be completed; rolling back")
        try:
            if created_hospital:
                db.table(HOSPITALS).delete().eq("id", created_hospital["id"]).execute()
            db.auth.admin.delete_user(user.id)
        except Exception:
            log.exception("Hospital registration rollback failed for %s", account["email"])
        return False, {"form": UNAVAILABLE}, False
    return True, {}, not res.session


@bp.route("/register", methods=["GET", "POST"])
def register():
    errors = {}
    if request.method == "POST":
        try:
            hospital, account = hs.validate_registration(request.form)
        except ValidationError as exc:
            errors = exc.errors
        else:
            try:
                ok, errors, needs_confirmation = create_hospital_account(get_supabase_admin(), get_supabase().auth, hospital, account)
            except Exception:
                log.exception("Hospital registration failed")
                ok, errors, needs_confirmation = False, {"form": UNAVAILABLE}, False
            if ok:
                flash("Hospital account created successfully." + (
                    " Check your email to confirm the address, then sign in." if needs_confirmation else " Sign in to open your dashboard."), "success")
                return redirect(url_for("hospital_auth.login"))
        if errors and "form" not in errors:
            flash("Please fix the highlighted fields.", "error")
        elif errors:
            flash(errors["form"], "error")
    return render_template("hospital/register.html", form=request.form, errors=errors, hospital_types=hs.HOSPITAL_TYPES,
                           min_password=hs.MIN_PASSWORD)
