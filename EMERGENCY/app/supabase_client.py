import base64
import json

from flask import current_app, has_request_context, request, session
from supabase import Client, ClientOptions, create_client
from supabase_auth import SyncSupportedStorage


class FlaskSessionStorage(SyncSupportedStorage):
    """Auth storage scoped to one browser instead of the whole server.

    The PKCE code verifier has to survive the round trip to Google, so it is
    kept in the signed Flask session cookie. Everything else (tokens, user
    object) only lives for the current request, which keeps the cookie small.
    """

    def __init__(self):
        self._memory = {}

    def get_item(self, key):
        if key.endswith("-code-verifier"):
            return session.get(key)
        return self._memory.get(key)

    def set_item(self, key, value):
        if key.endswith("-code-verifier"):
            session[key] = value
        else:
            self._memory[key] = value

    def remove_item(self, key):
        session.pop(key, None)
        self._memory.pop(key, None)


def get_supabase() -> Client:
    """Create a Supabase client for the current request.

    A fresh client per request stops one user's sign-in from leaking into
    another user's requests, which a single module-level client would do.
    """
    options = ClientOptions(storage=FlaskSessionStorage(), auto_refresh_token=False)
    if has_request_context() and request.user_agent.string:
        # Supabase records the caller's user agent on each session; pass the browser's on,
        # so the session list shows the real device instead of this server.
        options.headers["User-Agent"] = request.user_agent.string[:300]
    return create_client(current_app.config["SUPABASE_URL"], current_app.config["SUPABASE_KEY"], options=options)


TOKENS_KEY = "sb"


class SignInRequired(Exception):
    """The browser has no usable Supabase session; the user has to sign in again."""


def store_tokens(auth_session):
    """Keep the user's own Supabase session in the signed, HttpOnly Flask cookie.

    Needed for anything Supabase only allows the user themselves to do: MFA
    enrollment and verification, changing email, signing out other sessions.
    """
    session[TOKENS_KEY] = {
        "at": auth_session.access_token,
        "rt": auth_session.refresh_token,
        "exp": auth_session.expires_at or 0,
        "sid": token_claims(auth_session.access_token).get("session_id"),
    }


def token_claims(access_token):
    """Claims of the user's own JWT (session id, assurance level). Supabase verifies the token; this only reads it."""
    try:
        payload = access_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def current_session_id():
    return (session.get(TOKENS_KEY) or {}).get("sid")


def upgrade_legacy_session():
    """Give a browser that signed in before tokens were kept its own Supabase session.

    The Flask session already proves who the user is, so they are not sent back to the
    sign-in page. Accounts with MFA are the exception: a session minted here would skip
    the authenticator, so those have to sign in again.
    """
    user = session.get("user")
    if not user:
        raise SignInRequired("Sign in to continue.")
    try:
        admin = get_supabase_admin()
        auth_user = admin.auth.admin.get_user_by_id(user["id"]).user
        if any(f.status == "verified" for f in (auth_user.factors or [])):
            raise SignInRequired("Multi-factor authentication is on for this account. Sign out and sign in again "
                                 "with your authenticator to manage security settings.")
        link = admin.auth.admin.generate_link({"type": "magiclink", "email": auth_user.email})
        res = get_supabase().auth.verify_otp({"token_hash": link.properties.hashed_token, "type": "magiclink"})
    except SignInRequired:
        raise
    except Exception as exc:
        raise SignInRequired("For your security, sign out and sign in again to do this.") from exc
    if not res.session:
        raise SignInRequired("For your security, sign out and sign in again to do this.")
    store_tokens(res.session)


def get_user_client() -> Client:
    """Supabase client acting as the signed-in user (their JWT, not the service role)."""
    if TOKENS_KEY not in session:
        upgrade_legacy_session()
    tokens = session[TOKENS_KEY]
    client = get_supabase()
    res = client.auth.set_session(tokens["at"], tokens["rt"])
    if not res.session:
        raise SignInRequired("Your session has expired. Sign in again.")
    if res.session.access_token != tokens["at"]:
        store_tokens(res.session)
    return client


def get_supabase_admin() -> Client:
    """Service-role client for the admin portal. Bypasses RLS, so keep it server side."""
    key = current_app.config["SUPABASE_SERVICE_ROLE_KEY"]
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not set.")
    return create_client(
        current_app.config["SUPABASE_URL"],
        key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )
