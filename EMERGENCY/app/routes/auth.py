import logging
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from supabase_auth.errors import AuthError

from app import recovery
from app.account import AGENCY_TYPES, valid_totp_code
from app.supabase_client import TOKENS_KEY, get_supabase, get_supabase_admin, store_tokens

bp = Blueprint("auth", __name__)

ROLES = ("citizen", "responder", "organization")

ADMIN_MIN_PASSWORD = 12

log = logging.getLogger(__name__)

# A password sign-in waiting for its authenticator code is only kept this long.
MFA_PENDING_KEY = "mfa_pending"
MFA_PENDING_SECONDS = 600

GENDERS = ("Male", "Female", "Other", "Prefer not to say")

ORG_MIN_PASSWORD = 16


def console_password_problems(password):
    """The Master Command Console password rules; the strength meter on the form shows the same checklist."""
    problems = []
    if len(password) < ORG_MIN_PASSWORD:
        problems.append(f"at least {ORG_MIN_PASSWORD} characters")
    if not any(c.isupper() for c in password):
        problems.append("an uppercase letter")
    if not any(c.islower() for c in password):
        problems.append("a lowercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("a number")
    if all(c.isalnum() for c in password):
        problems.append("a special character")
    return problems


def _role_arg():
    role = request.args.get("role")
    return role if role in ROLES else None


def _error_text(exc):
    return getattr(exc, "message", None) or str(exc)


def _is_admin(user):
    # Only trust app_metadata (service-role writable) or the server allowlist.
    # user_metadata is set by the user at sign-up, so it can't grant admin.
    app_meta = user.app_metadata or {}
    return app_meta.get("role") == "admin" or (user.email or "").lower() in current_app.config["ADMIN_EMAILS"]


def verified_totp_factors(user):
    return [f for f in (user.factors or []) if f.factor_type == "totp" and f.status == "verified"]


def _start_session(user, fallback_role=None, auth_session=None):
    meta = user.user_metadata or {}
    role = meta.get("role") or fallback_role
    if role == "admin":
        role = None
    session["user"] = {
        "id": user.id,
        "email": user.email,
        "role": "admin" if _is_admin(user) else role,
        "name": meta.get("full_name") or meta.get("name") or user.email,
        "agency_name": meta.get("agency_name"),
        "agency_type": meta.get("agency_type"),
        "ori_code": meta.get("ori_code"),
        "jurisdiction": meta.get("jurisdiction"),
        "mfa_type": meta.get("mfa_type"),
        # Used to target announcements by responder branch and barangay.
        "branch": meta.get("branch"),
        "barangay": meta.get("barangay"),
        "avatar_url": meta.get("avatar_url"),
        # True only once an authenticator has been enrolled and verified with Supabase Auth.
        "mfa_enabled": bool(verified_totp_factors(user)),
    }
    if auth_session:
        store_tokens(auth_session)
    else:
        session.pop(TOKENS_KEY, None)


def _complete_sign_in(res, role, landing="main.dashboard"):
    """Start the session, or hold it until the authenticator code is verified when MFA is on."""
    if res.session and verified_totp_factors(res.user):
        session.pop("user", None)
        session.pop(TOKENS_KEY, None)
        session[MFA_PENDING_KEY] = {
            "at": res.session.access_token,
            "rt": res.session.refresh_token,
            "role": role,
            "until": int(time.time()) + MFA_PENDING_SECONDS,
        }
        return redirect(url_for("auth.login_mfa"))
    _start_session(res.user, role, res.session)
    return redirect(url_for(landing))


def _missing(form, fields):
    return [f for f in fields if not form.get(f, "").strip()]


def _finish_sign_up(email, password, role, metadata):
    """Create the Supabase account and route the user based on whether email confirmation is on."""
    try:
        res = get_supabase().auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {"role": role, **metadata},
                "email_redirect_to": url_for("auth.callback", _external=True),
            },
        })
    except AuthError as exc:
        flash(_error_text(exc), "error")
        return None

    if res.session and res.user:
        _start_session(res.user, role, res.session)
        flash("Account created. Welcome to ESRMS.", "success")
        return redirect(url_for("main.dashboard"))

    flash("Registration received. Check your email to confirm your account, then sign in.", "success")
    return redirect(url_for("auth.login", role=role))


@bp.route("/login", methods=["GET", "POST"])
def login():
    # "admin" only changes the page copy; the actual role is decided in _start_session.
    role = "admin" if request.args.get("role") == "admin" else _role_arg()
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Enter your email and password.", "error")
        else:
            try:
                res = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
            except AuthError as exc:
                flash(_error_text(exc), "error")
            else:
                return _complete_sign_in(res, role)
    return render_template("auth/login.html", role=role, form=request.form)


MFA_MAX_TRIES = 6


def _pending_user(pending):
    """The password-verified (not yet MFA-verified) user behind the pending sign-in."""
    client = get_supabase()
    signed_in = client.auth.set_session(pending["at"], pending["rt"])
    if not signed_in.user or not signed_in.session:
        raise AuthError("Sign in again to continue.", None)
    return client, signed_in.user


@bp.route("/login/mfa", methods=["GET", "POST"])
def login_mfa():
    """Second step of sign-in for accounts with a verified authenticator.

    Besides the authenticator code it offers the two recovery paths for a lost device:
    a single-use recovery code, or a reset request an administrator has to approve.
    Both are only reachable after the password was verified.
    """
    pending = session.get(MFA_PENDING_KEY)
    if not pending or pending.get("until", 0) < time.time():
        session.pop(MFA_PENDING_KEY, None)
        flash("Sign in with your email and password first.", "error")
        return redirect(url_for("auth.login"))

    mode = request.values.get("mode") if request.values.get("mode") in ("recovery", "reset") else "code"
    reset_code = None
    if request.method == "POST":
        tries = pending.get("tries", 0) + 1
        if tries > MFA_MAX_TRIES:
            session.pop(MFA_PENDING_KEY, None)
            flash("Too many attempts. Sign in again.", "error")
            return redirect(url_for("auth.login"))
        session[MFA_PENDING_KEY] = {**pending, "tries": tries}
        agent = request.user_agent.string
        try:
            client, user = _pending_user(pending)
            factors = verified_totp_factors(user)
            if not factors:
                raise AuthError("Sign in again to continue.", None)

            if mode == "reset":
                reset_code = recovery.create_reset_request(get_supabase_admin(), user.id, agent)
            elif mode == "recovery":
                if not recovery.recover_with_code(get_supabase_admin(), user.id, request.form.get("recovery_code", ""), agent):
                    flash("That recovery code is not valid or was already used.", "error")
                else:
                    # The lost authenticators are gone; end every other session of the account as well.
                    try:
                        client.auth.admin.sign_out(pending["at"], "others")
                    except Exception:
                        log.warning("Could not sign out other sessions after MFA recovery", exc_info=True)
                    session.pop(MFA_PENDING_KEY, None)
                    _start_session(client.auth.get_user().user, pending.get("role"), client.auth.get_session())
                    flash("Recovery code accepted. Your authenticators were removed, so MFA is now OFF. "
                          "Set it up again below.", "success")
                    return redirect(url_for("account.settings", section="security"))
            else:
                code = request.form.get("code", "").replace(" ", "")
                if not valid_totp_code(code):
                    flash("Enter the 6-digit code from your authenticator app.", "error")
                else:
                    verified = None
                    for factor in factors:   # the code may come from a backup authenticator
                        try:
                            verified = client.auth.mfa.challenge_and_verify({"factor_id": factor.id, "code": code})
                            break
                        except AuthError as exc:
                            failure = exc
                    if verified is None:
                        flash(_error_text(failure), "error")
                    else:
                        session.pop(MFA_PENDING_KEY, None)
                        _start_session(verified.user, pending.get("role"), client.auth.get_session())
                        return redirect(url_for("main.dashboard"))
        except AuthError as exc:
            flash(_error_text(exc), "error")
        except Exception:
            log.exception("MFA sign-in step failed")
            flash("That could not be completed right now. Try again.", "error")
    return render_template("auth/login_mfa.html", mode=mode, reset_code=reset_code)


@bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    role = _role_arg()
    email = request.form.get("email", "").strip()
    if not email:
        flash("Enter your email above, then click Forgot Password.", "error")
        return redirect(url_for("auth.login", role=role))
    try:
        get_supabase().auth.reset_password_for_email(
            email, {"redirect_to": url_for("auth.login", _external=True)}
        )
    except AuthError as exc:
        flash(_error_text(exc), "error")
    else:
        flash("If that account exists, a password reset link is on its way.", "success")
    return redirect(url_for("auth.login", role=role))


@bp.route("/login/google")
def login_google():
    session["pending_role"] = _role_arg()
    try:
        res = get_supabase().auth.sign_in_with_oauth({
            "provider": "google",
            "options": {"redirect_to": url_for("auth.callback", _external=True)},
        })
    except AuthError as exc:
        flash(_error_text(exc), "error")
        return redirect(url_for("auth.login"))
    return redirect(res.url)


@bp.route("/auth/callback")
def callback():
    if request.args.get("error"):
        flash(request.args.get("error_description") or "Sign-in was cancelled.", "error")
        return redirect(url_for("auth.login"))

    code = request.args.get("code")
    if not code:
        # Email confirmation links land here without a PKCE code.
        flash("Email confirmed. You can sign in now.", "success")
        return redirect(url_for("auth.login"))

    try:
        res = get_supabase().auth.exchange_code_for_session({"auth_code": code})
    except AuthError as exc:
        flash(_error_text(exc), "error")
        return redirect(url_for("auth.login"))

    return _complete_sign_in(res, session.pop("pending_role", None))


@bp.route("/register")
def register():
    if request.args.get("role") == "admin":
        return redirect(url_for("auth.register_admin"))
    if _role_arg() == "citizen":
        return redirect(url_for("auth.register_citizen"))
    return redirect(url_for("auth.register_organization"))


@bp.route("/register/citizen", methods=["GET", "POST"])
def register_citizen():
    form = request.form
    if request.method == "POST":
        errors = []
        if _missing(form, ("full_name", "email", "dob", "age", "address", "password")):
            errors.append("Fill in every field.")
        if form.get("gender") not in GENDERS:
            errors.append("Select a gender.")
        if not form.get("age", "").strip().isdigit():
            errors.append("Age must be a number.")
        if len(form.get("password", "")) < 8:
            errors.append("Password must be at least 8 characters.")
        if form.get("password") != form.get("confirm_password"):
            errors.append("Passwords do not match.")
        if not form.get("agree"):
            errors.append("You must agree to the Citizen Safety Terms and Privacy Policy.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            response = _finish_sign_up(
                form["email"].strip(),
                form["password"],
                "citizen",
                {
                    "full_name": form["full_name"].strip(),
                    "dob": form["dob"].strip(),
                    "age": int(form["age"]),
                    "gender": form["gender"],
                    "address": form["address"].strip(),
                    "barangay": form.get("barangay", "").strip()[:100] or None,
                },
            )
            if response:
                return response

    return render_template("auth/register_citizen.html", form=form, genders=GENDERS)


@bp.route("/register/admin", methods=["GET", "POST"])
def register_admin():
    form = request.form
    if request.method == "POST":
        errors = []
        if _missing(form, ("full_name", "email", "password")):
            errors.append("Fill in every field.")
        if len(form.get("password", "")) < ADMIN_MIN_PASSWORD:
            errors.append(f"Password must be at least {ADMIN_MIN_PASSWORD} characters.")
        if form.get("password") != form.get("confirm_password"):
            errors.append("Passwords do not match.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            email = form["email"].strip()
            try:
                # Admin is granted through app_metadata, which only the service role can write.
                get_supabase_admin().auth.admin.create_user({
                    "email": email,
                    "password": form["password"],
                    "email_confirm": True,
                    "user_metadata": {"full_name": form["full_name"].strip()},
                    "app_metadata": {"role": "admin"},
                })
                res = get_supabase().auth.sign_in_with_password({"email": email, "password": form["password"]})
            except (AuthError, RuntimeError) as exc:
                flash(_error_text(exc), "error")
            else:
                _start_session(res.user, auth_session=res.session)
                flash("Admin account created. Welcome to ESRMS.", "success")
                return redirect(url_for("admin.index"))

    return render_template("auth/register_admin.html", form=form, min_password=ADMIN_MIN_PASSWORD)


@bp.route("/register/organization", methods=["GET", "POST"])
def register_organization():
    form = request.form
    if request.method == "POST":
        errors = []
        required = ("agency_name", "ori_code", "jurisdiction", "admin_name", "email", "cad_cert", "psap_phone", "password")
        if _missing(form, required):
            errors.append("Fill in every required field.")
        if form.get("agency_type") not in AGENCY_TYPES:
            errors.append("Select an agency type.")
        password_problems = console_password_problems(form.get("password", ""))
        if password_problems:
            errors.append("Console password needs " + ", ".join(password_problems) + ".")
        if not (form.get("attest_authority") and form.get("attest_cjis")):
            errors.append("Both compliance attestations are required.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            response = _finish_sign_up(
                form["email"].strip(),
                form["password"],
                "organization",
                {
                    "full_name": form["admin_name"].strip(),
                    "agency_name": form["agency_name"].strip(),
                    "agency_type": form["agency_type"],
                    "ori_code": form["ori_code"].strip().upper(),
                    "jurisdiction": form["jurisdiction"].strip(),
                    "cad_cert": form["cad_cert"].strip(),
                    "psap_phone": form["psap_phone"].strip(),
                },
            )
            if response:
                return response

    return render_template("auth/register_organization.html", form=form, agency_types=AGENCY_TYPES)


@bp.route("/logout")
def logout():
    tokens = session.get(TOKENS_KEY)
    if tokens:
        # Also end this browser's Supabase session so its refresh token can't be reused.
        try:
            get_supabase().auth.admin.sign_out(tokens["at"], "local")
        except Exception:
            log.info("Could not revoke the Supabase session on sign-out", exc_info=True)
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("main.index"))
