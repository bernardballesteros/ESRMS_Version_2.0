"""Analytics tab API: KPIs, chart series, the incident activity table and report export.

Reads the same public.emergency_incidents and incident_events tables as the Dispatch
Center, through the same server-side client and role check (app/routes/dispatch.py).
The browser only receives computed figures and the paged table; contact numbers,
citizen names and descriptions are never sent from here.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, g, jsonify, request

from app import analytics as an
from app import analytics_export as export
from app import dispatch as dsp
from app import public_risk, risk
from app.routes.dispatch import EVENTS, INCIDENTS, db_error, dispatcher_api
from app.routes.dispatch import _db as _new_db

bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")
log = logging.getLogger(__name__)

# Only what the figures need; the whole comparison window is aggregated on the server.
SUMMARY_COLUMNS = "id, emergency_type, status, priority, created_at, resolved_at"
# High-Risk Area Detection: where and what, never who. No names, contact numbers or descriptions.
RISK_COLUMNS = "id, incident_number, emergency_type, status, priority, latitude, longitude, created_at, archived_at"
ORGANIZATIONS = "hospitals"   # organization bases (app/routes/hospital.py)
PUBLIC_FEED = "public_risk_feed"
TABLE_COLUMNS = ("id, incident_number, emergency_type, status, priority, latitude, longitude, location_verified, "
                 "created_at, resolved_at, archived_at")
FETCH_PAGE = 1000
MAX_SUMMARY_ROWS = 100_000
MAX_EXPORT_ROWS = 5000
PAGE_SIZES = (10, 25, 50)
SORTS = {"id": "incident_number", "category": "emergency_type", "status": "status", "reported": "created_at"}


class TooManyRows(Exception):
    pass


def _db():
    """One service-role client per request. Building a client and its TLS connection costs more than a query."""
    if "analytics_db" not in g:
        g.analytics_db = _new_db()
    return g.analytics_db


def _period():
    return an.resolve_period(request.args.get("range"), request.args.get("from"), request.args.get("to"))


def _invalid_range(exc):
    return jsonify(error=" ".join(exc.errors.values()), fields=exc.errors), 422


def _window_rows(db, period, columns=SUMMARY_COLUMNS):
    """Every incident reported in the selected period or the one before it, in pages."""
    rows, total = [], None
    while total is None or len(rows) < total:
        # The exact count is only asked for once; later pages just read rows.
        res = (db.table(INCIDENTS).select(columns, count="exact" if total is None else None)
               .gte("created_at", an.utc_param(period.prev_start)).lt("created_at", an.utc_param(period.end))
               .order("created_at").order("id").range(len(rows), len(rows) + FETCH_PAGE - 1).execute())
        if total is None:
            total = res.count if res.count is not None else len(res.data)
            if total > MAX_SUMMARY_ROWS:
                raise TooManyRows()
        if not res.data:
            break
        rows.extend(res.data)
    return rows


def _summary(db, period):
    return an.summarize(_window_rows(db, period), period)


def _quoted(value):
    """A PostgREST or() operand, quoted so commas, colons and parentheses in it are literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _like_literal(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filters():
    """Table filters from the query string; unknown values are ignored rather than trusted."""
    args = request.args
    group = args.get("status") if args.get("status") in an.GROUPS else None
    category = (args.get("category") or "").strip().lower()[:80]
    q = "".join(ch for ch in args.get("q", "") if ch.isalnum() or ch in " -_&").strip()[:60]
    return {"group": group, "category": category, "critical": args.get("priority") == "1", "q": q}


def _filters_text(f):
    parts = []
    if f["group"]:
        parts.append(f"status: {an.GROUPS[f['group']][0]}")
    if f["category"]:
        parts.append(f"category: {f['category']}")
    if f["critical"]:
        parts.append("priority: P1 critical")
    if f["q"]:
        parts.append(f"search: {f['q']}")
    return "; ".join(parts) or "none (all incidents in the date range)"


def _filtered(db, period, f, columns, count=None):
    query = (db.table(INCIDENTS).select(columns, count=count)
             .gte("created_at", an.utc_param(period.start)).lt("created_at", an.utc_param(period.end)))
    if f["group"]:
        query = query.in_("status", list(an.GROUPS[f["group"]][1]))
    if f["critical"]:
        query = query.eq("priority", an.CRITICAL_PRIORITY)
    if f["category"] == an.UNCATEGORIZED.lower():
        query = query.like("emergency_type", ":*")
    elif f["category"]:
        # Same rule as analytics.category_of(): the text before the first colon, or the whole type.
        name = _like_literal(f["category"])
        query = query.or_(f"emergency_type.ilike.{_quoted(name + ':*')},emergency_type.ilike.{_quoted(name)},"
                          f"emergency_type.ilike.{_quoted(name + ' :*')}")
    if f["q"]:
        text = _like_literal(f["q"])
        conditions = [f"emergency_type.ilike.{_quoted('*' + text + '*')}"]
        number = f["q"].upper().removeprefix("INC-").removeprefix("INC").strip().lstrip("0")
        if number.isdigit() and len(number) < 18:
            conditions.append(f"incident_number.eq.{number}")
        query = query.or_(",".join(conditions))
    return query


def _arrivals(db, period, ids):
    """incident id -> first 'on_scene' time, for response time. Events of a period's incidents start at period.start."""
    wanted, out, offset = set(ids), {}, 0
    if not wanted:
        return out
    while True:
        query = (db.table(EVENTS).select("incident_id, created_at").eq("event", "status").eq("to_status", "on_scene")
                 .order("created_at").order("id"))
        if len(wanted) <= 50:
            query = query.in_("incident_id", list(wanted))
        else:
            query = query.gte("created_at", an.utc_param(period.start))
        data = query.range(offset, offset + FETCH_PAGE - 1).execute().data
        for event in data:
            if event["incident_id"] in wanted:
                out.setdefault(event["incident_id"], event["created_at"])
        offset += len(data)
        if len(data) < FETCH_PAGE:
            return out


def _record(row, arrivals):
    located = dsp.valid_coordinates(row.get("latitude"), row.get("longitude"))
    response = an.response_seconds(row, arrivals.get(row["id"]))
    resolution = an.resolution_seconds(row)
    return {
        "id": row["id"],
        "code": dsp.incident_code(row),
        "emergency_type": row["emergency_type"],
        "category": an.category_of(row["emergency_type"]),
        "category_key": an.category_key(row["emergency_type"]),
        "status": row["status"],
        "status_label": dsp.STATUSES.get(row["status"], row["status"]),
        "group": an.GROUP_OF.get(row["status"]),
        "priority": row.get("priority"),
        "has_location": located,
        "latitude": row.get("latitude") if located else None,
        "longitude": row.get("longitude") if located else None,
        "location_verified": bool(row.get("location_verified")),
        "response_seconds": response,
        "response_label": an.format_duration(response),
        "resolution_seconds": resolution,
        "resolution_label": an.format_duration(resolution),
        "created_at": row["created_at"],
        "resolved_at": row.get("resolved_at"),
        "archived": bool(row.get("archived_at")),
    }


def _ordered(query, sort, descending):
    query = query.order(SORTS[sort], desc=descending)
    return query if sort == "id" else query.order("incident_number", desc=True)


@bp.route("/summary")
@dispatcher_api
def summary():
    """KPI cards, trend series, status donut and category breakdown for one period."""
    try:
        period = _period()
    except dsp.ValidationError as exc:
        return _invalid_range(exc)
    try:
        return jsonify(_summary(_db(), period))
    except TooManyRows:
        return jsonify(error="That range holds too many incidents to analyse at once. Choose a shorter range."), 422
    except Exception as exc:
        return db_error(exc, "load analytics")


@bp.route("/incidents")
@dispatcher_api
def incidents():
    """Recent Incident Activity: one page of incidents reported in the period, filtered and sorted in the database."""
    try:
        period = _period()
    except dsp.ValidationError as exc:
        return _invalid_range(exc)
    f = _filters()
    args = request.args
    sort = args.get("sort") if args.get("sort") in SORTS else "reported"
    descending = args.get("dir", "desc") != "asc"
    per_page = args.get("per_page", type=int)
    per_page = per_page if per_page in PAGE_SIZES else PAGE_SIZES[0]
    page = max(1, args.get("page", default=1, type=int))

    def run(page_number):
        first = (page_number - 1) * per_page
        return _ordered(_filtered(_db(), period, f, TABLE_COLUMNS, count="exact"), sort, descending) \
            .range(first, first + per_page - 1).execute()

    try:
        try:
            res = run(page)
        except Exception as exc:
            if getattr(exc, "code", None) != "PGRST103" or page == 1:  # page past the end after the data changed
                raise
            page = 1
            res = run(page)
        arrivals = _arrivals(_db(), period, [r["id"] for r in res.data])
    except Exception as exc:
        return db_error(exc, "load incident activity")
    total = res.count or 0
    return jsonify(
        items=[_record(r, arrivals) for r in res.data],
        total=total, page=page, per_page=per_page, pages=max(1, -(-total // per_page)),
        sort=sort, dir="desc" if descending else "asc",
    )


@bp.route("/export")
@dispatcher_api
def export_report():
    """CSV or PDF of the selected period: summaries for the whole range, records honouring the table filters."""
    fmt = request.args.get("format", "csv")
    if fmt not in ("csv", "pdf"):
        return jsonify(error="Choose CSV or PDF."), 422
    try:
        period = _period()
    except dsp.ValidationError as exc:
        return _invalid_range(exc)
    f = _filters()
    try:
        db = _db()
        summary_data = _summary(db, period)
        if not summary_data["kpis"]["total"]["value"]:
            return jsonify(error="No incidents were reported in this date range, so there is nothing to export."), 404
        rows, matching = [], None
        while len(rows) < MAX_EXPORT_ROWS and (matching is None or len(rows) < matching):
            res = _ordered(_filtered(db, period, f, TABLE_COLUMNS, count="exact" if matching is None else None), "reported", True) \
                .range(len(rows), min(len(rows) + FETCH_PAGE, MAX_EXPORT_ROWS) - 1).execute()
            if matching is None:
                matching = res.count or 0
            if not res.data:
                break
            rows.extend(res.data)
        rows = an.dedupe(rows)
        arrivals = _arrivals(db, period, [r["id"] for r in rows])
    except TooManyRows:
        return jsonify(error="That range holds too many incidents to export at once. Choose a shorter range."), 422
    except Exception as exc:
        return db_error(exc, "export the report")

    records = [_record(r, arrivals) for r in rows]
    # A list is only called complete when every matching incident is in it.
    meta = export.export_meta(summary_data, matching=max(matching or 0, len(records)), exported=len(records),
                              limit=MAX_EXPORT_ROWS, filters_text=_filters_text(f))
    try:
        if fmt == "pdf":
            body, mimetype = export.build_pdf(summary_data, records, meta), "application/pdf"
        else:
            body, mimetype = export.build_csv(summary_data, records, meta), "text/csv; charset=utf-8"
    except export.PdfUnavailable:
        return jsonify(error="PDF export needs the fpdf2 package. Run: pip install -r requirements.txt"), 501
    except Exception:
        log.exception("Analytics %s export could not be built", fmt)
        return jsonify(error=f"The {fmt.upper()} report could not be generated. Nothing was exported; "
                             "try again or use the other format."), 500
    response = Response(body, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{export.filename(summary_data, fmt, meta)}"'
    response.headers["X-Record-Count"] = str(meta["exported"])
    response.headers["X-Record-Total"] = str(meta["matching"])
    response.headers["X-Period-Total"] = str(meta["period_total"])
    response.headers["X-Export-Truncated"] = "1" if meta["truncated"] else "0"
    response.headers["Cache-Control"] = "no-store"
    return response


def _risk_filters():
    """The panel's own filters; unknown values are ignored rather than trusted."""
    args = request.args
    priority = args.get("priority", type=int)
    return {"category": (args.get("category") or "").strip().lower()[:80] or None,
            "priority": priority if priority in dsp.PRIORITIES else None}


def _risk_analysis(db, period):
    """Areas ranked by risk score, plus the organization bases they were named and measured against."""
    rows = _window_rows(db, period, RISK_COLUMNS)
    bases = _organization_bases(db)
    settings = risk.settings_from(current_app.config)
    return risk.analyze(rows, period, bases, settings, **_risk_filters()), bases, settings


def _organization_bases(db):
    return [b for b in db.table(ORGANIZATIONS).select("*").eq("is_active", True).execute().data
            if dsp.valid_coordinates(b.get("latitude"), b.get("longitude"))]


def publish_public_risk(db, config, now=None):
    """Recompute the areas for the fixed public period (no filters) and replace the citizens' copy.

    The same risk.analyze() the Analytics tab uses; public_risk.payload() then strips everything a
    citizen must not see. Needs migration 012. Returns what was published.
    """
    period = an.resolve_period(public_risk.PUBLIC_RANGE, now=now)
    bases = _organization_bases(db)
    settings = risk.settings_from(config)
    result = risk.analyze(_window_rows(db, period, RISK_COLUMNS), period, bases, settings)
    data = public_risk.payload(result, period, bases, settings, config.get("RISK_PUBLIC_MIN_INCIDENTS",
                                                                           public_risk.MIN_INCIDENTS))
    version = db.rpc("publish_public_risk_areas", {"p_areas": data["areas"], "p_meta": data["meta"]}).execute().data
    return {"version": version, "areas": len(data["areas"]), "analysed_areas": len(result["areas"]), **data["meta"]}


def _publish_if_stale(db):
    """Keep the citizens' copy fresh while dispatchers use Analytics. Never fails the request it rides on."""
    config = current_app.config
    if not config.get("RISK_PUBLIC_ENABLED", True):
        return
    try:
        feed = db.table(PUBLIC_FEED).select("published_at").eq("id", 1).execute().data
        published = an.parse_ts(feed[0].get("published_at")) if feed else None
        age = (datetime.now(timezone.utc) - published).total_seconds() / 60 if published else None
        if age is None or age >= config.get("RISK_PUBLIC_MAX_AGE_MIN", 60):
            log.info("Published public risk areas: %s", publish_public_risk(db, config))
    except Exception:
        log.warning("Could not publish public risk areas (is migration 012 applied?)", exc_info=True)


def _risk_response(build):
    try:
        period = _period()
    except dsp.ValidationError as exc:
        return _invalid_range(exc)
    try:
        return build(period)
    except TooManyRows:
        return jsonify(error="That range holds too many incidents to analyse at once. Choose a shorter range."), 422
    except Exception as exc:
        return db_error(exc, "load high-risk areas")


@bp.route("/risk")
@dispatcher_api
def risk_areas():
    """Risk Detection Radar: areas ranked by a score of frequency, severity and recency, with the incidents
    and organization bases placed around one centre."""
    def build(period):
        result, bases, settings = _risk_analysis(_db(), period)
        radar = risk.radar(result, bases, settings)
        _publish_if_stale(_db())
        return jsonify(**{**result, "areas": radar.pop("areas")}, radar=radar,
                       period=an.period_json(period), filters=_risk_filters(),
                       priorities={str(k): v for k, v in dsp.PRIORITIES.items()})
    return _risk_response(build)


@bp.route("/risk/publish", methods=["POST"])
@dispatcher_api
def risk_publish():
    """Publish the public-safe risk areas to the Citizen App now (it also happens automatically, hourly)."""
    if not current_app.config.get("RISK_PUBLIC_ENABLED", True):
        return jsonify(error="Publishing risk areas to citizens is switched off (RISK_PUBLIC_ENABLED=0)."), 409
    try:
        return jsonify(published=publish_public_risk(_db(), current_app.config))
    except TooManyRows:
        return jsonify(error="The public period holds too many incidents to analyse at once."), 422
    except Exception as exc:
        return db_error(exc, "publish the public risk areas")


@bp.route("/risk/area")
@dispatcher_api
def risk_area():
    """One area in full: breakdown, recent incidents, map points, nearby organization bases and coverage."""
    def build(period):
        result, bases, settings = _risk_analysis(_db(), period)
        area = next((a for a in result["areas"] if a["id"] == request.args.get("id")), None)
        if not area:
            return jsonify(error="That area has no incidents in this range and with these filters any more."), 404
        return jsonify(**risk.detail(area, bases, settings), period=an.period_json(period),
                       sufficient=result["sufficient"], reason=result["reason"], method=result["method"])
    return _risk_response(build)


def client_config():
    """Labels and rules the Analytics tab script needs, so they are defined once on the server."""
    return {
        "ranges": an.RANGES,
        "defaultRange": an.DEFAULT_RANGE,
        "today": datetime.now(timezone.utc).astimezone(an.PHT).date().isoformat(),
        "groups": {key: label for key, (label, _) in an.GROUPS.items()},
        "pageSizes": list(PAGE_SIZES),
    }
