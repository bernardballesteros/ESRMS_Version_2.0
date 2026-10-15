"""Live end-to-end check of the Dispatcher Dashboard against your Supabase project.

Run after applying supabase/migrations/003_dispatch.sql:
    .venv\\Scripts\\python scripts\\dispatch_e2e.py

Covers tests A, F, G, H and the database side of C/D/E (the coordinates the map
receives). The map zoom and red dot (C/D/E) and realtime arrival (B) must be
checked in a browser; see the setup notes. Test rows are removed at the end.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client  # noqa: E402

from app import create_app  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402

app = create_app()
app.config.update(TESTING=True)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def client_as(role, user_id=None):
    c = app.test_client()
    if role:
        with c.session_transaction() as s:
            s["user"] = {"id": user_id, "email": f"e2e-{role}@example.com", "role": role, "name": f"E2E {role}"}
    return c


def main():
    with app.app_context():
        db = get_supabase_admin()
        users = db.auth.admin.list_users(page=1, per_page=1)
        if not users:
            sys.exit("No auth users found. Create one account first; the test acts as that user.")
        dispatcher_id = users[0].id

        def insert(**row):
            # Simulates the Citizen Emergency App writing a report into the shared table.
            return db.table("emergency_incidents").insert({"description": "[E2E] test", **row}).execute().data[0]

    dispatcher = client_as("organization", dispatcher_id)
    citizen = client_as("citizen", "00000000-0000-0000-0000-0000000000c1")
    anon = client_as(None)

    try:
        with app.app_context():
            a = insert(emergency_type="Fire", latitude=7.0644, longitude=125.6078, priority=1, contact_number="+63 900 111 2222")
            b = insert(emergency_type="Medical Emergency", latitude=7.0905, longitude=125.6136, location_verified=True)
            e = insert(emergency_type="Flood", latitude=None, longitude=None)

        # Test A
        res = dispatcher.get("/api/dispatch/incidents")
        data = res.get_json()
        ids = [i["id"] for i in data["items"]]
        check("A. Inserted reports appear in the incoming panel", res.status_code == 200 and {a["id"], b["id"], e["id"]} <= set(ids), data)
        check("A2. Contact number is not sent in the list", all("contact_number" not in i for i in data["items"]))
        check("A3. P1 report sorted above an unprioritized one", ids.index(a["id"]) < ids.index(b["id"]))
        check("A4. Counters from the database", data["counts"]["active"] >= 3 and data["counts"]["critical"] >= 1, data["counts"])

        # C/D: coordinates the map will use are the stored ones
        items = {i["id"]: i for i in data["items"]}
        check("C. Report A carries its exact GPS coordinates",
              items[a["id"]]["has_location"] and (items[a["id"]]["latitude"], items[a["id"]]["longitude"]) == (7.0644, 125.6078))
        check("D. Report B carries different coordinates",
              (items[b["id"]]["latitude"], items[b["id"]]["longitude"]) == (7.0905, 125.6136))
        # E
        check("E. Report without GPS is flagged as no location", items[e["id"]]["has_location"] is False)

        detail = dispatcher.get(f"/api/dispatch/incidents/{a['id']}").get_json()["item"]
        check("A5. Opening an incident returns contact and history",
              detail["contact_number"] == "+63 900 111 2222" and detail["events"][0]["event"] == "reported")

        # Test F
        res = dispatcher.post(f"/api/dispatch/incidents/{a['id']}/status", json={"status": "acknowledged"})
        with app.app_context():
            stored = get_supabase_admin().table("emergency_incidents").select("*").eq("id", a["id"]).execute().data[0]
        check("F. Acknowledge saved to the database",
              res.status_code == 200 and stored["status"] == "acknowledged"
              and stored["acknowledged_by"] == dispatcher_id and stored["acknowledged_at"], res.get_json())
        after = {i["id"]: i for i in dispatcher.get("/api/dispatch/incidents").get_json()["items"]}
        check("F2. Dashboard shows the new status and keeps the report", after[a["id"]]["status"] == "acknowledged")
        res = dispatcher.post(f"/api/dispatch/incidents/{a['id']}/archive", json={})
        check("F3. Open incidents cannot be archived", res.status_code == 409)
        dispatcher.post(f"/api/dispatch/incidents/{a['id']}/status", json={"status": "resolved"})
        res = dispatcher.post(f"/api/dispatch/incidents/{a['id']}/archive", json={})
        hist = dispatcher.get("/api/dispatch/incidents/history").get_json()["items"]
        open_ids = [i["id"] for i in dispatcher.get("/api/dispatch/incidents").get_json()["items"]]
        check("F4. Resolved incident archives into Incident Report history",
              res.status_code == 200 and a["id"] in [i["id"] for i in hist] and a["id"] not in open_ids)
        events = dispatcher.get(f"/api/dispatch/incidents/{a['id']}").get_json()["item"]["events"]
        check("F5. Status history recorded", [ev["event"] for ev in events] == ["reported", "status", "status", "archived"], events)

        # Test G
        res = dispatcher.post("/api/dispatch/hotlines", json={"organization_name": "[E2E] Test Hotline", "phone_number": "+63 82 000 0000", "category": "medical"})
        hl = res.get_json().get("item", {})
        fresh = client_as("organization", dispatcher_id)  # a "page refresh": new client, data from the DB
        listed = [x for x in fresh.get("/api/dispatch/hotlines").get_json()["items"] if x["id"] == hl.get("id")]
        check("G. Hotline saved and still there after refresh", res.status_code == 201 and listed and listed[0]["tel"] == "tel:+63820000000")
        res = dispatcher.put(f"/api/dispatch/hotlines/{hl['id']}", json={"organization_name": "[E2E] Test Hotline 2", "phone_number": "911", "category": "police"})
        check("G2. Hotline edit saved", res.status_code == 200 and res.get_json()["item"]["phone_number"] == "911")

        # Test H
        codes = (citizen.post(f"/api/dispatch/incidents/{b['id']}/status", json={"status": "resolved"}).status_code,
                 anon.post(f"/api/dispatch/incidents/{b['id']}/status", json={"status": "resolved"}).status_code,
                 citizen.post("/api/dispatch/hotlines", json={"organization_name": "x", "phone_number": "911", "category": "police"}).status_code,
                 citizen.delete(f"/api/dispatch/hotlines/{hl['id']}").status_code,
                 citizen.get("/api/dispatch/incidents").status_code)
        check("H. Citizens/anonymous cannot read or modify incidents or hotlines via the server", codes == (403, 401, 403, 403, 403), codes)
        public = create_client(app.config["SUPABASE_URL"], app.config["SUPABASE_KEY"])
        check("H2. Public key cannot read incidents (contact numbers protected)",
              public.table("emergency_incidents").select("id, contact_number").execute().data == [])
        check("H3. Public key cannot update incidents",
              public.table("emergency_incidents").update({"status": "resolved"}).eq("id", b["id"]).execute().data == [])
        check("H4. Public key cannot read or change hotlines",
              public.table("responder_hotlines").select("id").execute().data == []
              and public.table("responder_hotlines").update({"phone_number": "000"}).eq("id", hl["id"]).execute().data == [])
        try:
            public.table("emergency_incidents").insert({"emergency_type": "Spoof"}).execute()
            blocked = False
        except Exception:
            blocked = True
        check("H5. Anonymous insert blocked (only signed-in citizens may report)", blocked)
        with app.app_context():
            still = get_supabase_admin().table("emergency_incidents").select("status").eq("id", b["id"]).execute().data[0]
        check("H6. Incident B unchanged after unauthorized attempts", still["status"] == "new")
    finally:
        with app.app_context():
            db = get_supabase_admin()
            db.table("emergency_incidents").delete().like("description", "[E2E]%").execute()
            db.table("responder_hotlines").delete().like("organization_name", "[E2E]%").execute()
        print("Cleaned up [E2E] rows.")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
