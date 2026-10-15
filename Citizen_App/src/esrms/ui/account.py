"""My Profile, Edit Profile and Security & MFA pages (opened from the profile menu).

Everything shown comes from Supabase Auth for the signed-in citizen, and every "saved", "changed"
or "enabled" message is shown only after Supabase confirmed it. Rules live in esrms/account.py,
network calls in esrms/backend.py.
"""
import base64
import os
import tempfile
from urllib.parse import unquote

import flet as ft

from esrms import account as rules
from esrms.backend import BackendError
from esrms.ui import theme as t

RECOVERY_TEXT = ("Lost your authenticator? The app cannot switch MFA off without a code, and it has no recovery "
                 "codes of its own. Contact your ESRMS administrator: after confirming who you are, they can "
                 "remove the authenticator from your account (ESRMS dashboard, or Supabase > Authentication > "
                 "Users), and you can then set up a new one here.")


# ---------------------------------------------------------------- shared pieces

def _page(app, title, controls, back):
    app._listeners.clear()
    app.show(ft.Container(ft.Column([
        ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, icon_color=t.CYAN, on_click=back),
                t.label(title, size=18, weight=ft.FontWeight.W_800, expand=True)]),
        ft.Divider(color=t.BORDER, height=1),
        *controls,
    ], spacing=14, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        bgcolor=t.BG, padding=t.pad(16, 12), expand=True))


def _loading(app, title, back):
    _page(app, title, [ft.Container(ft.ProgressRing(color=t.CYAN), alignment=ft.Alignment.CENTER, padding=40)], back)


def _input(value="", **kw):
    return ft.TextField(
        value=value or "", border_color=t.BORDER, focused_border_color=t.CYAN, color=t.TEXT, cursor_color=t.CYAN,
        border_radius=10, filled=True, fill_color=t.PANEL, text_size=14, content_padding=t.pad(14, 14),
        hint_style=ft.TextStyle(color=t.DIM), error_style=ft.TextStyle(color=t.RED), **kw)


def _labeled(text, control, required=False):
    spans = [ft.TextSpan(text, ft.TextStyle(color=t.TEXT))]
    if required:
        spans.append(ft.TextSpan(" *", ft.TextStyle(color=t.RED)))
    return ft.Column([ft.Text(spans=spans, size=13, weight=ft.FontWeight.W_500), control], spacing=6,
                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH)


def _section(text):
    return t.label(text, size=15, color=t.CYAN, weight=ft.FontWeight.W_700)


def _message(text="", color=None):
    return ft.Text(text, color=color or t.RED, size=13, visible=bool(text))


def _busy_button(text, on_click, **kw):
    """Primary button that shows a spinner and ignores taps while its handler runs."""
    ring = ft.ProgressRing(width=18, height=18, color=t.on(t.CYAN), visible=False)
    btn = t.primary_button(text, None, **kw)
    btn.content = ft.Row([ring, ft.Text(text, weight=ft.FontWeight.BOLD, color=t.on(t.CYAN))], tight=True, spacing=10,
                         alignment=ft.MainAxisAlignment.CENTER)

    async def run(e):
        if btn.disabled:
            return
        btn.disabled, ring.visible = True, True
        btn.update()
        try:
            await on_click(e)
        finally:
            btn.disabled, ring.visible = False, False
            try:
                btn.update()
            except Exception:
                pass  # the page was replaced while saving

    btn.on_click = run
    return btn


def avatar(name, email, src=None, size=96, on_camera=None):
    """Round avatar with cyan border: the picture if there is one, else initials. src: URL or image bytes."""
    inner = (ft.Image(src=src, width=size, height=size, fit=ft.BoxFit.COVER,
                      error_content=t.mono(rules.initials(name, email), size=size * 0.3, color=t.on(t.CYAN),
                                           weight=ft.FontWeight.BOLD))
             if src else t.mono(rules.initials(name, email), size=size * 0.3, color=t.on(t.CYAN),
                                weight=ft.FontWeight.BOLD))
    circle = ft.Container(inner, width=size, height=size, alignment=ft.Alignment.CENTER,
                          border_radius=ft.BorderRadius.all(size), clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                          gradient=ft.LinearGradient(begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
                                                     colors=[t.CYAN, "#0E7490"]),
                          border=ft.Border.all(3, t.CYAN))
    if not on_camera:
        return circle
    badge = ft.Container(ft.Icon(ft.Icons.PHOTO_CAMERA, color=t.TEXT, size=16), width=32, height=32,
                         alignment=ft.Alignment.CENTER, bgcolor=t.PANEL_HI, border_radius=ft.BorderRadius.all(8),
                         border=ft.Border.all(1, t.BORDER), right=0, bottom=0, on_click=on_camera,
                         tooltip="Change picture")
    return ft.Stack([circle, badge], width=size + 8, height=size + 4)


def _account(app):
    """(session, name, email) of the signed-in citizen."""
    s = app.backend.session
    return s, (s.metadata.get("full_name") or "") if s else "", (s.email or "") if s else ""


async def _refresh(app):
    """Reload the account from Supabase Auth. Returns an error text to show, or ''."""
    if not app.backend.session:
        return "You are offline. Showing nothing here until the phone reconnects."
    try:
        await app.backend.load_account()
        app.online = True
        return ""
    except BackendError as exc:
        app.online = exc.kind != "network"
        return ("No connection: showing the details saved on this phone." if exc.kind == "network" else str(exc))


# ---------------------------------------------------------------- my profile

async def show_profile(app, e=None, notice=None):
    back = lambda ev=None: app.home()  # noqa: E731
    _loading(app, "My Profile", back)
    warning = await _refresh(app)
    s, name, email = _account(app)
    if not s:
        return _page(app, "My Profile", [_message(warning, t.AMBER),
                                         t.ghost_button("Back", back, color=t.MUTED)], back)
    meta = s.metadata

    def shown(value):
        return _input(value or "", read_only=True, hint_text="Not provided")

    _page(app, "My Profile", [
        ft.Column([
            avatar(name, email, meta.get("avatar_url")),
            t.label(name or "No name saved", size=19, weight=ft.FontWeight.W_800),
            ft.Text("Quick access (no account)" if s.is_anonymous else email, color=t.CYAN, size=13),
            t.mono("ESRMS CITIZEN", size=11, color=t.CYAN, weight=ft.FontWeight.BOLD),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        *([t.panel(ft.Text(notice, color=t.GREEN, size=13), border_color=t.alpha(t.GREEN, 0.6))] if notice else []),
        *([t.panel(ft.Text(warning, color=t.AMBER, size=13), border_color=t.alpha(t.AMBER, 0.6))] if warning else []),
        *([t.panel(ft.Text(f"Waiting for confirmation: a link was sent to {s.new_email}. Your sign-in email "
                           "stays the same until you open that link.", color=t.AMBER, size=13),
                   border_color=t.alpha(t.AMBER, 0.6))] if s.new_email else []),
        ft.Divider(color=t.BORDER, height=1),
        _section("Personal Information"),
        _labeled("Full Name", shown(name), required=True),
        _labeled("Email Address", shown(email), required=True),
        _labeled("Phone Number", shown(meta.get("phone"))),
        _labeled("Address", shown(meta.get("address"))),
        ft.Divider(color=t.BORDER, height=1),
        t.primary_button("Edit Profile", lambda ev: show_edit_profile(app), disabled=bool(warning) or s.is_anonymous),
        *([ft.Text("Quick-access sessions have no profile to edit. Create an account to save your details.",
                   size=12, color=t.MUTED)] if s.is_anonymous else []),
    ], back)


def show_edit_profile(app, e=None):
    s, name, email = _account(app)
    meta = s.metadata
    _, has_password, _ = rules.sign_in_methods(s.providers, s.is_anonymous)
    back = lambda ev=None: app.page.run_task(show_profile, app)  # noqa: E731  (Cancel: nothing was saved)

    full_name = _input(name, autofocus=True)
    email_f = _input(email, keyboard_type=ft.KeyboardType.EMAIL, disabled=not has_password)
    phone = _input(meta.get("phone"), keyboard_type=ft.KeyboardType.PHONE, hint_text="+63 912 345 6789")
    address = _input(meta.get("address"), multiline=True, min_lines=2, max_lines=4)
    fields = {"full_name": full_name, "email": email_f, "phone": phone, "address": address}
    error = _message()
    picture = {"data": None, "mime": None, "ext": None, "remove": False}  # chosen but not saved yet
    holder = ft.Container(alignment=ft.Alignment.CENTER)
    remove_btn = ft.TextButton(content=ft.Text("Remove picture", color=t.MUTED, size=12))

    def draw_avatar():
        src = picture["data"] or (None if picture["remove"] else meta.get("avatar_url"))
        holder.content = avatar(full_name.value or name, email, src, on_camera=choose)
        remove_btn.visible = bool(src)

    async def choose(ev):
        try:
            files = await app.picker.pick_files(dialog_title="Choose a profile picture",
                                                file_type=ft.FilePickerFileType.IMAGE, with_data=True)
        except Exception:
            error.value, error.visible = "Couldn't open the picture chooser on this device.", True
            return app.page.update()
        if not files:
            return
        data = files[0].bytes
        if data is None and files[0].path:
            try:
                with open(files[0].path, "rb") as fh:
                    data = fh.read(rules.MAX_IMAGE_BYTES + 1)
            except OSError:
                data = None
        try:
            mime, ext = rules.validate_image(data or b"")
        except rules.ValidationError as exc:
            error.value, error.visible = exc.errors["file"], True
            return app.page.update()
        picture.update(data=data, mime=mime, ext=ext, remove=False)
        error.visible = False
        draw_avatar()   # preview only; it is uploaded when Save Changes succeeds
        app.page.update()

    def remove(ev):
        picture.update(data=None, remove=True)
        draw_avatar()
        app.page.update()

    remove_btn.on_click = remove

    async def save(ev):
        for f in fields.values():
            f.error = None
        error.visible = False
        try:
            clean = rules.validate_profile(full_name.value, email_f.value, phone.value, address.value)
        except rules.ValidationError as exc:
            for key, text in exc.errors.items():
                fields[key].error = text
            return app.page.update()
        notes = []
        try:
            synced = await app.backend.update_profile(clean["full_name"], clean["phone"], clean["address"])
            if clean["email"] != email.lower() and has_password:
                try:
                    if await app.backend.change_email(clean["email"]):
                        notes.append(f"A confirmation link was sent to {clean['email']}. Your sign-in email "
                                     "changes only after you open it.")
                except BackendError as exc:
                    if exc.kind in ("network", "auth"):
                        raise
                    fields["email"].error = str(exc)
                    error.value, error.visible = "Your other details were saved, but not the email address.", True
                    return app.page.update()
            if picture["data"]:
                await app.backend.set_avatar(picture["data"], picture["mime"], picture["ext"])
            elif picture["remove"] and meta.get("avatar_url"):
                await app.backend.remove_avatar()
        except BackendError as exc:
            app.online = exc.kind != "network"
            error.value = {"network": "NOT SAVED: no connection to the ESRMS server. Try again when you are online.",
                           }.get(exc.kind, f"Not saved: {exc}")
            if exc.kind == "not_configured":
                error.value = f"Your details were saved, but not the picture. {exc}"
            error.visible = True
            return app.page.update()
        app.online = True
        if not synced:
            notes.append("The dispatcher dashboard's copy could not be updated; it will show your old name and "
                         "phone until an administrator checks the users table.")
        await show_profile(app, notice=" ".join(["Profile saved.", *notes]))

    draw_avatar()
    _page(app, "Edit Profile", [
        ft.Column([holder, remove_btn, ft.Text("PNG, JPEG or WebP, up to 2 MB.", size=11, color=t.DIM)],
                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
        _labeled("Full Name", full_name, required=True),
        _labeled("Email Address", email_f, required=True),
        *([ft.Text("Changing your email sends a confirmation link to the new address." if has_password else
                   "This email belongs to your sign-in provider and can't be changed here.", size=12,
                   color=t.MUTED)]),
        _labeled("Phone Number", phone),
        _labeled("Address", address),
        error,
        ft.Row([t.ghost_button("Cancel", back, expand=True),
                ft.Container(_busy_button("Save Changes", save), expand=True)], spacing=12),
    ], back)


# ---------------------------------------------------------------- security & MFA

async def show_security(app, e=None, notice=None):
    back = lambda ev=None: app.home()  # noqa: E731
    _loading(app, "Security & MFA", back)
    warning = await _refresh(app)
    s, _, email = _account(app)
    if not s:
        return _page(app, "Security & MFA", [_message(warning, t.AMBER),
                                             t.ghost_button("Back", back, color=t.MUTED)], back)
    labels, has_password, google_only = rules.sign_in_methods(s.providers, s.is_anonymous)
    offline = bool(warning)

    # ---- change password
    def secret(hint=""):
        return _input(password=True, can_reveal_password=True, hint_text=hint)

    current, new, confirm = secret(), secret(), secret()
    pw_fields = {"current": current, "new": new, "confirm": confirm}
    pw_msg = _message()
    checklist = ft.Column(spacing=2)

    def draw_checklist(ev=None):
        checklist.controls = [
            ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.RADIO_BUTTON_UNCHECKED,
                            color=t.GREEN if ok else t.DIM, size=15),
                    ft.Text(label, size=12, color=t.TEXT if ok else t.MUTED)], spacing=8)
            for label, ok in rules.password_checks(new.value or "")]
        if ev:
            checklist.update()

    new.on_change = draw_checklist
    draw_checklist()

    async def change_password(ev):
        for f in pw_fields.values():
            f.error = None
        pw_msg.visible = False
        try:
            rules.validate_password_change(current.value, new.value, confirm.value)
        except rules.ValidationError as exc:
            for key, text in exc.errors.items():
                pw_fields[key].error = text
            draw_checklist()
            return app.page.update()
        try:
            await app.backend.change_password(current.value, new.value)
        except BackendError as exc:
            app.online = exc.kind != "network"
            if exc.kind == "wrong_password":
                current.error = str(exc)
            else:
                pw_msg.value = ("NOT CHANGED: no connection to the ESRMS server." if exc.kind == "network"
                                else f"Not changed: {exc}")
                pw_msg.color, pw_msg.visible = t.RED, True
            return app.page.update()
        for f in pw_fields.values():
            f.value = ""
        draw_checklist()
        pw_msg.value, pw_msg.color, pw_msg.visible = "Password changed.", t.GREEN, True
        app.page.update()

    if s.is_anonymous:
        password_block = [t.panel(ft.Text("Quick-access sessions have no password. Create an account to secure "
                                          "your reports with one.", size=13, color=t.MUTED))]
    elif google_only:
        password_block = [t.panel(ft.Column([
            ft.Text("You sign in with Google, so ESRMS holds no password for this account. Your password and "
                    "2-step verification are managed by Google.", size=13, color=t.MUTED),
            t.ghost_button("Open Google account security",
                           lambda ev: app.page.run_task(app.launcher.launch_url, rules.GOOGLE_SECURITY_URL),
                           icon="OPEN_IN_NEW"),
        ], spacing=10))]
    else:
        password_block = [
            _labeled("Current Password", current, required=True),
            _labeled("New Password", new, required=True),
            checklist,
            _labeled("Confirm New Password", confirm, required=True),
            pw_msg,
            _busy_button("Change Password", change_password, disabled=offline),
        ]

    # ---- MFA (status is the factor list Supabase Auth returned just now)
    factors = s.verified_totp
    enabled = bool(factors)
    if enabled:
        action = t.ghost_button("Disable MFA", lambda ev: _disable_mfa_dialog(app, factors[0]), color=t.RED)
        detail = ("An authenticator app is linked. You are asked for its 6-digit code when you sign in to "
                  "this app.")
    else:
        action = t.ghost_button("Enable MFA", lambda ev: app.page.run_task(show_mfa_setup, app))
        detail = ("Add a second step to sign-in: a 6-digit code from an authenticator app such as Google "
                  "Authenticator, Microsoft Authenticator or Aegis.")
    mfa_block = [] if s.is_anonymous else [
        _section("Multi-Factor Authentication (MFA)"),
        t.panel(ft.Column([
            ft.Row([ft.Icon(ft.Icons.PHONELINK_LOCK, color=t.CYAN, size=26),
                    ft.Column([ft.Text("Status", size=12, color=t.MUTED),
                               ft.Text("Enabled" if enabled else "Not Enabled", size=14, weight=ft.FontWeight.BOLD,
                                       color=t.GREEN if enabled else t.RED)], spacing=0, expand=True),
                    *([] if offline else [action])], spacing=12),
            ft.Text(detail, size=12, color=t.MUTED),
        ], spacing=10)),
        *([t.panel(ft.Text(RECOVERY_TEXT, size=12, color=t.MUTED))] if enabled else []),
    ]

    _page(app, "Security & MFA", [
        *([t.panel(ft.Text(notice, color=t.GREEN, size=13), border_color=t.alpha(t.GREEN, 0.6))] if notice else []),
        *([t.panel(ft.Text(warning, color=t.AMBER, size=13), border_color=t.alpha(t.AMBER, 0.6))] if warning else []),
        _section("Change Password"),
        *password_block,
        *mfa_block,
        _section("Sign-in Method"),
        t.panel(ft.Row([
            (ft.Image(src="google.svg", width=24, height=24) if "Google" in labels
             else ft.Icon(ft.Icons.MAIL_OUTLINE, color=t.CYAN, size=24)),
            ft.Column([ft.Text("Authentication Provider", size=12, color=t.MUTED),
                       ft.Text(" + ".join(labels), size=14, weight=ft.FontWeight.BOLD, color=t.TEXT),
                       ft.Text(email or "No email on this account", size=12, color=t.MUTED)], spacing=0, expand=True),
        ], spacing=12)),
    ], back)


def _disable_mfa_dialog(app, factor):
    code = _input(keyboard_type=ft.KeyboardType.NUMBER, max_length=6, hint_text="6-digit code", autofocus=True)
    msg = _message()

    async def confirm(ev):
        try:
            digits = rules.clean_code(code.value)
            await app.backend.mfa_remove(factor["id"], digits)
        except rules.ValidationError as exc:
            code.error = exc.errors["code"]
            return app.page.update()
        except BackendError as exc:
            msg.value = ("No connection. MFA is still enabled." if exc.kind == "network"
                         else f"MFA is still enabled: {exc}")
            msg.visible = True
            return app.page.update()
        app.page.pop_dialog()
        still = bool(app.backend.session.verified_totp)   # what the server says now
        await show_security(app, notice=None if still else "MFA disabled. Your account now signs in with one step.")

    app.page.show_dialog(ft.AlertDialog(
        bgcolor=t.PANEL, title=t.label("Disable MFA?", size=17), scrollable=True,
        content=ft.Column([
            ft.Text("Your account will be protected by its password only. Enter a current code from your "
                    "authenticator app to confirm it is you.", size=13, color=t.MUTED),
            code, msg], tight=True, spacing=10, width=360),
        actions=[ft.TextButton(content=ft.Text("Keep MFA", color=t.CYAN), on_click=lambda ev: app.page.pop_dialog()),
                 ft.TextButton(content=ft.Text("Disable", color=t.RED), on_click=confirm)]))


def _qr_file(qr_code):
    """Supabase returns the QR as an SVG data URI. Flet draws SVG from a file, so it is written to
    the app's private temp folder and deleted when the setup page is left (it encodes the setup key)."""
    if not qr_code:
        return None
    svg = qr_code
    if qr_code.startswith("data:"):
        head, _, body = qr_code.partition(",")
        svg = base64.b64decode(body).decode("utf-8", "replace") if ";base64" in head else unquote(body)
    try:
        fd, path = tempfile.mkstemp(suffix=".svg", prefix="esrms_mfa_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(svg)
        return path
    except OSError:
        return None


def _forget(path):
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


async def show_mfa_setup(app, e=None):
    qr_path = None

    def back(ev=None):
        _forget(qr_path)
        app.page.run_task(show_security, app)

    _loading(app, "Set up MFA", back)
    try:
        factor = await app.backend.mfa_enroll()
    except BackendError as exc:
        app.online = exc.kind != "network"
        text = ("No connection. MFA was not changed." if exc.kind == "network" else
                f"Could not start MFA setup: {exc}")
        return _page(app, "Set up MFA", [_message(text), t.ghost_button("Back", back, color=t.MUTED)], back)
    totp = factor.get("totp") or {}
    secret, uri = totp.get("secret") or "", totp.get("uri") or ""
    qr_path = _qr_file(totp.get("qr_code"))
    qr = (ft.Container(ft.Image(src=qr_path, width=200, height=200), bgcolor="white", padding=10,
                       border_radius=ft.BorderRadius.all(12)) if qr_path else None)
    code = _input(keyboard_type=ft.KeyboardType.NUMBER, max_length=6, hint_text="6-digit code")
    msg = _message()

    async def copy(ev):
        try:
            await app.clipboard.set(secret)
            app.toast("Setup key copied. Paste it into your authenticator app.")
        except Exception:
            app.toast("Couldn't copy. Type the key shown instead.", t.alpha(t.AMBER, 0.9))

    async def open_app(ev):
        try:
            await app.launcher.launch_url(uri)
        except Exception:
            app.toast("No authenticator app answered. Copy the setup key instead.", t.alpha(t.AMBER, 0.9))

    async def verify(ev):
        code.error, msg.visible = None, False
        try:
            digits = rules.clean_code(code.value)
            session = await app.backend.mfa_verify(factor["id"], digits)
        except rules.ValidationError as exc:
            code.error = exc.errors["code"]
            return app.page.update()
        except BackendError as exc:
            msg.value = ("No connection. MFA is NOT enabled yet." if exc.kind == "network"
                         else f"MFA is NOT enabled yet: {exc} Check the code and the phone's clock, then try again.")
            msg.visible = True
            return app.page.update()
        ok = any(f.get("id") == factor["id"] for f in session.verified_totp)
        _forget(qr_path)
        await show_security(app, notice="MFA enabled. You'll be asked for a code when you sign in." if ok else None)

    _page(app, "Set up MFA", [
        t.panel(ft.Column([
            t.label("1. Add ESRMS Citizen to your authenticator app", size=14),
            ft.Text("On this phone: tap the button below, or copy the setup key into the app. "
                    "On another device: scan the QR code with its authenticator app.", size=12, color=t.MUTED),
            t.ghost_button("Open authenticator app", open_app, icon="OPEN_IN_NEW"),
            t.mono("SETUP KEY", size=10, color=t.CYAN),
            ft.Row([ft.Container(ft.Text(secret, selectable=True, font_family=t.MONO, size=13, color=t.TEXT),
                                 expand=True),
                    ft.IconButton(ft.Icons.COPY, icon_color=t.CYAN, on_click=copy, tooltip="Copy key")]),
            *([ft.Row([qr], alignment=ft.MainAxisAlignment.CENTER)] if qr else []),
        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)),
        t.panel(ft.Column([
            t.label("2. Enter the 6-digit code it shows", size=14),
            code, msg,
            ft.Text("MFA is switched on only after this code is confirmed by the server.", size=12, color=t.MUTED),
            _busy_button("Verify and enable MFA", verify),
        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)),
        t.ghost_button("Cancel", back, color=t.MUTED),
    ], back)


# ---------------------------------------------------------------- MFA step at sign-in

def mfa_challenge(app):
    """Shown after password sign-in when the account has an authenticator: the app opens only after
    Supabase Auth verified a code for this session."""
    factor = app.backend.session.verified_totp[0]
    code = _input(keyboard_type=ft.KeyboardType.NUMBER, max_length=6, hint_text="6-digit code", autofocus=True)
    msg = _message()

    async def verify(ev):
        code.error, msg.visible = None, False
        try:
            session = await app.backend.mfa_verify(factor["id"], rules.clean_code(code.value))
        except rules.ValidationError as exc:
            code.error = exc.errors["code"]
            return app.page.update()
        except BackendError as exc:
            msg.value = "No connection. Try again." if exc.kind == "network" else f"Code not accepted: {exc}"
            msg.visible = True
            return app.page.update()
        if session.needs_mfa:
            msg.value, msg.visible = "The server did not confirm the code. Try again.", True
            return app.page.update()
        app.open_main()

    async def sign_out(ev):
        from esrms.ui import screens
        await app.backend.sign_out()
        app.show(screens.sign_in(app))

    return ft.Container(ft.Column([
        ft.Container(ft.Icon(ft.Icons.PHONELINK_LOCK, color=t.CYAN, size=56), alignment=ft.Alignment.CENTER),
        ft.Container(t.label("Enter your authenticator code", size=19), alignment=ft.Alignment.CENTER),
        ft.Text(f"{app.backend.session.email or 'This account'} is protected by MFA. Open your authenticator "
                "app and enter the 6-digit code for ESRMS Citizen.", size=13, color=t.MUTED,
                text_align=ft.TextAlign.CENTER),
        code, msg,
        _busy_button("Verify", verify),
        t.ghost_button("Sign out", sign_out, color=t.MUTED),
        t.panel(ft.Text(RECOVERY_TEXT, size=12, color=t.MUTED)),
        t.ghost_button(f"Call {app.config.emergency_hotline}", app.call_hotline, color=t.RED, icon="PHONE"),
    ], spacing=14, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        bgcolor=t.BG, padding=t.pad(20, 28), expand=True)
