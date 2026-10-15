"""RADAR tab: the Safety Radar, a public-awareness view of ESRMS high-risk areas.

Shows only the generalized zones ESRMS published for citizens (public_risk_areas). It is not a live
incident tracker: no incident, responder, organization or other citizen is ever shown, and the
citizen's own position is used on the phone only, to measure distance to the published zones.
"""
import asyncio
import json
import logging
import math
import time

import flet as ft

from esrms import radar
from esrms.backend import BackendError
from esrms.ui import theme as t

log = logging.getLogger(__name__)

REFRESH_S = 300          # how often the published copy is re-checked while the tab is open
SWEEP_PERIOD = 6         # seconds per sweep, same as the logo
RADAR_SIZE = 320


def level_color(level):
    return {radar.HIGH: t.RED, radar.MODERATE: t.AMBER, radar.LOW: t.GREEN}.get(level, t.MUTED)


class RadarController:
    def __init__(self, app):
        self.app = app
        self.feed, self.zones = None, []      # what ESRMS published (None = nothing loaded yet)
        self.fetched_at = 0.0                 # monotonic time of the last successful check
        self.error = None                     # text shown when the copy can't be loaded
        self.loading = False
        self.general = False                  # citizen chose the general map instead of their location
        self.holder = ft.Container(expand=True)
        self._signature = None
        self._restored = False

    # ------------------------------------------------------------ data

    async def restore(self):
        """Last published copy seen on this phone, so the tab opens offline. Public data only."""
        if self._restored:
            return
        self._restored = True
        try:
            cached = json.loads(await self.app.pref("radar_cache") or "null")
        except ValueError:
            cached = None
        if cached and self.feed is None:
            self.feed, self.zones = cached.get("feed"), radar.parse(cached.get("areas"))

    async def refresh(self, force=False):
        """Fetch the published copy when it is due. Returns True if what is shown may have changed."""
        if self.loading or not self.app.backend.session:
            return False
        if not force and self.fetched_at and time.monotonic() - self.fetched_at < REFRESH_S:
            return False
        self.loading = True
        try:
            if self.feed and not force:   # cheap check first: one tiny row
                version = (await self.app.backend.public_risk_feed() or {}).get("version")
                if version == self.feed.get("version"):
                    self.fetched_at, self.error = time.monotonic(), None
                    return True   # same data, but "updated ... ago" moves on
            data = await self.app.backend.public_risk()
            self.feed, self.zones = data["feed"], radar.parse(data["areas"])
            self.fetched_at, self.error = time.monotonic(), None
            self.app.online = True
            await self.app.set_pref("radar_cache", json.dumps(data))
        except BackendError as exc:
            self.app.online = exc.kind != "network"
            self.error = {"network": "No connection. Showing the last risk areas saved on this phone."
                          if self.feed else "No connection. Risk areas could not be loaded.",
                          "not_configured": str(exc)}.get(exc.kind, f"Could not load risk areas: {exc}")
        finally:
            self.loading = False
        return True

    async def tick(self):
        """Called by the app's heartbeat while this tab is open."""
        if await self.refresh():
            self.render(force=True)

    # ------------------------------------------------------------ view state

    def _center(self):
        """((lat, lng), located): the citizen's position if known and wanted, else the general map centre."""
        loc = self.app.location
        if loc and not self.general:
            return (loc.latitude, loc.longitude), True
        return radar.general_center(self.zones), False

    def build(self):
        self.app.on_status_change(self.render)
        self._signature = None
        self.render(force=True)
        self.app.page.run_task(self._open)
        return self.holder

    async def _open(self):
        await self.restore()
        self.render(force=True)
        if self.app.location_state in ("ok", "unknown") and not (self.app.location and self.app.location.is_current()):
            self.app.page.run_task(self.app.locate, False)   # no permission dialog: never forced on the citizen
        if await self.refresh():
            self.render(force=True)

    def render(self, force=False):
        """Redraw only when something shown changed (the app pings listeners every second while locating)."""
        loc = self.app.location
        signature = (self.app.location_state, round(loc.latitude, 3) if loc else None,
                     round(loc.longitude, 3) if loc else None, self.general, self.loading,
                     self.error, (self.feed or {}).get("version"), t.name)
        if not force and signature == self._signature:
            return
        self._signature = signature
        self.holder.content = self._content()
        try:
            self.holder.update()
        except Exception:
            pass   # not on screen yet

    # ------------------------------------------------------------ drawing

    def _canvas(self, placed, reach, located):
        size, half = RADAR_SIZE, RADAR_SIZE / 2
        scale = half / reach
        layers = [ft.Container(width=size, height=size, border_radius=ft.BorderRadius.all(size),
                               bgcolor=t.BG_TOP, border=ft.Border.all(1.5, t.alpha(t.CYAN, 0.6)))]
        for fraction in (0.25, 0.5, 0.75):
            d = size * fraction
            layers.append(ft.Container(width=d, height=d, left=half - d / 2, top=half - d / 2,
                                       border_radius=ft.BorderRadius.all(d),
                                       border=ft.Border.all(1, t.alpha(t.CYAN, 0.28))))
        layers += [ft.Container(width=1, height=size, left=half, top=0, bgcolor=t.alpha(t.CYAN, 0.18)),
                   ft.Container(width=size, height=1, left=0, top=half, bgcolor=t.alpha(t.CYAN, 0.18))]
        layers.append(self._sweep(size))   # decorative only: it reads and changes no data
        for zone in reversed(placed):   # nearest drawn last, on top
            if zone.distance_km - zone.radius_km > reach:
                continue
            color = level_color(zone.level)
            r = max(11.0, zone.radius_km * scale)
            # Zones partly beyond the rim are pulled in so their marker stays tappable.
            pull = min(1.0, (half - 12) / max(1e-6, zone.distance_km * scale))
            x, y = half + zone.east_km * scale * pull, half - zone.north_km * scale * pull
            layers.append(ft.Container(
                width=2 * r, height=2 * r, left=x - r, top=y - r, border_radius=ft.BorderRadius.all(2 * r),
                bgcolor=t.alpha(color, 0.30), border=ft.Border.all(2, color), tooltip=radar.AREA_TITLE[zone.level],
                on_click=lambda e, z=zone: self.show_zone(z, located)))
        marker = t.CYAN if located else t.MUTED
        layers += [
            ft.Container(width=14, height=14, left=half - 7, top=half - 7, bgcolor=marker,
                         border_radius=ft.BorderRadius.all(14), border=ft.Border.all(2, t.TEXT)),
            ft.Container(t.mono("YOU" if located else "MAP CENTRE", size=9, color=marker, weight=ft.FontWeight.BOLD),
                         left=half - 40, top=half + 9, width=80, alignment=ft.Alignment.CENTER),
            # distance labels on the rings
            *[ft.Container(t.mono(f"{reach * f:g} km", size=9, color=t.MUTED), left=half + 4,
                           top=half - size * f / 2 + 3) for f in (0.5, 1)],
        ]
        return ft.Container(ft.Stack(layers, width=size, height=size), alignment=ft.Alignment.CENTER)

    def _sweep(self, size):
        sweep = ft.Container(
            width=size, height=size, border_radius=ft.BorderRadius.all(size),
            gradient=ft.SweepGradient(colors=["#00000000", "#00000000", t.alpha(t.CYAN, 0.28)], stops=[0.0, 0.8, 1.0]),
            rotate=ft.Rotate(0), animate_rotation=ft.Animation(SWEEP_PERIOD * 1000, ft.AnimationCurve.LINEAR))

        async def spin():
            turns = 0
            await asyncio.sleep(0.3)
            while True:
                turns += 1
                sweep.rotate = ft.Rotate(turns * 2 * math.pi)
                try:
                    sweep.update()
                except Exception:
                    return   # this radar was redrawn or the tab was left
                await asyncio.sleep(SWEEP_PERIOD)

        self.app.page.run_task(spin)
        return sweep

    def _legend(self):
        def item(level, text):
            return ft.Row([t.dot(level_color(level), 10), t.mono(text, size=10, color=t.TEXT)], spacing=6, tight=True)
        return ft.Row([item(radar.LOW, "LOW"), item(radar.MODERATE, "MODERATE"), item(radar.HIGH, "HIGH")],
                      alignment=ft.MainAxisAlignment.CENTER, spacing=18)

    def _status_card(self, placed, located, sufficient):
        state = self.app.location_state
        if not located:
            finding = state == "locating" and not self.general
            title = "Finding your area..." if finding else "Location unavailable"
            text = ("Getting your position on this phone to compare it with the published risk areas."
                    if finding else
                    "Showing the general risk map. Your nearby status needs your location, which is used on "
                    "this phone only and is not sent or shared for this.")
            buttons = [] if finding else [
                t.primary_button("ENABLE LOCATION", lambda e: self._enable_location(), icon="MY_LOCATION"),
                *([] if self.general or not placed else [
                    t.ghost_button("VIEW GENERAL RISK MAP", lambda e: self._set_general(True), icon="MAP")]),
            ]
            return t.panel(ft.Column([
                t.mono("NEARBY SAFETY STATUS", size=10, color=t.CYAN, weight=ft.FontWeight.BOLD),
                ft.Row([ft.Icon(ft.Icons.LOCATION_SEARCHING if finding else ft.Icons.LOCATION_OFF,
                                color=t.CYAN if finding else t.AMBER, size=20), t.label(title, size=16)], spacing=8),
                ft.Text(text, size=13, color=t.MUTED), *buttons,
            ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=16)
        level = radar.status(placed, sufficient)
        color = level_color(level)
        close = radar.nearby(placed)
        return t.panel(ft.Column([
            t.mono("NEARBY SAFETY STATUS", size=10, color=t.CYAN, weight=ft.FontWeight.BOLD),
            t.mono("YOUR AREA", size=11, color=t.MUTED),
            ft.Row([t.dot(color, 14), t.label(radar.LEVEL_TITLE[level], size=20, color=color,
                                              weight=ft.FontWeight.W_800)], spacing=10),
            ft.Text(radar.NEARBY_TEXT[level], size=14, color=t.TEXT),
            ft.Text("Based on historical emergency activity.", size=12, color=t.MUTED),
            *([t.ghost_button("VIEW DETAILS", lambda e: self.show_zone(
                max(close, key=lambda z: radar.RANK[z.level]), True), icon="INFO_OUTLINE")] if close else []),
            *([ft.TextButton(content=ft.Text("View general risk map", color=t.CYAN, size=12),
                             on_click=lambda e: self._set_general(True))] if placed else []),
        ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=16,
            border_color=t.alpha(color, 0.6))

    def _zone_row(self, zone, located):
        color = level_color(zone.level)
        return t.accent_card(ft.Row([
            ft.Column([
                ft.Row([t.pill(radar.AREA_TITLE[zone.level], color)], spacing=6),
                t.label(radar.zone_name(zone, located), size=15),
                *([ft.Text(radar.distance_text(zone), size=12, color=t.MUTED)] if located else []),
            ], spacing=4, expand=True),
            ft.Icon(ft.Icons.CHEVRON_RIGHT, color=t.MUTED),
        ], spacing=10), color, on_click=lambda e: self.show_zone(zone, located))

    def _content(self):
        app = self.app
        center, located = self._center()
        sufficient = bool((self.feed or {}).get("sufficient"))
        placed = radar.place(list(self.zones), center) if center else []
        reach = radar.range_km(placed, radar.MAX_RANGE_KM if located else None)
        refresh = ft.IconButton(ft.Icons.REFRESH, icon_color=t.CYAN, tooltip="Refresh",
                                on_click=lambda e: app.page.run_task(self._manual_refresh))
        updated = ("Loading risk areas..." if self.loading and not self.feed else
                   radar.updated_text((self.feed or {}).get("published_at")))
        notes = []
        if self.error:
            notes.append(t.panel(ft.Text(self.error, color=t.AMBER, size=13), border_color=t.alpha(t.AMBER, 0.6)))
        if self.feed and not sufficient:
            notes.append(t.panel(ft.Text(radar.NEARBY_TEXT[radar.UNKNOWN], color=t.MUTED, size=13)))
        far = [z for z in placed if located and z.distance_km - z.radius_km > reach]
        controls = [
            ft.Row([ft.Icon(ft.Icons.RADAR, color=t.CYAN, size=26),
                    ft.Column([t.label("SAFETY RADAR", size=20, weight=ft.FontWeight.W_800),
                               ft.Text("Stay aware of areas with elevated emergency activity.", size=12,
                                       color=t.MUTED)], spacing=0, expand=True), refresh], spacing=10),
            ft.Row([t.mono(updated.upper(), size=10), ft.Container(expand=True),
                    t.mono("GENERAL MAP" if not located else "YOUR AREA", size=10, color=t.CYAN)]),
            *notes,
            self._canvas(placed, reach, located),
            self._legend(),
            *([ft.Row([ft.TextButton(content=ft.Text("Back to my area", color=t.CYAN, size=12),
                                     on_click=lambda e: self._set_general(False))],
                      alignment=ft.MainAxisAlignment.CENTER)] if self.general and app.location else []),
            self._status_card(placed, located, sufficient),
            *([t.mono("RISK AREAS", size=10, color=t.CYAN, weight=ft.FontWeight.BOLD),
               *[self._zone_row(z, located) for z in placed[:12]]] if placed else []),
            *([ft.Text(f"{len(far)} more published area{'' if len(far) == 1 else 's'} farther than {reach:g} km "
                       "are in the list but not drawn.", size=12, color=t.MUTED)] if far else []),
            t.panel(ft.Column([
                t.mono("SAFETY AWARENESS", size=10, color=t.CYAN, weight=ft.FontWeight.BOLD),
                ft.Row([ft.Icon(ft.Icons.WARNING_AMBER, color=t.AMBER, size=20), t.label("STAY ALERT", size=15)],
                       spacing=8),
                ft.Text("This radar shows areas with elevated historical emergency activity. It does not indicate "
                        "an active emergency, and a high-risk area does not mean an emergency is happening now.",
                        size=13, color=t.TEXT),
                ft.Text("For active emergencies, follow official ESRMS announcements and emergency instructions.",
                        size=13, color=t.MUTED),
                t.ghost_button("Open announcements", lambda e: app.switch_tab("announcement"), icon="CAMPAIGN"),
            ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=16),
        ]
        return ft.Container(ft.Column(controls, spacing=14, scroll=ft.ScrollMode.AUTO,
                                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                            bgcolor=t.BG, padding=t.pad(16, 14), expand=True)

    # ------------------------------------------------------------ actions

    def show_zone(self, zone, located):
        """Public information card for one published zone: level, why, reminder, rounded distance, date."""
        app, color = self.app, level_color(zone.level)
        days = (self.feed or {}).get("period_days")
        basis = f"Based on emergency reports from the last {days} days." if days else "Based on historical emergency activity."
        app.page.show_dialog(ft.AlertDialog(
            bgcolor=t.PANEL, title=t.mono(radar.AREA_TITLE[zone.level], size=12, color=color, weight=ft.FontWeight.BOLD),
            scrollable=True,
            content=ft.Column([
                t.label(radar.zone_name(zone, located), size=19, weight=ft.FontWeight.W_800),
                ft.Row([t.dot(color, 12), t.label(zone.level.upper(), size=15, color=color)], spacing=8),
                *([ft.Text(radar.distance_text(zone), size=13, color=t.TEXT)] if located else []),
                ft.Divider(color=t.BORDER),
                t.label("Why am I seeing this?", size=14),
                ft.Text(f"{zone.summary} {basis}", size=13, color=t.MUTED),
                t.label("Safety reminder", size=14),
                ft.Text("Stay alert and follow official emergency announcements. This is not a report of an "
                        "emergency happening now.", size=13, color=t.MUTED),
                t.mono("UPDATED", size=10, color=t.CYAN),
                ft.Text(radar.updated_date(zone.updated_at or (self.feed or {}).get("published_at")), size=13,
                        color=t.TEXT),
            ], tight=True, spacing=10, width=360),
            actions=[ft.TextButton(content=ft.Text("Close", color=t.CYAN), on_click=lambda e: app.page.pop_dialog())]))

    def _enable_location(self):
        self.general = False
        self.app.page.run_task(self.app.locate, True)   # asks permission only because the citizen pressed this

    def _set_general(self, value):
        self.general = value
        self.render()

    async def _manual_refresh(self):
        self.loading = False
        self.render()
        if await self.refresh(force=True):
            self.render(force=True)
