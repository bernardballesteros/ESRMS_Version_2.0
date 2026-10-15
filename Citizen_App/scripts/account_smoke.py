"""Drive My Profile and Security & MFA in the real Flet client against tests/fake_supabase.py.

Fills the real form fields and presses the real buttons, then checks what the (fake) server holds.
No request leaves this machine and no real account is touched.

    .venv\\Scripts\\python scripts\\account_smoke.py
"""
import asyncio
import os
import sys
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")]

import flet as ft  # noqa: E402
from fake_supabase import GOOD_CODE, FakeSupabase  # noqa: E402

import esrms  # noqa: E402
from esrms.backend import Backend, Session  # noqa: E402
from esrms.report import Location  # noqa: E402
from esrms.ui import account, announcements_view, screens, settings  # noqa: E402
from esrms.ui import theme as t  # noqa: E402
from esrms.ui.app import CitizenApp  # noqa: E402

SHOTS = os.environ.get("SMOKE_SHOTS")


def walk(control):
    if control is None:
        return
    yield control
    for name in ("content", "title"):
        child = getattr(control, name, None)
        if isinstance(child, ft.Control):
            yield from walk(child)
    for child in getattr(control, "controls", None) or []:
        yield from walk(child)


def texts(control):
    out = []
    for c in walk(control):
        if isinstance(c, ft.Text):
            out.append(c.value or "".join(s.text or "" for s in (c.spans or [])))
    return " | ".join(x for x in out if x)


async def main(page: ft.Page):
    page.bgcolor, page.padding, page.theme_mode = t.BG, 0, ft.ThemeMode.DARK
    page.theme = page.dark_theme = t.theme()
    page.window.width, page.window.height = 412, 900
    failures = []
    app = CitizenApp(page)
    page.data = app
    shooter = ft.Screenshot(content=page.controls[0], expand=True)
    page.controls[0] = shooter

    fake = FakeSupabase()
    juan = fake.add_user("juan@example.ph", "OldPass#123", full_name="Juan Dela Cruz", phone="0917 123 4567")
    fake.add_user("gina@example.ph", "unused", provider="google", full_name="Gina Reyes")
    app.backend = Backend(app.config, transport=fake.transport())
    app.announcements = announcements_view.AnnouncementsController(app)   # not started: no network
    app.announcements.loaded = True
    await app.backend.sign_in("juan@example.ph", "OldPass#123")

    def fields():
        return [c for c in walk(app.body.content) if isinstance(c, ft.TextField)]

    async def press(label):
        for c in walk(app.body.content):
            if getattr(c, "on_click", None) and label in texts(c) and not isinstance(c, (ft.Column, ft.Row)):
                result = c.on_click(None)
                if asyncio.iscoroutine(result):
                    await result
                await asyncio.sleep(0.5)   # handlers started with run_task
                return
        raise AssertionError(f"no button '{label}' on: {texts(app.body.content)[:200]}")

    async def step(name, fn):
        try:
            await fn()
            page.update()
            await asyncio.sleep(0.7)
            if SHOTS:
                with open(os.path.join(SHOTS, "acct_" + name.replace(" ", "_") + ".png"), "wb") as f:
                    f.write(await shooter.capture(pixel_ratio=1.0))
            print("OK  ", name)
        except Exception:
            failures.append(name)
            print("FAIL", name)
            traceback.print_exc()

    async def profile_loads():
        await account.show_profile(app)
        values = [f.value for f in fields()]
        assert values == ["Juan Dela Cruz", "juan@example.ph", "0917 123 4567", ""], values

    async def invalid_edit_rejected():
        await press("Edit Profile")
        name, email, phone, address = fields()
        name.value, phone.value = "", "abc"
        await press("Save Changes")
        assert name.error and phone.error, "invalid input must be flagged"
        assert fake.users[juan]["user_metadata"]["full_name"] == "Juan Dela Cruz", "nothing may be saved"

    async def cancel_discards():
        fields()[0].value = "Someone Else"
        await press("Cancel")
        assert fields()[0].value == "Juan Dela Cruz" and fake.users[juan]["user_metadata"]["full_name"] == "Juan Dela Cruz"

    async def save_updates_server():
        await press("Edit Profile")
        name, email, phone, address = fields()
        name.value, phone.value, address.value = "Juan D. Cruz", "+63 912 345 6789", "Davao City, Philippines"
        await press("Save Changes")
        meta = fake.users[juan]["user_metadata"]
        assert (meta["full_name"], meta["phone"], meta["address"]) == (
            "Juan D. Cruz", "+63 912 345 6789", "Davao City, Philippines"), meta
        assert fake.rows[juan]["phone"] == "+63 912 345 6789"
        assert "Profile saved." in texts(app.body.content)
        assert [f.value for f in fields()][0] == "Juan D. Cruz"

    async def reopened_profile_persists():
        app.home()
        await account.show_profile(app)
        assert [f.value for f in fields()] == ["Juan D. Cruz", "juan@example.ph", "+63 912 345 6789",
                                              "Davao City, Philippines"]

    async def offline_save_is_not_success():
        await press("Edit Profile")
        fields()[0].value = "Offline Name"
        fake.down = True
        await press("Save Changes")
        fake.down = False
        assert "NOT SAVED" in texts(app.body.content) and fake.users[juan]["user_metadata"]["full_name"] == "Juan D. Cruz"
        await press("Cancel")

    async def wrong_current_password():
        await account.show_security(app)
        current, new, confirm = fields()
        current.value, new.value, confirm.value = "wrong", "NewPass#2026", "NewPass#2026"
        await press("Change Password")
        assert current.error and fake.passwords[juan] == "OldPass#123"

    async def weak_password_rejected():
        current, new, confirm = fields()
        current.value, new.value, confirm.value = "OldPass#123", "short", "short"
        await press("Change Password")
        assert new.error and fake.passwords[juan] == "OldPass#123"

    async def password_changes():
        current, new, confirm = fields()
        current.value, new.value, confirm.value = "OldPass#123", "NewPass#2026", "NewPass#2026"
        await press("Change Password")
        assert fake.passwords[juan] == "NewPass#2026" and "Password changed." in texts(app.body.content)
        assert [f.value for f in fields()] == ["", "", ""], "password fields must be cleared"

    async def mfa_setup_opens():
        assert "Not Enabled" in texts(app.body.content)
        await press("Enable MFA")
        assert "JBSWY3DPEHPK3PXP" in texts(app.body.content)
        assert not app.backend.session.verified_totp

    async def wrong_code_does_not_enable():
        fields()[0].value = "000000"
        await press("Verify and enable MFA")
        assert "NOT enabled" in texts(app.body.content) and fake.users[juan]["factors"][0]["status"] == "unverified"

    async def right_code_enables():
        fields()[0].value = GOOD_CODE
        await press("Verify and enable MFA")
        shown = texts(app.body.content)
        assert fake.users[juan]["factors"][0]["status"] == "verified" and "Enabled" in shown and "Disable MFA" in shown

    async def sign_in_asks_for_code():
        await app.backend.sign_out()
        await app.backend.sign_in("juan@example.ph", "NewPass#2026")
        app.open_main()
        assert "Enter your authenticator code" in texts(app.body.content), "dashboard must stay closed"
        fields()[0].value = "111111"
        await press("Verify")
        assert "Code not accepted" in texts(app.body.content)
        fields()[0].value = GOOD_CODE
        await press("Verify")
        assert "DISPATCH SERVICES" in texts(app.body.content), "dashboard opens after the code"

    async def choose(open_dialog, value):
        """Open a settings dialog, pick `value` and press Apply (dialogs are overlays, so capture them)."""
        shown, real_show, real_pop = [], page.show_dialog, page.pop_dialog
        page.show_dialog, page.pop_dialog = shown.append, lambda: None
        try:
            open_dialog(app)
            dialog = shown[0]
            group = next(c for c in walk(dialog) if isinstance(c, ft.RadioGroup))
            radios = {c.value: c for c in walk(dialog) if isinstance(c, ft.Radio)}
            group.value = value
            await dialog.actions[1].on_click(None)          # Apply
            return group, radios
        finally:
            page.show_dialog, page.pop_dialog = real_show, real_pop

    async def settings_loads_real_account_info():
        await app.set_language("en")
        await app.set_theme("dark")
        await settings.show(app)
        shown = texts(app.body.content)
        for expected in ("Account Settings", "English", "Dark", "Citizen", "Apr 15, 2025", esrms.__version__):
            assert expected in shown, f"{expected!r} missing from: {shown}"

    async def language_changes_interface():
        _, radios = await choose(settings.language_dialog, "fil")
        assert radios["ceb"].disabled and not radios["fil"].disabled, "untranslated language must not be selectable"
        shown = texts(app.body.content)
        assert app.lang == "fil" and "Mga Setting ng Account" in shown and "Filipino" in shown, shown
        app.home()
        shown = texts(app.body.content)
        assert "MGA SERBISYO NG DISPATCH" in shown and "ANUNSYO" in shown, shown

    async def theme_changes_screens():
        await settings.show(app)
        await choose(settings.theme_dialog, "light")
        assert t.name == "light" and page.theme_mode == ft.ThemeMode.LIGHT and page.bgcolor == "#F3F6FA"
        assert app.body.content.bgcolor == "#F3F6FA" and "Maliwanag" in texts(app.body.content)

    async def preferences_survive_restart():
        app.lang = "en"
        app._apply_theme("dark")                      # what a fresh start looks like before prefs load
        await app.load_preferences()
        assert (app.lang, t.name) == ("fil", "light"), (app.lang, t.name)
        app.home()

    async def settings_offline_shows_saved_info():
        await app.set_language("en")
        fake.down = True
        await settings.show(app)
        fake.down = False
        shown = texts(app.body.content)
        assert "No connection" in shown and "Citizen" in shown and "Apr 15, 2025" in shown, shown

    async def back_to_dark():
        await app.set_theme("dark")
        await settings.show(app)
        assert t.name == "dark" and "Dark" in texts(app.body.content)

    # ---- Safety Radar (published public risk areas, fake server)
    me = (7.0700, 125.6100)

    def zone(area_id, level, north_km=0.0, east_km=0.0, name=None):
        return {"area_id": area_id, "area_name": name, "risk_level": level, "radius_km": 0.71,
                "center_latitude": me[0] + north_km / 110.574, "center_longitude": me[1] + east_km / 110.5,
                "public_summary": f"Published summary for a {level} area.", "updated_at": "2026-10-05T03:00:00Z"}

    def publish(areas, sufficient=True):
        fake.risk_areas = areas
        fake.risk_feed.update(version=fake.risk_feed["version"] + 1, sufficient=sufficient, period_days=30,
                              published_at=datetime.now(timezone.utc).isoformat())

    async def open_radar():
        app.tab = "command"
        app.switch_tab("radar")
        await asyncio.sleep(1.2)     # restore + fetch
        return texts(app.body.content)

    async def radar_tab_before_any_publication():
        app.location, app.location_state = None, "denied"
        shown = await open_radar()
        assert all(x in shown for x in ("COMMAND", "ANNOUNCEMENT", "RADAR", "SAFETY RADAR")), shown
        assert "has not been published by the ESRMS server yet" in shown and "LOW RISK" not in shown

    async def radar_shows_published_areas_near_me():
        publish([zone("a", "high", north_km=1.8), zone("b", "moderate", east_km=0.9, name="Near Talomo Barangay Hall"),
                 zone("c", "low", north_km=-4.0, east_km=3.0)])
        app.location, app.location_state = Location("gps", *me, accuracy_m=9, fixed_at=datetime.now(timezone.utc)), "ok"
        await app.radar._manual_refresh()
        shown = texts(app.body.content)
        for expected in ("YOUR AREA", "MODERATE RISK", "Elevated emergency activity has been observed nearby.",
                         "Near Talomo Barangay Hall", "Approximately 1.8 km away", "HIGH-RISK AREA", "LOW-RISK AREA",
                         "It does not indicate an active emergency", "UPDATED JUST NOW"):
            assert expected in shown, f"{expected!r} missing from: {shown}"
        for private in ("7.07", "125.61", "INC-", "incident", "responder", "hospital"):
            assert private not in shown.replace("historical emergency", ""), f"{private!r} must not be shown"

    async def radar_zone_details():
        shown_dialogs, real_show = [], page.show_dialog
        page.show_dialog = shown_dialogs.append
        try:
            await press("Near Talomo Barangay Hall")
        finally:
            page.show_dialog = real_show
        card = texts(shown_dialogs[0])
        for expected in ("MODERATE-RISK AREA", "Why am I seeing this?", "Published summary for a moderate area.",
                         "Based on emergency reports from the last 30 days.", "Safety reminder", "Oct 05, 2026"):
            assert expected in card, f"{expected!r} missing from: {card}"

    async def radar_updates_when_esrms_republishes():
        publish([zone("a", "high", north_km=0.2)])
        app.radar.fetched_at -= 400            # the periodic check is due
        await app.radar.tick()
        shown = texts(app.body.content)
        assert "HIGH RISK" in shown and "Near Talomo Barangay Hall" not in shown, shown

    async def radar_without_location():
        app.location, app.location_state = None, "denied"
        app.notify_status()
        shown = texts(app.body.content)
        assert "Location unavailable" in shown and "ENABLE LOCATION" in shown and "YOUR AREA" not in shown.replace(
            "YOUR AREA\n", ""), shown
        await press("VIEW GENERAL RISK MAP")
        shown = texts(app.body.content)
        assert "GENERAL MAP" in shown and "MAP CENTRE" in shown and "Approximately" not in shown, shown

    async def radar_not_enough_data():
        publish([], sufficient=False)
        await app.radar._manual_refresh()
        app.location, app.location_state = Location("gps", *me, fixed_at=datetime.now(timezone.utc)), "ok"
        app.radar._set_general(False)
        shown = texts(app.body.content)
        assert "NOT ENOUGH DATA" in shown and "LOW RISK" not in shown, shown

    async def radar_sweep_is_always_on():
        sweeps = [c for c in walk(app.body.content) if isinstance(getattr(c, "gradient", None), ft.SweepGradient)]
        assert len(sweeps) == 1 and not [c for c in walk(app.body.content) if isinstance(c, ft.Switch)]

    async def other_tabs_still_work():
        app.switch_tab("command")
        assert "DISPATCH SERVICES" in texts(app.body.content)
        app.switch_tab("announcement")
        assert "ANNOUNCEMENTS" in texts(app.body.content)
        app.switch_tab("radar")
        await asyncio.sleep(0.5)
        assert "SAFETY RADAR" in texts(app.body.content)
        app.switch_tab("command")

    # ---- Google sign-in, phone flow (the browser part is played by the fake server)
    opened = []
    gina = next(u for u in fake.users if fake.users[u]["email"] == "gina@example.ph")

    async def capture_url(url, **kw):
        opened.append(url)

    async def google_signin_screen():
        await app.backend.sign_out()
        type(app).is_desktop = property(lambda self: False)      # act as the Android build
        app.launcher.launch_url = capture_url                     # instead of opening a real browser
        app.show(screens.sign_in(app))
        await press("Continue with Google")
        q = parse_qs(urlparse(opened[-1]).query)
        assert q["provider"] == ["google"] and q["redirect_to"] == ["ph.esrms.citizen://auth/callback"], q
        assert "Waiting for Google..." in texts(app.body.content)
        return q["code_challenge"][0]

    async def google_returns_signed_in():
        challenge = await google_signin_screen()
        code = fake.google_code(gina, challenge)                  # Google + Supabase approved the user
        await app._route_changed(SimpleNamespace(route=f"/callback?code={code}"))
        s = app.backend.session
        assert s and s.user_id == gina and s.providers == ("google",), "must be signed in as the Google user"
        assert s.metadata.get("role") == "citizen" and "DISPATCH SERVICES" in texts(app.body.content)
        assert await app.pref("oauth_pkce") is None, "sign-in secret must be cleared"

    async def google_cancelled_in_browser():
        await google_signin_screen()
        await app._lifecycle_changed(SimpleNamespace(state=ft.AppLifecycleState.RESUME))   # back, no result
        await asyncio.sleep(5)
        shown = texts(app.body.content)
        assert "cancelled or not finished" in shown and "Continue with Google" in shown and not app.backend.session

    async def google_denied():
        await google_signin_screen()
        await app._route_changed(SimpleNamespace(route="/callback?error=access_denied"))
        assert "Google sign-in was cancelled." in texts(app.body.content) and not app.backend.session

    async def google_link_not_started_here():
        code = fake.google_code(gina, "a-challenge-from-somewhere-else")
        await app._route_changed(SimpleNamespace(route=f"/callback?code={code}"))
        assert "not started on this phone" in texts(app.body.content) and not app.backend.session
        del type(app).is_desktop

    async def google_account_guidance():
        app.backend.session = Session.from_auth(
            fake.session(next(u for u in fake.users if fake.users[u]["email"] == "gina@example.ph")))
        await account.show_security(app)
        shown = texts(app.body.content)
        assert "managed by Google" in shown and not fields(), "no password form for Google-only accounts"

    for name, fn in (("profile loads", profile_loads), ("invalid edit rejected", invalid_edit_rejected),
                     ("cancel discards", cancel_discards), ("save updates server", save_updates_server),
                     ("reopened profile persists", reopened_profile_persists),
                     ("offline save is not success", offline_save_is_not_success),
                     ("wrong current password", wrong_current_password),
                     ("weak password rejected", weak_password_rejected), ("password changes", password_changes),
                     ("mfa setup opens", mfa_setup_opens), ("wrong code does not enable", wrong_code_does_not_enable),
                     ("right code enables", right_code_enables), ("sign in asks for code", sign_in_asks_for_code),
                     ("settings loads real account info", settings_loads_real_account_info),
                     ("language changes interface", language_changes_interface),
                     ("theme changes screens", theme_changes_screens),
                     ("preferences survive restart", preferences_survive_restart),
                     ("settings offline shows saved info", settings_offline_shows_saved_info),
                     ("back to dark", back_to_dark),
                     ("radar tab before any publication", radar_tab_before_any_publication),
                     ("radar shows published areas near me", radar_shows_published_areas_near_me),
                     ("radar zone details", radar_zone_details),
                     ("radar updates when esrms republishes", radar_updates_when_esrms_republishes),
                     ("radar without location", radar_without_location),
                     ("radar not enough data", radar_not_enough_data),
                     ("radar sweep is always on", radar_sweep_is_always_on),
                     ("other tabs still work", other_tabs_still_work),
                     ("google returns signed in", google_returns_signed_in),
                     ("google cancelled in browser", google_cancelled_in_browser),
                     ("google denied", google_denied),
                     ("google link not started here", google_link_not_started_here),
                     ("google account guidance", google_account_guidance)):
        await step(name, fn)
    # leave this PC's saved preferences as they were by default
    await app.set_language("en")
    await app.set_theme("dark")
    await app.set_pref("account_info", None)
    await app.set_pref("radar_cache", None)

    print("\nACCOUNT SMOKE RESULT:", "PASS" if not failures else f"FAIL {failures}")
    await asyncio.sleep(0.5)
    await page.window.close()
    os._exit(1 if failures else 0)


ft.run(main, assets_dir=os.path.join(ROOT, "src", "assets"))
