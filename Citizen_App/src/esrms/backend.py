"""Talks to the shared ESRMS Supabase project over its REST and Auth APIs.

Plain httpx instead of supabase-py keeps the Android build free of native wheels.
Every call runs as the signed-in citizen (anon key + user JWT), so Row Level Security
decides what is allowed:
  * emergency_incidents: insert own 'new' report, read own reports (003_dispatch.sql)
  * citizen_announcements(): published announcements for this citizen (004_citizen_app.sql)
  * announcement_feed: change counter only
"""
import asyncio
import base64
import json
import logging
import time
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass, replace
from typing import Optional

import httpx

from esrms import oauth

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# PostgREST "not in schema cache" / Postgres "undefined column/function": the migration isn't applied.
SCHEMA_ERRORS = ("PGRST202", "PGRST204", "PGRST205", '"42703"', '"42883"')
REPORT_COLUMNS = ("id,incident_number,emergency_type,status,latitude,longitude,"
                  "location_source,created_at,updated_at,client_request_id")
# Same bucket and object names as the dispatcher dashboard (005_account_settings.sql).
AVATAR_BUCKET = "esrms-media"


class BackendError(Exception):
    """kind: network | auth | rate_limited | duplicate | validation | schema | server |
    wrong_password | not_configured"""

    def __init__(self, message, kind="server", status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str
    email: Optional[str]
    is_anonymous: bool
    metadata: dict
    providers: tuple = ()   # sign-in methods Supabase Auth holds for this account: ("email",), ("google",)...
    factors: tuple = ()     # MFA factors as Supabase Auth reports them: dicts with id, factor_type, status
    new_email: Optional[str] = None  # address still waiting for its confirmation link
    created_at: Optional[str] = None  # when Supabase Auth created the account (ISO timestamp)

    @classmethod
    def from_auth(cls, data):
        user = data.get("user") or {}
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data.get("expires_at") or time.time() + int(data.get("expires_in", 3600)),
            user_id=user["id"],
            **_user_fields(user),
        )

    def with_user(self, user):
        """Same tokens, account details replaced by what Supabase Auth just returned."""
        return replace(self, **_user_fields(user))

    @property
    def verified_totp(self):
        return [f for f in self.factors if f.get("factor_type") == "totp" and f.get("status") == "verified"]

    @property
    def aal(self):
        """Assurance level of this session ('aal2' = an MFA code was verified), read from the JWT."""
        try:
            payload = self.access_token.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("aal") or "aal1"
        except Exception:
            return "aal1"

    @property
    def needs_mfa(self):
        """An authenticator is enrolled but this session has not passed its code check."""
        return bool(self.verified_totp) and self.aal != "aal2"


def _user_fields(user):
    app_meta = user.get("app_metadata") or {}
    providers = app_meta.get("providers") or ([app_meta["provider"]] if app_meta.get("provider") else [])
    return dict(
        email=user.get("email") or None,
        is_anonymous=bool(user.get("is_anonymous")),
        metadata=user.get("user_metadata") or {},
        providers=tuple(providers),
        factors=tuple(user.get("factors") or ()),
        new_email=user.get("new_email") or None,
        created_at=user.get("created_at") or None,
    )


def _message(resp):
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return (body.get("msg") or body.get("message") or body.get("error_description")
                or body.get("error") or str(body))[:300]
    return str(body)[:300]


class Backend:
    def __init__(self, config, transport=None):
        self.config = config
        self.session: Optional[Session] = None
        self.on_session_change = None  # called with Session or None so the app can persist it
        self._http = httpx.AsyncClient(base_url=config.supabase_url, timeout=TIMEOUT, transport=transport)

    async def close(self):
        await self._http.aclose()

    # ------------------------------------------------------------ plumbing

    def _headers(self, auth=True):
        token = self.session.access_token if (auth and self.session) else self.config.supabase_anon_key
        return {"apikey": self.config.supabase_anon_key, "Authorization": f"Bearer {token}"}

    async def _send(self, method, path, *, auth=True, **kwargs):
        if auth:
            await self._ensure_fresh()
        headers = {**self._headers(auth), **kwargs.pop("headers", {})}
        try:
            return await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BackendError("The server did not answer in time. Check your connection.", "network") from exc
        except httpx.HTTPError as exc:
            raise BackendError("No connection to the ESRMS server. Check your internet.", "network") from exc

    def _set_session(self, session):
        self.session = session
        if self.on_session_change:
            self.on_session_change(session)

    async def _ensure_fresh(self):
        if self.session and self.session.expires_at - time.time() < 60:
            await self.refresh()

    # ------------------------------------------------------------ auth

    async def _auth(self, path, payload):
        resp = await self._send("POST", path, auth=False, json=payload)
        if resp.status_code >= 400:
            kind = "network" if resp.status_code >= 500 else "auth"
            raise BackendError(_message(resp), kind, resp.status_code)
        return resp.json()

    async def sign_in(self, email, password):
        data = await self._auth("/auth/v1/token?grant_type=password", {"email": email, "password": password})
        self._set_session(Session.from_auth(data))
        return self.session

    async def sign_up(self, email, password, metadata):
        """Returns the Session, or None when Supabase requires email confirmation first."""
        data = await self._auth("/auth/v1/signup", {
            "email": email, "password": password, "data": {"role": "citizen", **metadata},
        })
        if data.get("access_token"):
            self._set_session(Session.from_auth(data))
            return self.session
        return None

    async def sign_in_anonymously(self):
        """Works only if Anonymous Sign-Ins are enabled in Supabase Auth settings."""
        data = await self._auth("/auth/v1/signup", {"data": {"role": "citizen"}})
        self._set_session(Session.from_auth(data))
        return self.session

    async def exchange_google_code(self, code, verifier):
        """Finish Google sign-in: trade the code Supabase sent back (plus the PKCE verifier) for a session."""
        try:
            data = await self._auth("/auth/v1/token?grant_type=pkce", {"auth_code": code, "code_verifier": verifier})
        except BackendError as exc:
            if exc.kind == "auth":
                raise BackendError("Google sign-in could not be completed. Start it again.", "auth",
                                   exc.status) from exc
            raise
        self._set_session(Session.from_auth(data))
        await self.ensure_citizen_role()
        return self.session

    async def ensure_citizen_role(self):
        """A first Google sign-in creates the account without the sign-up form, so its metadata has no
        role. Give it the same 'citizen' marker the registration form sends. Never changes a role that
        is already set (a dashboard responder or organization keeps theirs)."""
        if self.session.is_anonymous or self.session.metadata.get("role"):
            return
        try:
            user = await self._account("PUT", "/auth/v1/user", "save your account type",
                                       json={"data": {"role": "citizen"}})
            self._set_session(self.session.with_user(user))
        except BackendError:
            log.warning("Could not mark the Google account as a citizen account", exc_info=True)

    async def sign_in_with_google(self, open_url, port=53682, timeout=300):
        """Desktop only (flet run on a PC): Supabase OAuth (PKCE) through the system browser.

        A one-shot local HTTP server on 127.0.0.1:<port> catches the redirect, so
        http://localhost:<port>/callback must be in Supabase > Auth > URL Configuration > Redirect URLs.
        Phones use the deep link in esrms/oauth.py instead (see CitizenApp.start_google_sign_in).
        """
        verifier, challenge = oauth.new_pkce()
        redirect = f"http://localhost:{port}/callback"
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        async def handle(reader, writer):
            line = (await reader.readline()).decode(errors="ignore")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            query = parse_qs(urlparse(line.split(" ")[1] if " " in line else "").query)
            ok = "code" in query
            body = ("<h2>Signed in. You can close this tab and return to ESRMS.</h2>" if ok else
                    "<h2>Google sign-in failed. Return to ESRMS and try again.</h2>")
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\n"
                         f"Connection: close\r\n\r\n{body}".encode())
            await writer.drain()
            writer.close()
            if not result.done() and ("code" in query or "error" in query):
                result.set_result(query)

        try:
            server = await asyncio.start_server(handle, "127.0.0.1", port)
        except OSError as exc:
            raise BackendError(f"Could not start Google sign-in (port {port} busy).", "auth") from exc
        try:
            await open_url(oauth.authorize_url(self.config.supabase_url, redirect, challenge))
            try:
                query = await asyncio.wait_for(result, timeout)
            except asyncio.TimeoutError as exc:
                raise BackendError("Google sign-in timed out.", "auth") from exc
        finally:
            server.close()
        if "code" not in query:
            msg = (query.get("error_description") or query.get("error") or ["unknown error"])[0]
            raise BackendError(f"Google sign-in failed: {msg}", "auth")
        return await self.exchange_google_code(query["code"][0], verifier)

    async def refresh(self, refresh_token=None):
        token = refresh_token or (self.session and self.session.refresh_token)
        if not token:
            raise BackendError("Please sign in again.", "auth")
        try:
            data = await self._auth("/auth/v1/token?grant_type=refresh_token", {"refresh_token": token})
        except BackendError as exc:
            if exc.kind == "auth":
                self._set_session(None)
            raise
        self._set_session(Session.from_auth(data))
        return self.session

    async def sign_out(self):
        if self.session:
            try:
                await self._send("POST", "/auth/v1/logout", auth=True)
            except BackendError:
                pass  # local sign-out still happens
        self._set_session(None)

    # ------------------------------------------------------------ account (Supabase Auth, own user only)

    async def _account(self, method, path, action, **kwargs):
        """Auth API call as the signed-in user. The JWT decides whose account it is; no user id is sent."""
        if not self.session:
            raise BackendError("Sign in again.", "auth")
        resp = await self._send(method, path, **kwargs)
        if resp.status_code >= 400:
            text = _message(resp)
            log.warning("Supabase %s -> HTTP %s: %s", action, resp.status_code, text)
            if resp.status_code >= 500:
                raise BackendError(f"Could not {action}: the server had a problem. Try again.", "server",
                                   resp.status_code)
            if resp.status_code == 429:
                raise BackendError("Too many attempts. Wait a minute and try again.", "rate_limited", 429)
            if resp.status_code == 401:
                raise BackendError("Your session expired. Sign in again.", "auth", 401)
            raise BackendError(text, "validation", resp.status_code)
        return resp.json() if resp.content else {}

    async def load_account(self):
        """Fresh account details (profile fields, sign-in methods, MFA factors) from Supabase Auth."""
        user = await self._account("GET", "/auth/v1/user", "load your account")
        self._set_session(self.session.with_user(user))
        return self.session

    async def account_info(self):
        """Account type and creation date of the signed-in account, for Account Settings.

        The role comes from the account's public.users row, which a citizen cannot change (the
        users_protect_columns trigger); sign-up metadata is only the fallback when that row is missing.
        """
        await self.load_account()
        s, row = self.session, {}
        resp = await self._send("GET", "/rest/v1/users", params={
            "select": "role,created_at", "id": f"eq.{s.user_id}", "limit": "1"})
        if resp.status_code == 200 and resp.json():
            row = resp.json()[0]
        return {"user_id": s.user_id, "is_anonymous": s.is_anonymous,
                "role": row.get("role") or s.metadata.get("role"),
                "created_at": s.created_at or row.get("created_at")}

    async def update_profile(self, full_name, phone, address):
        """Save the editable profile fields on the signed-in account, then on its public.users row.

        Returns True when the dashboard's copy (public.users) was updated too.
        """
        user = await self._account("PUT", "/auth/v1/user", "save your profile", json={
            "data": {"full_name": full_name, "phone": phone or None, "address": address or None}})
        self._set_session(self.session.with_user(user))
        # The dispatcher dashboard reads public.users; RLS limits this update to the caller's own row.
        resp = await self._send("PATCH", "/rest/v1/users", params={"id": f"eq.{self.session.user_id}"},
                                json={"full_name": full_name, "phone": phone or None})
        if resp.status_code >= 400:
            log.warning("users row update -> HTTP %s: %s", resp.status_code, _message(resp))
        return resp.status_code < 400

    async def change_email(self, email):
        """Ask Supabase Auth to change the sign-in address. Returns True if a confirmation link is pending."""
        user = await self._account("PUT", "/auth/v1/user", "change your email", json={"email": email})
        self._set_session(self.session.with_user(user))
        return (self.session.email or "").lower() != email.lower()

    async def change_password(self, current, new):
        """Check the current password with Supabase Auth, then set the new one. Passwords are never stored."""
        email = self.session.email if self.session else None
        if not email:
            raise BackendError("This account has no password.", "validation")
        try:
            probe = await self._auth("/auth/v1/token?grant_type=password", {"email": email, "password": current})
        except BackendError as exc:
            if exc.kind == "auth":
                raise BackendError("The current password is not correct.", "wrong_password", exc.status) from exc
            raise
        try:  # the check opened a second session; close just that one
            await self._http.post("/auth/v1/logout?scope=local", headers={
                "apikey": self.config.supabase_anon_key, "Authorization": f"Bearer {probe['access_token']}"})
        except httpx.HTTPError:
            pass
        await self._account("PUT", "/auth/v1/user", "change your password",
                            json={"password": new, "current_password": current})

    async def mfa_enroll(self):
        """Start authenticator enrolment. Returns Supabase's factor: id and totp {qr_code, secret, uri}."""
        await self.load_account()
        for f in self.session.factors:  # an abandoned attempt blocks a new one; unverified factors protect nothing
            if f.get("factor_type") == "totp" and f.get("status") != "verified":
                await self._account("DELETE", f"/auth/v1/factors/{f['id']}", "restart authenticator setup")
        return await self._account("POST", "/auth/v1/factors", "start authenticator setup", json={
            "factor_type": "totp", "issuer": "ESRMS Citizen", "friendly_name": f"Authenticator {int(time.time())}"})

    async def mfa_verify(self, factor_id, code):
        """Check a 6-digit code with Supabase Auth. On success the session becomes MFA-verified (aal2)."""
        challenge = await self._account("POST", f"/auth/v1/factors/{factor_id}/challenge", "check the code")
        data = await self._account("POST", f"/auth/v1/factors/{factor_id}/verify", "check the code",
                                   json={"challenge_id": challenge["id"], "code": code})
        if data.get("access_token"):
            if (data.get("user") or {}).get("id"):
                self._set_session(Session.from_auth(data))
            else:
                self._set_session(replace(self.session, access_token=data["access_token"],
                                          refresh_token=data["refresh_token"],
                                          expires_at=time.time() + int(data.get("expires_in", 3600))))
        return await self.load_account()

    async def mfa_remove(self, factor_id, code):
        """Remove an authenticator. Supabase requires an MFA-verified session, so the code is checked first."""
        await self.mfa_verify(factor_id, code)
        await self._account("DELETE", f"/auth/v1/factors/{factor_id}", "remove the authenticator")
        return await self.load_account()

    # ------------------------------------------------------------ profile picture (Supabase Storage)

    async def set_avatar(self, data, mime, ext):
        """Upload the signed-in citizen's picture to their own object and save its URL. Returns the URL."""
        path = f"{AVATAR_BUCKET}/avatars/{self.session.user_id}.{ext}"
        resp = await self._send("POST", f"/storage/v1/object/{path}", content=data, headers={
            "Content-Type": mime, "x-upsert": "true", "cache-control": "3600"})
        if resp.status_code >= 400:
            log.warning("avatar upload -> HTTP %s: %s", resp.status_code, _message(resp))
            if any(w in resp.text.lower() for w in ("row-level security", "bucket not found", "unauthorized")):
                raise BackendError(
                    "Profile pictures are not enabled on the ESRMS server yet. The administrator needs to run "
                    "supabase/007_citizen_avatars.sql.", "not_configured", resp.status_code)
            raise BackendError(f"Could not upload the picture: {_message(resp)}", "validation", resp.status_code)
        # Version the URL so the previous picture is not shown from cache (same as the dashboard).
        url = f"{self.config.supabase_url}/storage/v1/object/public/{path}?v={int(time.time())}"
        await self._save_avatar_url(url)
        return url

    async def remove_avatar(self):
        await self._save_avatar_url(None)
        resp = await self._send("DELETE", f"/storage/v1/object/{AVATAR_BUCKET}", json={
            "prefixes": [f"avatars/{self.session.user_id}.{ext}" for ext in ("png", "jpg", "webp")]})
        if resp.status_code >= 400:
            log.info("avatar delete -> HTTP %s: %s", resp.status_code, _message(resp))

    async def _save_avatar_url(self, url):
        user = await self._account("PUT", "/auth/v1/user", "save your picture", json={"data": {"avatar_url": url}})
        self._set_session(self.session.with_user(user))
        await self._send("PATCH", "/rest/v1/users", params={"id": f"eq.{self.session.user_id}"},
                         json={"avatar_url": url})

    # ------------------------------------------------------------ incidents

    async def upload_incident_photo(self, data, mime, ext, photo_id):
        """Upload the photo a citizen attached to an SOS report. Returns its public URL.

        The object is incidents/<own user id>/<photo_id>.<ext> (supabase/008_incident_photos.sql).
        Sending the same photo again after a lost reply finds the object already there, which counts.
        """
        path = f"{AVATAR_BUCKET}/incidents/{self.session.user_id}/{photo_id}.{ext}"
        resp = await self._send("POST", f"/storage/v1/object/{path}", content=data, headers={
            "Content-Type": mime, "cache-control": "3600"})
        if resp.status_code >= 400 and resp.status_code != 409 and "duplicate" not in resp.text.lower():
            log.warning("incident photo upload -> HTTP %s: %s", resp.status_code, _message(resp))
            if resp.status_code >= 500:
                raise BackendError("The server had a problem storing the photo.", "server", resp.status_code)
            if resp.status_code == 401:
                raise BackendError("Your session expired. Sign in again.", "auth", 401)
            if any(w in resp.text.lower() for w in ("row-level security", "bucket not found", "unauthorized")):
                raise BackendError(
                    "Report photos are not enabled on the ESRMS server yet. The administrator needs to run "
                    "supabase/008_incident_photos.sql.", "not_configured", resp.status_code)
            raise BackendError(f"Could not upload the photo: {_message(resp)}", "validation", resp.status_code)
        return f"{self.config.supabase_url}/storage/v1/object/public/{path}"

    def _raise_for(self, resp, action):
        msg = _message(resp)
        # Technical detail for developers; no tokens or request bodies.
        log.warning("Supabase %s -> HTTP %s: %s", action, resp.status_code, msg)
        if any(code in resp.text for code in SCHEMA_ERRORS):
            raise BackendError(
                f"Could not {action}: the ESRMS database is missing the Citizen App update "
                "(migration 004_citizen_app.sql). Please tell the ESRMS administrator.",
                "schema", resp.status_code)
        if resp.status_code == 401:
            raise BackendError("Your session expired. Sign in again.", "auth", 401)
        if "rate_limited" in resp.text:
            raise BackendError(msg, "rate_limited", resp.status_code)
        if "duplicate_report" in resp.text:
            raise BackendError(msg, "duplicate", resp.status_code)
        if resp.status_code in (400, 403, 422):
            raise BackendError(f"Could not {action}: {msg}", "validation", resp.status_code)
        raise BackendError(f"Could not {action}: {msg}", "server", resp.status_code)

    async def submit_incident(self, row):
        """Insert the report and return the stored row. Never returns without database confirmation.

        If the insert reached the database earlier (network dropped before the reply), the retry
        hits the client_request_id unique index; the stored report is then fetched and returned.
        """
        resp = await self._send(
            "POST", f"/rest/v1/emergency_incidents?select={REPORT_COLUMNS}",
            json=row, headers={"Prefer": "return=representation"},
        )
        if resp.status_code == 201:
            data = resp.json()
            if data:
                return data[0]
            raise BackendError("The database did not confirm the report.", "server", 201)
        if resp.status_code == 409 and row.get("client_request_id"):
            existing = await self.find_report(row["client_request_id"])
            if existing:
                return existing
        self._raise_for(resp, "send the report")

    async def find_report(self, client_request_id):
        resp = await self._send("GET", "/rest/v1/emergency_incidents", params={
            "select": REPORT_COLUMNS, "client_request_id": f"eq.{client_request_id}", "limit": "1",
        })
        if resp.status_code != 200:
            self._raise_for(resp, "check the report")
        data = resp.json()
        return data[0] if data else None

    async def my_reports(self, limit=20):
        resp = await self._send("GET", "/rest/v1/emergency_incidents", params={
            "select": REPORT_COLUMNS, "order": "created_at.desc", "limit": str(limit),
        })
        if resp.status_code != 200:
            self._raise_for(resp, "load your reports")
        return resp.json()

    # ------------------------------------------------------------ safety radar (public risk areas)

    async def _public_risk_get(self, table, params):
        resp = await self._send("GET", f"/rest/v1/{table}", params=params)
        if resp.status_code == 404 or any(code in resp.text for code in SCHEMA_ERRORS):
            raise BackendError("Safety Radar data has not been published by the ESRMS server yet.",
                               "not_configured", resp.status_code)
        if resp.status_code != 200:
            self._raise_for(resp, "load the risk areas")
        return resp.json()

    async def public_risk_feed(self):
        """When ESRMS last published the public risk areas (one small row), or None."""
        rows = await self._public_risk_get("public_risk_feed", {
            "select": "version,published_at,sufficient,period_days,cell_km", "id": "eq.1"})
        return rows[0] if rows else None

    async def public_risk(self):
        """The public-safe risk areas ESRMS Analytics published for citizens, and their feed row.

        Read-only generalized zones: no incident, person, responder or organization data exists in
        these tables. The citizen's location is not sent; nearby areas are worked out on the phone.
        """
        feed = await self.public_risk_feed()
        areas = await self._public_risk_get("public_risk_areas", {
            "select": "area_id,area_name,risk_level,center_latitude,center_longitude,radius_km,public_summary,"
                      "updated_at", "limit": "500"})
        return {"feed": feed or {"version": 0, "published_at": None, "sufficient": False}, "areas": areas}

    # ------------------------------------------------------------ announcements

    async def announcements(self, barangay=None):
        resp = await self._send("POST", "/rest/v1/rpc/citizen_announcements", json={"p_barangay": barangay})
        if resp.status_code != 200:
            self._raise_for(resp, "load announcements")
        return resp.json()

    async def announcement_feed_version(self):
        resp = await self._send("GET", "/rest/v1/announcement_feed",
                                params={"select": "version", "id": "eq.1"}, auth=False)
        if resp.status_code != 200:
            self._raise_for(resp, "check for announcements")
        data = resp.json()
        return data[0]["version"] if data else None
