"""ANNOUNCEMENT tab: official announcements from the shared ESRMS database."""
import asyncio
import logging
from datetime import timedelta, timezone

import flet as ft

from esrms import announcements as ann
from esrms.backend import BackendError
from esrms.realtime import AnnouncementListener
from esrms.ui import theme as t

log = logging.getLogger(__name__)
MANILA = timezone(timedelta(hours=8))
POLL_S = 30          # feed-counter check when realtime is down
FULL_REFRESH_S = 120  # scheduled posts go live by the clock, which doesn't move the counter
AUDIENCE = {"all_citizens": "All citizens", "barangay": "Barangay"}
def _priority_color(prio):
    return {"critical": t.RED, "high": t.AMBER, "medium": t.CYAN}.get(prio, t.MUTED)


class AnnouncementsController:
    def __init__(self, app):
        self.app = app
        self.rows = []
        self.error = None
        self.loaded = False
        self.loading = False
        self.realtime_ok = False
        self._known_ids = None
        self._feed_version = None
        self._listener = AnnouncementListener(app.config, self._on_feed_change, self._on_realtime_status)
        self._poll_task = None
        self.list = ft.Column(spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.status = t.mono("", size=10)

    def start(self):
        self._listener.start()
        self._poll_task = asyncio.get_running_loop().create_task(self._poll())
        self.app.page.run_task(self.refresh)

    async def stop(self):
        await self._listener.stop()
        if self._poll_task:
            self._poll_task.cancel()

    # ------------------------------------------------------------ data

    async def _on_feed_change(self):
        await self.refresh()

    def _on_realtime_status(self, ok):
        if ok != self.realtime_ok:
            self.realtime_ok = ok
            self._render_status()
            self.app.notify_status()

    async def _poll(self):
        elapsed = 0
        while True:
            await asyncio.sleep(POLL_S)
            elapsed += POLL_S
            try:
                if elapsed >= FULL_REFRESH_S:
                    elapsed = 0
                    await self.refresh()
                elif not self.realtime_ok:
                    version = await self.app.backend.announcement_feed_version()
                    if version != self._feed_version:
                        self._feed_version = version
                        await self.refresh()
                else:
                    self._render()  # drop posts that expired while the screen is open
                    self.app.page.update()
            except Exception:
                log.debug("announcement poll failed", exc_info=True)

    async def refresh(self, force=False):
        if self.loading and not force:
            return
        if not self.app.backend.session:
            return
        self.loading = True
        try:
            rows = await self.app.backend.announcements(self.app.barangay or None)
            self.error = None
            self.app.online = True
        except BackendError as exc:
            self.error = str(exc)
            if exc.kind == "network":
                self.app.online = False
            rows = None
        finally:
            self.loading = False
            self.loaded = True
        if rows is not None:
            self._detect_new(rows)
            self.rows = rows
        self._render()
        self.app.notify_status()

    def _detect_new(self, rows):
        ids = {r["id"] for r in rows}
        if self._known_ids is not None:
            fresh = [r for r in rows if r["id"] not in self._known_ids]
            urgent = [r for r in fresh if ann.is_urgent(r)]
            if fresh and self.app.tab != "announcement":
                self.app.unseen_urgent += len(urgent) or 0
                top = ann.sort_rows(fresh)[0]
                label = ann.style(top)[0]
                self.app.toast(f"New {label} announcement: {top['title']}",
                               t.alpha(t.RED, 0.9) if ann.is_urgent(top) else None)
        self._known_ids = ids

    # ------------------------------------------------------------ view

    def build(self):
        self._render()
        self._render_status()
        brgy = self.app.barangay or "not set"
        return ft.Container(ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.CAMPAIGN, color=t.CYAN, size=26),
                ft.Column([t.mono("ESRMS NET", size=11, color=t.CYAN, weight=ft.FontWeight.BOLD),
                           t.label("ANNOUNCEMENTS", size=20, weight=ft.FontWeight.W_800)], spacing=0, expand=True),
                ft.IconButton(ft.Icons.REFRESH, icon_color=t.CYAN, tooltip="Refresh",
                              on_click=lambda e: self.app.page.run_task(self.refresh, True)),
            ]),
            ft.Row([self.status, ft.Container(expand=True), t.mono(f"BARANGAY: {brgy.upper()}"[:30], size=10)]),
            self.list,
        ], spacing=12, scroll=ft.ScrollMode.AUTO, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            bgcolor=t.BG, padding=t.pad(16, 14), expand=True)

    def _render_status(self):
        self.status.value = "● LIVE UPDATES" if self.realtime_ok else "○ AUTO-REFRESH"
        self.status.color = t.TEAL if self.realtime_ok else t.MUTED

    def _render(self):
        rows = ann.visible(self.rows)
        items = []
        if self.error:
            items.append(t.panel(ft.Column([
                ft.Text(self.error, color=t.RED),
                ft.Text("Showing the last announcements loaded." if rows else "", size=12, color=t.MUTED),
            ], spacing=4), border_color=t.alpha(t.RED, 0.5)))
        if not self.loaded:
            items.append(ft.Container(ft.ProgressRing(color=t.CYAN), alignment=ft.Alignment.CENTER, padding=30))
        elif not rows and not self.error:
            items.append(t.panel(ft.Text("No active announcements for your area right now.", color=t.MUTED)))
        items += [_card(r) for r in rows]
        self.list.controls = items


def _card(r):
    label, color_key, icon = ann.style(r)
    color = t.accent(color_key)
    urgent = ann.is_urgent(r)
    published = ann.parse_ts(r.get("published_at"))
    expires = ann.parse_ts(r.get("expires_at"))
    when = published.astimezone(MANILA).strftime("%b %d, %Y  %I:%M %p") if published else ""
    audience = AUDIENCE.get(r.get("target_audience"), r.get("target_audience") or "")
    if r.get("target_audience") == "barangay" and r.get("barangay"):
        audience = f"Barangay {r['barangay']}"
    prio = r.get("priority") or "medium"
    return t.accent_card(
        ft.Column([
            ft.Row([
                t.pill(label, color, icon=icon),
                t.pill(prio.upper(), _priority_color(prio)),
                *([ft.Icon(ft.Icons.PUSH_PIN, color=t.CYAN, size=16)] if r.get("is_pinned") else []),
            ], spacing=6, wrap=True),
            t.label(r.get("title", ""), size=17, weight=ft.FontWeight.W_700),
            ft.Text(r.get("message", ""), color=t.TEXT, size=14, selectable=True),
            ft.Row([ft.Icon(ft.Icons.SCHEDULE, size=13, color=t.MUTED), t.mono(when, size=10)], spacing=4),
            ft.Row([ft.Icon(ft.Icons.GROUPS, size=13, color=t.MUTED), t.mono(audience.upper(), size=10)], spacing=4),
            *([t.mono("UNTIL " + expires.astimezone(MANILA).strftime("%b %d %I:%M %p"), size=10, color=t.DIM)]
              if expires else []),
        ], spacing=8),
        color,
        bgcolor=t.CRITICAL_BG if urgent else t.PANEL,   # tinted, opaque so the stripe gradient stays crisp
        border_color=t.alpha(color, 0.5) if urgent else t.BORDER,
    )
