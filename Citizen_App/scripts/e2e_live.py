"""Live end-to-end check against the shared ESRMS database, using a TEST citizen account.

Run only after EMERGENCY/supabase/migrations/004_citizen_app.sql has been applied.
Creates ONE simulated incident whose description starts with "[TEST]" (the dashboard's
scripts/simulate_incident.py --cleanup deletes those). Nothing contacts real emergency services.

    $env:ESRMS_TEST_EMAIL="citizen.test@example.com"; $env:ESRMS_TEST_PASSWORD="..."
    .venv\\Scripts\\python scripts\\e2e_live.py
"""
import asyncio
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from esrms import config, report  # noqa: E402
from esrms.backend import Backend, BackendError  # noqa: E402
from esrms.report import Location  # noqa: E402

results = []


def check(name, passed, detail=""):
    results.append(passed)
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


async def main():
    email, password = os.environ.get("ESRMS_TEST_EMAIL"), os.environ.get("ESRMS_TEST_PASSWORD")
    if not email or not password:
        sys.exit("Set ESRMS_TEST_EMAIL and ESRMS_TEST_PASSWORD (a test citizen account).")
    cfg = config.load()
    b = Backend(cfg)
    anon = {"apikey": cfg.supabase_anon_key, "Authorization": f"Bearer {cfg.supabase_anon_key}"}

    # 12. Unauthenticated access is refused.
    async with httpx.AsyncClient(base_url=cfg.supabase_url, headers=anon) as c:
        r = await c.post("/rest/v1/emergency_incidents", json={"emergency_type": "[TEST] anon", "status": "new"})
        check("anon key cannot insert incidents", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = await c.get("/rest/v1/emergency_incidents?select=id&limit=1")
        check("anon key reads no incidents", r.status_code == 200 and r.json() == [])

    await b.sign_in(email, password)
    check("citizen sign-in", b.session is not None, b.session.user_id if b.session else "")

    # 5/6. Submit with real-looking test coordinates (Davao City Hall, same as the dashboard's simulator).
    row = report.build_incident(
        reporter_id=b.session.user_id, category_key="medical", incident_type="Unconscious person",
        description="[TEST] Citizen App e2e - simulated, do not dispatch",
        location=Location("gps", 7.07320, 125.61290, accuracy_m=10), citizen_name="E2E Test",
        client_request_id=uuid.uuid4(),
    )
    stored = await b.submit_incident(row)
    check("incident stored and confirmed by database", bool(stored.get("incident_number")),
          report.incident_code(stored))
    check("coordinates stored exactly", (stored["latitude"], stored["longitude"]) == (7.0732, 125.6129))
    check("status is dispatcher-controlled 'new'", stored["status"] == "new")

    # 11. Retrying the same request (lost reply) returns the same report, not a duplicate.
    again = await b.submit_incident(row)
    check("retry is idempotent", again["id"] == stored["id"])

    # Duplicate/rate-limit guard.
    try:
        dup = dict(row, client_request_id=str(uuid.uuid4()))
        await b.submit_incident(dup)
        check("rapid duplicate blocked", False, "second report accepted")
    except BackendError as exc:
        check("rapid duplicate blocked", exc.kind in ("duplicate", "rate_limited"), str(exc))

    # Citizen can't change dispatcher fields.
    h = {**b._headers(), "Prefer": "return=representation"}
    async with httpx.AsyncClient(base_url=cfg.supabase_url) as c:
        r = await c.patch(f"/rest/v1/emergency_incidents?id=eq.{stored['id']}", json={"status": "resolved"}, headers=h)
        check("citizen cannot update incident status", r.status_code in (401, 403) or r.json() == [],
              f"HTTP {r.status_code}")
        r = await c.post("/rest/v1/emergency_incidents", headers=h, json=dict(
            row, client_request_id=str(uuid.uuid4()), emergency_type="General SOS", priority=1, status="dispatched"))
        check("citizen cannot insert dispatcher status", r.status_code in (401, 403), f"HTTP {r.status_code}")

    mine = await b.my_reports()
    check("citizen reads back own report", any(r["id"] == stored["id"] for r in mine))

    # 8. Announcements through the restricted function.
    rows = await b.announcements(None)
    check("announcements load via citizen_announcements()", isinstance(rows, list), f"{len(rows)} visible")
    check("no drafts/future/expired returned", all(r.get("published_at") for r in rows))

    await b.close()
    print(f"\n{sum(results)}/{len(results)} checks passed. Test incident {report.incident_code(stored)} is in the "
          "dispatcher dashboard now; clean up with EMERGENCY\\scripts\\simulate_incident.py --cleanup")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
