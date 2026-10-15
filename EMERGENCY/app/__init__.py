import logging
import time

from flask import Flask, request, session
from supabase_auth.errors import AuthApiError

from config import Config

log = logging.getLogger(__name__)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    from app.routes.account import bp as account_bp
    from app.routes.admin import bp as admin_bp
    from app.routes.analytics import bp as analytics_bp
    from app.routes.announcements import bp as announcements_bp
    from app.routes.api import bp as api_bp
    from app.routes.dispatch import bp as dispatch_bp
    from app.routes.hospital import bp as hospital_bp
    from app.routes.hospital_auth import bp as hospital_auth_bp
    from app.routes.auth import bp as auth_bp
    from app.routes.main import bp as main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(announcements_bp)
    app.register_blueprint(dispatch_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(hospital_bp)
    app.register_blueprint(hospital_auth_bp)
    app.register_blueprint(account_bp)

    @app.before_request
    def keep_supabase_session():
        """Renew the user's Supabase session when its token expires.

        If Supabase refuses (the session was signed out from another device, or the
        password was changed elsewhere), this browser is signed out too.
        """
        from app.supabase_client import TOKENS_KEY, get_supabase, store_tokens

        tokens = session.get(TOKENS_KEY)
        if not tokens or "user" not in session or request.endpoint == "static":
            return
        if tokens.get("exp", 0) - 60 > time.time():
            return check_session_not_revoked(tokens)
        try:
            res = get_supabase().auth.refresh_session(tokens["rt"])
        except AuthApiError as exc:
            if exc.status in (400, 401, 403, 404):
                session.clear()  # the views answer with their usual "sign in" response
            return
        except Exception:
            # Network trouble is not a sign-out; try again on the next request.
            log.warning("Could not refresh the Supabase session", exc_info=True)
            return
        if res.session:
            store_tokens(res.session)

    # Until this time the "is my session still there" check is skipped (function missing or database down).
    session_check_retry = {"at": 0.0}

    def check_session_not_revoked(tokens):
        """Once a minute, confirm this browser's Supabase session still exists (see migration 006).

        "Sign out this device / other sessions" deletes the session in Supabase; without this
        check the signed-out browser would keep working until its token expired.
        """
        from app.supabase_client import TOKENS_KEY, get_supabase_admin

        now = time.time()
        if not tokens.get("sid") or now - tokens.get("chk", 0) < 60 or now < session_check_retry["at"]:
            return
        try:
            active = get_supabase_admin().rpc("account_session_active", {
                "p_user_id": session["user"]["id"], "p_session_id": tokens["sid"]}).execute().data
        except Exception:
            session_check_retry["at"] = now + 300
            return
        if active is False:
            session.clear()
        else:
            session[TOKENS_KEY] = {**tokens, "chk": int(now)}

    @app.context_processor
    def inject_user():
        from app.routes.account import display_scale_for

        user = session.get("user")
        return {
            "current_user": user,
            # Settings -> Display: the root text size every rem-based size follows (None when signed out).
            "display_scale": display_scale_for(user),
            "can_manage_announcements": bool(user) and user.get("role") in app.config["ANNOUNCEMENT_MANAGER_ROLES"],
            "can_manage_agency": bool(user) and user.get("role") in app.config["AGENCY_ADMIN_ROLES"],
            "can_dispatch": bool(user) and user.get("role") in app.config["DISPATCH_ROLES"],
        }

    return app
