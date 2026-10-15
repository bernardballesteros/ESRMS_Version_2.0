"""App controller: session, permissions, GPS, navigation between screens."""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import flet as ft
import flet_geolocator as ftg
import flet_permission_handler as fph

from esrms import config as cfg
from esrms import gps, i18n, oauth, report
from esrms.backend import Backend, BackendError
from esrms.outbox import Outbox, parse_time
from esrms.ui import theme as t

log = logging.getLogger(__name__)

PREF = "esrms."
GRANTED = {ftg.GeolocatorPermissionStatus.WHILE_IN_USE, ftg.GeolocatorPermissionStatus.ALWAYS}
HEARTBEAT_SECONDS = 30


class CitizenApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.config = cfg.load()
        self.backend = Backend(self.config)
        self.backend.on_session_change = lambda s: page.run_task(self._persist_session, s)

        # Services must stay referenced or Flet unregisters them.
        self.prefs = ft.SharedPreferences()
        self.geo = ftg.Geolocator(configuration=ftg.GeolocatorConfiguration(
            accuracy=ftg.GeolocatorPositionAccuracy.BEST,
            time_limit=ft.Duration(seconds=self.config.gps_timeout_seconds)))
        self.perms = fph.PermissionHandler()
        self.launcher = ft.UrlLauncher()
        self.connectivity = ft.Connectivity(on_change=self._connectivity_changed)
        self.picker = ft.FilePicker()     # profile picture
        self.clipboard = ft.Clipboard()   # MFA setup key
        page.on_app_lifecycle_state_change = self._lifecycle_changed
        page.on_route_change = self._route_changed   # Google sign-in returns as a deep link
        self.google_pending = False       # the browser is open on Google's sign-in page
        self.on_google_result = None      # sign-in screen callback: (error text or None)

        # Only ever a fix that was fresh when the device returned it (never invented, never a cached
        # position). Kept when a later attempt fails; it then ages visibly by its own timestamp.
        self.location = None
        # A stored position the device answered with instead of a fresh fix. Shown separately with
        # its age and attached to a report only if the citizen chooses it.
        self.old_location = None
        self.location_state = "unknown"   # unknown | locating | ok | denied | denied_forever | off | error
        self.location_error = None        # after a failed run: timeout | stale | failed | off | denied
        self.locating_since = 0.0
        self.want_fix = False             # an SOS is being written: keep trying for a current fix
        self.notification_state = "unknown"
        self.online = None                # did the ESRMS server answer the last request
        self.has_network = None           # does the OS report any data connection
        self.profile = None               # who is signed in, saved so an SOS can be written offline
        self.outbox = Outbox(self._read_outbox, self._write_outbox)
        self.delivered = {}               # outbox id -> stored row, for reports confirmed this session
        self.offline_view = None          # the "no connection" screen while the session can't be restored
        self.barangay = ""
        self.lang = i18n.DEFAULT
        self.account_info = None          # role + creation date last seen for the signed-in account
        self.unseen_urgent = 0
        self.tab = "command"
        self._listeners = []              # callbacks re-rendering status widgets

        self.body = ft.Container(expand=True)
        page.add(ft.SafeArea(content=self.body, expand=True))

    # ------------------------------------------------------------ prefs

    async def pref(self, key, default=None):
        try:
            value = await self.prefs.get(PREF + key)
        except Exception:
            return default
        return default if value is None else value

    async def set_pref(self, key, value):
        try:
            if value is None:
                await self.prefs.remove(PREF + key)
            else:
                await self.prefs.set(PREF + key, value)
        except Exception:
            log.warning("Could not save preference %s", key, exc_info=True)

    async def _persist_session(self, session):
        # Only the refresh token is stored; access tokens are short-lived and kept in memory.
        await self.set_pref("refresh_token", session.refresh_token if session else None)
        # Name/phone/user id (no tokens) so an SOS can be written before the session is restored.
        self.profile = self.identity() if session else None
        await self.set_pref("profile", json.dumps(self.profile) if session else None)
        if not session or (self.account_info or {}).get("user_id") != session.user_id:
            self.account_info = None      # never show one account's details under another
            await self.set_pref("account_info", None)

    # ------------------------------------------------------------ language, theme, account info

    def tr(self, key, **values):
        return i18n.tr(key, self.lang, **values)

    async def _save_pref(self, key, value):
        """True only if the phone stored the preference (read back)."""
        await self.set_pref(key, value)
        return await self.pref(key) == value

    async def set_language(self, code):
        self.lang = i18n.normalize(code)
        return await self._save_pref("language", self.lang)

    def _apply_theme(self, name):
        t.apply(name)
        self.page.theme = self.page.dark_theme = t.theme()
        self.page.theme_mode, self.page.bgcolor = t.mode(), t.BG

    async def set_theme(self, name):
        self._apply_theme(name)
        return await self._save_pref("theme", t.name)

    async def load_preferences(self):
        """Restore the saved language and theme (app start)."""
        self.lang = i18n.normalize(await self.pref("language"))
        self._apply_theme(await self.pref("theme", "dark"))

    async def refresh_account_info(self):
        self.account_info = await self.backend.account_info()
        await self.set_pref("account_info", json.dumps(self.account_info))
        return self.account_info

    def identity(self):
        """The signed-in citizen, or the one last signed in on this phone when offline. None if nobody."""
        s = self.backend.session
        if not s:
            return self.profile
        return {"user_id": s.user_id, "email": s.email, "is_anonymous": s.is_anonymous,
                "full_name": s.metadata.get("full_name") or "", "phone": s.metadata.get("phone") or ""}

    async def _read_outbox(self):
        return await self.prefs.get(PREF + "outbox")

    async def _write_outbox(self, text):
        return await self.prefs.set(PREF + "outbox", text)

    # ------------------------------------------------------------ startup

    # ------------------------------------------------------------ Google sign-in (phone: deep link)

    @property
    def is_desktop(self):
        return self.page.platform in (ft.PagePlatform.WINDOWS, ft.PagePlatform.MACOS, ft.PagePlatform.LINUX)

    async def start_google_sign_in(self):
        """Open Google's account chooser in the browser. Returns an error text, or None once it is open
        (phones) / once signed in (desktop). The phone flow finishes in _finish_google()."""
        if self.is_desktop:
            try:
                await self.backend.sign_in_with_google(self.launcher.launch_url)
            except BackendError as exc:
                return str(exc)
            return await self._after_google()
        verifier, challenge = oauth.new_pkce()
        # Kept on the phone so the sign-in can finish even if Android closes the app meanwhile.
        if not await self._save_pref("oauth_pkce", json.dumps({"verifier": verifier, "at": time.time()})):
            return "The phone could not store the sign-in request. Try again."
        self.google_pending = True
        try:
            await self.launcher.launch_url(oauth.authorize_url(self.config.supabase_url, oauth.REDIRECT_URL,
                                                               challenge),
                                           mode=ft.LaunchMode.EXTERNAL_APPLICATION)
        except Exception:
            log.warning("Could not open the browser for Google sign-in", exc_info=True)
            self.google_pending = False
            await self.set_pref("oauth_pkce", None)
            return "Couldn't open a browser for Google sign-in."
        return None

    async def _route_changed(self, e):
        if oauth.is_callback(e.route):
            await self._finish_google(e.route)

    async def _finish_google(self, route):
        """The browser handed the app back its Google sign-in result."""
        self.google_pending = False
        result = oauth.parse_callback(route)
        error = result.get("error")
        try:
            saved = json.loads(await self.pref("oauth_pkce") or "null")
        except ValueError:
            saved = None
        await self.set_pref("oauth_pkce", None)   # single use, whatever happens
        if not error and (not saved or time.time() - saved.get("at", 0) > oauth.VERIFIER_MAX_AGE):
            error = "That Google sign-in link has expired or was not started on this phone. Try again."
        if not error:
            try:
                await self.backend.exchange_google_code(result["code"], saved["verifier"])
            except BackendError as exc:
                error = ("No connection. Google sign-in was not completed." if exc.kind == "network"
                         else str(exc))
        if not error:
            error = await self._after_google()
        if error:
            self._google_failed(error)

    async def _after_google(self):
        self.online = True
        self.open_main()   # shows the MFA code screen first if the account has an authenticator
        return None

    def _google_failed(self, error):
        from esrms.ui import screens
        if self.on_google_result:
            self.on_google_result(error)
        else:   # cold start from the link: no sign-in screen yet
            self.show(screens.sign_in(self, notice=error))

    async def _google_maybe_cancelled(self):
        """Back in the app while Google was open: if no result arrives shortly, the browser was closed."""
        await asyncio.sleep(4)
        if self.google_pending and not self.backend.session:
            self.google_pending = False
            await self.set_pref("oauth_pkce", None)
            self._google_failed("Google sign-in was cancelled or not finished.")

    async def start(self):
        from esrms.ui import screens
        self.barangay = await self.pref("barangay", "")
        await self.load_preferences()
        try:
            self.profile = json.loads(await self.pref("profile") or "null")
            info = json.loads(await self.pref("account_info") or "null")
            self.account_info = info if info and self.profile and info.get("user_id") == self.profile.get(
                "user_id") else None
        except ValueError:
            self.profile = None
        await self.outbox.load()
        try:
            self._set_network(await self.connectivity.get_connectivity())
        except Exception:
            log.info("Connectivity status unavailable", exc_info=True)
        self.page.run_task(self._heartbeat)
        if not await self.pref("onboarded", False):
            self.show(screens.onboarding(self))
            return
        await self.after_onboarding()

    async def after_onboarding(self):
        from esrms.ui import screens
        await self.refresh_permission_states()
        token = await self.pref("refresh_token")
        if token:
            self.show(screens.splash("Restoring your session..."))
            try:
                await self.backend.refresh(token)
                self.online = True
            except BackendError as exc:
                self.online = exc.kind != "network"
                if exc.kind == "network":
                    self.show_offline()
                    return
        if oauth.is_callback(self.page.route):
            # Android started the app from the Google sign-in link (it had been closed meanwhile).
            self.show(screens.splash("Finishing Google sign-in..."))
            await self._finish_google(self.page.route)
            return
        if self.backend.session:
            self.open_main()
        else:
            self.show(screens.sign_in(self))

    def show_offline(self):
        from esrms.ui import screens
        self._listeners.clear()
        if not self.identity():
            return self.show(screens.sign_in(self))
        self.offline_view = screens.offline_restore(self)
        self.show(self.offline_view)

    def home(self):
        """Where 'back' leads: the dashboard when signed in, the offline screen when the session isn't restored."""
        self.want_fix = False
        if not self.backend.session:
            self.show_offline()
        elif getattr(self, "announcements", None):
            self.render_main()
        else:
            self.open_main()

    # ------------------------------------------------------------ connection + pending reports

    def _set_network(self, kinds):
        self.has_network = any(k not in (ft.ConnectivityType.NONE, ft.ConnectivityType.BLUETOOTH) for k in kinds)
        if not self.has_network:
            self.online = False

    async def _connectivity_changed(self, e):
        self._set_network(e.connectivity)
        self.notify_status()
        if self.has_network:
            await self.reconnect()

    def my_pending(self):
        who = self.identity()
        return self.outbox.pending(who["user_id"]) if who else []

    async def reconnect(self, e=None):
        """Restore the session if needed, then send this citizen's saved reports.

        Returns None when everything saved was confirmed by ESRMS, else the BackendError that stopped it.
        """
        who = self.identity()
        if not who:
            return None
        if not self.backend.session:
            token = await self.pref("refresh_token")
            if not token:
                return BackendError("Sign in again to send your saved reports.", "auth")
            try:
                await self.backend.refresh(token)
            except BackendError as exc:
                self.online = exc.kind != "network"
                self.notify_status()
                if exc.kind == "auth" and self.offline_view is not None and self.body.content is self.offline_view:
                    from esrms.ui import screens
                    self.show(screens.sign_in(self))
                return exc
            self.online = True
        events, error = await self.outbox.flush(self.backend.submit_incident, who["user_id"])
        if error:
            self.online = error.kind != "network"
        elif events:
            self.online = True
        for kind, entry, stored in events:
            if kind == "sent":
                self.delivered[entry["id"]] = stored
                self.toast(f"Saved SOS now RECEIVED by ESRMS: {report.incident_code(stored)}", t.alpha(t.GREEN, 0.9))
            else:
                self.toast("ESRMS refused a saved SOS. Open My reports.", t.alpha(t.RED, 0.9))
        if self.offline_view is not None and self.body.content is self.offline_view:
            self.offline_view = None
            self.open_main()
        self.notify_status()
        return error

    async def _heartbeat(self):
        """While the app is open: retry saved reports, keep trying for a GPS fix, refresh shown ages."""
        tick = 0
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            tick += 1
            try:
                waiting = self.my_pending() or (self.identity() and not self.backend.session)
                # The OS connectivity flag can be wrong, so every 4th tick tries anyway.
                if waiting and (self.has_network is not False or tick % 4 == 0):
                    await self.reconnect()
                if (self.want_fix and self.location_state in ("ok", "error", "unknown")
                        and not (self.location and self.location.is_current())):
                    self.page.run_task(self.locate, False)
                if self.tab == "radar" and getattr(self, "radar", None) and self.backend.session:
                    await self.radar.tick()   # re-checks the published risk areas every few minutes
                self.notify_status()
            except Exception:
                log.debug("heartbeat failed", exc_info=True)

    async def open_sms(self, entry_or_text):
        """Open the phone's own SMS app with the SOS text filled in. The app cannot tell if it was sent."""
        text = entry_or_text if isinstance(entry_or_text, str) else report.sms_text(
            entry_or_text["row"], fixed_at=parse_time(entry_or_text.get("fixed_at")),
            cached=entry_or_text.get("cached"), place=entry_or_text.get("place"),
            written_at=parse_time(entry_or_text.get("saved_at")))
        try:
            await self.launcher.launch_url(report.sms_url(self.config.sos_sms_number, text))
        except Exception:
            log.info("Could not open the SMS app", exc_info=True)
            self.toast("Couldn't open the SMS app on this device.", t.alpha(t.AMBER, 0.9))

    def open_main(self, tab="command"):
        from esrms.ui import announcements_view
        self.offline_view = None
        if self.backend.session and self.backend.session.needs_mfa:
            from esrms.ui import account
            self._listeners.clear()
            return self.show(account.mfa_challenge(self))  # dashboard opens only after the code is verified
        self.tab = tab
        if not getattr(self, "announcements", None):
            self.announcements = announcements_view.AnnouncementsController(self)
            self.announcements.start()
        self.render_main()
        if self.location_state in ("ok", "unknown"):
            self.page.run_task(self.locate, False)
        if self.my_pending():
            self.page.run_task(self.reconnect)  # deliver anything saved while offline

    def render_main(self):
        from esrms.ui import command
        self._listeners.clear()
        if self.tab == "radar":
            from esrms.ui import radar_view
            if not getattr(self, "radar", None):
                self.radar = radar_view.RadarController(self)
            content = self.radar.build()
        else:
            content = command.build(self) if self.tab == "command" else self.announcements.build()
        self.show(ft.Column([ft.Container(content, expand=True), self._nav()], spacing=0, expand=True))

    def switch_tab(self, tab):
        if tab != self.tab:
            self.tab = tab
            if tab == "announcement":
                self.unseen_urgent = 0
            self.render_main()

    def _nav(self):
        def item(key, icon, text):
            on = self.tab == key
            color = t.CYAN if on else t.MUTED
            badge = None
            if key == "announcement" and self.unseen_urgent and not on:
                badge = ft.Container(t.dot(t.RED, 9), right=-4, top=-2)
            icon_stack = ft.Stack([ft.Icon(getattr(ft.Icons, icon), color=color, size=26)] + ([badge] if badge else []))
            return ft.Container(
                ft.Column([icon_stack, t.mono(text, size=11, color=color, weight=ft.FontWeight.BOLD, max_lines=1,
                                              overflow=ft.TextOverflow.ELLIPSIS),
                           ft.Container(height=3, width=56, bgcolor=t.CYAN if on else None,
                                        border_radius=ft.BorderRadius.all(3))],
                          spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                expand=True, padding=t.pad(0, 10), on_click=lambda e: self.switch_tab(key), ink=True,
            )

        return ft.Container(
            ft.Row([item("command", "SHIELD", self.tr("nav.command")),
                    item("announcement", "CAMPAIGN", self.tr("nav.announcement"))], spacing=0),
            bgcolor=t.BG_TOP, border=ft.Border(top=ft.BorderSide(1, t.BORDER)),
        )

    def show(self, control):
        self.body.content = control
        self.page.update()

    def on_status_change(self, fn):
        self._listeners.append(fn)

    def notify_status(self):
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.debug("status listener failed", exc_info=True)
        self.page.update()

    def toast(self, message, color=None):
        self.page.show_dialog(ft.SnackBar(ft.Text(message, color=t.TEXT), bgcolor=color or t.PANEL_HI,
                                          behavior=ft.SnackBarBehavior.FLOATING))

    async def call_hotline(self, e=None):
        await self.launcher.launch_url(f"tel:{self.config.emergency_hotline}")

    # ------------------------------------------------------------ permissions

    async def refresh_permission_states(self):
        try:
            status = await self.geo.get_permission_status()
            if status in GRANTED:
                if self.location_state in ("denied", "denied_forever", "error"):
                    self.location_state = "unknown"
            elif status == ftg.GeolocatorPermissionStatus.DENIED_FOREVER:
                self.location_state = "denied_forever"
            else:
                self.location_state = "denied"
        except Exception:
            log.info("Location permission status unavailable", exc_info=True)
        try:
            s = await self.perms.get_status(fph.Permission.NOTIFICATION)
            self.notification_state = s.value if s else "unknown"
        except Exception:
            self.notification_state = "unsupported"

    async def request_location_permission(self):
        """Show the OS dialog once. Returns True if foreground location is granted."""
        try:
            status = await self.geo.request_permission()
        except Exception:
            log.warning("Location permission request failed", exc_info=True)
            self.location_state = "error"
            return False
        await self.set_pref("location_asked", True)
        if status in GRANTED:
            self.location_state = "unknown"
            return True
        self.location_state = "denied_forever" if status == ftg.GeolocatorPermissionStatus.DENIED_FOREVER else "denied"
        return False

    async def request_notification_permission(self):
        try:
            s = await self.perms.request(fph.Permission.NOTIFICATION)
            self.notification_state = s.value if s else "unknown"
        except Exception:
            self.notification_state = "unsupported"
        await self.set_pref("notification_asked", True)
        return self.notification_state == "granted"

    async def open_app_settings(self, e=None):
        try:
            await self.perms.open_app_settings()
        except Exception:
            self.toast("Open your phone Settings > Apps > ESRMS Citizen > Permissions.")

    async def open_location_settings(self, e=None):
        try:
            await self.geo.open_location_settings()
        except Exception:
            self.toast("Turn on Location in your phone's quick settings.")

    # ------------------------------------------------------------ GPS

    async def locate(self, ask=True):
        """Make a new high-accuracy location request and return the fresh fix, or None.

        Called when the SOS screen opens and by every Retry GPS press. Needs no internet: on a phone
        the fix comes from the GNSS receiver. A stored position the device answers with is rejected
        (kept in self.old_location) and the request is repeated until config.gps_timeout_seconds.
        ask=False is used for the passive dashboard refresh: it won't pop a permission dialog.
        """
        if self.location_state == "locating":  # a request is already running; share its result
            while self.location_state == "locating":
                await asyncio.sleep(0.3)
            return self.location if self.location and self.location.is_current() else None
        try:
            status = await self.geo.get_permission_status()
        except Exception:
            self.location_state = "error"
            self.notify_status()
            return None
        if status not in GRANTED:
            forever = status == ftg.GeolocatorPermissionStatus.DENIED_FOREVER
            if forever or not ask:
                self.location_state = "denied_forever" if forever else "denied"
                self.notify_status()
                return None
            if not await self.request_location_permission():  # sets location_state
                self.notify_status()
                return None
        try:
            if not await self.geo.is_location_service_enabled():
                self.location_state = "off"
                self.notify_status()
                return None
        except Exception:
            pass  # not every platform reports this; try the fix anyway
        return await self._acquire()

    async def _acquire(self):
        """One acquisition run: real location requests until a fresh fix or the configured timeout."""
        self.location_state, self.location_error = "locating", None
        self.locating_since = time.monotonic()
        self.notify_status()
        ticker = asyncio.create_task(self._tick_while_locating())
        windows = self.page.platform == ft.PagePlatform.WINDOWS
        try:
            if self.old_location is None:
                try:  # shown separately as an old position while waiting; never used as the result
                    last = gps.to_location(await self.geo.get_last_known_position(), datetime.now(timezone.utc),
                                           windows)
                    if last and not last.is_current():
                        last.cached = True
                        self.old_location = last
                        self.notify_status()
                except Exception:
                    log.info("No last known position", exc_info=True)

            def request(seconds_left):
                return self.geo.get_current_position(ftg.GeolocatorConfiguration(
                    accuracy=ftg.GeolocatorPositionAccuracy.BEST,
                    time_limit=ft.Duration(seconds=max(1, int(seconds_left)))))

            fresh, stale, reason = await gps.acquire(request, self.config.gps_timeout_seconds,
                                                     seconds_as_millis=windows)
        finally:
            ticker.cancel()
        if stale and (self.old_location is None or not self.old_location.fixed_at
                      or (stale.fixed_at and stale.fixed_at > self.old_location.fixed_at)):
            self.old_location = stale
        if fresh:
            self.location, self.old_location, self.location_state = fresh, None, "ok"
        else:
            # self.location (an earlier fresh fix from this session, if any) is kept and ages visibly.
            self.location_error = reason
            self.location_state = {"off": "off", "denied": "denied"}.get(reason, "error")
        self.notify_status()
        return fresh

    async def _tick_while_locating(self):
        while True:
            await asyncio.sleep(1)
            self.notify_status()  # live "Acquiring... 12 s" counter

    def locating_seconds(self):
        return int(time.monotonic() - self.locating_since) if self.location_state == "locating" else 0

    async def _lifecycle_changed(self, e):
        """Back in the foreground: permissions or location may have been changed in phone Settings,
        and a request made before the pause may never answer. Check again and re-request."""
        if e.state not in (ft.AppLifecycleState.RESUME, ft.AppLifecycleState.SHOW):
            return
        if self.google_pending:
            self.page.run_task(self._google_maybe_cancelled)
        await self.refresh_permission_states()
        self.notify_status()
        if self.want_fix and self.location_state != "locating":
            self.page.run_task(self.locate, False)
        if self.my_pending():
            self.page.run_task(self.reconnect)
