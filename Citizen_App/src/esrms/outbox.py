"""SOS reports written on this phone that the ESRMS database has not confirmed yet.

An entry stays here until submit_incident() returns the stored row, so "saved on this phone"
and "received by ESRMS" can never be confused. Nothing in this module talks to a satellite or
any other channel: delivery is only the normal HTTPS insert, retried when a connection exists.

Storage is one JSON string behind two async callables (SharedPreferences in the app, a dict in
tests/test_outbox.py). Every write is read back before it counts as saved.
"""
import json
import logging
from datetime import datetime, timezone

from esrms.backend import BackendError

log = logging.getLogger(__name__)

# The server answered and refused this row; sending the same row again cannot succeed.
PERMANENT = ("validation", "schema")


class OutboxError(Exception):
    pass


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_time(text):
    return datetime.fromisoformat(text) if text else None


class Outbox:
    def __init__(self, read, write):
        self._read, self._write = read, write
        self.entries = []
        self._flushing = False

    async def load(self):
        try:
            data = json.loads(await self._read() or "[]")
            self.entries = [e for e in data if isinstance(e, dict) and e.get("id") and isinstance(e.get("row"), dict)]
        except Exception:
            log.error("Could not read the saved SOS reports", exc_info=True)

    async def _save(self):
        text = json.dumps(self.entries)
        try:
            ok = await self._write(text)
            stored = await self._read()
        except Exception as exc:
            raise OutboxError("The phone could not save the report.") from exc
        if ok is False or stored != text:
            raise OutboxError("The phone could not save the report.")

    async def add(self, row, *, saved_at, fixed_at=None, cached=False, place=""):
        """Keep a report on the phone. Raises OutboxError if it could not be written and read back."""
        entry = {
            "id": row["client_request_id"], "row": row, "state": "pending",
            "saved_at": _iso(saved_at), "fixed_at": _iso(fixed_at), "cached": bool(cached), "place": place or "",
            "attempts": 0, "last_attempt_at": None, "last_error": None,
        }
        before = list(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry["id"]] + [entry]
        try:
            await self._save()
        except OutboxError:
            self.entries = before
            raise
        return entry

    async def remove(self, entry_id):
        before = list(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        try:
            await self._save()
        except OutboxError:
            self.entries = before
            raise

    def get(self, entry_id):
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def for_user(self, user_id):
        """Only this account's reports: one citizen's SOS is never sent or shown under another account."""
        return [e for e in self.entries if user_id and e["row"].get("reporter_id") == user_id]

    def pending(self, user_id):
        return [e for e in self.for_user(user_id) if e["state"] == "pending"]

    async def flush(self, submit, user_id):
        """Try to deliver this user's pending reports, oldest first.

        Returns (events, error). events: ("sent", entry, stored_row) once the database confirmed it,
        or ("rejected", entry, None) when the server refused the row for good (kept, never retried).
        error is the BackendError that stopped the run (no connection, expired session, rate limit);
        the remaining reports stay pending for the next run.
        """
        if self._flushing:
            return [], None
        self._flushing = True
        events, error = [], None
        try:
            for entry in self.pending(user_id):
                entry["attempts"] += 1
                entry["last_attempt_at"] = _iso(datetime.now(timezone.utc))
                try:
                    stored = await submit(entry["row"])
                except BackendError as exc:
                    entry["last_error"] = str(exc)
                    if exc.kind in PERMANENT:
                        entry["state"] = "rejected"
                        events.append(("rejected", entry, None))
                        continue
                    error = exc
                    break
                # Confirmed by the database. If this save fails the entry is re-sent later and the
                # client_request_id unique index returns the same stored report, not a second one.
                self.entries.remove(entry)
                events.append(("sent", entry, stored))
            try:
                await self._save()
            except OutboxError:
                log.error("Could not update the saved SOS reports", exc_info=True)
        finally:
            self._flushing = False
        return events, error
