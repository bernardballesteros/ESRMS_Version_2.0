"""A small stand-in for Supabase Auth / REST / Storage that enforces the same rules the real one does
(own user only, password check, MFA code check, AAL2 before removing a factor, storage policy).

Used by tests/test_account.py and scripts/ui_smoke.py through httpx.MockTransport. The app itself
never uses this; it only ever talks to the real project in config.json.
"""
import base64
import hashlib
import json
import uuid

import httpx

GOOD_CODE = "123456"   # what the fake "authenticator" currently shows


def _jwt(uid, aal):
    body = base64.urlsafe_b64encode(json.dumps({"sub": uid, "aal": aal}).encode()).decode().rstrip("=")
    return f"h.{body}.{uuid.uuid4().hex}"


class FakeSupabase:
    def __init__(self):
        self.users, self.passwords, self.tokens = {}, {}, {}   # tokens: access_token -> (uid, aal)
        self.rows = {}              # public.users rows by id
        self.objects = {}           # storage objects by path
        self.storage_policies = True
        self.codes = {}             # Google sign-in: code -> (user id, PKCE challenge)
        self.risk_areas = None      # public_risk_areas rows; None = migration 012 not applied
        self.risk_feed = {"id": 1, "version": 0, "published_at": None, "sufficient": False, "period_days": None,
                          "cell_km": None}
        self.down = False
        self.calls = []

    def add_user(self, email, password, provider="email", **meta):
        uid = str(uuid.uuid4())
        base = {} if provider == "google" else {"role": "citizen"}   # Google sign-ups skip the form
        self.users[uid] = {"id": uid, "email": email, "user_metadata": {**base, **meta},
                           "app_metadata": {"provider": provider, "providers": [provider]}, "factors": [],
                           "created_at": "2025-04-15T03:20:00Z"}
        self.passwords[uid] = password
        self.rows[uid] = {"id": uid, "full_name": meta.get("full_name"), "phone": meta.get("phone"),
                          "role": "citizen", "created_at": "2025-04-15T03:20:01Z"}
        return uid

    def google_code(self, uid, challenge):
        """What Supabase hands the browser after Google approved the user (one-time code)."""
        code = uuid.uuid4().hex
        self.codes[code] = (uid, challenge)
        return code

    def session(self, uid, aal="aal1"):
        token = _jwt(uid, aal)
        self.tokens[token] = (uid, aal)
        return {"access_token": token, "refresh_token": uuid.uuid4().hex, "expires_in": 3600,
                "user": json.loads(json.dumps(self.users[uid]))}

    def transport(self):
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------ request handling

    def handle(self, req):
        if self.down:
            raise httpx.ConnectError("no signal")
        path, method = req.url.path, req.method
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith(
            "application/json") else {}
        self.calls.append((method, path))
        token = req.headers.get("authorization", "").removeprefix("Bearer ")
        uid, aal = self.tokens.get(token, (None, None))

        if path == "/auth/v1/token" and req.url.params.get("grant_type") == "password":
            match = next((u for u in self.users.values() if u["email"] == body.get("email")), None)
            if not match or self.passwords[match["id"]] != body.get("password"):
                return httpx.Response(400, json={"error_code": "invalid_credentials", "msg": "Invalid login credentials"})
            return httpx.Response(200, json=self.session(match["id"]))
        if path == "/auth/v1/token" and req.url.params.get("grant_type") == "pkce":
            uid_challenge = self.codes.pop(body.get("auth_code"), None)     # single use
            if not uid_challenge:
                return httpx.Response(404, json={"error_code": "flow_state_not_found", "msg": "invalid flow state"})
            digest = base64.urlsafe_b64encode(hashlib.sha256(body.get("code_verifier", "").encode()).digest())
            if digest.rstrip(b"=").decode() != uid_challenge[1]:
                return httpx.Response(400, json={"error_code": "bad_code_verifier", "msg": "code challenge does not match"})
            return httpx.Response(200, json=self.session(uid_challenge[0]))
        if path == "/auth/v1/logout":
            self.tokens.pop(token, None)
            return httpx.Response(204)
        if uid is None:
            return httpx.Response(401, json={"msg": "invalid JWT"})
        user = self.users[uid]

        if path == "/auth/v1/user" and method == "GET":
            return httpx.Response(200, json=user)
        if path == "/auth/v1/user" and method == "PUT":
            if "password" in body:
                if body.get("current_password") != self.passwords[uid]:
                    return httpx.Response(400, json={"msg": "Current password is incorrect"})
                if len(body["password"]) < 6:
                    return httpx.Response(422, json={"error_code": "weak_password", "msg": "Password is too weak"})
                self.passwords[uid] = body["password"]
            if "email" in body:
                if any(u["email"] == body["email"] for u in self.users.values() if u["id"] != uid):
                    return httpx.Response(422, json={"msg": "A user with this email address has already been registered"})
                user["new_email"] = body["email"]          # changes only after the emailed link is opened
            for key, value in (body.get("data") or {}).items():
                if value is None:
                    user["user_metadata"].pop(key, None)
                else:
                    user["user_metadata"][key] = value
            return httpx.Response(200, json=user)

        if path == "/auth/v1/factors" and method == "POST":
            factor = {"id": str(uuid.uuid4()), "factor_type": "totp", "status": "unverified",
                      "friendly_name": body.get("friendly_name")}
            user["factors"].append(factor)
            return httpx.Response(200, json={**factor, "type": "totp", "totp": {
                "secret": "JBSWY3DPEHPK3PXP", "uri": "otpauth://totp/ESRMS%20Citizen:test?secret=JBSWY3DPEHPK3PXP",
                "qr_code": "data:image/svg+xml;utf-8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 4 4'>"
                           "<rect width='4' height='4' fill='white'/><rect width='2' height='2' fill='black'/></svg>"}})
        if path.startswith("/auth/v1/factors/"):
            parts = path.split("/")
            factor = next((f for f in user["factors"] if f["id"] == parts[4]), None)
            if factor is None:          # includes "someone else's factor": it is not in this user's list
                return httpx.Response(404, json={"msg": "MFA factor not found"})
            if len(parts) == 6 and parts[5] == "challenge":
                return httpx.Response(200, json={"id": str(uuid.uuid4())})
            if len(parts) == 6 and parts[5] == "verify":
                if body.get("code") != GOOD_CODE:
                    return httpx.Response(422, json={"error_code": "mfa_verification_failed",
                                                     "msg": "Invalid TOTP code entered"})
                factor["status"] = "verified"
                return httpx.Response(200, json=self.session(uid, "aal2"))
            if method == "DELETE":
                if any(f["status"] == "verified" for f in user["factors"]) and aal != "aal2":
                    return httpx.Response(403, json={"error_code": "insufficient_aal", "msg": "AAL2 required"})
                user["factors"].remove(factor)
                return httpx.Response(200, json={"id": factor["id"]})

        if path in ("/rest/v1/public_risk_feed", "/rest/v1/public_risk_areas") and method == "GET":
            if self.risk_areas is None:
                return httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table"})
            return httpx.Response(200, json=[self.risk_feed] if path.endswith("feed") else self.risk_areas)
        if path == "/rest/v1/users" and method == "GET":
            target = req.url.params.get("id", "").removeprefix("eq.")
            return httpx.Response(200, json=[self.rows[uid]] if target == uid else [])   # RLS: own row only
        if path == "/rest/v1/users" and method == "PATCH":
            target = req.url.params.get("id", "").removeprefix("eq.")
            if target == uid:               # RLS: auth.uid() = id
                self.rows[uid].update(body)
            return httpx.Response(204)
        if path.startswith("/storage/v1/object/esrms-media"):
            name = path.removeprefix("/storage/v1/object/esrms-media/")
            if method == "DELETE":
                for key in body.get("prefixes", []):
                    if key.startswith(f"avatars/{uid}."):
                        self.objects.pop(key, None)
                return httpx.Response(200, json=[])
            allowed = self.storage_policies and (name in [f"avatars/{uid}.{e}" for e in ("png", "jpg", "webp")]
                                                 or name.startswith(f"incidents/{uid}/"))
            if not allowed:
                return httpx.Response(400, json={"statusCode": "403", "error": "Unauthorized",
                                                 "message": "new row violates row-level security policy"})
            self.objects[name] = req.content
            return httpx.Response(200, json={"Key": f"esrms-media/{name}"})
        return httpx.Response(200, json=[])
