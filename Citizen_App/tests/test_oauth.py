"""Google sign-in (Supabase OAuth with PKCE): URL building, callback parsing and the code exchange
against tests/fake_supabase.py. No network, no real Google account."""
import asyncio
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fake_supabase import FakeSupabase

from esrms import oauth
from esrms.backend import Backend, BackendError
from esrms.config import Config

CFG = Config("https://example.supabase.co", "anon-key")


def run(coro):
    return asyncio.run(coro)


def test_authorize_url_asks_supabase_for_google_with_pkce():
    verifier, challenge = oauth.new_pkce()
    q = parse_qs(urlparse(oauth.authorize_url(CFG.supabase_url, oauth.REDIRECT_URL, challenge)).query)
    assert q == {"provider": ["google"], "redirect_to": ["ph.esrms.citizen://auth/callback"],
                 "code_challenge": [challenge], "code_challenge_method": ["s256"]}
    assert verifier not in oauth.authorize_url(CFG.supabase_url, oauth.REDIRECT_URL, challenge)   # never sent


def test_redirect_matches_the_android_deep_link():
    android = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8"))["tool"]["flet"]["android"]["deep_linking"]
    assert oauth.REDIRECT_URL == f"{android['scheme']}://{android['host']}{oauth.CALLBACK_PATH}"


@pytest.mark.parametrize("route,expected", [
    ("/callback?code=abc", {"code": "abc"}),
    ("/callback/?code=abc&state=x", {"code": "abc"}),
    ("/callback#code=abc", {"code": "abc"}),
    ("/callback?error=access_denied&error_description=The+user+cancelled", {"error": "The user cancelled"}),
    ("/callback?error=access_denied", {"error": "Google sign-in was cancelled."}),
    ("/callback#error=server_error&error_description=Unable+to+exchange+external+code",
     {"error": "Unable to exchange external code"}),
    ("/callback", {"error": "Google did not return a sign-in code."}),
])
def test_callback_parsing(route, expected):
    assert oauth.is_callback(route)
    assert oauth.parse_callback(route) == expected


def test_other_routes_are_not_callbacks():
    for route in ("/", "", None, "/callbacks", "/settings?code=1"):
        assert not oauth.is_callback(route)


def test_code_exchange_signs_in_and_marks_new_google_accounts_as_citizens():
    fake = FakeSupabase()
    gina = fake.add_user("gina@gmail.com", "unused", provider="google", full_name="Gina Reyes")
    b = Backend(CFG, transport=fake.transport())
    verifier, challenge = oauth.new_pkce()
    s = run(b.exchange_google_code(fake.google_code(gina, challenge), verifier))
    assert s.user_id == gina and s.providers == ("google",) and s.metadata["role"] == "citizen"
    assert fake.users[gina]["user_metadata"]["full_name"] == "Gina Reyes"


def test_existing_role_is_never_overwritten():
    fake = FakeSupabase()
    uid = fake.add_user("resp@gmail.com", "unused", provider="google", role="responder")
    b = Backend(CFG, transport=fake.transport())
    verifier, challenge = oauth.new_pkce()
    run(b.exchange_google_code(fake.google_code(uid, challenge), verifier))
    assert fake.users[uid]["user_metadata"]["role"] == "responder"


def test_wrong_verifier_reused_code_and_offline_fail_without_a_session():
    fake = FakeSupabase()
    uid = fake.add_user("gina@gmail.com", "unused", provider="google")
    b = Backend(CFG, transport=fake.transport())
    _, challenge = oauth.new_pkce()
    code = fake.google_code(uid, challenge)
    with pytest.raises(BackendError):
        run(b.exchange_google_code(code, "someone-elses-verifier"))
    with pytest.raises(BackendError):
        run(b.exchange_google_code(code, "anything"))                 # codes are single use
    verifier, challenge = oauth.new_pkce()
    code = fake.google_code(uid, challenge)
    fake.down = True
    with pytest.raises(BackendError) as exc:
        run(b.exchange_google_code(code, verifier))
    assert exc.value.kind == "network" and b.session is None
