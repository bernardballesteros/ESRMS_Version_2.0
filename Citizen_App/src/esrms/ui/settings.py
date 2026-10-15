"""Account Settings page (profile menu → Settings): language, theme and account information.

Language and theme are device preferences kept in SharedPreferences. Account type and member-since
come from the signed-in account on the server; the last values seen are kept on the phone so the
page still opens offline.
"""
from datetime import datetime

import flet as ft

import esrms
from esrms import i18n, report
from esrms.backend import BackendError
from esrms.ui import theme as t


def _row(icon, title, value, on_click=None, value_color=None):
    return ft.Container(
        ft.Row([
            *([ft.Container(ft.Icon(icon, color=t.TEXT, size=20), width=40, height=40, alignment=ft.Alignment.CENTER,
                            border_radius=ft.BorderRadius.all(40), border=ft.Border.all(1, t.BORDER),
                            bgcolor=t.PANEL_HI)] if icon else []),
            ft.Column([ft.Text(title, size=14, color=t.TEXT, weight=ft.FontWeight.W_500),
                       ft.Text(value, size=13, color=value_color or t.CYAN)], spacing=3, expand=True),
            *([ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.MUTED)] if on_click else []),
        ], spacing=14),
        padding=t.pad(14, 12), on_click=on_click, ink=on_click is not None)


def _card(rows):
    controls = []
    for i, row in enumerate(rows):
        if i:
            controls.append(ft.Container(ft.Divider(color=t.BORDER, height=1), padding=t.pad(14, 0)))
        controls.append(row)
    return ft.Container(ft.Column(controls, spacing=0), bgcolor=t.PANEL, border_radius=ft.BorderRadius.all(14),
                        border=ft.Border.all(1, t.alpha(t.CYAN, 0.25)), clip_behavior=ft.ClipBehavior.ANTI_ALIAS)


def _section(text):
    return t.label(text, size=15, color=t.CYAN, weight=ft.FontWeight.W_700)


def member_since(created_at):
    """'Apr 15, 2025' in Philippine time, or None when the timestamp is missing or unreadable."""
    try:
        return report.local_time(datetime.fromisoformat(str(created_at).replace("Z", "+00:00")), "%b %d, %Y")
    except (TypeError, ValueError):
        return None


def role_label(app, info):
    if info.get("is_anonymous"):
        return app.tr("role.guest")
    role = (info.get("role") or "").lower()
    return app.tr(f"role.{role}") if f"role.{role}" in i18n.STRINGS["en"] else app.tr("set.unavailable")


async def show(app, e=None, notice=None, refresh=True):
    """Draw the page from what is saved on the phone, then refresh the account details from the server."""
    back = lambda ev=None: app.home()  # noqa: E731
    warning = None
    if refresh and app.backend.session:
        _draw(app, back, notice, None, loading=True)
        try:
            await app.refresh_account_info()
            app.online = True
        except BackendError as exc:
            app.online = exc.kind != "network"
            warning = app.tr("set.offline") if exc.kind == "network" else str(exc)
    elif refresh:
        warning = app.tr("set.offline")
    _draw(app, back, notice, warning)


def _draw(app, back, notice, warning, loading=False):
    tr, info = app.tr, app.account_info or {}
    unavailable = "..." if loading and not info else tr("set.unavailable")
    since = member_since(info.get("created_at"))
    app._listeners.clear()
    app.show(ft.Container(ft.Column([
        ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, icon_color=t.CYAN, on_click=back),
                t.label(tr("set.title"), size=18, weight=ft.FontWeight.W_800, expand=True)]),
        ft.Divider(color=t.BORDER, height=1),
        *([t.panel(ft.Text(notice, color=t.GREEN, size=13), border_color=t.alpha(t.GREEN, 0.6))] if notice else []),
        *([t.panel(ft.Text(warning, color=t.AMBER, size=13), border_color=t.alpha(t.AMBER, 0.6))] if warning else []),
        _section(tr("set.preferences")),
        _card([
            _row(ft.Icons.LANGUAGE, tr("set.language"), i18n.LANGUAGES[app.lang][0], lambda ev: language_dialog(app)),
            _row(ft.Icons.PALETTE_OUTLINED, tr("set.theme"), tr(f"set.theme.{t.name}"), lambda ev: theme_dialog(app)),
        ]),
        ft.Divider(color=t.BORDER, height=1),
        _section(tr("set.info")),
        _card([
            _row(None, tr("set.account_type"), role_label(app, info) if info else unavailable),
            _row(None, tr("set.member_since"), since or unavailable),
            _row(None, tr("set.version"), esrms.__version__, value_color=t.TEXT),
        ]),
        ft.Divider(color=t.BORDER, height=1),
        _section(tr("set.more")),
        _card([_row(ft.Icons.TUNE, tr("set.permissions"), tr("set.permissions_sub"),
                    lambda ev: app.page.run_task(_open_permissions, app), value_color=t.MUTED)]),
    ], spacing=14, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        bgcolor=t.BG, padding=t.pad(16, 12), expand=True))


async def _open_permissions(app):
    from esrms.ui import screens
    await screens.open_settings(app)


def _choice_dialog(app, title, options, current, on_apply):
    """Radio list with Cancel / Apply. options: [(value, label, enabled, note)]."""
    group = ft.RadioGroup(value=current, content=ft.Column([
        ft.Column([
            ft.Radio(value=value, label=label, disabled=not enabled, active_color=t.CYAN,
                     label_style=ft.TextStyle(color=t.TEXT if enabled else t.DIM, size=15)),
            *([ft.Container(ft.Text(note, size=12, color=t.MUTED), padding=ft.Padding(left=48, top=0, right=0, bottom=0))]
              if note else []),
        ], spacing=0) for value, label, enabled, note in options], spacing=6, tight=True))

    async def apply(ev):
        app.page.pop_dialog()
        if group.value != current:
            await on_apply(group.value)

    app.page.show_dialog(ft.AlertDialog(
        bgcolor=t.PANEL, title=t.label(title, size=17), scrollable=True,
        content=ft.Container(group, width=340),
        actions=[ft.TextButton(content=ft.Text(app.tr("set.cancel"), color=t.MUTED),
                               on_click=lambda ev: app.page.pop_dialog()),
                 ft.TextButton(content=ft.Text(app.tr("set.apply"), color=t.CYAN), on_click=apply)]))


def language_dialog(app, e=None):
    async def apply(code):
        saved = await app.set_language(code)
        await show(app, refresh=False, notice=app.tr("set.language_saved", name=i18n.LANGUAGES[app.lang][0])
                   if saved else app.tr("set.not_saved"))

    _choice_dialog(app, app.tr("set.choose_language"),
                   [(code, name, i18n.selectable(code), note) for code, (name, _, note) in i18n.LANGUAGES.items()],
                   app.lang, apply)


def theme_dialog(app, e=None):
    async def apply(name):
        saved = await app.set_theme(name)
        await show(app, refresh=False, notice=app.tr("set.theme_saved", name=app.tr(f"set.theme.{t.name}"))
                   if saved else app.tr("set.not_saved"))

    _choice_dialog(app, app.tr("set.choose_theme"),
                   [(name, app.tr(f"set.theme.{name}"), True, "") for name in t.PALETTES], t.name, apply)
