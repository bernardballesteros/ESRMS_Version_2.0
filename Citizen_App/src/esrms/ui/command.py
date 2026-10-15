"""COMMAND tab: the Citizen Dashboard from the reference design."""
import flet as ft

from esrms import catalog, report
from esrms.ui import screens, sos
from esrms.ui import theme as t


def build(app):
    return ft.Container(
        ft.Column([
            _header(app),
            pending_banner(app),
            _status_panel(app),
            _services_header(app),
            *[_service_card(app, c) for c in catalog.CATEGORIES],
            _my_reports_link(app),
            _telemetry(app),
        ], spacing=14, scroll=ft.ScrollMode.AUTO),
        bgcolor=t.BG, padding=t.pad(16, 14), expand=True,
    )


def _header(app):
    bell_dot = ft.Container(t.dot(t.RED, 9), right=8, top=8, visible=app.unseen_urgent > 0)
    bell = ft.Stack([
        ft.IconButton(ft.Icons.NOTIFICATIONS_OUTLINED, icon_color=t.TEXT, tooltip="Announcements",
                      on_click=lambda e: app.switch_tab("announcement")),
        bell_dot,
    ])
    app.on_status_change(lambda: setattr(bell_dot, "visible", app.unseen_urgent > 0))
    profile = ft.Container(
        ft.Icon(ft.Icons.PERSON, color=t.CYAN), width=44, height=44, alignment=ft.Alignment.CENTER,
        border_radius=ft.BorderRadius.all(44), border=ft.Border.all(1.5, t.alpha(t.CYAN, 0.7)),
        on_click=lambda e: screens.open_profile_menu(app), tooltip="Account",
    )
    return ft.Row([ft.Container(screens.brand(app.tr("dash.title"), app.tr("dash.tagline"), app), expand=True), bell,
                   profile], spacing=4)


def pending_banner(app):
    """Shown only while an SOS exists on this phone that ESRMS has not confirmed."""
    text = t.label("", size=14, color=t.AMBER, expand=True)
    box = t.panel(ft.Row([ft.Icon(ft.Icons.CLOUD_OFF, color=t.AMBER, size=22), text,
                          ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.AMBER)], spacing=10),
                  padding=12, border_color=t.alpha(t.AMBER, 0.8), bgcolor=t.alpha(t.AMBER, 0.08),
                  on_click=lambda e: app.page.run_task(sos.show_my_reports, app))

    def refresh():
        n = len(app.my_pending())
        box.visible = n > 0
        text.value = app.tr("dash.pending", n=n)

    refresh()
    app.on_status_change(refresh)
    return box


def _location_line(app):
    state, loc = app.location_state, app.location
    if loc:
        acc = f" ±{loc.accuracy_m:.0f}M" if loc.accuracy_m is not None else ""
        ns, ew = ("N" if loc.latitude >= 0 else "S"), ("E" if loc.longitude >= 0 else "W")
        coords = f"{ns} {abs(loc.latitude):.5f}°  {ew} {abs(loc.longitude):.5f}°{acc}"
        if loc.is_current():
            return f"{coords}\n" + app.tr("dash.fix_current", age=report.age_text(loc.age_seconds()).upper()), t.TEXT
        retry = (app.tr("dash.fix_acquiring", s=app.locating_seconds()) if state == "locating"
                 else app.tr("dash.fix_retry"))
        return f"{coords}\n{loc.freshness().upper()} - {retry}", t.AMBER
    return {
        "locating": (app.tr("dash.gps_acquiring", s=app.locating_seconds()), t.CYAN),
        "denied": (app.tr("dash.gps_denied"), t.AMBER),
        "denied_forever": (app.tr("dash.gps_blocked"), t.AMBER),
        "off": (app.tr("dash.gps_off"), t.AMBER),
        "error": (app.tr("dash.gps_error"), t.AMBER),
    }.get(state, (app.tr("dash.gps_standby"), t.MUTED))


def _status_panel(app):
    net_dot, net_text = t.dot(t.GREEN), t.mono("", color=t.GREEN, weight=ft.FontWeight.BOLD)
    loc_text = t.mono("", size=12)
    radar_text = t.mono("", size=11, weight=ft.FontWeight.BOLD)
    radar_icon = ft.Icon(ft.Icons.SHIELD, size=18)

    def refresh():
        online = app.online is not False
        net_dot.bgcolor = t.GREEN if online else t.RED
        net_text.value, net_text.color = ((app.tr("dash.network_ready"), t.GREEN) if online
                                           else (app.tr("dash.offline"), t.RED))
        loc_text.value, loc_text.color = _location_line(app)
        ready = online and app.backend.session is not None
        radar_text.value = app.tr("dash.radar_active") if ready else app.tr("dash.radar_offline")
        radar_text.color = radar_icon.color = t.TEAL if ready else t.AMBER

    refresh()
    app.on_status_change(refresh)
    where = app.barangay.upper() if app.barangay else app.tr("dash.location")

    return t.panel(ft.Column([
        ft.Row([net_dot, net_text, t.mono("//", color=t.DIM), t.dot(t.CYAN, 6),
                t.mono(app.tr("dash.live"), color=t.CYAN, weight=ft.FontWeight.BOLD)], spacing=8),
        t.label(app.tr("dash.sector", where=where)[:32], size=22, weight=ft.FontWeight.W_800),
        ft.Row([ft.Icon(ft.Icons.LOCATION_ON, color=t.CYAN, size=16), ft.Container(loc_text, expand=True)],
               spacing=6),
        ft.Container(
            ft.Row([radar_icon, ft.Container(radar_text, expand=True)], spacing=8),
            padding=t.pad(12, 8), border_radius=ft.BorderRadius.all(24), border=ft.Border.all(1, t.BORDER),
        ),
    ], spacing=8), padding=16, border_color=t.alpha(t.CYAN, 0.45), bgcolor=t.BG_TOP,
        on_click=lambda e: app.page.run_task(app.locate, True))


def _services_header(app):
    return ft.Row([
        ft.Icon(ft.Icons.SENSORS, color=t.CYAN, size=20),
        t.label(app.tr("dash.services"), size=15, weight=ft.FontWeight.W_800),
        t.mono(app.tr("dash.quick_act"), size=10, color=t.MUTED),
    ], spacing=8)


def _service_card(app, cat):
    color = t.accent(cat.color)
    return t.accent_card(
        ft.Row([
            ft.Container(ft.Icon(getattr(ft.Icons, cat.icon), color=color, size=30), width=56, height=56,
                         alignment=ft.Alignment.CENTER, border_radius=ft.BorderRadius.all(14),
                         bgcolor=t.alpha(color, 0.14), border=ft.Border.all(1, t.alpha(color, 0.5))),
            ft.Column([
                ft.Row([ft.Container(t.label(cat.label, size=16), expand=True), t.pill(cat.badge, color)], spacing=6),
                ft.Text(cat.subtitle, size=12, color=t.MUTED, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            ], spacing=4, expand=True),
            ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.MUTED),
        ], spacing=12),
        color, on_click=lambda e: sos.start(app, category_key=cat.key),
    )


def _my_reports_link(app):
    return t.panel(ft.Row([
        ft.Icon(ft.Icons.HISTORY, color=t.CYAN, size=18),
        t.label(app.tr("dash.my_reports"), size=14, expand=True),
        ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.MUTED),
    ], spacing=10), padding=12, on_click=lambda e: app.page.run_task(sos.show_my_reports, app))


def _telemetry(app):
    text = t.mono("", size=10)
    dots = ft.Row([t.dot(t.CYAN, 6), t.dot(t.CYAN, 6), t.dot(t.DIM, 6)], spacing=4)

    def refresh():
        live = getattr(app, "announcements", None) and app.announcements.realtime_ok
        text.value = app.tr("dash.link_live") if live else app.tr("dash.link_polling")
        text.color = t.TEAL if live else t.MUTED

    refresh()
    app.on_status_change(refresh)
    return t.panel(ft.Row([ft.Icon(ft.Icons.SATELLITE_ALT, color=t.CYAN, size=16), ft.Container(text, expand=True),
                           dots], spacing=8), padding=t.pad(12, 8), radius=12)
