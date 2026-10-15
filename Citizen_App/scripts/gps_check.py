"""Check GPS acquisition on THIS machine with the real location API. Sends nothing, signs in nowhere.

Opens the SOS screen (which starts a location request), then presses Retry GPS twice, and prints
what the device answered each time. On a PC there is no GPS receiver, so this only proves that
each press makes new requests and that old answers are rejected; satellite fixes need the phone.

    .venv\\Scripts\\python scripts\\gps_check.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import flet as ft  # noqa: E402

from esrms.ui import sos  # noqa: E402
from esrms.ui import theme as t  # noqa: E402
from esrms.ui.app import CitizenApp  # noqa: E402


async def main(page: ft.Page):
    page.bgcolor, page.padding, page.theme_mode = t.BG, 0, ft.ThemeMode.DARK
    page.window.width, page.window.height = 412, 900
    app = CitizenApp(page)
    page.data = app

    requests = []
    real_request = app.geo.get_current_position

    async def counted(configuration=None):  # passes straight through to the real geolocator
        pos = await real_request(configuration)
        requests.append(pos.timestamp)
        return pos

    app.geo.get_current_position = counted

    def report_state(label, before, started):
        fix, old = app.location, app.old_location
        print(f"\n{label}: {len(requests) - before} new device request(s) in {time.monotonic() - started:.1f} s"
              f" (timeout {app.config.gps_timeout_seconds} s)")
        print(f"  state={app.location_state} reason={app.location_error}")
        if fix and fix.is_current():
            print(f"  FRESH FIX accuracy={fix.accuracy_m} m  fix time={fix.fixed_at.astimezone():%Y-%m-%d %H:%M:%S}"
                  f"  age={fix.age_seconds():.0f} s")
        else:
            print("  no fresh fix")
        if old:
            print(f"  old stored position kept apart: {old.freshness()}")
        print(f"  raw timestamp from device: {requests[-1]!r}" if requests else "  device returned nothing")

    print("platform:", page.platform)
    flow = sos.SosFlow(app, None)
    flow.show_select()
    before, started = len(requests), time.monotonic()
    await app.locate(True)                      # what sos.start() runs when the SOS screen opens
    report_state("SOS screen opened", before, started)
    for n in (1, 2):
        before, started = len(requests), time.monotonic()
        flow._retry_gps()                       # the Retry GPS / Refresh GPS button handler
        await app.locate(True)                  # joins the request the button just started
        report_state(f"Retry GPS press {n}", before, started)
    await page.window.close()
    os._exit(0)


ft.run(main)
