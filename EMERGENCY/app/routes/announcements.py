"""Announcement dashboard, Citizen Announcement Viewer, and their JSON API.

Both pages read the same public.announcements table through these routes. The
browser never queries that table directly: RLS denies it, and the service-role
client used here stays on the server. Browsers only subscribe to the
announcement_feed change counter and refetch from here when it moves.
"""
import logging
from datetime import timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from app import announcements as ann
from app.supabase_client import get_supabase_admin

bp = Blueprint("announcements", __name__)
log = logging.getLogger(__name__)

TABLE = "announcements"
TEMPLATES = "announcement_templates"
LIST_LIMIT = 200
MANILA = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- auth helpers

def can_manage(user):
    return bool(user) and user.get("role") in current_app.config["ANNOUNCEMENT_MANAGER_ROLES"]


def manager_page(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = session.get("user")
        if not user:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("auth.login"))
        if not can_manage(user):
            flash("You do not have permission to manage announcements.", "error")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def manager_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = session.get("user")
        if not user:
            return jsonify(error="Sign in to manage announcements."), 401
        if not can_manage(user):
            return jsonify(error="You do not have permission to manage announcements."), 403
        # Writes must be JSON. Browsers can't send a cross-site JSON request without a
        # CORS preflight this app never approves, which blocks CSRF from other sites.
        if request.method in ("POST", "PUT", "PATCH") and not request.is_json:
            return jsonify(error="Expected a JSON request."), 415
        return view(*args, **kwargs)
    return wrapped


def db_error(exc, action):
    """Log the real error, return a JSON failure. Never reports success."""
    log.exception("Announcement %s failed", action)
    detail = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    return jsonify(error=f"Could not {action}: {detail}"), 502


def _db():
    return get_supabase_admin()


# ---------------------------------------------------------------- serialization

def _authors(rows):
    ids = {r[k] for r in rows for k in ("created_by", "published_by", "updated_by") if r.get(k)}
    if not ids:
        return {}
    try:
        res = _db().table("users").select("id, email, full_name, agency_name").in_("id", list(ids)).execute()
    except Exception:  # names are a nicety; the list still works without them
        log.warning("Could not load announcement author names", exc_info=True)
        return {}
    return {u["id"]: u.get("full_name") or u.get("agency_name") or u.get("email") for u in res.data}


def _admin_json(row, names, now):
    out = dict(row)
    out["effective_status"] = ann.effective_status(row, now)
    for key in ("created_by", "published_by", "updated_by"):
        out[f"{key}_name"] = names.get(row.get(key))
    return out


def _public_json(row):
    """What citizens get. No author ids or internal fields."""
    keys = ("id", "title", "message", "category", "priority", "target_audience",
            "barangay", "is_pinned", "published_at", "expires_at", "updated_at")
    return {k: row.get(k) for k in keys}


def _fetch(announcement_id):
    res = _db().table(TABLE).select("*").eq("id", announcement_id).limit(1).execute()
    return res.data[0] if res.data else None


# ---------------------------------------------------------------- pages

@bp.route("/announcements")
def viewer():
    user = session.get("user")
    return render_template(
        "announcements/viewer.html",
        account_barangay=(user or {}).get("barangay"),
        realtime=_realtime_config(),
        categories=ann.CATEGORIES,
        priorities=ann.PRIORITIES,
    )


@bp.route("/announcements/manage")
@manager_page
def manage():
    return render_template("announcements/manage.html", **manage_context())


def manage_context():
    """Template variables for the dashboard. Also used by the organization dashboard tab."""
    return {
        "realtime": _realtime_config(),
        "ann_options": {
            "categories": ann.CATEGORIES,
            "priorities": ann.PRIORITIES,
            "audiences": ann.AUDIENCES,
            "groups": ann.GROUPS,
            "statuses": ann.STATUSES,
        },
    }


def _realtime_config():
    # The anon key is public by design; RLS limits it to the announcement_feed counter.
    return {"url": current_app.config["SUPABASE_URL"], "key": current_app.config["SUPABASE_KEY"]}


# ---------------------------------------------------------------- admin API

@bp.route("/api/announcements")
@manager_api
def list_announcements():
    now = ann.now_utc()
    args = request.args
    status = args.get("status", "")
    try:
        db = _db()

        # Status counts and the featured post ignore the filters, so they're queried separately.
        stats_rows = db.table(TABLE).select("id, status, published_at, expires_at").execute().data
        live = (
            db.table(TABLE).select("*")
            .in_("status", ["active", "scheduled"])
            .order("published_at", desc=True)
            .limit(LIST_LIMIT)
            .execute().data
        )

        query = db.table(TABLE).select("*")
        q = ann.clean_search(args.get("q"))
        if q:
            query = query.or_(f"title.ilike.*{q}*,message.ilike.*{q}*")
        if args.get("category") in ann.CATEGORIES:
            query = query.eq("category", args["category"])
        if args.get("priority") in ann.PRIORITIES:
            query = query.eq("priority", args["priority"])
        if status in ("draft", "archived"):
            query = query.eq("status", status)
        elif status in ("active", "scheduled", "expired"):
            # The time-based part is applied below with the same rule the viewer uses.
            query = query.in_("status", ["active", "scheduled"])
        for arg, op, suffix in (("date_from", "gte", "T00:00:00+08:00"), ("date_to", "lte", "T23:59:59+08:00")):
            value = args.get(arg, "")
            if len(value) == 10 and value[4] == "-" and value[7] == "-":
                query = getattr(query, op)("published_at", value + suffix)
        newest = args.get("sort") != "oldest"
        query = (
            query.order("published_at", desc=newest, nullsfirst=newest)
            .order("created_at", desc=newest)
            .limit(LIST_LIMIT)
        )
        rows = query.execute().data
    except Exception as exc:
        return db_error(exc, "load announcements")

    if status in ("active", "scheduled", "expired"):
        rows = [r for r in rows if ann.effective_status(r, now) == status]

    featured = ann.pick_featured(live, now)
    names = _authors(rows + ([featured] if featured else []))
    return jsonify(
        items=[_admin_json(r, names, now) for r in rows],
        featured=_admin_json(featured, names, now) if featured else None,
        counts=ann.status_counts(stats_rows, now),
        server_time=now.isoformat(),
    )


@bp.route("/api/announcements", methods=["POST"])
@manager_api
def create_announcement():
    try:
        row = ann.validate(request.get_json(silent=True) or {})
    except ann.ValidationError as exc:
        return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422

    user_id = session["user"]["id"]
    row["created_by"] = row["updated_by"] = user_id
    if row["status"] in ("active", "scheduled"):
        row["published_by"] = user_id
    try:
        if row["is_pinned"]:
            _unpin_all()
        created = _db().table(TABLE).insert(row).execute().data
    except Exception as exc:
        return db_error(exc, "save the announcement")
    if not created:
        return jsonify(error="The database did not confirm the save. Nothing was published."), 502
    return jsonify(item=_admin_json(created[0], {}, ann.now_utc())), 201


@bp.route("/api/announcements/<uuid:announcement_id>", methods=["PUT"])
@manager_api
def update_announcement(announcement_id):
    announcement_id = str(announcement_id)
    try:
        existing = _fetch(announcement_id)
    except Exception as exc:
        return db_error(exc, "load the announcement")
    if not existing:
        return jsonify(error="That announcement no longer exists."), 404

    payload = request.get_json(silent=True) or {}
    try:
        row = ann.validate(payload)
    except ann.ValidationError as exc:
        return jsonify(error="Please fix the highlighted fields.", fields=exc.errors), 422

    user_id = session["user"]["id"]
    row["updated_by"] = user_id
    if row["status"] in ("active", "scheduled") and existing["status"] in ("draft", "archived"):
        row["published_by"] = user_id
    return _apply(announcement_id, row, "update the announcement", unpin_others=row["is_pinned"])


@bp.route("/api/announcements/<uuid:announcement_id>/archive", methods=["POST"])
@manager_api
def archive_announcement(announcement_id):
    return _apply(str(announcement_id), {"status": "archived", "is_pinned": False,
                                         "updated_by": session["user"]["id"]}, "archive the announcement")


@bp.route("/api/announcements/<uuid:announcement_id>/restore", methods=["POST"])
@manager_api
def restore_announcement(announcement_id):
    announcement_id = str(announcement_id)
    try:
        existing = _fetch(announcement_id)
    except Exception as exc:
        return db_error(exc, "load the announcement")
    if not existing:
        return jsonify(error="That announcement no longer exists."), 404

    published_at = ann.parse_ts(existing.get("published_at"))
    now = ann.now_utc()
    changes = {"updated_by": session["user"]["id"]}
    if published_at is None:
        changes["status"] = "draft"
    else:
        changes["status"] = "scheduled" if published_at > now else "active"
        changes["published_by"] = existing.get("published_by") or session["user"]["id"]
    return _apply(announcement_id, changes, "restore the announcement")


@bp.route("/api/announcements/<uuid:announcement_id>/pin", methods=["POST"])
@manager_api
def pin_announcement(announcement_id):
    pinned = bool((request.get_json(silent=True) or {}).get("pinned"))
    return _apply(str(announcement_id), {"is_pinned": pinned, "updated_by": session["user"]["id"]},
                  "pin the announcement" if pinned else "unpin the announcement", unpin_others=pinned)


@bp.route("/api/announcements/<uuid:announcement_id>", methods=["DELETE"])
@manager_api
def delete_announcement(announcement_id):
    try:
        deleted = _db().table(TABLE).delete().eq("id", str(announcement_id)).execute().data
    except Exception as exc:
        return db_error(exc, "delete the announcement")
    if not deleted:
        return jsonify(error="That announcement no longer exists."), 404
    return jsonify(ok=True)


def _unpin_all(except_id=None):
    query = _db().table(TABLE).update({"is_pinned": False}).eq("is_pinned", True)
    if except_id:
        query = query.neq("id", except_id)
    query.execute()


def _apply(announcement_id, changes, action, unpin_others=False):
    try:
        if unpin_others:
            _unpin_all(except_id=announcement_id)
        updated = _db().table(TABLE).update(changes).eq("id", announcement_id).execute().data
    except Exception as exc:
        return db_error(exc, action)
    if not updated:
        return jsonify(error="That announcement no longer exists."), 404
    return jsonify(item=_admin_json(updated[0], {}, ann.now_utc()))


# ---------------------------------------------------------------- templates API

@bp.route("/api/announcement-templates")
@manager_api
def list_templates():
    try:
        rows = _db().table(TEMPLATES).select("*").order("name").execute().data
    except Exception as exc:
        return db_error(exc, "load templates")
    return jsonify(items=rows)


@bp.route("/api/announcement-templates", methods=["POST"])
@manager_api
def create_template():
    p = request.get_json(silent=True) or {}
    name = (p.get("name") or "").strip()
    errors = {}
    if not name:
        errors["name"] = "Template name is required."
    if p.get("category") not in ann.CATEGORIES:
        errors["category"] = "Choose a category."
    if p.get("priority") not in ann.PRIORITIES:
        errors["priority"] = "Choose a priority."
    if errors:
        return jsonify(error="Please fix the highlighted fields.", fields=errors), 422
    row = {
        "name": name[:120],
        "title": (p.get("title") or "").strip()[: ann.TITLE_MAX],
        "message": (p.get("message") or "").strip()[: ann.MESSAGE_MAX],
        "category": p["category"],
        "priority": p["priority"],
        "created_by": session["user"]["id"],
    }
    try:
        created = _db().table(TEMPLATES).insert(row).execute().data
    except Exception as exc:
        return db_error(exc, "save the template")
    if not created:
        return jsonify(error="The database did not confirm the save."), 502
    return jsonify(item=created[0]), 201


@bp.route("/api/announcement-templates/<uuid:template_id>", methods=["DELETE"])
@manager_api
def delete_template(template_id):
    try:
        deleted = _db().table(TEMPLATES).delete().eq("id", str(template_id)).execute().data
    except Exception as exc:
        return db_error(exc, "delete the template")
    if not deleted:
        return jsonify(error="That template no longer exists."), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------- public API

@bp.route("/api/announcements/public")
def public_announcements():
    """Announcements the current viewer may see right now. Used by the Citizen Announcement Viewer."""
    now = ann.now_utc()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    viewer = ann.viewer_context(session.get("user"), request.args.get("barangay"))
    try:
        db = _db()
        rows = (
            db.table(TABLE).select("*")
            .in_("status", ["active", "scheduled"])
            .lte("published_at", stamp)
            .or_(f"expires_at.is.null,expires_at.gt.{stamp}")
            .order("published_at", desc=True)
            .limit(LIST_LIMIT)
            .execute().data
        )
        upcoming = (
            db.table(TABLE).select("published_at, target_audience, barangay, target_groups")
            .in_("status", ["active", "scheduled"])
            .gt("published_at", stamp)
            .order("published_at")
            .limit(50)
            .execute().data
        )
    except Exception:
        log.exception("Public announcement fetch failed")
        return jsonify(error="Announcements are temporarily unavailable."), 502

    visible = ann.sort_for_viewer([r for r in rows if ann.is_publicly_visible(r, viewer, now)])

    # Tell the page when to refetch next even if nothing in the database changes:
    # the next scheduled post for this viewer goes live, or a visible post expires.
    times = [ann.parse_ts(r["published_at"]) for r in upcoming if ann.audience_matches(r, viewer)]
    times += [ann.parse_ts(r["expires_at"]) for r in visible if r.get("expires_at")]
    next_change = min(times) if times else None

    return jsonify(
        items=[_public_json(r) for r in visible],
        barangay=viewer["barangay"],
        next_change_at=next_change.isoformat() if next_change else None,
        server_time=now.isoformat(),
    )
