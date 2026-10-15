"""In-memory stand-in for the PostgREST calls the Analytics and Hospital Signal APIs make.

Implements the filters with real semantics, including or() strings with quoted operands and
in.() lists, exact counts, range paging, conditional updates, identity columns and unique
indexes, so the tests exercise the queries rather than mocks.
"""
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from app.analytics import parse_ts

MAX_ROWS = 1000  # PostgREST's default page cap
ACTIVE = ("sent", "acknowledged", "accepted", "arrived", "handoff")


class RangeError(Exception):
    code = "PGRST103"


class UniqueViolation(Exception):
    code = "23505"


def _value(row, col):
    value = row.get(col)
    return parse_ts(value) if (col.endswith("_at") or col == "eta") and value else value


def _like(pattern, text, flags):
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out += re.escape(pattern[i + 1])
            i += 1
        elif ch in "*%":
            out += ".*"
        elif ch == "_":
            out += "."
        else:
            out += re.escape(ch)
        i += 1
    return text is not None and re.fullmatch(out, str(text), flags | re.S) is not None


def _or_conditions(text):
    """Split 'a.op.v,b.op."quoted, value",c.in.(x,y)' at top-level commas and unquote the operands."""
    parts, current, quoted, depth, i = [], "", False, 0, 0
    while i < len(text):
        ch = text[i]
        if quoted and ch == "\\":
            current += text[i + 1]
            i += 1
        elif ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted and depth == 0:
            parts.append(current)
            current = ""
        else:
            depth += (ch == "(") - (ch == ")") if not quoted else 0
            current += ch
        i += 1
    parts.append(current)
    return [p.split(".", 2) for p in parts]


class Query:
    def __init__(self, db, name):
        self.db, self.name, self.rows = db, name, db.tables.setdefault(name, [])
        self.filters, self.orders, self.window, self.count = [], [], None, None
        self.op, self.payload, self.opts, self.cap = "select", None, {}, None

    # ---- operations
    def select(self, _columns="*", count=None):
        self.count = count
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

    def gte(self, col, value):
        return self._add(lambda r: _value(r, col) is not None and _value(r, col) >= parse_ts(value))

    def lt(self, col, value):
        return self._add(lambda r: _value(r, col) is not None and _value(r, col) < parse_ts(value))

    def eq(self, col, value):
        return self._add(lambda r: r.get(col) == value)

    def in_(self, col, values):
        return self._add(lambda r: r.get(col) in values)

    def is_(self, col, _null):
        negate, self.negate = getattr(self, "negate", False), False
        return self._add(lambda r: (r.get(col) is not None) if negate else (r.get(col) is None))

    @property
    def not_(self):
        self.negate = True
        return self

    def like(self, col, pattern):
        return self._add(lambda r: _like(pattern, r.get(col), 0))

    def ilike(self, col, pattern):
        return self._add(lambda r: _like(pattern, r.get(col), re.I))

    def or_(self, text):
        conditions = _or_conditions(text.strip("()"))

        def match(row):
            for col, op, operand in conditions:
                if op == "ilike" and _like(operand, row.get(col), re.I):
                    return True
                if op == "eq" and str(row.get(col)) == operand:
                    return True
                if op == "in" and str(row.get(col)) in operand.strip("()").split(","):
                    return True
            return False
        return self._add(match)

    def order(self, col, desc=False):
        self.orders.append((col, desc))
        return self

    def range(self, first, last):
        self.window = (first, last)
        return self

    def limit(self, n):
        self.cap = n
        return self

    # ---- run
    def execute(self):
        self.db.queries += 1
        if self.db.fail:
            raise self.db.fail
        match = [r for r in self.rows if all(f(r) for f in self.filters)]
        if self.op == "select":
            for col, desc in reversed(self.orders):
                match.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            total = len(match)
            if self.window:
                first, last = self.window
                if first and first >= total:
                    raise RangeError("Requested range not satisfiable")
                match = match[first:last + 1]
            if self.cap is not None:
                match = match[:self.cap]
            return SimpleNamespace(data=[dict(r) for r in match[:MAX_ROWS]], count=total if self.count else None)
        if self.op == "update":
            # hospital_signals_active_bay_idx: one active report per bay.
            bay = self.payload.get("bay_id") if self.name == "hospital_signals" else None
            if bay and any(r.get("bay_id") == bay and r.get("status") in ACTIVE and r not in match for r in self.rows):
                raise UniqueViolation("duplicate key value violates unique constraint hospital_signals_active_bay_idx")
            for r in match:
                r.update(self.payload)
                r["updated_at"] = datetime.now(timezone.utc).isoformat()
            return SimpleNamespace(data=[dict(r) for r in match], count=None)
        if self.op == "delete":
            for r in match:
                self.rows.remove(r)
            return SimpleNamespace(data=[dict(r) for r in match], count=None)
        out = []
        for item in (self.payload if isinstance(self.payload, list) else [self.payload]):
            if self.op == "upsert":
                existing = next((r for r in self.rows if all(r.get(k) == item.get(k) for k in self.opts["keys"])), None)
                if existing is not None:
                    if not self.opts["ignore"]:
                        existing.update(item)
                        out.append(dict(existing))
                    continue
            for col in self.db.unique.get(self.name, ()):
                if item.get(col) is not None and any(r.get(col) == item[col] for r in self.rows):
                    raise UniqueViolation(f"duplicate key value violates unique constraint on {col}")
            now = datetime.now(timezone.utc).isoformat()
            row = {**self.db.defaults.get(self.name, {}), "created_at": now, "updated_at": now, **item}
            row.setdefault("id", str(uuid.uuid4()))
            for col in self.db.identity.get(self.name, ()):
                self.db.counter += 1
                row[col] = self.db.counter
            for col in self.db.stamps.get(self.name, ()):
                row.setdefault(col, now)
            self.rows.append(row)
            out.append(dict(row))
        return SimpleNamespace(data=out, count=None)


class FakeIncidentsDB:
    def __init__(self, incidents=(), events=(), **tables):
        self.tables = {"emergency_incidents": list(incidents), "incident_events": list(events),
                       **{k: list(v) for k, v in tables.items()}}
        self.queries = 0
        self.fail = None      # set to an exception to make every query fail
        self.counter = 0
        self.auth = mock.MagicMock()   # Supabase Auth admin API, configured by the tests that need it
        blank = dict.fromkeys(("incident_id", "patient_name", "patient_age", "patient_gender", "eta", "additional_notes",
                               "acknowledged_by", "acknowledged_at", "accepted_by", "accepted_at", "received_by",
                               "received_at", "rejected_by", "rejected_at", "rejection_reason", "cancelled_by",
                               "cancelled_at", "client_request_id", "medic_unit", "transport_type", "triage_priority",
                               "chief_complaint", "heart_rate", "bp_systolic", "bp_diastolic", "spo2", "temperature",
                               "respiratory_rate", "bay_id", "receiving_team", "arrived_by", "arrived_at",
                               "handoff_started_by", "handoff_started_at", "completed_by", "completed_at",
                               "receiving_staff", "handoff_notes"))
        self.defaults = {
            "hospital_signals": {**blank, "status": "sent"},
            "hospitals": {"address": None, "phone_number": None, "latitude": None, "longitude": None, "city": None,
                          "province": None, "country": None, "official_email": None, "ed_contact": None,
                          "hospital_type": None, "license_number": None, "verified": True,
                          "availability": "ready", "availability_updated_at": None, "is_active": True},
            "hospital_staff": {"staff_role": "reception", "position": None},
            "hospital_bays": {"kind": "trauma", "status": "ready"},
        }
        self.identity = {"hospital_signals": ("signal_number",), "hospital_signal_events": ("id",)}
        self.stamps = {"hospital_signals": ("sent_at",)}
        self.unique = {"hospital_signals": ("client_request_id",)}

    def table(self, name):
        return Query(self, name)
