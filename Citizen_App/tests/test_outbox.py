"""Offline SOS outbox: a report stays on the phone until the database confirms it. No network."""
import asyncio
from datetime import datetime, timezone

import pytest

from esrms.backend import BackendError
from esrms.outbox import Outbox, OutboxError

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
UID, OTHER = "u1", "u2"


def run(coro):
    return asyncio.run(coro)


class Store:
    """Stands in for SharedPreferences."""

    def __init__(self):
        self.text, self.broken = None, False

    async def read(self):
        return self.text

    async def write(self, text):
        if self.broken:
            return False
        self.text = text
        return True


def row(rid, user=UID):
    return {"client_request_id": rid, "reporter_id": user, "emergency_type": "General SOS",
            "latitude": 7.07, "longitude": 125.61}


def outbox(store=None):
    store = store or Store()
    return Outbox(store.read, store.write), store


def test_saved_report_survives_app_restart():
    box, store = outbox()
    run(box.add(row("a"), saved_at=NOW, fixed_at=NOW))
    reopened = Outbox(store.read, store.write)
    run(reopened.load())
    assert [e["id"] for e in reopened.pending(UID)] == ["a"]
    assert reopened.get("a")["row"]["latitude"] == 7.07


def test_failed_save_is_reported_not_hidden():
    box, store = outbox()
    store.broken = True
    with pytest.raises(OutboxError):
        run(box.add(row("a"), saved_at=NOW))
    assert box.entries == []


def test_report_stays_pending_while_offline():
    box, _ = outbox()
    run(box.add(row("a"), saved_at=NOW))

    async def submit(r):
        raise BackendError("No connection", "network")

    events, error = run(box.flush(submit, UID))
    assert events == [] and error.kind == "network"
    entry = box.get("a")
    assert entry["state"] == "pending" and entry["attempts"] == 1 and entry["last_error"] == "No connection"


def test_report_leaves_outbox_only_after_database_confirms():
    box, store = outbox()
    run(box.add(row("a"), saved_at=NOW))
    run(box.add(row("b"), saved_at=NOW))
    sent = []

    async def submit(r):
        sent.append(r["client_request_id"])
        return {"incident_number": 7, "client_request_id": r["client_request_id"]}

    events, error = run(box.flush(submit, UID))
    assert error is None and sent == ["a", "b"]
    assert [(k, e["id"], s["incident_number"]) for k, e, s in events] == [("sent", "a", 7), ("sent", "b", 7)]
    assert box.entries == [] and store.text == "[]"


def test_connection_lost_midway_keeps_the_rest():
    box, _ = outbox()
    for rid in ("a", "b", "c"):
        run(box.add(row(rid), saved_at=NOW))

    async def submit(r):
        if r["client_request_id"] == "b":
            raise BackendError("timeout", "network")
        return {"incident_number": 1}

    events, error = run(box.flush(submit, UID))
    assert [e["id"] for _, e, _ in events] == ["a"] and error.kind == "network"
    assert [e["id"] for e in box.pending(UID)] == ["b", "c"]


def test_refused_report_is_kept_and_not_retried():
    box, _ = outbox()
    run(box.add(row("a"), saved_at=NOW))
    calls = []

    async def submit(r):
        calls.append(1)
        raise BackendError("bad row", "validation")

    events, error = run(box.flush(submit, UID))
    assert error is None and events[0][0] == "rejected"
    assert box.get("a")["state"] == "rejected" and box.pending(UID) == []
    run(box.flush(submit, UID))
    assert len(calls) == 1


def test_rate_limit_and_expired_session_are_retried_later():
    for kind in ("rate_limited", "duplicate", "auth", "server"):
        box, _ = outbox()
        run(box.add(row("a"), saved_at=NOW))

        async def submit(r, kind=kind):
            raise BackendError("later", kind)

        _, error = run(box.flush(submit, UID))
        assert error.kind == kind and box.get("a")["state"] == "pending"


def test_one_citizens_report_is_never_sent_as_another():
    box, _ = outbox()
    run(box.add(row("mine"), saved_at=NOW))
    run(box.add(row("theirs", OTHER), saved_at=NOW))
    sent = []

    async def submit(r):
        sent.append(r["client_request_id"])
        return {"incident_number": 1}

    run(box.flush(submit, UID))
    assert sent == ["mine"]
    assert [e["id"] for e in box.entries] == ["theirs"] and box.pending(None) == []


def test_same_report_saved_twice_is_one_entry():
    box, _ = outbox()
    run(box.add(row("a"), saved_at=NOW))
    run(box.add(row("a"), saved_at=NOW))
    assert len(box.entries) == 1
