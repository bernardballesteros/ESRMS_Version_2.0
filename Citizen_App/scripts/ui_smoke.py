"""Render every screen once in the real Flet client with fake data, then close. Sends nothing.

    .venv\\Scripts\\python scripts\\ui_smoke.py
"""
import asyncio
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from datetime import datetime, timedelta, timezone  # noqa: E402

import flet as ft  # noqa: E402
import httpx  # noqa: E402

from esrms import report  # noqa: E402
from esrms.backend import Backend, Session  # noqa: E402
from esrms.outbox import Outbox  # noqa: E402
from esrms.report import Location  # noqa: E402
from esrms.ui import announcements_view, command, screens, sos  # noqa: E402
from esrms.ui import theme as t  # noqa: E402
from esrms.ui.app import CitizenApp  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 600))
ROWS = [
    dict(id="a1", title="Typhoon signal no. 2", message="Prepare go-bags. Evacuation centers are open.",
         category="emergency", priority="critical", target_audience="all_citizens", barangay=None,
         is_pinned=True, published_at=NOW, expires_at=None),
    dict(id="a2", title="Water interruption", message="Poblacion, 1PM-5PM.", category="maintenance",
         priority="low", target_audience="barangay", barangay="Poblacion", is_pinned=False,
         published_at=NOW, expires_at=None),
]
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "assets", "icon.png"),
          "rb") as _f:
    PHOTO = _f.read()  # stands in for a picture from the gallery or camera
SHOTS = os.environ.get("SMOKE_SHOTS")  # folder to save a PNG of each screen, optional
STORED = {"id": "i1", "incident_number": 123, "status": "new", "emergency_type": "Medical & EMS: Unconscious person",
          "latitude": 7.0732, "longitude": 125.6129, "created_at": NOW}


async def main(page: ft.Page):
    page.bgcolor, page.padding, page.theme_mode = t.BG, 0, ft.ThemeMode.DARK
    page.window.width, page.window.height = 412, 900
    failures = []

    async def step(name, fn):
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            page.update()
            await asyncio.sleep(0.8)
            if SHOTS:
                png = await shooter.capture(pixel_ratio=1.0)
                with open(os.path.join(SHOTS, name.replace(" ", "_").replace("(", "").replace(")", "") + ".png"), "wb") as f:
                    f.write(png)
            print("OK  ", name)
        except Exception:
            failures.append(name)
            print("FAIL", name)
            traceback.print_exc()

    app = CitizenApp(page)
    page.data = app
    app._apply_theme(os.environ.get("SMOKE_THEME", "dark"))   # SMOKE_THEME=light renders every screen in light
    app.lang = os.environ.get("SMOKE_LANG", "en")
    shooter = ft.Screenshot(content=page.controls[0], expand=True)
    page.controls[0] = shooter
    app.backend.session = Session("jwt", "r", time.time() + 3600, "u1", "c@x.ph", False,
                                  {"full_name": "Juan Dela Cruz", "phone": "+63 900 000 0000"})
    app.online = True
    app.announcements = announcements_view.AnnouncementsController(app)  # not started: no network
    app.announcements.rows, app.announcements.loaded = ROWS, True

    await step("onboarding", lambda: app.show(screens.onboarding(app)))
    await step("sign in", lambda: app.show(screens.sign_in(app)))
    await step("register", lambda: app.show(screens.sign_in(app, "register")))
    await step("offline restore", lambda: app.show(screens.offline_restore(app)))

    utc = datetime.now(timezone.utc)
    app.location_state, app.location = "ok", Location("gps", 7.073210, 125.612901, accuracy_m=9, fixed_at=utc)
    await step("command (gps ok)", lambda: app.render_main())

    # An SOS written offline: in-memory outbox, nothing is sent or stored outside this process.
    saved = {}

    async def read():
        return saved.get("v")

    async def write(v):
        saved["v"] = v
        return True

    app.outbox = Outbox(read, write)

    # A fake ESRMS server that is unreachable until link["up"]; the real one is never contacted here.
    link, received = {"up": False}, []

    def server(req):
        if not link["up"]:
            raise httpx.ConnectError("no signal")
        if req.method == "POST" and req.url.path == "/rest/v1/emergency_incidents":
            received.append(json.loads(req.content))
            return httpx.Response(201, json=[{**STORED, **{k: received[-1][k] for k in (
                "client_request_id", "emergency_type", "latitude", "longitude", "location_source")}}])
        return httpx.Response(200, json=[])

    real_backend = app.backend
    app.backend = Backend(app.config, transport=httpx.MockTransport(server))
    app.backend.session = real_backend.session
    old =Location("gps", 7.073210, 125.612901, accuracy_m=35, fixed_at=utc - timedelta(minutes=14), cached=True)
    entry = await app.outbox.add(
        report.build_incident(reporter_id="u1", category_key="medical", incident_type="Injury or accident",
                              description="Fell on the trail", location=old, citizen_name="Juan Dela Cruz",
                              contact_number="+63 900 000 0000", queued_at=utc),
        saved_at=utc, fixed_at=old.fixed_at, cached=True)
    # The device answered only with a stored position: it is kept apart and is not the location.
    app.online, app.location, app.old_location = False, None, old
    app.location_state, app.location_error = "error", "stale"
    await step("command (offline, sos pending)", lambda: app.render_main())
    await step("sos pending (not sent)", lambda: sos.show_pending(app, entry["id"]))
    await step("no signal help", lambda: sos.no_signal_help(app))
    page.pop_dialog()
    offline_flow = sos.SosFlow(app, None)
    app.location_state, app.locating_since = "locating", time.monotonic() - 12
    await step("sos select (acquiring)", lambda: offline_flow.show_select())
    app.location_state = "error"
    await step("sos select (no fresh fix)", lambda: offline_flow.show_select())
    assert offline_flow._location() is None, "an old stored position must not be used by itself"
    offline_flow._set_use_old(True)
    await step("sos select (old position chosen)", lambda: offline_flow.show_select())
    await step("sos confirm (offline)", lambda: offline_flow.show_confirm())
    await step("my reports (offline)", lambda: sos.show_my_reports(app))
    await step("sos delivered late", lambda: sos.show_delivered(app, entry, STORED))
    await app.outbox.remove(entry["id"])

    async def send_with_no_signal():
        offline_flow._set_photo(PHOTO)  # not uploadable with no signal: the report is saved without it
        offline_flow.show_confirm()
        await offline_flow._send(None)
        assert len(app.my_pending()) == 1 and not received, "report must be saved, not sent"
        assert json.loads(saved["v"])[0]["row"]["latitude"] == old.latitude, "coordinates must be on disk"

    async def signal_returns():
        link["up"] = True
        error = await app.reconnect()
        assert error is None and len(received) == 1 and not app.my_pending(), "saved report must be delivered once"
        sent = received[0]
        assert (sent["latitude"], sent["longitude"], sent["contact_number"]) == (old.latitude, old.longitude,
                                                                                "+63 900 000 0000")
        assert "OFFLINE REPORT" in sent["description"] and "LAST KNOWN" in sent["description"]
        assert await app.reconnect() is None and len(received) == 1, "must not be sent twice"

    await step("send with no signal, saved", send_with_no_signal)
    await step("signal returns, received", signal_returns)
    app.backend = real_backend
    app.online, app.old_location, app.location_error = True, None, None

    app.location_state, app.location = "denied", None
    await step("command (gps denied)", lambda: app.render_main())

    flow = sos.SosFlow(app, "medical")
    await step("sos select (medical)", lambda: flow.show_select())
    flow.incident_type = "Unconscious person"
    flow.manual = Location("manual", note="Near the plaza fountain")
    await step("sos select (manual loc)", lambda: flow.show_select())
    await step("sos photo options", lambda: flow._photo_options())
    page.pop_dialog()
    await step("sos select (photo chosen)", lambda: flow._set_photo(PHOTO))
    await step("sos confirm", lambda: flow.show_confirm())
    await step("sos confirm (network error)", lambda: flow.show_confirm(error="NOT CONFIRMED: test", retry_hint=True))
    await step("sos result", lambda: flow.show_result(STORED))
    general = sos.SosFlow(app, None)
    await step("sos general", lambda: general.show_select())
    await step("my reports card", lambda: app.show(ft.Column([sos._report_card(STORED)])))

    app.tab = "announcement"
    await step("announcements", lambda: app.render_main())
    await step("profile menu", lambda: screens.open_profile_menu(app))
    page.pop_dialog()  # My Profile and Security & MFA are driven by scripts/account_smoke.py
    await step("settings dialog", lambda: screens.open_settings(app))

    print("\nSMOKE RESULT:", "PASS" if not failures else f"FAIL {failures}")
    await asyncio.sleep(1)
    await page.window.close()
    os._exit(1 if failures else 0)


ft.run(main, assets_dir=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "assets"))
