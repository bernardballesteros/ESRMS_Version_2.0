"""SOS workflow: choose incident -> confirm -> submit -> database-confirmed result.

Tapping a category or holding SOS never sends anything by itself. A report is only
inserted after the citizen presses SEND on the confirmation screen, and success is
only shown after the database returns the stored row.

With no connection the report goes to the outbox (esrms/outbox.py) and the citizen sees
"NOT SENT - saved on this phone" until the database confirms it. The app has no satellite
or other off-grid link; the only extra channels offered are the phone's own call and SMS apps.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

import flet as ft
import flet_camera as ftc
import flet_permission_handler as fph

from esrms import account as rules
from esrms import catalog, report
from esrms.announcements import parse_ts
from esrms.backend import BackendError
from esrms.outbox import OutboxError, parse_time
from esrms.report import MANILA
from esrms.ui import theme as t

log = logging.getLogger(__name__)

# No reply, expired session or server fault: the report itself is fine, so keep it and retry.
KEEP_ON_PHONE = ("network", "auth", "server")
GPS_OFFLINE_HINT = ("GPS works with no signal or internet, but a first fix can then take a few minutes. "
                    "Stay under open sky, away from cliffs and thick trees.")
DESKTOP = (ft.PagePlatform.WINDOWS, ft.PagePlatform.MACOS, ft.PagePlatform.LINUX)


def start(app, category_key=None):
    flow = SosFlow(app, category_key)
    app.want_fix = True
    flow.show_select()
    app.page.run_task(app.locate, True)  # every time the SOS screen opens: a new location request


def _screen(app, title, controls, back):
    return ft.Container(ft.Column([
        ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, icon_color=t.TEXT, on_click=back),
                t.label(title, size=18, weight=ft.FontWeight.W_800, expand=True)]),
        *controls,
    ], spacing=14, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        bgcolor=t.BG, padding=t.pad(16, 12), expand=True)


class SosFlow:
    def __init__(self, app, category_key):
        self.app = app
        self.category_key = category_key
        self.general = category_key is None
        self.incident_type = None
        meta = app.identity() or {}
        self.reporter_id = meta.get("user_id")  # fixed now: the report stays this citizen's even if the session drops
        self.photo = None           # {"id", "data", "mime", "ext"} chosen by the citizen, not uploaded yet
        self.photo_url = None       # set once the photo is stored on the server
        self.photo_note = None      # why the report went without its photo
        self.name = t.field("Your name (optional)", value=meta.get("full_name") or "")
        self.phone = t.field("Contact number (optional)", value=meta.get("phone") or "",
                             keyboard_type=ft.KeyboardType.PHONE)
        self.manual = None          # report.Location entered by the citizen
        self.use_old = False        # citizen chose to attach the device's old stored position
        self.request_id = None      # one per confirmed report, reused on retry
        self.sending = False

    def back_to_dashboard(self, e=None):
        self.app.home()

    # ------------------------------------------------------------ location box

    def _location(self):
        """The location that will be sent. Manual entry wins, then a fix that was fresh when this app
        received it (sent with its time if it has aged since). A stored position from the device is
        attached only after the citizen pressed "Use old position", and is reported as old."""
        if self.manual:
            return self.manual
        return self.app.location or (self.app.old_location if self.use_old else None)

    def _retry_gps(self, e=None):
        self.app.page.run_task(self.app.locate, True)

    def _set_use_old(self, value):
        self.use_old = value
        self.app.notify_status()

    def _location_box(self):
        icon = ft.Icon(ft.Icons.GPS_FIXED, size=20)
        title = t.label("", size=14)
        detail = ft.Text("", size=12, color=t.MUTED, font_family=t.MONO)
        old_text = ft.Text("", size=12, color=t.AMBER, font_family=t.MONO, visible=False)
        actions = ft.Row(wrap=True, spacing=4)

        def describe(loc):
            acc = f"±{loc.accuracy_m:.0f} m" if loc.accuracy_m is not None else "accuracy not reported"
            when = (f"{report.local_time(loc.fixed_at, '%b %d, %Y %I:%M:%S %p')} ({report.age_text(loc.age_seconds())} ago)"
                    if loc.fixed_at else "time unknown")
            return f"Lat {loc.latitude:.6f}  Lng {loc.longitude:.6f}\nAccuracy {acc}\nFix time {when}"

        def button(text, handler):
            actions.controls.append(ft.TextButton(content=ft.Text(text, color=t.CYAN), on_click=handler))

        def refresh():
            app, state, fix = self.app, self.app.location_state, self.app.location
            actions.controls, old_text.visible = [], False
            if self.manual:
                loc = self.manual
                icon.icon, icon.color = ft.Icons.EDIT_LOCATION_ALT, t.AMBER
                title.value = "Manual location (unverified)"
                coords = (f"{loc.latitude:.5f}, {loc.longitude:.5f}" if loc.has_coordinates else "No coordinates")
                detail.value = f"{coords}\n{loc.note}".strip()
                button("Use GPS instead", self._clear_manual)
            elif fix and fix.is_current():
                icon.icon, icon.color = ft.Icons.GPS_FIXED, t.GREEN
                title.value = "GPS location - current"
                detail.value = describe(fix)
                if fix.mocked:
                    detail.value += "\nWarning: the phone reports a mock-location app."
                if state != "locating":
                    button("Refresh GPS", self._retry_gps)
            else:
                if state == "locating":
                    icon.icon, icon.color = ft.Icons.LOCATION_SEARCHING, t.CYAN
                    title.value = (f"Acquiring GPS location... {app.locating_seconds()} s "
                                   f"of {app.config.gps_timeout_seconds} s")
                    detail.value = GPS_OFFLINE_HINT
                else:
                    icon.icon, icon.color = ft.Icons.LOCATION_OFF, t.AMBER
                    title.value = {
                        "denied": "Location permission not granted",
                        "denied_forever": "Location is blocked for this app",
                        "off": "Phone location is turned off",
                        "error": "Unable to obtain a fresh GPS fix",
                    }.get(state, "No GPS location yet")
                    detail.value = {
                        "denied": "Allow location for this app, or enter your location manually.",
                        "denied_forever": "Enable it in Settings > Apps > ESRMS Citizen > Permissions, or enter it manually.",
                        "off": "Turn on Location in quick settings, then press Retry GPS.",
                        "error": {
                            "stale": "The device answered only with a stored position, not a new fix. ",
                            "timeout": f"No satellite fix within {app.config.gps_timeout_seconds} s. ",
                        }.get(app.location_error, "The location request failed. ") + GPS_OFFLINE_HINT,
                    }.get(state, "Press Retry GPS, or enter a landmark or coordinates.")
                    if state == "error" and app.page.platform in DESKTOP:
                        detail.value += ("\nThis is a computer without a GPS receiver; test satellite fixes on "
                                         "the Android build.")
                    button("Retry GPS", self._retry_gps)
                    if state == "denied_forever":
                        button("Open settings", app.open_app_settings)
                    if state == "off":
                        button("Location settings", app.open_location_settings)
                old = fix or app.old_location
                if old:
                    old_text.visible = True
                    if fix:
                        old_text.value = ("EARLIER FIX (not current) - sent with the report as an old position:\n"
                                          + describe(fix))
                    else:
                        old_text.value = (("OLD STORED POSITION - will be sent, marked as old:\n" if self.use_old else
                                           "OLD STORED POSITION - NOT your current location, not used:\n")
                                          + describe(old))
                        if self.use_old:
                            button("Don't use old position", lambda e: self._set_use_old(False))
                        else:
                            button("Use old position", lambda e: self._set_use_old(True))
            button("Enter manually", self._manual_dialog)

        refresh()
        self.app.on_status_change(refresh)
        return t.panel(ft.Column([ft.Row([icon, ft.Container(title, expand=True)]), detail, old_text, actions],
                                 spacing=6))

    def _clear_manual(self, e):
        self.manual = None
        self.app.notify_status()

    def _manual_dialog(self, e):
        cur = self.manual
        lat = t.field("Latitude (optional)", value=str(cur.latitude) if cur and cur.latitude is not None else "",
                      keyboard_type=ft.KeyboardType.NUMBER)
        lng = t.field("Longitude (optional)", value=str(cur.longitude) if cur and cur.longitude is not None else "",
                      keyboard_type=ft.KeyboardType.NUMBER)
        note = t.field("Landmark / address", value=cur.note if cur else "", multiline=True, max_length=300)
        err = ft.Text("", color=t.RED, size=12)

        def save(ev):
            try:
                self.manual = report.parse_manual_location(lat.value, lng.value, note.value)
            except report.ReportError as exc:
                err.value = str(exc)
                self.app.page.update()
                return
            self.app.page.pop_dialog()
            self.app.notify_status()

        self.app.page.show_dialog(ft.AlertDialog(
            bgcolor=t.PANEL, title=t.label("Enter your location", size=17), scrollable=True,
            content=ft.Column([
                ft.Text("Describe where you are. Coordinates are optional; dispatchers will see this as an "
                        "unverified, manually entered location.", size=12, color=t.MUTED),
                note, lat, lng, err], tight=True, spacing=10, width=360),
            actions=[ft.TextButton(content=ft.Text("Cancel", color=t.MUTED), on_click=lambda ev: self.app.page.pop_dialog()),
                     ft.TextButton(content=ft.Text("Use this location", color=t.CYAN), on_click=save)],
        ))

    # ------------------------------------------------------------ photo box

    def _photo_box(self):
        head = ft.Row([
            ft.Container(ft.Icon(ft.Icons.PHOTO_CAMERA, color=t.CYAN, size=22), width=44, height=44,
                         alignment=ft.Alignment.CENTER, bgcolor=t.alpha(t.CYAN, 0.12),
                         border_radius=ft.BorderRadius.all(12), border=ft.Border.all(1, t.alpha(t.CYAN, 0.5))),
            ft.Text("Attach a photo of the incident (optional).", size=13, color=t.MUTED, expand=True),
        ], spacing=12)
        if not self.photo:
            body = [head, t.ghost_button("Upload Photo", self._photo_options, icon="ADD_A_PHOTO")]
        else:
            body = [
                head,
                ft.Container(ft.Image(src=self.photo["data"], height=200, fit=ft.BoxFit.CONTAIN),
                             alignment=ft.Alignment.CENTER, bgcolor=t.BG, border_radius=ft.BorderRadius.all(12),
                             border=ft.Border.all(1, t.BORDER), clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
                ft.Row([t.ghost_button("Change Photo", self._photo_options, icon="ADD_A_PHOTO", expand=True),
                        t.ghost_button("Remove", self._remove_photo, color=t.RED, icon="DELETE_OUTLINE",
                                       expand=True)], spacing=12),
            ]
        return t.panel(ft.Column(body, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH))

    def _photo_options(self, e=None):
        page = self.app.page

        def option(icon, text, handler):
            return ft.ListTile(leading=ft.Icon(icon, color=t.CYAN), title=t.label(text), on_click=handler)

        page.show_dialog(ft.BottomSheet(bgcolor=t.PANEL, content=ft.Container(ft.Column([
            t.mono("UPLOAD PHOTO", color=t.CYAN),
            option(ft.Icons.PHOTO_LIBRARY, "Choose from Gallery", self._from_gallery),
            option(ft.Icons.PHOTO_CAMERA, "Take a Picture", self._take_picture),
            t.ghost_button("Cancel", lambda ev: page.pop_dialog(), color=t.MUTED),
        ], tight=True, spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=16)))

    def _set_photo(self, data):
        try:
            mime, ext = rules.validate_image(data or b"")
        except rules.ValidationError as exc:
            return self.app.toast(exc.errors["file"].replace("picture", "photo"), t.alpha(t.RED, 0.9))
        self.photo = {"id": uuid.uuid4().hex, "data": data, "mime": mime, "ext": ext}
        self.photo_url = self.photo_note = None
        self.show_select()

    def _remove_photo(self, e=None):
        self.photo = self.photo_url = self.photo_note = None
        self.show_select()

    async def _from_gallery(self, e=None):
        app = self.app
        app.page.pop_dialog()
        try:
            # Phone photos are several MB; the server accepts 2 MB, so the picker shrinks them.
            files = await app.picker.pick_files(dialog_title="Choose a photo of the incident",
                                                file_type=ft.FilePickerFileType.IMAGE, with_data=True,
                                                compression_quality=60)
        except Exception:
            return app.toast("Couldn't open the photo gallery on this device.", t.alpha(t.RED, 0.9))
        if not files:
            return
        data = files[0].bytes
        if data is None and files[0].path:
            try:
                with open(files[0].path, "rb") as fh:
                    data = fh.read(rules.MAX_IMAGE_BYTES + 1)
            except OSError:
                data = None
        self._set_photo(data)

    async def _take_picture(self, e=None):
        app, page = self.app, self.app.page
        page.pop_dialog()
        if page.platform in DESKTOP and not page.web:
            return app.toast("Taking a picture works on the phone. On a computer, choose a file instead.")
        try:
            status = await app.perms.request(fph.Permission.CAMERA)
        except Exception:
            status = None  # no permission API here (browser): the camera asks by itself
        if status in (fph.PermissionStatus.DENIED, fph.PermissionStatus.PERMANENTLY_DENIED):
            return app.toast("Camera permission not granted. Allow it in Settings > Apps > ESRMS Citizen > "
                             "Permissions, or choose from gallery.", t.alpha(t.RED, 0.9))

        cam = ftc.Camera(expand=True)
        note = ft.Text("Starting camera...", size=12, color=t.MUTED)
        shoot = t.primary_button("Capture", None, icon="PHOTO_CAMERA", disabled=True)

        async def capture(ev):
            shoot.disabled = True
            page.update()
            try:
                data = bytes(await cam.take_picture())
            except Exception:
                log.warning("Camera capture failed", exc_info=True)
                note.value, note.color, shoot.disabled = "Could not take the picture. Try again.", t.AMBER, False
                return page.update()
            page.pop_dialog()
            self._set_photo(data)

        shoot.on_click = capture
        page.show_dialog(ft.AlertDialog(
            bgcolor=t.PANEL, title=t.label("Take a picture", size=17),
            content=ft.Column([
                ft.Container(cam, width=320, height=400, bgcolor="black", border_radius=ft.BorderRadius.all(12),
                             clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
                note], tight=True, spacing=8),
            actions=[ft.TextButton(content=ft.Text("Cancel", color=t.MUTED), on_click=lambda ev: page.pop_dialog()),
                     shoot],
        ))
        try:
            await asyncio.sleep(0.3)  # the preview must exist on the phone before it can be started
            cameras = await cam.get_available_cameras()
            back = next((c for c in cameras if c.lens_direction == ftc.CameraLensDirection.BACK), cameras[0])
            # HIGH is 720p: sharp enough for a dispatcher and well under the 2 MB limit. No microphone.
            await cam.initialize(back, ftc.ResolutionPreset.HIGH, enable_audio=False)
        except Exception:
            log.warning("Camera could not be started", exc_info=True)
            note.value, note.color = "The camera could not be started. Close this and choose from gallery.", t.AMBER
        else:
            note.value, shoot.disabled = "Point at the incident and press Capture.", False
        page.update()

    # ------------------------------------------------------------ step 1: select

    def show_select(self, error=None):
        app = self.app
        app._listeners.clear()
        cat = catalog.BY_KEY.get(self.category_key)

        chips = ft.Row(wrap=True, spacing=8, run_spacing=8)
        for c in catalog.CATEGORIES:
            on = c.key == self.category_key
            col = t.accent(c.color)
            chips.controls.append(ft.Container(
                ft.Row([ft.Icon(getattr(ft.Icons, c.icon), color=col, size=16), t.label(c.label, size=12)],
                       spacing=6, tight=True),
                padding=t.pad(10, 8), border_radius=ft.BorderRadius.all(20),
                border=ft.Border.all(1.5 if on else 1, col if on else t.BORDER),
                bgcolor=t.alpha(col, 0.15) if on else t.PANEL,
                on_click=lambda e, k=c.key: self._pick_category(k),
            ))
        if self.general:
            on = self.category_key is None
            chips.controls.append(ft.Container(
                t.label("Not sure / General SOS", size=12), padding=t.pad(10, 8),
                border_radius=ft.BorderRadius.all(20), border=ft.Border.all(1.5 if on else 1, t.RED if on else t.BORDER),
                bgcolor=t.alpha(t.RED, 0.15) if on else t.PANEL, on_click=lambda e: self._pick_category(None),
            ))

        types = ft.Column(spacing=6)
        if cat:
            col = t.accent(cat.color)
            for it in cat.incident_types:
                on = it == self.incident_type
                types.controls.append(ft.Container(
                    ft.Row([ft.Icon(ft.Icons.RADIO_BUTTON_CHECKED if on else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                    color=col if on else t.MUTED, size=20), t.label(it, size=14, expand=True)]),
                    padding=t.pad(12, 12), border_radius=ft.BorderRadius.all(12), bgcolor=t.PANEL,
                    border=ft.Border.all(1, col if on else t.BORDER),
                    on_click=lambda e, v=it: self._pick_type(v),
                ))

        heading = cat.label if cat and not self.general else "Emergency SOS"
        controls = [
            *([t.mono("1. TYPE OF EMERGENCY", color=t.CYAN), chips] if self.general else []),
            *([t.mono(("2. " if self.general else "1. ") + "WHAT HAPPENED", color=t.CYAN), types] if cat else []),
            t.mono("LOCATION", color=t.CYAN), self._location_box(),
            t.mono("UPLOAD PHOTO (OPTIONAL)", color=t.CYAN), self._photo_box(), self.name, self.phone,
            *([t.panel(ft.Text(error, color=t.RED), border_color=t.alpha(t.RED, 0.6))] if error else []),
            ft.Row([t.ghost_button("Cancel", self.back_to_dashboard, color=t.MUTED, expand=True),
                    t.primary_button("REVIEW", self.show_confirm, expand=True, icon="ARROW_FORWARD")], spacing=12),
        ]
        app.show(_screen(app, heading, controls, self.back_to_dashboard))

    def _pick_category(self, key):
        if key != self.category_key:
            self.category_key, self.incident_type = key, None
        self.show_select()

    def _pick_type(self, value):
        self.incident_type = value
        self.show_select()

    # ------------------------------------------------------------ step 2: confirm

    def _row(self, queued_at=None):
        return report.build_incident(
            reporter_id=self.reporter_id,
            category_key=self.category_key, incident_type=self.incident_type if self.category_key else None,
            location=self._location(),
            citizen_name=self.name.value, contact_number=self.phone.value, client_request_id=self.request_id,
            queued_at=queued_at, photo_url=self.photo_url,
        )

    def show_confirm(self, e=None, error=None, retry_hint=False):
        app = self.app
        if self.category_key and not self.incident_type:
            return self.show_select("Choose what happened before continuing.")
        if self.request_id is None:
            self.request_id = uuid.uuid4()
        try:
            row = self._row()
        except report.ReportError as exc:
            return self.show_select(str(exc))
        app._listeners.clear()

        loc = self._location()
        fresh = bool(loc and loc.has_coordinates and (loc.source != "gps" or loc.is_current()))
        if loc and loc.has_coordinates:
            src = f"GPS {loc.freshness()}" if loc.source == "gps" else "Manual (unverified)"
            acc = f" ±{loc.accuracy_m:.0f} m" if loc.accuracy_m is not None else ""
            where = f"{loc.latitude:.6f}, {loc.longitude:.6f}{acc}  [{src}]"
        elif loc:
            where = f"Manual, no coordinates: {loc.note}"
        else:
            where = "NO LOCATION - dispatchers will have to ask you where you are"

        def item(k, v, color=t.TEXT):
            return ft.Column([t.mono(k, size=10, color=t.MUTED), ft.Text(v, color=color, size=15, selectable=True)],
                             spacing=2)

        send_btn = t.primary_button("SEND SOS REPORT", self._send, color=t.RED, icon="SOS", expand=True)
        controls = [
            t.panel(ft.Column([
                item("EMERGENCY", row["emergency_type"], t.TEXT),
                item("LOCATION", where, t.TEXT if fresh else t.AMBER),
                *([ft.Column([t.mono("PHOTO", size=10, color=t.MUTED),
                              ft.Image(src=self.photo["data"], height=140, fit=ft.BoxFit.CONTAIN,
                                       border_radius=ft.BorderRadius.all(10))], spacing=4)] if self.photo else []),
                *([item("NAME", row["citizen_name"])] if row["citizen_name"] else []),
                *([item("CONTACT", row["contact_number"])] if row["contact_number"] else []),
            ], spacing=12), border_color=t.alpha(t.RED, 0.5), padding=16),
            ft.Text("This sends your report to ESRMS dispatchers. It does not place a phone call. If you can, also "
                    f"call {app.config.emergency_hotline}.", size=12, color=t.MUTED),
            *([t.panel(ft.Text(
                "No connection right now. If sending fails, the report is SAVED ON THIS PHONE and sent when a "
                "connection returns. Until then ESRMS has not received it.", size=13, color=t.AMBER),
                border_color=t.alpha(t.AMBER, 0.6))]
              if (app.online is False or not app.backend.session) and not error else []),
            *([t.panel(ft.Column([ft.Text(error, color=t.RED)] + (
                [ft.Text("Retrying is safe: the same report will not be stored twice.", size=12, color=t.MUTED)]
                if retry_hint else [])), border_color=t.alpha(t.RED, 0.6))] if error else []),
            ft.Row([send_btn]),
            ft.Row([t.ghost_button("Edit", lambda e: self.show_select(), expand=True),
                    t.ghost_button("Cancel", self.back_to_dashboard, color=t.MUTED, expand=True)], spacing=12),
            t.ghost_button(f"Call {app.config.emergency_hotline}", app.call_hotline, color=t.RED, icon="PHONE"),
        ]
        self._send_btn = send_btn
        app.show(_screen(app, "Confirm SOS", controls, lambda e: self.show_select()))

    # ------------------------------------------------------------ step 3: submit

    async def _send(self, e):
        if self.sending:
            return  # double tap guard
        app = self.app
        self.sending = True
        self._send_btn.disabled = True
        self._send_btn.content = ft.Row([ft.ProgressRing(width=18, height=18, color="white"),
                                         ft.Text("SENDING... waiting for confirmation", color="white")],
                                        tight=True, spacing=10)
        app.page.update()
        try:
            row = self._row()
            if not app.backend.session:  # offline start: the session has to be restored first
                err = await app.reconnect()
                if not app.backend.session:
                    raise err or BackendError("Sign in again to send.", "auth")
            if self.photo and not self.photo_url:
                try:
                    self.photo_url = await app.backend.upload_incident_photo(
                        self.photo["data"], self.photo["mime"], self.photo["ext"], self.photo["id"])
                    row = self._row()
                except BackendError as exc:
                    if exc.kind in KEEP_ON_PHONE:
                        raise
                    # The server refused the photo: the SOS itself must still go out.
                    self.photo_note = f"The photo was NOT attached. {exc}"
            stored = await app.backend.submit_incident(row)
        except report.ReportError as exc:
            self.sending = False
            return self.show_select(str(exc))
        except BackendError as exc:
            app.online = exc.kind != "network"
            if exc.kind in KEEP_ON_PHONE:
                return await self._keep_on_phone()
            self.sending = False
            if exc.kind in ("duplicate", "rate_limited"):
                self.request_id = None  # nothing was stored; a later attempt is a new report
            return self.show_confirm(error=str(exc))
        self.sending = False
        app.online = True
        app.want_fix = False
        self.show_result(stored)

    async def _keep_on_phone(self):
        """Sending failed or could not be confirmed: store the report locally and say so plainly."""
        app, loc, now = self.app, self._location(), datetime.now(timezone.utc)
        gps = loc if loc and loc.source == "gps" else None
        try:
            entry = await app.outbox.add(
                self._row(queued_at=now), saved_at=now, fixed_at=gps.fixed_at if gps else None,
                cached=bool(gps and gps.cached), place=loc.note if loc else "")
        except (OutboxError, report.ReportError):
            self.sending = False
            return self.show_confirm(retry_hint=True, error=(
                "NOT SENT AND NOT SAVED: there is no connection and the phone could not store the report. "
                "Stay on this screen and retry, or call / send an SMS."))
        self.sending = False
        app.want_fix = False
        app.notify_status()
        # Only the report text fits in the phone's small store; a photo that was not uploaded is not kept.
        show_pending(app, entry["id"], "The photo is NOT saved with this report. It will be sent without it."
                     if self.photo and not self.photo_url else None)

    def show_result(self, stored):
        who = self.app.identity() or {}
        name = (self.name.value or "").strip() or who.get("full_name") or ""
        note = "Photo attached to the report." if self.photo_url else self.photo_note
        _result_screen(self.app, stored, "", name, (self.phone.value or "").strip(), note)


def _result_screen(app, stored, description, name, phone, late_note=None):
    """The only screen that says 'received': it is built from the row the database returned."""
    back = lambda e=None: app.home()  # noqa: E731
    created = parse_ts(stored.get("created_at"))
    when = created.astimezone(MANILA).strftime("%b %d, %Y %I:%M:%S %p") if created else "-"
    status = report.STATUS_TEXT.get(stored.get("status"), stored.get("status") or "-")
    coords = (f"{stored['latitude']:.6f}, {stored['longitude']:.6f}" if stored.get("latitude") is not None
              else "Not included")
    source = {"gps": "GPS from this phone", "manual": "Entered manually (unverified)",
              "none": "No location available"}.get(stored.get("location_source"), "-")
    who = app.identity() or {}
    account = "Anonymous quick report" if who.get("is_anonymous") else (who.get("email") or "")
    reporter = " · ".join(x for x in (name, account, phone) if x) or "-"
    app._listeners.clear()
    controls = [
        ft.Container(ft.Icon(ft.Icons.CHECK_CIRCLE, color=t.GREEN, size=64), alignment=ft.Alignment.CENTER),
        ft.Container(t.label("Report received by ESRMS", size=20), alignment=ft.Alignment.CENTER),
        t.panel(ft.Column([
            t.mono("REFERENCE", size=10), ft.Text(report.incident_code(stored), size=26, color=t.CYAN,
                                                  weight=ft.FontWeight.W_900, font_family=t.MONO, selectable=True),
            t.mono("TYPE", size=10), ft.Text(stored.get("emergency_type", ""), color=t.TEXT),
            *([t.mono("DESCRIPTION", size=10), ft.Text(description, color=t.TEXT, selectable=True)]
              if description else []),
            t.mono("REPORTED BY", size=10), ft.Text(reporter, color=t.TEXT),
            t.mono("RECEIVED BY ESRMS", size=10), ft.Text(when, color=t.TEXT),
            *([ft.Text(late_note, color=t.AMBER, size=12)] if late_note else []),
            t.mono("LOCATION SENT", size=10), ft.Text(coords, color=t.TEXT, font_family=t.MONO),
            t.mono("LOCATION SOURCE", size=10),
            ft.Text(source, color=t.TEXT if stored.get("location_source") == "gps" else t.AMBER),
            t.mono("DISPATCH STATUS", size=10), ft.Text(status, color=t.AMBER),
        ], spacing=4), padding=16, border_color=t.alpha(t.GREEN, 0.5)),
        ft.Text("Responders have not been confirmed yet. Keep your phone with you; status updates appear in "
                "My reports when the dispatcher acts on it.", size=13, color=t.MUTED),
        t.primary_button("My reports", lambda e: app.page.run_task(show_my_reports, app), icon="HISTORY"),
        t.ghost_button(f"Call {app.config.emergency_hotline}", app.call_hotline, color=t.RED, icon="PHONE"),
        t.ghost_button("Back to dashboard", back, color=t.MUTED),
    ]
    app.show(_screen(app, "SOS sent", controls, back))


# ---------------------------------------------------------------- saved on this phone, not sent

def _user_note(row):
    return "\n".join(ln for ln in (row.get("description") or "").split("\n") if ln and not ln.startswith("["))


def no_signal_help(app, e=None):
    """What can and cannot get a message out with no signal. No claim is made for any service."""
    hotline = app.config.emergency_hotline
    points = [
        ("This app has no satellite link.", "GPS satellites only tell the phone where it is. They cannot carry "
         "a message, so your coordinates stay on the phone until it reaches a cell or Wi-Fi network."),
        (f"Try calling {hotline}.", "An emergency call may go through another network's tower even when your "
         "own SIM shows no service."),
        ("Try SMS.", "A text needs only a weak cell signal, not mobile data. 'Send by SMS' opens your "
         "messaging app with your coordinates filled in. The app cannot confirm that an SMS was delivered."),
        ("Move if it is safe.", "Ridges, summits and open ground reach towers that valleys and forest block."),
        ("Satellite services are separate from this app.", "If your SIM or phone has a satellite-to-phone "
         "service that is active here, or you carry a satellite messenger or beacon, use it and read out the "
         "coordinates shown in this app."),
        ("Keep the app open and save battery.", "Saved reports are only sent while the app is open. Lower the "
         "screen brightness and close other apps."),
    ]
    app.page.show_dialog(ft.AlertDialog(
        bgcolor=t.PANEL, title=t.label("No signal: what can work", size=17), scrollable=True,
        content=ft.Column([ft.Column([t.label(h, size=14), ft.Text(b, size=13, color=t.MUTED)], spacing=2)
                           for h, b in points], tight=True, spacing=12, width=360),
        actions=[ft.TextButton(content=ft.Text("Close", color=t.CYAN), on_click=lambda ev: app.page.pop_dialog())],
    ))


def show_delivered(app, entry, stored):
    saved = parse_time(entry.get("saved_at"))
    note = (f"Written on this phone {report.local_time(saved, '%b %d, %Y %I:%M:%S %p')} and delivered late."
            if saved else None)
    row = entry["row"]
    _result_screen(app, stored, _user_note(row), row.get("citizen_name") or "", row.get("contact_number") or "", note)


def show_pending(app, entry_id, notice=None):
    """A report that exists only on this phone. Never worded or coloured like a received report."""
    entry = app.outbox.get(entry_id)
    if entry is None:
        return app.home()
    app._listeners.clear()
    row, rejected = entry["row"], entry["state"] == "rejected"
    back = lambda e=None: app.home()  # noqa: E731
    now = datetime.now(timezone.utc)
    saved, fixed = parse_time(entry.get("saved_at")), parse_time(entry.get("fixed_at"))

    if row.get("latitude") is not None:
        acc = row.get("location_accuracy_m")
        coords = f"{row['latitude']:.6f}, {row['longitude']:.6f}" + (f"  ±{acc:.0f} m" if acc is not None else "")
    else:
        coords = "No coordinates" + (f" - {entry['place']}" if entry.get("place") else "")
    if row.get("location_source") != "gps":
        position = {"manual": "Entered manually (unverified)"}.get(row.get("location_source"), "No location")
    elif fixed:
        position = (f"{'Last known position' if entry.get('cached') else 'GPS fix'} taken "
                    f"{report.local_time(fixed, '%b %d %I:%M:%S %p')} ({report.age_text((now - fixed).total_seconds())} ago)")
    else:
        position = "GPS position, time unknown"

    status = ft.Text(notice or "", color=t.AMBER, size=13, visible=bool(notice))
    try_btn = t.primary_button("TRY SENDING NOW", None, icon="REFRESH")

    async def try_now(e):
        try_btn.disabled, status.value, status.visible = True, "Trying to reach ESRMS...", True
        app.page.update()
        err = await app.reconnect()
        if entry_id in app.delivered or app.outbox.get(entry_id) is None:
            return  # the listener below already switched to the received screen
        show_pending(app, entry_id, f"Still NOT sent. {err}" if err else "Still NOT sent.")

    try_btn.on_click = try_now

    def on_change():
        if entry_id in app.delivered:
            show_delivered(app, entry, app.delivered[entry_id])

    async def discard(e):
        app.page.pop_dialog()
        try:
            await app.outbox.remove(entry_id)
        except OutboxError:
            return app.toast("Could not delete it. Try again.", t.alpha(t.RED, 0.9))
        app.notify_status()
        app.home()

    def ask_discard(e):
        app.page.show_dialog(ft.AlertDialog(
            bgcolor=t.PANEL, title=t.label("Delete this unsent report?", size=17),
            content=ft.Text("It has NOT reached ESRMS. If you delete it, it never will.", color=t.MUTED),
            actions=[ft.TextButton(content=ft.Text("Keep it", color=t.CYAN), on_click=lambda ev: app.page.pop_dialog()),
                     ft.TextButton(content=ft.Text("Delete", color=t.RED), on_click=discard)]))

    headline = ("ESRMS REFUSED this report. It has not been received and will not be retried."
                if rejected else
                "ESRMS has NOT received this report. No dispatcher or rescuer can see it yet.")
    controls = [
        ft.Container(ft.Icon(ft.Icons.CLOUD_OFF, color=t.RED if rejected else t.AMBER, size=64),
                     alignment=ft.Alignment.CENTER),
        ft.Container(t.label("NOT SENT - saved on this phone only", size=19, color=t.AMBER),
                     alignment=ft.Alignment.CENTER),
        t.panel(ft.Text(headline, color=t.RED, weight=ft.FontWeight.BOLD), border_color=t.alpha(t.RED, 0.7)),
        t.panel(ft.Column([
            t.mono("TYPE", size=10), ft.Text(row.get("emergency_type", ""), color=t.TEXT),
            t.mono("COORDINATES SAVED", size=10), ft.Text(coords, color=t.TEXT, font_family=t.MONO, selectable=True),
            t.mono("POSITION", size=10), ft.Text(position, color=t.TEXT),
            t.mono("WRITTEN", size=10),
            ft.Text(report.local_time(saved, "%b %d, %Y %I:%M:%S %p") if saved else "-", color=t.TEXT),
            t.mono("SEND ATTEMPTS", size=10),
            ft.Text(f"{entry['attempts']}" + (f" - last: {entry['last_error']}" if entry.get("last_error") else ""),
                    color=t.TEXT),
        ], spacing=4), padding=16, border_color=t.alpha(t.AMBER, 0.6)),
        status,
        *([] if rejected else [
            ft.Text("The app sends it automatically when the phone gets a connection, as long as the app stays "
                    "open. It counts as received only when this screen shows an ESRMS reference number.",
                    size=13, color=t.MUTED),
            try_btn]),
        t.ghost_button("Send by SMS", lambda e: app.page.run_task(app.open_sms, entry), icon="SMS"),
        t.ghost_button(f"Call {app.config.emergency_hotline}", app.call_hotline, color=t.RED, icon="PHONE"),
        t.ghost_button("No signal: what can work", lambda e: no_signal_help(app), color=t.MUTED, icon="HELP_OUTLINE"),
        t.ghost_button("Back", back, color=t.MUTED),
        ft.TextButton(content=ft.Text("Delete this unsent report", color=t.DIM, size=12), on_click=ask_discard),
    ]
    app.on_status_change(on_change)
    app.show(_screen(app, "SOS not sent", controls, back))


def _pending_card(app, entry):
    rejected = entry["state"] == "rejected"
    saved = parse_time(entry.get("saved_at"))
    color = t.RED if rejected else t.AMBER
    return t.panel(ft.Column([
        ft.Row([t.mono("ON THIS PHONE ONLY", color=color, weight=ft.FontWeight.BOLD), ft.Container(expand=True),
                t.pill("REFUSED" if rejected else "NOT SENT", color)]),
        t.label(entry["row"].get("emergency_type", ""), size=14),
        ft.Text("ESRMS refused it. Tap for options." if rejected else
                "Not received by ESRMS yet. Tap to retry or send by SMS.", size=12, color=color),
        t.mono("Written " + report.local_time(saved, "%b %d %I:%M %p") if saved else "", size=10),
    ], spacing=4), padding=12, border_color=t.alpha(color, 0.7), on_click=lambda e: show_pending(app, entry["id"]))


# ---------------------------------------------------------------- my reports

async def show_my_reports(app, e=None):
    app._listeners.clear()
    lst = ft.Column(spacing=10)
    who = app.identity() or {}
    local = [_pending_card(app, x) for x in reversed(app.outbox.for_user(who.get("user_id")))]
    refresh = t.ghost_button("Refresh", lambda e: app.page.run_task(show_my_reports, app), icon="REFRESH")
    app.show(_screen(app, "My reports", [lst], lambda e: app.home()))
    if not app.backend.session:
        lst.controls = [*local, ft.Text("Reports already received by ESRMS are listed here once the phone is "
                                        "connected.", color=t.MUTED), refresh]
        app.page.update()
        return
    lst.controls = [*local, ft.ProgressRing(color=t.CYAN)]
    app.page.update()
    try:
        rows = await app.backend.my_reports()
        app.online = True
    except BackendError as exc:
        app.online = exc.kind != "network"
        lst.controls = [*local, ft.Text(str(exc), color=t.RED), refresh]
        app.page.update()
        return
    if not rows and not local:
        lst.controls = [ft.Text("You haven't sent any reports from this account.", color=t.MUTED)]
    else:
        lst.controls = [*local, *[_report_card(r) for r in rows]]
    lst.controls.append(refresh)
    app.page.update()


def _report_card(r):
    status = r.get("status")
    color = {"new": t.AMBER, "resolved": t.GREEN, "cancelled": t.MUTED}.get(status, t.CYAN)
    created = parse_ts(r.get("created_at"))
    when = created.astimezone(MANILA).strftime("%b %d %I:%M %p") if created else ""
    return t.panel(ft.Column([
        ft.Row([t.mono(report.incident_code(r), color=t.CYAN, weight=ft.FontWeight.BOLD), ft.Container(expand=True),
                t.pill((status or "?").upper().replace("_", " "), color)]),
        t.label(r.get("emergency_type", ""), size=14),
        ft.Text(report.STATUS_TEXT.get(status, status or ""), size=12, color=color),
        t.mono(when, size=10),
    ], spacing=4), padding=12)
