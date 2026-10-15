"""Live end-to-end check of the announcement workflow against your Supabase project.

Run after applying supabase/migrations/002_announcements.sql:
    .venv\\Scripts\\python scripts\\announcements_e2e.py

It drives the real Flask routes (admin dashboard API and Citizen Viewer API)
with the Flask test client, so every write goes to the shared database. Rows it
creates are titled "[E2E] ..." and are deleted at the end, even on failure.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client  # noqa: E402

from app import create_app  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402

app = create_app()
app.config.update(TESTING=True)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def client_as(role, user_id=None, **extra):
    c = app.test_client()
    if role:
        with c.session_transaction() as s:
            s["user"] = {"id": user_id, "email": f"e2e-{role}@example.com", "role": role, "name": f"E2E {role}", **extra}
    return c


def public_titles(client, barangay=None):
    res = client.get("/api/announcements/public" + (f"?barangay={barangay}" if barangay else ""))
    assert res.status_code == 200, res.get_json()
    return {a["title"] for a in res.get_json()["items"]}


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def main():
    with app.app_context():
        db = get_supabase_admin()
        # created_by references auth.users, so act as a real account (any one will do).
        users = db.auth.admin.list_users(page=1, per_page=1)
        if not users:
            sys.exit("No auth users found. Create one account first; the test acts as that user.")
        admin_id = users[0].id

    admin = client_as("admin", admin_id)
    anon = client_as(None)
    citizen = client_as("citizen", "00000000-0000-0000-0000-0000000000c1")
    responder = client_as("responder", "00000000-0000-0000-0000-0000000000r1", branch="fire")
    now = datetime.now(timezone.utc)
    base = {"message": "Typhoon signal no. 2 raised. Stay indoors and monitor official channels.",
            "category": "emergency", "priority": "critical", "target_audience": "all_citizens",
            "status": "active", "action": "publish"}

    try:
        # Test 1
        res = admin.post("/api/announcements", json={**base, "title": "[E2E] Emergency Weather Advisory"})
        check("1. Admin publishes 'Emergency Weather Advisory'", res.status_code == 201, res.get_json())
        item = res.get_json()["item"]
        ann_id = item["id"]

        # Test 2
        with app.app_context():
            stored = get_supabase_admin().table("announcements").select("*").eq("id", ann_id).execute().data
        check("2. Saved in the database with publisher and timestamp",
              len(stored) == 1 and stored[0]["status"] == "active" and stored[0]["published_by"] == admin_id
              and stored[0]["published_at"] is not None, stored)

        # Test 3
        listing = admin.get("/api/announcements?q=Emergency Weather").get_json()
        check("3. Appears in the Announcement Dashboard",
              any(a["id"] == ann_id and a["effective_status"] == "active" for a in listing["items"]), listing)
        check("3b. Status counts come from the database", listing["counts"]["active"] >= 1, listing["counts"])

        # Test 4
        check("4. Appears in the Citizen Viewer (anonymous)", "[E2E] Emergency Weather Advisory" in public_titles(anon))

        # Test 5
        res = admin.put(f"/api/announcements/{ann_id}", json={**base, "title": "[E2E] Emergency Weather Advisory (Updated)",
                                                                "published_at": item["published_at"]})
        check("5a. Edit saved", res.status_code == 200, res.get_json())
        dash_titles = {a["title"] for a in admin.get("/api/announcements?q=E2E").get_json()["items"]}
        check("5b. Edit shows in dashboard", "[E2E] Emergency Weather Advisory (Updated)" in dash_titles)
        titles = public_titles(anon)
        check("5c. Edit shows in Citizen Viewer",
              "[E2E] Emergency Weather Advisory (Updated)" in titles and "[E2E] Emergency Weather Advisory" not in titles)

        # Test 6
        res = admin.post(f"/api/announcements/{ann_id}/archive", json={})
        check("6. Archive removes it from public display",
              res.status_code == 200 and "[E2E] Emergency Weather Advisory (Updated)" not in public_titles(anon), res.get_json())
        res = admin.post(f"/api/announcements/{ann_id}/restore", json={})
        check("6b. Restore brings it back", res.status_code == 200 and "[E2E] Emergency Weather Advisory (Updated)" in public_titles(anon))

        # Test 7
        admin.post("/api/announcements", json={**base, "title": "[E2E] Draft", "action": "draft"})
        res = admin.post("/api/announcements", json={**base, "title": "[E2E] Future",
                                                     "published_at": iso(now + timedelta(hours=2))})
        check("7a. Future publication stored as scheduled", res.get_json()["item"]["status"] == "scheduled", res.get_json())
        titles = public_titles(anon) | public_titles(citizen)
        check("7b. Draft and future scheduled are not public", not ({"[E2E] Draft", "[E2E] Future"} & titles))

        # Test 8
        res = citizen.post("/api/announcements", json={**base, "title": "[E2E] Hacked"})
        res2 = anon.put(f"/api/announcements/{ann_id}", json={**base, "title": "[E2E] Hacked"})
        res3 = responder.delete(f"/api/announcements/{ann_id}")
        check("8a. Citizen / anonymous / responder rejected by the server",
              (res.status_code, res2.status_code, res3.status_code) == (403, 401, 403))
        anon_db = create_client(app.config["SUPABASE_URL"], app.config["SUPABASE_KEY"])
        try:
            anon_db.table("announcements").insert({**{k: base[k] for k in ("message", "category", "priority")},
                                                   "title": "[E2E] Direct insert"}).execute()
            direct_insert_blocked = False
        except Exception:
            direct_insert_blocked = True
        check("8b. RLS blocks direct insert with the public key", direct_insert_blocked)
        direct_read = anon_db.table("announcements").select("id").execute().data
        check("8c. RLS blocks direct reads with the public key", direct_read == [], direct_read)
        upd = anon_db.table("announcements").update({"title": "[E2E] Hacked"}).eq("id", ann_id).execute().data
        check("8d. RLS blocks direct update with the public key", upd == [], upd)

        # Test 9
        admin.post("/api/announcements", json={**base, "title": "[E2E] Poblacion only",
                                               "target_audience": "barangay", "barangay": "Poblacion"})
        admin.post("/api/announcements", json={**base, "title": "[E2E] Fire crews only",
                                               "target_audience": "selected_groups", "target_groups": ["fire"]})
        check("9a. Barangay post hidden from other viewers",
              "[E2E] Poblacion only" not in public_titles(anon) | public_titles(anon, "Talomo"))
        check("9b. Barangay post shown in that barangay", "[E2E] Poblacion only" in public_titles(anon, "poblacion"))
        check("9c. Group post hidden from citizens", "[E2E] Fire crews only" not in public_titles(citizen))
        check("9d. Group post shown to fire responders", "[E2E] Fire crews only" in public_titles(responder))

        # Test 10: the database rejects a row (constraint), API must report failure.
        res = admin.post("/api/announcements", json={**base, "title": "[E2E] Bad", "category": "nope"})
        check("10a. Invalid data rejected, no success", res.status_code == 422 and "item" not in res.get_json())
        res = admin.delete("/api/announcements/00000000-0000-0000-0000-000000000000")
        check("10b. Deleting a missing row is reported as failure", res.status_code == 404)
    finally:
        with app.app_context():
            get_supabase_admin().table("announcements").delete().like("title", "[E2E]%").execute()
        print("Cleaned up [E2E] rows.")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
