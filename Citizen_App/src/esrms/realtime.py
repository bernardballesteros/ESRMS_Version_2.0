"""Supabase Realtime listener for public.announcement_feed (the content-free change counter).

When the dispatcher publishes, edits or archives an announcement, the counter row changes and
on_change() is called; the app then refetches through citizen_announcements(). The socket only
ever sees the counter, never announcement content. If the socket can't connect, the
announcement screen's polling fallback keeps working.
"""
import asyncio
import json
import logging
from urllib.parse import urlparse

import websockets

log = logging.getLogger(__name__)
HEARTBEAT_S = 25
TOPIC = "realtime:esrms-citizen-announcements"


def socket_url(supabase_url, anon_key):
    host = urlparse(supabase_url).netloc
    return f"wss://{host}/realtime/v1/websocket?apikey={anon_key}&vsn=1.0.0"


def join_message(anon_key, ref="1"):
    return {
        "topic": TOPIC,
        "event": "phx_join",
        "payload": {
            "config": {
                "broadcast": {"self": False},
                "presence": {"key": ""},
                "postgres_changes": [{"event": "*", "schema": "public", "table": "announcement_feed"}],
            },
            "access_token": anon_key,
        },
        "ref": ref,
        "join_ref": ref,
    }


class AnnouncementListener:
    def __init__(self, config, on_change, on_status=None):
        self.config = config
        self.on_change = on_change          # async callable
        self.on_status = on_status or (lambda connected: None)
        self._task = None
        self._stop = False

    def start(self):
        if not self._task:
            self._stop = False
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        delay = 2
        while not self._stop:
            try:
                await self._session()
                delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network drops, DNS, server restarts
                log.info("Realtime disconnected: %s", exc)
            self.on_status(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self):
        url = socket_url(self.config.supabase_url, self.config.supabase_anon_key)
        async with websockets.connect(url, open_timeout=15, ping_interval=None) as ws:
            await ws.send(json.dumps(join_message(self.config.supabase_anon_key)))
            beat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    event = msg.get("event")
                    if event == "phx_reply" and msg.get("ref") == "1":
                        ok = (msg.get("payload") or {}).get("status") == "ok"
                        self.on_status(ok)
                        if ok:
                            await self.on_change()  # catch up on anything missed while offline
                    elif event == "postgres_changes":
                        await self.on_change()
                    elif event in ("phx_error", "phx_close"):
                        raise ConnectionError(f"channel {event}")
            finally:
                beat.cancel()

    async def _heartbeat(self, ws):
        ref = 100
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            ref += 1
            await ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": str(ref)}))
