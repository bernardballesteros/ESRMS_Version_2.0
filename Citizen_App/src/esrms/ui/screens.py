"""First-launch permission onboarding, sign-in/registration, and settings."""
from datetime import date, datetime

import flet as ft

from esrms.backend import BackendError
from esrms.ui import theme as t


def brand(subtitle="CITIZEN DASHBOARD", tagline="SAFER COMMUNITY. STRONGER TOGETHER.", app=None):
    """Header brand row. With `app`, the emblem gets the rotating radar sweep like the web portal's."""
    emblem = _logo(app, 52) if app else ft.Container(
        ft.Image(src="radar_logo.svg", width=52, height=52), bgcolor=t.LOGO_BG,
        border_radius=ft.BorderRadius.all(12), border=ft.Border.all(1, t.alpha(t.CYAN, 0.4)))
    return ft.Row([
        emblem,
        ft.Column([
            t.mono("ESRMS NET", size=12, color=t.CYAN, weight=ft.FontWeight.BOLD),
            t.label(subtitle, size=18, weight=ft.FontWeight.W_800, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            t.mono(tagline, size=9, color=t.MUTED),
        ], spacing=0, expand=True),
    ], spacing=12)


def splash(message):
    return ft.Container(
        ft.Column([brand(), ft.Container(height=30), ft.ProgressRing(color=t.CYAN), t.mono(message)],
                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14),
        alignment=ft.Alignment.CENTER, expand=True, bgcolor=t.BG, padding=24,
    )


def _page(controls, scroll=True):
    return ft.Container(
        ft.Column(controls, spacing=16, scroll=ft.ScrollMode.AUTO if scroll else None,
                  horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        expand=True, bgcolor=t.BG, padding=t.pad(20, 20),
    )


# ---------------------------------------------------------------- onboarding

def onboarding(app):
    """Explain each permission BEFORE the OS dialog. Shown once; the choice is remembered."""
    steps = [
        dict(icon="LOCATION_ON", color=t.CYAN, title="Share your location in an emergency",
             body=("When you send an SOS, ESRMS attaches your phone's GPS coordinates so the 911 dispatcher "
                   "can see where you are on their map.\n\nLocation is read only while the app is open and only "
                   "sent with a report you confirm. No background tracking."),
             allow="Allow location", skip="Not now",
             denied=("Automatic GPS is off. You can still send reports by typing a landmark or coordinates. "
                     "To turn it on later: Settings > Apps > ESRMS Citizen > Permissions > Location."),
             request=app.request_location_permission),
        dict(icon="NOTIFICATIONS_ACTIVE", color=t.AMBER, title="Official announcements",
             body=("Notification permission lets ESRMS alert you about official announcements and updates "
                   "on your reports.\n\nNote: push notifications from the ESRMS server are not set up yet. "
                   "For now, new announcements appear in the app while it is open."),
             allow="Allow notifications", skip="Not now",
             denied=("Notifications are off. The app still works. To turn them on later: "
                     "Settings > Apps > ESRMS Citizen > Notifications."),
             request=app.request_notification_permission),
    ]
    state = {"i": 0}
    holder = ft.Container(expand=True)

    def render(note=None):
        s = steps[state["i"]]
        holder.content = _page([
            brand(),
            t.mono(f"SETUP {state['i'] + 1}/{len(steps)}", color=t.CYAN),
            t.panel(ft.Column([
                ft.Icon(getattr(ft.Icons, s["icon"]), color=s["color"], size=44),
                t.label(s["title"], size=20),
                ft.Text(s["body"], color=t.MUTED, size=14),
            ], spacing=12), padding=20),
            *( [t.panel(ft.Text(note, color=t.AMBER, size=13), border_color=t.alpha(t.AMBER, 0.6))] if note else [] ),
            ft.Row([t.ghost_button(s["skip"], lambda e: next_step(s["denied"]), color=t.MUTED, expand=True),
                    t.primary_button(s["allow"], allow, expand=True)], spacing=12),
        ])
        app.page.update()

    async def allow(e):
        s = steps[state["i"]]
        granted = await s["request"]()
        next_step(None if granted else s["denied"])

    def next_step(note):
        if note and not state.get("noted"):
            state["noted"] = True
            render(note)           # tell the citizen what "denied" means before moving on
            return
        state["noted"] = False
        state["i"] += 1
        if state["i"] < len(steps):
            render()
        else:
            app.page.run_task(finish)

    async def finish():
        await app.set_pref("onboarded", True)
        await app.after_onboarding()

    render()
    return holder


# ---------------------------------------------------------------- sign in / register

def _auth_input(hint, icon, **kw):
    prefix = (dict(prefix=ft.Text("@  ", color=t.MUTED, size=15)) if icon == "@"
              else dict(prefix_icon=getattr(ft.Icons, icon)))
    return ft.TextField(
        hint_text=hint, hint_style=ft.TextStyle(color=t.DIM), **prefix,
        border_color=t.BORDER, focused_border_color=t.CYAN, color=t.TEXT, cursor_color=t.CYAN,
        border_radius=12, filled=True, fill_color=t.alpha(t.BG, 0.7), text_size=14,
        content_padding=t.pad(12, 14), **kw,
    )


def _labeled(label_text, control, trailing=None, expand=None):
    head = [t.label(label_text, size=13)]
    if trailing is not None:
        head.append(ft.Container(expand=True))
        head.append(trailing)
    return ft.Column([ft.Row(head), control], spacing=6, expand=expand)


RADAR_PERIOD = 6  # seconds per sweep, same as .radar-sweep in the web portal


def _logo(app, size=120):
    """The ESRMS radar emblem from the web portal (EMERGENCY/app/templates/partials/_logo.html)."""
    sweep = ft.Container(
        left=0, top=0, right=0, bottom=0,
        gradient=ft.SweepGradient(colors=["#00000000", "#00000000", t.alpha("#00D9FF", 0.22)],
                                  stops=[0.0, 0.75, 1.0]),
        rotate=ft.Rotate(0), animate_rotation=ft.Animation(RADAR_PERIOD * 1000, ft.AnimationCurve.LINEAR),
    )

    async def spin():
        import asyncio, math
        turns = 0
        await asyncio.sleep(0.3)  # let the screen mount first
        while True:
            turns += 1
            sweep.rotate = ft.Rotate(turns * 2 * math.pi)
            try:
                sweep.update()
            except Exception:
                return  # logo left the screen
            await asyncio.sleep(RADAR_PERIOD)

    app.page.run_task(spin)
    return ft.Container(
        ft.Stack([sweep, ft.Image(src="radar_logo.svg", width=size, height=size)]),
        width=size, height=size, bgcolor=t.LOGO_BG, border_radius=ft.BorderRadius.all(max(12, size * 22 // 120)),
        border=ft.Border.all(1, t.alpha("#22D3EE", 0.4)), clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        shadow=ft.BoxShadow(blur_radius=size / 4, spread_radius=size / 60, color=t.alpha("#0EA5E9", 0.3)),
    )


def _auth_header(app):
    return ft.Column([
        ft.Container(ft.Row([
            ft.Row([t.dot(t.CYAN, 6), t.mono("ESRMS // SECURE", size=10, color=t.TEXT, weight=ft.FontWeight.BOLD)],
                   spacing=6),
            ft.Container(t.mono("LIVE 14MS", size=9, color=t.GREEN, weight=ft.FontWeight.BOLD),
                         padding=t.pad(6, 3), border_radius=ft.BorderRadius.all(4),
                         border=ft.Border.all(1, t.alpha(t.GREEN, 0.6)), bgcolor=t.alpha(t.GREEN, 0.08)),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            padding=t.pad(12, 8), border_radius=ft.BorderRadius.all(8),
            border=ft.Border.all(1, t.BORDER), bgcolor=t.alpha(t.BG, 0.8)),
        ft.Row([ft.Container(ft.Row([
            t.dot(t.CYAN, 6),
            t.mono("SECURE RESPONSE NETWORK", size=9, color=t.CYAN, weight=ft.FontWeight.BOLD),
            ft.Container(width=1, height=12, bgcolor=t.alpha(t.CYAN, 0.5)),
            t.mono("ONLINE", size=9, color=t.TEXT, weight=ft.FontWeight.BOLD),
        ], spacing=8, tight=True), padding=t.pad(12, 6), border_radius=ft.BorderRadius.all(20),
            border=ft.Border.all(1, t.alpha(t.CYAN, 0.5)), bgcolor=t.alpha(t.CYAN, 0.06))],
               alignment=ft.MainAxisAlignment.CENTER),
        ft.Row([_logo(app)], alignment=ft.MainAxisAlignment.CENTER),
        ft.Row([t.label("ESRMS", size=26, weight=ft.FontWeight.W_900),
                ft.Container(t.mono("V4.8-MOBILE", size=9, color=t.CYAN, weight=ft.FontWeight.BOLD),
                             padding=t.pad(6, 3), border_radius=ft.BorderRadius.all(4),
                             border=ft.Border.all(1, t.alpha(t.CYAN, 0.6)))],
               alignment=ft.MainAxisAlignment.CENTER, spacing=10),
        ft.Row([ft.Text(spans=[ft.TextSpan("Emergency SOS", ft.TextStyle(color=t.CYAN, weight=ft.FontWeight.BOLD)),
                               ft.TextSpan(" & Response Management System", ft.TextStyle(color=t.MUTED))],
                        size=12)], alignment=ft.MainAxisAlignment.CENTER),
    ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)


def _or_divider(text):
    return ft.Row([ft.Container(ft.Divider(color=t.BORDER), expand=True),
                   t.mono(text, size=9, color=t.DIM),
                   ft.Container(ft.Divider(color=t.BORDER), expand=True)], spacing=8)


def sign_in(app, mode="signin", notice=None):
    holder = ft.Container(expand=True)
    register = mode == "register"
    email = _auth_input("username@organization.org" if register else "username@gmail.com", "@",
                        keyboard_type=ft.KeyboardType.EMAIL, autofocus=not register)
    password = _auth_input("••••••••••••" if register else "eg. 12345678", "LOCK_OUTLINE",
                           password=True, can_reveal_password=True)
    full_name = _auth_input("e.g. John Doe", "PERSON_OUTLINE", autofocus=register)
    phone = _auth_input("e.g. 0917 123 4567", "PHONE_OUTLINED", keyboard_type=ft.KeyboardType.PHONE)
    age = _auth_input("28", "TAG", keyboard_type=ft.KeyboardType.NUMBER)

    def set_age(born):
        today = date.today()
        age.value = str(today.year - born.year - ((today.month, today.day) < (born.month, born.day)))

    def dob_typed(e):
        try:
            born = date.fromisoformat((dob.value or "").strip())
        except ValueError:
            return  # leave partial/odd input alone; it's validated on submit
        if born <= date.today():
            set_age(born)
            app.page.update()

    def dob_picked(e):
        if picker.value:
            born = picker.value.date() if isinstance(picker.value, datetime) else picker.value
            dob.value = born.isoformat()
            set_age(born)
            app.page.update()

    picker = ft.DatePicker(
        first_date=date(1900, 1, 1), last_date=date.today(), current_date=date.today(),
        date_picker_mode=ft.DatePickerMode.YEAR, help_text="Select date of birth", on_change=dob_picked,
    )

    def open_calendar(e):
        try:
            picker.value = date.fromisoformat((dob.value or "").strip())
        except ValueError:
            picker.value = None
        app.page.show_dialog(picker)

    dob = _auth_input("YYYY-MM-DD", "CALENDAR_MONTH_OUTLINED", keyboard_type=ft.KeyboardType.DATETIME,
                      on_change=dob_typed)
    dob.prefix_icon = None
    dob.prefix = ft.IconButton(ft.Icons.CALENDAR_MONTH_OUTLINED, icon_color=t.CYAN, icon_size=18,
                               on_click=open_calendar, tooltip="Pick from calendar",
                               style=ft.ButtonStyle(padding=0), width=28, height=28)
    gender = ft.Dropdown(
        value="Male", options=[ft.dropdown.Option(g) for g in ("Male", "Female", "Other")],
        border_color=t.BORDER, focused_border_color=t.CYAN, color=t.TEXT, border_radius=12,
        filled=True, fill_color=t.alpha(t.BG, 0.7), text_size=14, expand=True,
    )
    address = _auth_input("e.g. 742 Evergreen Terrace, Sector 4", "LOCATION_ON_OUTLINED")
    barangay = _auth_input("Barangay (for local announcements)", "HOLIDAY_VILLAGE_OUTLINED", value=app.barangay)
    confirm = _auth_input("••••••••••••", "LOCK_OUTLINE", password=True)
    terms = ft.Checkbox(value=False, active_color=t.CYAN, check_color=t.on(t.CYAN))
    error = ft.Text(notice or "", color=t.RED, size=13, visible=bool(notice))
    busy = ft.ProgressRing(width=22, height=22, color=t.CYAN, visible=False)

    def fail(msg):
        error.value, error.visible, busy.visible = msg, True, False
        app.page.update()

    async def submit(e):
        error.visible, busy.visible = False, True
        app.page.update()
        em, pw = (email.value or "").strip(), password.value or ""
        if not em or not pw:
            return fail("Enter your email and password.")
        try:
            if mode == "signin":
                await app.backend.sign_in(em, pw)
            else:
                if not (full_name.value or "").strip():
                    return fail("Enter your full name.")
                if len(pw) < 8:
                    return fail("Password must be at least 8 characters.")
                if pw != confirm.value:
                    return fail("Passwords do not match.")
                if (dob.value or "").strip():
                    try:
                        if date.fromisoformat(dob.value.strip()) > date.today():
                            raise ValueError
                    except ValueError:
                        return fail("Date of birth must be a real date like 1998-04-21, or pick it from the calendar.")
                if not terms.value:
                    return fail("Please agree to the ESRMS Citizen Safety Terms and Privacy Policy.")
                brgy = (barangay.value or "").strip()[:100]
                session = await app.backend.sign_up(em, pw, {
                    "full_name": full_name.value.strip(), "phone": (phone.value or "").strip()[:32],
                    "barangay": brgy or None,
                    "dob": (dob.value or "").strip()[:10] or None,
                    "age": (age.value or "").strip()[:3] or None,
                    "gender": gender.value,
                    "address": (address.value or "").strip()[:200] or None,
                })
                if brgy:
                    app.barangay = brgy
                    await app.set_pref("barangay", brgy)
                if session is None:
                    app.show(sign_in(app))
                    app.toast("Account created. Confirm your email, then sign in.", t.alpha(t.GREEN, 0.9))
                    return
        except BackendError as exc:
            return fail(str(exc))
        app.online = True
        app.open_main()

    async def anonymous(e):
        busy.visible = True
        app.page.update()
        try:
            await app.backend.sign_in_anonymously()
        except BackendError as exc:
            return fail("Quick access is not enabled on the ESRMS server. Create an account, or call "
                        f"{app.config.emergency_hotline} directly. ({exc})")
        app.open_main()

    def not_ready(what):
        return lambda e: app.toast(f"{what} is not set up on the ESRMS server yet.", t.alpha(t.AMBER, 0.9))

    google_label = t.label("Continue with Google", size=15, color=t.CYAN)
    google_busy = ft.ProgressRing(width=18, height=18, color=t.CYAN, visible=False)

    def google_done(message):
        """Called by the app when Google sign-in failed or was cancelled (success opens the dashboard)."""
        google_label.value, google_busy.visible = "Continue with Google", False
        fail(message)

    async def google_sign_in(e):
        if google_busy.visible:
            return  # already waiting for Google
        error.visible = False
        google_label.value, google_busy.visible = "Waiting for Google...", True
        app.page.update()
        app.on_google_result = google_done
        problem = await app.start_google_sign_in()
        if problem:
            google_done(problem)

    def switch(e):
        app.show(sign_in(app, "signin" if register else "register"))

    cta = ft.Container(
        ft.Row([t.label("Complete Registration" if register else "Sign In", size=15, color=t.ON_ACCENT,
                        weight=ft.FontWeight.BOLD),
                ft.Icon(ft.Icons.ARROW_FORWARD, color=t.ON_ACCENT, size=18), busy],
               alignment=ft.MainAxisAlignment.CENTER, spacing=8),
        height=52, border_radius=ft.BorderRadius.all(12), on_click=submit, ink=True,
        gradient=ft.LinearGradient(begin=ft.Alignment.CENTER_LEFT, end=ft.Alignment.CENTER_RIGHT,
                                   colors=[t.CYAN, "#0EA5E9"]),
        shadow=ft.BoxShadow(blur_radius=22, color=t.alpha(t.CYAN, 0.45)),
    )
    google = ft.Container(
        ft.Row([ft.Image(src="google.svg", width=20, height=20), google_label, google_busy],
               alignment=ft.MainAxisAlignment.CENTER, spacing=12),
        height=52, border_radius=ft.BorderRadius.all(12), border=ft.Border.all(1, t.alpha(t.CYAN, 0.45)),
        shadow=ft.BoxShadow(blur_radius=14, color=t.alpha(t.CYAN, 0.12)),
        bgcolor=t.alpha(t.BG, 0.7), on_click=google_sign_in, ink=True,
    )

    if register:
        form = [
            t.label("Create Account", size=24, weight=ft.FontWeight.W_800),
            ft.Text("Create your emergency SOS account to request immediate assistance and access safety alerts.",
                    color=t.MUTED, size=12),
            _labeled("Full Name", full_name),
            _labeled("Email Address", email),
            _labeled("Phone Number", phone),
            ft.Row([_labeled("DOB", dob, expand=4), _labeled("Age", age, expand=3),
                    _labeled("Gender", gender, expand=3)], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.START),
            _labeled("Residential Address / City", address),
            _labeled("Password", password, trailing=t.mono("Min. 8 chars", size=9, color=t.DIM)),
            _labeled("Confirm Password", confirm),
            ft.Row([terms, ft.Text(spans=[
                ft.TextSpan("I agree to the ", ft.TextStyle(color=t.MUTED)),
                ft.TextSpan("ESRMS Citizen Safety Terms", ft.TextStyle(color=t.CYAN)),
                ft.TextSpan(" and Privacy Policy.", ft.TextStyle(color=t.MUTED))], size=12, expand=True)],
                spacing=4),
        ]
    else:
        form = [
            t.label("Welcome Back", size=24, weight=ft.FontWeight.W_800),
            ft.Text("Sign in to access emergency response network.", color=t.MUTED, size=12),
            _labeled("Email", email, trailing=t.mono("CHANNEL AUTH", size=9, color=t.DIM)),
            _labeled("Password", password, trailing=ft.GestureDetector(
                ft.Text("Forgot Password?", color=t.CYAN, size=12, weight=ft.FontWeight.BOLD),
                on_tap=not_ready("Password reset"))),
        ]

    card = ft.Container(
        ft.Column([
            *form,
            error,
            cta,
            _or_divider("OR REGISTER WITH" if register else "OR CONTINUE WITH"),
            google,
            ft.Row([ft.Text("Already have an account?" if register else "Don't have an account?",
                            color=t.MUTED, size=12),
                    ft.GestureDetector(ft.Text("Sign In" if register else "Register for free", color=t.CYAN,
                                               size=12, weight=ft.FontWeight.BOLD), on_tap=switch)],
                   alignment=ft.MainAxisAlignment.CENTER, spacing=6),
            ft.Divider(color=t.BORDER),
            ft.Row([t.mono("SECURE CONNECTION • 256-BIT ENCRYPTED • ESRMS PROTOCOL", size=8, color=t.DIM)],
                   alignment=ft.MainAxisAlignment.CENTER),
        ], spacing=14, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        padding=20, border_radius=ft.BorderRadius.all(20), border=ft.Border.all(1, t.alpha(t.CYAN, 0.35)),
        gradient=ft.LinearGradient(begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
                                   colors=[t.PANEL_HI, t.PANEL]),
        shadow=ft.BoxShadow(blur_radius=30, color=t.alpha(t.CYAN, 0.12)),
    )

    danger = ft.Container(
        ft.Column([
            ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=t.RED, size=16),
                    t.mono("IN DANGER RIGHT NOW?", size=11, color=t.RED, weight=ft.FontWeight.BOLD)], spacing=6),
            ft.Row([t.primary_button(f"CALL {app.config.emergency_hotline}", app.call_hotline, color=t.RED,
                                     icon="PHONE", expand=True)]),
            ft.Row([ft.TextButton(content=ft.Text("Report without an account", color=t.MUTED, size=12),
                                  on_click=anonymous)], alignment=ft.MainAxisAlignment.CENTER),
        ], spacing=8),
        padding=16, border_radius=ft.BorderRadius.all(20), border=ft.Border.all(1, t.alpha(t.RED, 0.45)),
        bgcolor=t.alpha(t.RED, 0.06),
    )

    content = ft.Column([
        _auth_header(app),
        card,
        danger,
        ft.Row([t.mono("© ESRMS MISSION CRITICAL COMMAND. AUTHORIZED ACCESS ONLY.", size=8, color=t.DIM)],
               alignment=ft.MainAxisAlignment.CENTER),
    ], spacing=18, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    # Grid background like the mockup: faint cyan lines behind a centred, phone-width column.
    grid = ft.Container(expand=True, bgcolor=t.BG,
                        gradient=ft.RadialGradient(center=ft.Alignment(0, -0.6), radius=1.0,
                                                   colors=[t.alpha(t.CYAN, 0.08), t.BG]))
    lines = ft.Stack(
        [ft.Container(left=x, top=0, bottom=0, width=1, bgcolor=t.alpha(t.CYAN, 0.04)) for x in range(0, 2000, 28)]
        + [ft.Container(top=y, left=0, right=0, height=1, bgcolor=t.alpha(t.CYAN, 0.04)) for y in range(0, 2400, 28)],
        expand=True,
    )
    holder.content = ft.Stack([
        grid, lines,
        ft.Container(
            ft.Column([ft.Container(content, padding=t.pad(16, 16))], scroll=ft.ScrollMode.AUTO,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            expand=True, alignment=ft.Alignment.TOP_CENTER,
        ),
    ], expand=True)
    return holder


def offline_restore(app):
    """No connection at launch. An SOS can still be written with GPS and is kept on the phone until sent."""
    from esrms.ui import command, sos

    async def retry(e):
        app.show(splash("Reconnecting..."))
        await app.after_onboarding()

    return _page([
        brand(),
        command.pending_banner(app),
        t.panel(ft.Column([
            ft.Row([ft.Icon(ft.Icons.WIFI_OFF, color=t.AMBER), t.label("No connection to ESRMS")]),
            ft.Text("Nothing can reach ESRMS until the phone has a signal. You can still write an SOS now: it "
                    "records your GPS position and is SAVED ON THIS PHONE, then sent when a connection returns "
                    "while the app is open. Saved is not the same as received.", color=t.MUTED),
        ], spacing=10), border_color=t.alpha(t.AMBER, 0.6)),
        t.primary_button(f"CALL {app.config.emergency_hotline}", app.call_hotline, color=t.RED, icon="PHONE"),
        t.primary_button("WRITE SOS (SAVED ON PHONE)", lambda e: sos.start(app), color=t.AMBER,
                         icon="SOS"),
        t.ghost_button("No signal: what can work", lambda e: sos.no_signal_help(app), color=t.MUTED,
                       icon="HELP_OUTLINE"),
        t.ghost_button("Retry connection", retry, icon="REFRESH"),
    ])


# ---------------------------------------------------------------- settings

def _perm_text():
    return {
        "granted": ("Allowed", t.GREEN), "denied": ("Not allowed", t.AMBER),
        "permanentlyDenied": ("Blocked - change in phone Settings", t.RED), "denied_forever": ("Blocked - change in phone Settings", t.RED),
        "restricted": ("Restricted by device", t.RED), "limited": ("Limited", t.AMBER),
        "unsupported": ("Not supported on this device", t.MUTED), "unknown": ("Not asked yet", t.MUTED),
    }


async def open_settings(app, e=None):
    await app.refresh_permission_states()
    s = app.backend.session
    loc_key = "granted" if app.location_state in ("ok", "unknown", "locating", "off", "error") else app.location_state
    barangay = t.field("Barangay for announcements", value=app.barangay,
                       helper="A barangay saved on your account takes priority.")

    def perm_row(title, key, on_request):
        text, color = _perm_text().get(key, (key, t.MUTED))
        return ft.Column([
            ft.Row([t.label(title, size=14), ft.Container(expand=True), t.pill(text.upper()[:22], color)]),
            ft.Row([ft.TextButton(content=ft.Text("Ask again", color=t.CYAN), on_click=on_request,
                                  visible=key not in ("granted", "unsupported")),
                    ft.TextButton(content=ft.Text("Open phone settings", color=t.CYAN), on_click=app.open_app_settings)]),
        ], spacing=2)

    async def ask_loc(e):
        app.page.pop_dialog()
        await app.locate(True)
        await open_settings(app)

    async def ask_notif(e):
        app.page.pop_dialog()
        await app.request_notification_permission()
        await open_settings(app)

    async def save(e):
        app.barangay = (barangay.value or "").strip()[:100]
        await app.set_pref("barangay", app.barangay or None)
        app.page.pop_dialog()
        await app.announcements.refresh(force=True)

    async def sign_out(e):
        app.page.pop_dialog()
        if app.my_pending():
            # Signing out drops the session the saved report needs in order to be sent.
            return app.toast("You have an SOS that is NOT SENT yet. Send or delete it in My reports before "
                             "signing out.", t.alpha(t.AMBER, 0.9))
        await app.backend.sign_out()
        await app.announcements.stop()
        app.announcements = None
        app.show(sign_in(app))

    account = ("Quick access (no account)" if s and s.is_anonymous else (s.email if s else "Not signed in"))
    loc_note = ("Location is used only while the app is open, when you send a report. Background location is "
                "not requested.")
    notif_note = ("Push notifications from the ESRMS server are not configured yet. New announcements show "
                  "in the app while it's open.")
    app.page.show_dialog(ft.AlertDialog(
        bgcolor=t.PANEL, title=t.label("Settings", size=18), scrollable=True,
        content=ft.Column([
            t.mono("ACCOUNT", color=t.CYAN), ft.Text(account, color=t.TEXT),
            ft.Divider(color=t.BORDER),
            t.mono("PERMISSIONS", color=t.CYAN),
            perm_row("Location", loc_key, ask_loc), ft.Text(loc_note, size=12, color=t.MUTED),
            perm_row("Notifications", app.notification_state, ask_notif), ft.Text(notif_note, size=12, color=t.MUTED),
            ft.Divider(color=t.BORDER),
            t.mono("ANNOUNCEMENTS", color=t.CYAN), barangay,
        ], spacing=8, tight=True, width=380),
        actions=[ft.TextButton(content=ft.Text("Sign out", color=t.RED), on_click=sign_out),
                 ft.TextButton(content=ft.Text("Save", color=t.CYAN), on_click=save)],
    ))


# ---------------------------------------------------------------- profile menu

def open_profile(app, e=None):
    from esrms.ui import account
    app.page.run_task(account.show_profile, app)


def open_security(app, e=None):
    from esrms.ui import account
    app.page.run_task(account.show_security, app)


def open_account_settings(app, e=None):
    from esrms.ui import settings
    app.page.run_task(settings.show, app)


def open_profile_menu(app, e=None):
    """Dropdown under the profile button: account header, then My profile / Settings / Security & MFA."""
    from esrms.ui import account
    who = app.identity() or {}
    name = who.get("full_name") or ("Guest" if who.get("is_anonymous") else "Citizen")
    email = "Quick access (no account)" if who.get("is_anonymous") else (who.get("email") or "Not signed in")

    def item(icon, text, action):
        async def tap(ev):
            app.page.pop_dialog()
            result = action(app)
            if hasattr(result, "__await__"):
                await result
        return ft.Container(
            ft.Row([ft.Icon(icon, color=t.MUTED, size=20), t.label(text, size=16, weight=ft.FontWeight.W_500)],
                   spacing=14),
            padding=t.pad(18, 13), on_click=tap, ink=True)

    header = ft.Container(ft.Row([
        account.avatar(who.get("full_name"), who.get("email"),
                       app.backend.session.metadata.get("avatar_url") if app.backend.session else None, size=52),
        ft.Column([
            t.label(name, size=17, weight=ft.FontWeight.W_800, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(email, size=13, color=t.MUTED, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            t.mono(app.tr("menu.barangay", name=app.barangay.upper()) if app.barangay else app.tr("menu.citizen"),
                   size=11, color=t.CYAN,
                   weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
        ], spacing=1, expand=True),
    ], spacing=14), padding=t.pad(18, 16))

    app.page.show_dialog(ft.AlertDialog(
        bgcolor=t.BG_TOP, alignment=ft.Alignment.TOP_RIGHT, inset_padding=ft.Padding(left=24, top=64, right=12, bottom=24),
        content_padding=0, shape=ft.RoundedRectangleBorder(radius=14, side=ft.BorderSide(1, t.BORDER)),
        content=ft.Column([
            header,
            ft.Divider(color=t.BORDER, height=1),
            ft.Container(height=4),
            item(ft.Icons.PERSON_OUTLINE, app.tr("menu.profile"), open_profile),
            item(ft.Icons.SETTINGS_OUTLINED, app.tr("menu.settings"), open_account_settings),
            item(ft.Icons.LOCK_OUTLINE, app.tr("menu.security"), open_security),
            ft.Container(height=4),
        ], spacing=0, tight=True, width=340),
    ))
