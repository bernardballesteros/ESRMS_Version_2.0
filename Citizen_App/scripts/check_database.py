"""Read-only check of the shared ESRMS database from the Citizen App's point of view (public anon key).

    .venv\\Scripts\\python scripts\\check_database.py

Writes nothing. Reports which pieces of the schema the app needs are present, and confirms that the
anon key can NOT read incidents or announcements directly.
"""
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from esrms import config  # noqa: E402


def main():
    cfg = config.load()
    h = {"apikey": cfg.supabase_anon_key, "Authorization": f"Bearer {cfg.supabase_anon_key}"}
    c = httpx.Client(base_url=cfg.supabase_url, headers=h, timeout=20)
    ok = True

    def show(name, passed, detail=""):
        nonlocal ok
        ok &= passed
        print(f"[{'OK ' if passed else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))

    r = c.get("/rest/v1/emergency_incidents", params={"select": "id,latitude,longitude,emergency_type", "limit": "5"})
    show("emergency_incidents exists (003_dispatch.sql)", r.status_code == 200, f"HTTP {r.status_code}")
    show("anon key sees no incidents (RLS)", r.status_code == 200 and r.json() == [], f"rows={len(r.json()) if r.status_code == 200 else '?'}")

    r = c.get("/rest/v1/emergency_incidents", params={"select": "location_source,client_request_id", "limit": "1"})
    show("004: location_source / client_request_id columns", r.status_code == 200,
         "" if r.status_code == 200 else r.json().get("message", r.text)[:120])

    r = c.get("/rest/v1/announcements", params={"select": "id", "limit": "5"})
    show("anon key cannot read announcements table", r.status_code != 200 or r.json() == [], f"HTTP {r.status_code}")

    r = c.post("/rest/v1/rpc/citizen_announcements", json={"p_barangay": None})
    body = r.text[:160]
    exists = "PGRST202" not in body and r.status_code != 404
    show("004: citizen_announcements() installed", exists, f"HTTP {r.status_code} {body if not exists else ''}")
    show("citizen_announcements() closed to anon", r.status_code in (401, 403) or (exists and r.json() == []),
         f"HTTP {r.status_code}")

    r = c.get("/rest/v1/announcement_feed", params={"select": "version", "id": "eq.1"})
    show("announcement_feed readable (realtime counter)", r.status_code == 200 and len(r.json()) == 1, r.text[:80])

    r = c.get("/auth/v1/settings")
    if r.status_code == 200:
        s = r.json()
        print(f"[INFO] email sign-up enabled: {not s.get('disable_signup')}, "
              f"email confirmation required: {not s.get('mailer_autoconfirm')}, "
              f"anonymous sign-in enabled: {s.get('external', {}).get('anonymous_users')}")
    print("\nAll required pieces present." if ok else "\nSome checks failed - run EMERGENCY/supabase/migrations/004_citizen_app.sql "
                                                       "in the Supabase SQL Editor.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
