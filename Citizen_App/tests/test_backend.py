"""Backend behaviour against a fake Supabase (httpx.MockTransport). No network, no real database."""
import asyncio
import json
import time

import httpx
import pytest

from esrms.backend import Backend, BackendError
from esrms.config import Config, _looks_like_service_key

CFG = Config("https://example.supabase.co", "anon-key")
AUTH = {"access_token": "jwt", "refresh_token": "r1", "expires_in": 3600,
        "user": {"id": "u1", "email": "c@x.ph", "user_metadata": {"full_name": "Juan"}}}
STORED = {"id": "i1", "incident_number": 42, "status": "new", "emergency_type": "General SOS",
          "latitude": 7.0, "longitude": 125.0, "created_at": "2026-10-03T04:00:00+00:00"}


def run(coro):
    return asyncio.run(coro)


def backend(handler):
    b = Backend(CFG, transport=httpx.MockTransport(handler))
    return b


def signed_in(handler):
    b = backend(handler)
    from esrms.backend import Session
    b.session = Session.from_auth(AUTH)
    return b


def test_sign_in_stores_session_and_uses_anon_key():
    seen = {}

    def h(req):
        seen["apikey"] = req.headers["apikey"]
        return httpx.Response(200, json=AUTH)

    b = backend(h)
    run(b.sign_in("c@x.ph", "pw"))
    assert b.session.user_id == "u1" and seen["apikey"] == "anon-key"


def test_registration_phone_is_saved_and_comes_back_on_sign_in():
    saved = {}

    def h(req):
        if req.url.path == "/auth/v1/signup":
            saved.update(json.loads(req.content)["data"])
        return httpx.Response(200, json={**AUTH, "user": {**AUTH["user"], "user_metadata": saved}})

    b = backend(h)
    run(b.sign_up("c@x.ph", "password", {"full_name": "Juan", "phone": "0917 123 4567"}))
    assert saved["phone"] == "0917 123 4567"
    run(b.sign_out())
    run(b.sign_in("c@x.ph", "password"))
    assert b.session.metadata["phone"] == "0917 123 4567"


def test_submit_sends_user_jwt_and_returns_db_row():
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        seen["prefer"] = req.headers.get("prefer")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json=[STORED])

    b = signed_in(h)
    out = run(b.submit_incident({"client_request_id": "c1", "emergency_type": "General SOS"}))
    assert out["incident_number"] == 42
    assert seen["auth"] == "Bearer jwt" and seen["prefer"] == "return=representation"


def test_network_failure_is_not_success():
    def h(req):
        raise httpx.ConnectError("down")

    b = signed_in(h)
    with pytest.raises(BackendError) as exc:
        run(b.submit_incident({"client_request_id": "c1"}))
    assert exc.value.kind == "network"


def test_retry_after_lost_reply_returns_stored_report_once():
    def h(req):
        if req.method == "POST":
            return httpx.Response(409, json={"code": "23505", "message": "duplicate key"})
        assert "client_request_id=eq.c1" in str(req.url)
        return httpx.Response(200, json=[STORED])

    b = signed_in(h)
    assert run(b.submit_incident({"client_request_id": "c1"}))["id"] == "i1"


def test_empty_201_is_not_treated_as_success():
    b = signed_in(lambda req: httpx.Response(201, json=[]))
    with pytest.raises(BackendError):
        run(b.submit_incident({"client_request_id": "c1"}))


def test_rate_limit_and_rls_errors_are_reported():
    b = signed_in(lambda req: httpx.Response(400, json={"code": "P0001", "message": "Too many", "hint": "rate_limited"}))
    with pytest.raises(BackendError) as exc:
        run(b.submit_incident({"client_request_id": "c1"}))
    assert exc.value.kind == "rate_limited"

    b = signed_in(lambda req: httpx.Response(403, json={"code": "42501", "message": "new row violates row-level security policy"}))
    with pytest.raises(BackendError) as exc:
        run(b.submit_incident({"client_request_id": "c2"}))
    assert exc.value.kind == "validation"


def test_missing_migration_is_a_clear_schema_error_not_success():
    # Exact replies the live database gave before 004_citizen_app.sql was applied.
    missing_col = {"code": "PGRST204", "message": "Could not find the 'client_request_id' column of "
                                                 "'emergency_incidents' in the schema cache"}
    b = signed_in(lambda req: httpx.Response(400, json=missing_col))
    with pytest.raises(BackendError) as exc:
        run(b.submit_incident({"client_request_id": "c1"}))
    assert exc.value.kind == "schema" and "004_citizen_app.sql" in str(exc.value)

    missing_fn = {"code": "PGRST202", "message": "Could not find the function "
                                                "public.citizen_announcements(p_barangay) in the schema cache"}
    b = signed_in(lambda req: httpx.Response(404, json=missing_fn))
    with pytest.raises(BackendError) as exc:
        run(b.announcements(None))
    assert exc.value.kind == "schema"


def test_expired_token_is_refreshed_before_request():
    calls = []

    def h(req):
        calls.append(req.url.path)
        if req.url.path == "/auth/v1/token":
            return httpx.Response(200, json={**AUTH, "access_token": "jwt2", "refresh_token": "r2"})
        assert req.headers["authorization"] == "Bearer jwt2"
        return httpx.Response(200, json=[])

    b = signed_in(h)
    b.session.expires_at = time.time() - 5
    run(b.my_reports())
    assert calls == ["/auth/v1/token", "/rest/v1/emergency_incidents"]


def test_announcements_use_rpc_with_barangay():
    def h(req):
        assert req.url.path == "/rest/v1/rpc/citizen_announcements"
        assert json.loads(req.content) == {"p_barangay": "Poblacion"}
        return httpx.Response(200, json=[])

    run(signed_in(h).announcements("Poblacion"))


def test_service_role_key_refused():
    import base64
    def jwt(role):
        body = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
        return f"h.{body}.s"
    assert _looks_like_service_key(jwt("service_role"))
    assert not _looks_like_service_key(jwt("anon"))
    assert _looks_like_service_key("sb_secret_abc")
