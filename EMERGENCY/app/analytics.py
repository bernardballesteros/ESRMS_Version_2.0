"""Analytics rules for emergency incidents: reporting periods, status groups, KPIs and chart series.

Free of Flask/Supabase so the calculations can be unit tested (tests/test_analytics.py).

Definitions (all figures describe incidents REPORTED in the selected period, i.e. whose
emergency_incidents.created_at falls inside it; archived incidents are included):

  total            distinct incidents reported in the period.
  resolved         of those, status = 'resolved'.
  resolution rate  resolved / total * 100 (cohort of incidents reported in the period, not
                   incidents that happened to be closed during it).
  pending          status in ('new', 'acknowledged'): waiting for acknowledgment or dispatch.
                   Same rule as the Dispatch Center "queue" counter (app/dispatch.py counts()).
  in progress      status in ('dispatched', 'en_route', 'on_scene').
  cancelled        status = 'cancelled'.
  critical         priority = 1. A priority, not a status, so it is reported next to the status
                   groups and never added to them.
  resolution time  resolved_at - created_at, averaged over resolved incidents with both
                   timestamps and resolved_at >= created_at. Cancelled incidents are excluded
                   even though resolved_at is also stamped when an incident is cancelled.
  response time    first 'on_scene' entry in incident_events - created_at (responder arrival).
                   Not the same thing as resolution time.
  change           (current - previous) / previous * 100 against the period of equal length
                   immediately before. None when the previous value is 0 or unknown.

Days, hours and months are Philippine Time (UTC+8, no daylight saving), like the rest of the UI.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.dispatch import ValidationError

PHT = timezone(timedelta(hours=8), "PHT")
TIMEZONE_LABEL = "Philippine Time (UTC+8)"

RANGES = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "year": "This year",
    "custom": "Custom range",
}
DEFAULT_RANGE = "30d"
MAX_CUSTOM_DAYS = 1830  # five years

# Status groups for the donut. Every incident status belongs to exactly one group.
GROUPS = {
    "resolved": ("Resolved", ("resolved",)),
    "in_progress": ("In Progress", ("dispatched", "en_route", "on_scene")),
    "pending": ("Pending", ("new", "acknowledged")),
    "cancelled": ("Cancelled", ("cancelled",)),
}
GROUP_OF = {status: key for key, (_, statuses) in GROUPS.items() for status in statuses}
CRITICAL_PRIORITY = 1
UNCATEGORIZED = "Uncategorized"


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start_date: date      # first PHT day, inclusive
    end_date: date        # last PHT day, inclusive
    start: datetime       # aware; start_date 00:00 PHT
    end: datetime         # aware, exclusive; the day after end_date 00:00 PHT
    prev_start: datetime  # previous period of the same length: [prev_start, start)
    granularity: str      # hour | day | month

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1


def _parse_day(value, field, errors):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        errors[field] = "Choose a valid date."
        return None


def resolve_period(key=None, start=None, end=None, now=None):
    """Turn a range choice into concrete PHT day boundaries. Raises ValidationError for a bad custom range."""
    today = (now or datetime.now(timezone.utc)).astimezone(PHT).date()
    key = key if key in RANGES else DEFAULT_RANGE
    if key == "custom":
        errors = {}
        first, last = _parse_day(start, "from", errors), _parse_day(end, "to", errors)
        if first and last:
            if first > last:
                errors["from"] = "The start date cannot be later than the end date."
            elif (last - first).days >= MAX_CUSTOM_DAYS:
                errors["to"] = "Choose a range of five years or less."
        if errors:
            raise ValidationError(errors)
        label = f"{first:%b %d, %Y} – {last:%b %d, %Y}"
    else:
        last = today
        first = {
            "today": today,
            "7d": today - timedelta(days=6),
            "30d": today - timedelta(days=29),
            "90d": today - timedelta(days=89),
            "year": date(today.year, 1, 1),
        }[key]
        label = RANGES[key]
    days = (last - first).days + 1
    start_at = datetime.combine(first, time.min, PHT)
    return Period(
        key=key, label=label, start_date=first, end_date=last,
        start=start_at, end=start_at + timedelta(days=days), prev_start=start_at - timedelta(days=days),
        granularity="hour" if days <= 2 else "day" if days <= 92 else "month",
    )


def parse_ts(value):
    """Aware datetime from a Supabase timestamptz string, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utc_param(moment):
    """Timestamp for a PostgREST filter. 'Z' form: a '+' offset would need URL escaping."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def category_of(emergency_type):
    """The Citizen App sends 'Category: specific emergency' in emergency_type; the category is the first part."""
    return (emergency_type or "").split(":", 1)[0].strip() or UNCATEGORIZED


def category_key(emergency_type):
    return category_of(emergency_type).lower()


def format_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def pct_change(current, previous):
    """None when there is nothing to compare with; the UI says so instead of showing 0% or infinity."""
    if current is None or not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def resolution_seconds(row):
    """Seconds from report to resolution, or None if the incident is not a validly timed resolved one."""
    if row.get("status") != "resolved":
        return None
    created, resolved = parse_ts(row.get("created_at")), parse_ts(row.get("resolved_at"))
    if not created or not resolved or resolved < created:
        return None
    return (resolved - created).total_seconds()


def response_seconds(row, arrived_at):
    """Seconds from report to the first responder arrival (on_scene), or None."""
    created, arrived = parse_ts(row.get("created_at")), parse_ts(arrived_at)
    if not created or not arrived or arrived < created:
        return None
    return (arrived - created).total_seconds()


def dedupe(rows):
    """One row per incident id, so a repeated page boundary can never inflate a count."""
    seen = {}
    for row in rows:
        seen.setdefault(row["id"], row)
    return list(seen.values())


def split_periods(rows, period):
    """(current, previous) rows by created_at."""
    current, previous = [], []
    for row in dedupe(rows):
        created = parse_ts(row.get("created_at"))
        if not created:
            continue
        if period.start <= created < period.end:
            current.append(row)
        elif period.prev_start <= created < period.start:
            previous.append(row)
    return current, previous


def _totals(rows):
    groups = Counter(GROUP_OF.get(r.get("status")) for r in rows)
    durations = [s for s in map(resolution_seconds, rows) if s is not None]
    return {
        "total": len(rows),
        "groups": groups,
        "critical": sum(1 for r in rows if r.get("priority") == CRITICAL_PRIORITY),
        "avg_resolution": sum(durations) / len(durations) if durations else None,
        "timed": len(durations),
    }


def _bucket_key(moment, granularity):
    local = moment.astimezone(PHT)
    return local.strftime({"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "month": "%Y-%m"}[granularity])


def _buckets(period, now):
    """Every hour/day/month of the period up to now, so quiet intervals show as zero instead of vanishing."""
    last = min(period.end, now.astimezone(PHT) + timedelta(microseconds=1))
    out, cursor = [], period.start
    while cursor < last:
        if period.granularity == "hour":
            label, title, step = f"{cursor:%H}:00", f"{cursor:%b %d, %Y} {cursor:%H}:00–{cursor:%H}:59", cursor + timedelta(hours=1)
        elif period.granularity == "day":
            label, title, step = f"{cursor:%b %d}", f"{cursor:%a, %b %d, %Y}", cursor + timedelta(days=1)
        else:
            label = title = f"{cursor:%b %Y}"
            step = datetime(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1, tzinfo=PHT)
        out.append({"key": _bucket_key(cursor, period.granularity), "label": label, "title": title,
                    "total": 0, "resolved": 0, "pending": 0})
        cursor = step
    return out


def trend(rows, period, now):
    buckets = _buckets(period, now)
    by_key = {b["key"]: b for b in buckets}
    for row in rows:
        bucket = by_key.get(_bucket_key(parse_ts(row["created_at"]), period.granularity))
        if not bucket:
            continue
        bucket["total"] += 1
        group = GROUP_OF.get(row.get("status"))
        if group in ("resolved", "pending"):
            bucket[group] += 1
    return {"granularity": period.granularity, "buckets": buckets}


def categories(rows):
    counts, spellings = Counter(), {}
    for row in rows:
        key = category_key(row.get("emergency_type"))
        counts[key] += 1
        spellings.setdefault(key, Counter())[category_of(row.get("emergency_type"))] += 1
    out = [{"key": key, "label": spellings[key].most_common(1)[0][0], "count": n, "pct": pct(n, len(rows))}
           for key, n in counts.items()]
    return sorted(out, key=lambda c: (-c["count"], c["label"].lower()))


def period_json(period):
    prev_end = period.start_date - timedelta(days=1)
    return {
        "key": period.key, "label": period.label, "days": period.days, "granularity": period.granularity,
        "from": period.start_date.isoformat(), "to": period.end_date.isoformat(),
        "previous": {"from": (period.start_date - timedelta(days=period.days)).isoformat(), "to": prev_end.isoformat()},
        "timezone": TIMEZONE_LABEL,
    }


def summarize(rows, period, now=None):
    """Everything the dashboard shows except the incident table, from rows covering [prev_start, end)."""
    now = now or datetime.now(timezone.utc)
    current, previous = split_periods(rows, period)
    cur, prev = _totals(current), _totals(previous)
    total = cur["total"]

    def kpi(group):
        return {"value": cur["groups"][group], "previous": prev["groups"][group],
                "change": pct_change(cur["groups"][group], prev["groups"][group])}

    return {
        "period": period_json(period),
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "kpis": {
            "total": {"value": total, "previous": prev["total"], "change": pct_change(total, prev["total"])},
            "resolution": {
                "seconds": cur["avg_resolution"], "label": format_duration(cur["avg_resolution"]),
                "previous_seconds": prev["avg_resolution"], "previous_label": format_duration(prev["avg_resolution"]),
                "change": pct_change(cur["avg_resolution"], prev["avg_resolution"]), "sample": cur["timed"],
            },
            "resolved": {**kpi("resolved"), "rate": pct(cur["groups"]["resolved"], total)},
            "pending": {**kpi("pending"), "share": pct(cur["groups"]["pending"], total)},
        },
        "status": [{"key": key, "label": label, "statuses": list(statuses),
                    "count": cur["groups"][key], "pct": pct(cur["groups"][key], total)}
                   for key, (label, statuses) in GROUPS.items()],
        "critical": {"count": cur["critical"], "pct": pct(cur["critical"], total)},
        "categories": categories(current),
        "trend": trend(current, period, now),
    }
