"""Live end-to-end check of incident photos against your Supabase project.

    .venv\\Scripts\\python scripts\\incident_photos_e2e.py

A. Can a signed-in citizen upload a photo? (needs supabase/migrations/013_incident_photos.sql)
B-E. Does the Dispatch Center receive that photo for the right incident, and none for a report without one?

Acts as a temporary citizen account. If A fails the photo is stored with the service role instead, so
the dashboard side is still checked. The account, the photo and the test reports are removed at the end.
Add --keep to leave the two test reports in the Dispatch Center for a look in the browser; run again
without it to remove them.
"""
import os
import struct
import sys
import uuid
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from supabase import create_client  # noqa: E402

from app import create_app  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402

EMAIL = "e2e-incident-photo@example.com"
PASSWORD = uuid.uuid4().hex + "aA1!"
KEEP = "--keep" in sys.argv

app = create_app()
app.config.update(TESTING=True)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def png(width=640, height=360):
    """A real PNG (red/blue gradient), built without an imaging library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"".join(bytes((200, 40 + x * 100 // width, y * 255 // height)) for x in range(width))
                    for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def cleanup(db, bucket):
    db.table("emergency_incidents").delete().like("description", "[E2E photo]%").execute()
    for user in db.auth.admin.list_users(page=1, per_page=1000):
        if user.email == EMAIL:
            files = [f"incidents/{user.id}/{o['name']}" for o in bucket.list(f"incidents/{user.id}")]
            if files:
                bucket.remove(files)
            db.auth.admin.delete_user(user.id)


def main():
    with app.app_context():
        db = get_supabase_admin()
        bucket = db.storage.from_(app.config["MEDIA_BUCKET"])
        cleanup(db, bucket)   # leftovers of an earlier --keep run
        dispatcher_id = db.auth.admin.list_users(page=1, per_page=1)[0].id
        citizen = db.auth.admin.create_user({"email": EMAIL, "password": PASSWORD, "email_confirm": True,
                                             "user_metadata": {"role": "citizen", "full_name": "[E2E] Photo Check"}}).user
        try:
            # A. Exactly what the Citizen App does (src/esrms/backend.py upload_incident_photo).
            public = create_client(app.config["SUPABASE_URL"], app.config["SUPABASE_KEY"])
            token = public.auth.sign_in_with_password({"email": EMAIL, "password": PASSWORD}).session.access_token
            path = f"incidents/{citizen.id}/{uuid.uuid4().hex}.png"
            base = f"{app.config['SUPABASE_URL'].rstrip('/')}/storage/v1/object"
            image = png()
            res = httpx.post(f"{base}/{app.config['MEDIA_BUCKET']}/{path}", content=image, timeout=30, headers={
                "apikey": app.config["SUPABASE_KEY"], "Authorization": f"Bearer {token}", "Content-Type": "image/png"})
            uploaded = res.status_code < 400
            check("A. A signed-in citizen can upload an incident photo (013_incident_photos.sql)", uploaded,
                  f"HTTP {res.status_code} {res.text[:160]} -> run supabase/migrations/013_incident_photos.sql in the Supabase SQL Editor")
            if not uploaded:
                bucket.upload(path, image, {"content-type": "image/png"})
            url = f"{base}/public/{app.config['MEDIA_BUCKET']}/{path}"

            row = {"reporter_id": citizen.id, "emergency_type": "Fire", "latitude": 7.0780, "longitude": 125.5234}
            incidents = db.table("emergency_incidents")
            with_photo = incidents.insert({**row, "description": f"[E2E photo] smoke from a roof\n[Photo attached by citizen: {url}]"}).execute().data[0]
            without = incidents.insert({**row, "description": "[E2E photo] report without a photo"}).execute().data[0]

            c = app.test_client()
            with c.session_transaction() as s:
                s["user"] = {"id": dispatcher_id, "email": "e2e-dispatcher@example.com", "role": "organization", "name": "E2E"}
            listed = {i["id"]: i for i in c.get("/api/dispatch/incidents").get_json()["items"]}
            detail = c.get(f"/api/dispatch/incidents/{with_photo['id']}").get_json()["item"]
            check("B. The incident carries its photo in the list and when opened",
                  listed[with_photo["id"]]["photos"] == [url] and detail["photos"] == [url], detail.get("photos"))
            check("C. The photo link is no longer shown as report text", detail["description"] == "[E2E photo] smoke from a roof", detail["description"])
            check("D. A report without a photo has none", listed[without["id"]]["photos"] == []
                  and c.get(f"/api/dispatch/incidents/{without['id']}").get_json()["item"]["photos"] == [])
            got = httpx.get(url, timeout=30)
            check("E. The dispatcher's browser can load the stored photo",
                  got.status_code == 200 and got.headers.get("content-type") == "image/png" and got.content == image,
                  f"HTTP {got.status_code} {got.headers.get('content-type')}")
        finally:
            if KEEP:
                print("Kept the two [E2E photo] reports and the temporary citizen. Run again without --keep to remove them.")
            else:
                cleanup(db, bucket)
                print("Removed the temporary citizen, the photo and the [E2E photo] reports.")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
