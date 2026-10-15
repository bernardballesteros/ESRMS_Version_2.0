"""Google sign-in through Supabase Auth (OAuth 2 with PKCE). No Flet code; tested in tests/test_oauth.py.

Flow on a phone:
  1. new_pkce() -> verifier + challenge. The verifier is kept on the phone (it must survive Android
     closing the app while the browser is open) and the challenge goes into authorize_url().
  2. The system browser shows Google's account chooser and consent screen; Supabase receives
     Google's answer and redirects to REDIRECT_URL with ?code=... (or ?error=...).
  3. Android opens the app on that link; Flet reports it as the route "/callback?code=...".
     parse_callback() reads it and the backend exchanges code + verifier for a session.
No client secret is involved: Google's credentials stay in the Supabase dashboard.
"""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

# Must match [tool.flet.android.deep_linking] in pyproject.toml (scheme + host) and be listed in
# Supabase > Authentication > URL Configuration > Redirect URLs.
SCHEME, HOST, CALLBACK_PATH = "ph.esrms.citizen", "auth", "/callback"
REDIRECT_URL = f"{SCHEME}://{HOST}{CALLBACK_PATH}"
VERIFIER_MAX_AGE = 15 * 60   # seconds a started sign-in stays valid on the phone


def new_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(supabase_url, redirect_to, challenge):
    return f"{supabase_url}/auth/v1/authorize?" + urlencode({
        "provider": "google", "redirect_to": redirect_to,
        "code_challenge": challenge, "code_challenge_method": "s256",
    })


def is_callback(route):
    """True for the route Flet reports when Android opens the app on REDIRECT_URL."""
    return bool(route) and urlparse(route).path.rstrip("/") == CALLBACK_PATH


def parse_callback(route):
    """{'code': ...} or {'error': message} from the callback route (query or #fragment)."""
    parsed = urlparse(route or "")
    params = {**parse_qs(parsed.fragment), **parse_qs(parsed.query)}
    if params.get("code"):
        return {"code": params["code"][0]}
    error = (params.get("error_description") or params.get("error") or [""])[0]
    if (params.get("error") or [""])[0] == "access_denied" and "cancel" not in error.lower():
        error = "Google sign-in was cancelled."
    return {"error": error or "Google did not return a sign-in code."}
