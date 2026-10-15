"""A small in-memory stand-in for the Supabase client, enough for the delivery and recovery tests.

It implements the PostgREST calls the app uses (filters, conditional updates, upsert with a
conflict key) with real semantics, so tests exercise claim/dedupe logic instead of mocks.
"""
from types import SimpleNamespace
from unittest import mock


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.filters, self.op, self.payload, self.opts = [], "select", None, {}
        self._limit = None
        self._order = None
        self.not_ = self

    # ---- operations
    def select(self, *_):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        self.op, self.payload = "upsert", payload
        self.opts = {"keys": [k.strip() for k in (on_conflict or "id").split(",")], "ignore": ignore_duplicates}
        return self

    def delete(self):
        self.op = "delete"
        return self

    # ---- filters
    def _add(self, fn):
        self.filters.append(fn)
        return self

    def eq(self, col, value):
        return self._add(lambda r: r.get(col) == value)

    def gt(self, col, value):
        return self._add(lambda r: r.get(col) is not None and r.get(col) > value)

    def lt(self, col, value):
        return self._add(lambda r: r.get(col) is not None and r.get(col) < value)

    def lte(self, col, value):
        return self._add(lambda r: r.get(col) is not None and r.get(col) <= value)

    def in_(self, col, values):
        return self._add(lambda r: r.get(col) in values)

    def is_(self, col, value):
        return self._add(lambda r: r.get(col) is None) if value == "null" else self.eq(col, value)

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    # ---- run
    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        match = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "select":
            if self._order:
                match = sorted(match, key=lambda r: (r.get(self._order[0]) is None, r.get(self._order[0])), reverse=self._order[1])
            if self._limit is not None:
                match = match[:self._limit]
            return SimpleNamespace(data=[dict(r) for r in match])
        if self.op == "update":
            for r in match:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in match])
        if self.op == "delete":
            for r in match:
                rows.remove(r)
            return SimpleNamespace(data=[dict(r) for r in match])
        items = self.payload if isinstance(self.payload, list) else [self.payload]
        out = []
        for item in items:
            existing = None
            if self.op == "upsert":
                existing = next((r for r in rows if all(r.get(k) == item.get(k) for k in self.opts["keys"])), None)
            if existing is not None:
                if not self.opts["ignore"]:
                    existing.update(item)
                    out.append(dict(existing))
                continue
            row = {**self.db.defaults.get(self.name, {}), **item}
            row.setdefault("id", self.db.next_id())
            rows.append(row)
            out.append(dict(row))
        return SimpleNamespace(data=out)


class FakeDB:
    def __init__(self, **tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.defaults = {
            "notification_deliveries": {"status": "pending", "attempts": 0, "next_attempt_at": "2000-01-01T00:00:00+00:00",
                                        "last_error": None, "provider_id": None, "sent_at": None, "claimed_at": None},
            "notification_events": {"status": "pending", "claimed_at": None},
            "push_subscriptions": {"created_at": "2026-10-04T00:00:00+00:00", "last_success_at": None},
            "mfa_recovery_codes": {"used_at": None, "created_at": "2026-10-04T00:00:00+00:00"},
            "mfa_reset_requests": {"status": "pending", "attempts": 0, "expires_at": "2999-01-01T00:00:00+00:00",
                                   "created_at": "2026-10-04T00:00:00+00:00"},
        }
        self._id = 100
        self.auth = mock.MagicMock()
        self.rpc = mock.MagicMock()

    def next_id(self):
        self._id += 1
        return self._id

    def table(self, name):
        return Query(self, name)
