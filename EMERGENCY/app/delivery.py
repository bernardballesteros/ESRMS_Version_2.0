"""Email and push delivery for ESRMS notifications.

Database triggers (supabase/migrations/006) write one row to notification_events for every
real event: a new incident, a status change, a published announcement, a security change.
The worker here claims each event once, records one notification_deliveries row per
recipient and channel, and sends them:

  * email through Resend (https://resend.com), and
  * web push through the browser push services, signed with the VAPID key.

A delivery is only marked "sent" after the provider accepted it. Failures are retried with
backoff on the same row (emails carry an idempotency key), so a retry cannot produce a
second copy. Nothing is sent for a category the recipient switched off.
"""
import html
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import account as acct
from app import announcements as ann
from app import duty

log = logging.getLogger(__name__)

EVENTS = "notification_events"
DELIVERIES = "notification_deliveries"
SUBSCRIPTIONS = "push_subscriptions"
PREFERENCES = "notification_preferences"

RESEND_URL = "https://api.resend.com/emails"
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (60, 300, 900, 3600)   # wait after the 1st, 2nd, 3rd, 4th failed attempt
STALE_SECONDS = 300                      # a claim older than this belonged to a worker that died
MAX_EVENT_AGE = timedelta(hours=2)       # an alert this late is noise, not news
BATCH = 25

CATEGORY_LABELS = {k: v[0] for k, v in acct.NOTIFICATION_CATEGORIES.items()}


class DeliveryError(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class SubscriptionGone(DeliveryError):
    """The push service says this device subscription no longer exists."""

    def __init__(self, message):
        super().__init__(message, retryable=False)


# ---------------------------------------------------------------- configuration

def email_configured(cfg):
    return bool(cfg.get("RESEND_API_KEY") and cfg.get("EMAIL_FROM"))


def push_configured(cfg):
    return bool(cfg.get("VAPID_PUBLIC_KEY") and cfg.get("VAPID_PRIVATE_KEY") and cfg.get("VAPID_SUBJECT"))


def absolute_url(cfg, path):
    base = (cfg.get("APP_BASE_URL") or "").rstrip("/")
    if not base or not path:
        return None
    return base + (path if path.startswith("/") else "/" + path)


# ---------------------------------------------------------------- recipients (pure)

def user_role(user, admin_emails):
    """Same rule as sign-in: admin only from app_metadata or the server allowlist."""
    if (user.get("app_metadata") or {}).get("role") == "admin" or (user.get("email") or "").lower() in admin_emails:
        return "admin"
    role = (user.get("user_metadata") or {}).get("role")
    return None if role == "admin" else role


def directory_entry(user, admin_emails):
    meta = user.get("user_metadata") or {}
    return {
        "id": user["id"],
        "email": user.get("email"),
        "verified": bool(user.get("email_confirmed_at")),
        "banned": bool(user.get("banned_until")),
        "role": user_role(user, admin_emails),
        "branch": meta.get("branch"),
        "barangay": meta.get("barangay"),
    }


# Incident notifications only reach dispatchers who are On Duty (app/duty.py).
INCIDENT_CATEGORIES = {"emergency_incidents", "new_reports", "dispatch_updates", "status_updates"}


def recipients_for(event, users, dispatch_roles, announcement=None, now=None, on_duty=None):
    """Who an event is for. `users` are directory_entry() dicts; `on_duty` the ids of On Duty dispatchers."""
    users = [u for u in users if not u["banned"] and u["id"] != event.get("actor_id")]
    category = event["category"]
    if event.get("target_user_id"):
        return [u for u in users if u["id"] == event["target_user_id"]]
    if category == "security_alerts":
        return []  # security alerts are always addressed to one account
    if category == "system_announcements":
        if not announcement:
            return []
        return [u for u in users if ann.is_publicly_visible(announcement, ann.viewer_context(u), now)]
    dispatchers = [u for u in users if u["role"] in dispatch_roles]
    if category in INCIDENT_CATEGORIES:
        return [u for u in dispatchers if u["id"] in (on_duty or set())]
    return dispatchers


def message_for(event):
    """(title, body) shown in the email and the push notification."""
    if event["category"] == "security_alerts":
        label = acct.SECURITY_EVENTS.get(event.get("code"), "A security setting changed")
        return "Security alert", f"{label} on your ESRMS account. If this wasn't you, change your password and review your sessions."
    return event["title"], event.get("body") or ""


def plan_deliveries(event, recipients, prefs_by_user, subs_by_user, email_on, push_on):
    """One row per recipient and channel, skipping anything the recipient has switched off."""
    rows = []
    for u in recipients:
        prefs = acct.preferences_from_row(prefs_by_user.get(u["id"]))
        if not prefs[event["category"]]:
            continue
        if email_on and prefs["email_enabled"] and u["verified"] and u["email"]:
            rows.append({"event_id": event["id"], "user_id": u["id"], "channel": "email", "target": u["email"]})
        if push_on:
            for sub in subs_by_user.get(u["id"], []):
                rows.append({"event_id": event["id"], "user_id": u["id"], "channel": "push", "target": sub["id"]})
    return rows


def next_attempt(attempts, now):
    return now + timedelta(seconds=BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1])


# ---------------------------------------------------------------- senders

def render_email(cfg, title, body, category, url, when):
    stamp = when.astimezone(timezone(timedelta(hours=8))).strftime("%b %d, %Y %I:%M %p PHT")
    label = CATEGORY_LABELS.get(category, "Notification")
    link = absolute_url(cfg, url)
    settings = absolute_url(cfg, "/account/notifications")
    e = html.escape
    button = (f'<p style="margin:24px 0 0"><a href="{e(link)}" style="display:inline-block;padding:10px 18px;border-radius:8px;'
              f'background:#00D9FF;color:#050A12;font-weight:600;text-decoration:none">Open in ESRMS</a></p>') if link else ""
    manage = (f' <a href="{e(settings)}" style="color:#64748b">Manage notification settings</a>.' if settings else "")
    html_body = f"""<!doctype html><html><body style="margin:0;background:#080c14;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#080c14;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#0f1422;border:1px solid #1e3a4a;border-radius:12px">
<tr><td style="padding:18px 24px;border-bottom:1px solid #1e293b">
<span style="font-size:20px;font-weight:800;letter-spacing:2px;color:#ffffff">ESRMS</span>
<span style="font-size:11px;color:#00e5ff;margin-left:8px">Emergency SOS &amp; Response Management System</span></td></tr>
<tr><td style="padding:24px">
<p style="margin:0;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#00e5ff">{e(label)}</p>
<h1 style="margin:6px 0 0;font-size:20px;color:#ffffff">{e(title)}</h1>
<p style="margin:12px 0 0;font-size:14px;line-height:1.5;color:#cbd5e1">{e(body)}</p>
<p style="margin:16px 0 0;font-size:12px;color:#64748b">{e(stamp)}</p>{button}</td></tr>
<tr><td style="padding:14px 24px;border-top:1px solid #1e293b;font-size:11px;color:#64748b">
You are receiving this because email notifications for {e(label)} are on for your ESRMS account.{manage}</td></tr>
</table></td></tr></table></body></html>"""
    text = f"ESRMS - {label}\n\n{title}\n{body}\n\n{stamp}"
    if link:
        text += f"\n\nOpen in ESRMS: {link}"
    if settings:
        text += f"\n\nManage notification settings: {settings}"
    return f"[ESRMS] {title}", html_body, text


def send_email(cfg, to, title, body, category, url, when, idempotency_key):
    """Returns Resend's message id once Resend has accepted the email."""
    subject, html_body, text = render_email(cfg, title, body, category, url, when)
    try:
        res = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {cfg['RESEND_API_KEY']}", "Idempotency-Key": idempotency_key},
            json={"from": cfg["EMAIL_FROM"], "to": [to], "subject": subject, "html": html_body, "text": text},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(f"Could not reach the email provider: {exc.__class__.__name__}") from exc
    if res.status_code in (200, 201, 202):
        try:
            message_id = res.json().get("id")
        except ValueError:
            message_id = None
        if message_id:
            return message_id
        raise DeliveryError("The email provider did not confirm the message.")
    try:
        detail = res.json().get("message") or res.text
    except ValueError:
        detail = res.text
    # Bad key or unverified domain can be fixed by the operator; a rejected recipient cannot.
    retryable = res.status_code in (401, 403, 408, 409, 429) or res.status_code >= 500
    raise DeliveryError(f"Email provider answered {res.status_code}: {str(detail)[:300]}", retryable)


def send_push(cfg, subscription, title, body, category, url, tag):
    """Hands the message to the device's push service. Returns once the service accepted it."""
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": title, "body": body, "category": category, "url": url or "/dashboard", "tag": tag})
    try:
        res = webpush(
            subscription_info={"endpoint": subscription["endpoint"],
                               "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]}},
            data=payload,
            vapid_private_key=cfg["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": cfg["VAPID_SUBJECT"]},   # a fresh dict: pywebpush adds aud/exp to it
            ttl=3600,
            timeout=15,
            headers={"Urgency": "high" if category in ("emergency_incidents", "new_reports") else "normal"},
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            raise SubscriptionGone("The device's push subscription has expired or was removed.") from exc
        retryable = status is None or status in (408, 429) or status >= 500
        raise DeliveryError(f"Push service answered {status or 'no response'}.", retryable) from exc
    except Exception as exc:
        raise DeliveryError(f"Could not reach the push service: {exc.__class__.__name__}") from exc
    return str(getattr(res, "status_code", "accepted"))


# ---------------------------------------------------------------- worker

def _iso(dt):
    return dt.isoformat()


def load_directory(db, admin_emails):
    users, page = [], 1
    while True:
        batch = db.auth.admin.list_users(page=page, per_page=200)
        users += [directory_entry(u.model_dump(), admin_emails) for u in batch]
        if len(batch) < 200:
            return users
        page += 1


def fan_out(db, cfg, event, now):
    """Record who gets this event on which channel. Idempotent: rows are unique per recipient and device."""
    email_on, push_on = email_configured(cfg), push_configured(cfg)
    reference = ann.parse_ts(event.get("available_at")) or now
    if not (email_on or push_on) or now - reference > MAX_EVENT_AGE:
        return 0
    announcement = None
    if event["category"] == "system_announcements":
        rows = db.table("announcements").select("*").eq("id", event["subject_id"]).limit(1).execute().data
        announcement = rows[0] if rows else None
    on_duty = duty.on_duty_ids(db) if event["category"] in INCIDENT_CATEGORIES and not event.get("target_user_id") else None
    recipients = recipients_for(event, load_directory(db, cfg.get("ADMIN_EMAILS", set())),
                                cfg.get("DISPATCH_ROLES", set()), announcement, now, on_duty)
    if not recipients:
        return 0
    ids = [u["id"] for u in recipients]
    prefs = {r["user_id"]: r for r in db.table(PREFERENCES).select("*").in_("user_id", ids).execute().data}
    subs = {}
    if push_on:
        for s in db.table(SUBSCRIPTIONS).select("id, user_id").in_("user_id", ids).execute().data:
            subs.setdefault(s["user_id"], []).append(s)
    rows = plan_deliveries(event, recipients, prefs, subs, email_on, push_on)
    if rows:
        db.table(DELIVERIES).upsert(rows, on_conflict="event_id,user_id,channel,target", ignore_duplicates=True).execute()
    return len(rows)


def deliver(db, cfg, delivery, event, now):
    """Send one claimed delivery and record what really happened."""
    title, body = message_for(event)
    when = ann.parse_ts(event.get("created_at")) or now
    try:
        if delivery["channel"] == "email":
            provider_id = send_email(cfg, delivery["target"], title, body, event["category"], event.get("url"), when,
                                     idempotency_key=f"esrms-delivery-{delivery['id']}")
        else:
            subs = db.table(SUBSCRIPTIONS).select("*").eq("id", delivery["target"]).limit(1).execute().data
            if not subs:
                raise SubscriptionGone("The device is no longer subscribed.")
            provider_id = send_push(cfg, subs[0], title, body, event["category"], event.get("url"), f"esrms-{event['id']}")
            db.table(SUBSCRIPTIONS).update({"last_success_at": _iso(now), "failure_count": 0}).eq("id", subs[0]["id"]).execute()
    except SubscriptionGone as exc:
        db.table(SUBSCRIPTIONS).delete().eq("id", delivery["target"]).execute()
        db.table(DELIVERIES).update({"status": "dead", "last_error": str(exc)[:500]}).eq("id", delivery["id"]).execute()
        return "dead"
    except DeliveryError as exc:
        final = not exc.retryable or delivery["attempts"] >= MAX_ATTEMPTS
        db.table(DELIVERIES).update({
            "status": "dead" if final else "failed", "last_error": str(exc)[:500],
            "next_attempt_at": _iso(next_attempt(delivery["attempts"], now)),
        }).eq("id", delivery["id"]).execute()
        log.warning("Notification delivery %s failed (attempt %s): %s", delivery["id"], delivery["attempts"], exc)
        return "dead" if final else "failed"
    db.table(DELIVERIES).update({"status": "sent", "provider_id": provider_id, "sent_at": _iso(now), "last_error": None}) \
        .eq("id", delivery["id"]).execute()
    return "sent"


def process_once(db, cfg, now=None):
    """One pass over the outbox. Safe to run from several processes: every row is claimed first."""
    now = now or datetime.now(timezone.utc)
    stats = {"events": 0, "queued": 0, "sent": 0, "failed": 0, "dead": 0}
    stale = _iso(now - timedelta(seconds=STALE_SECONDS))
    db.table(EVENTS).update({"status": "pending"}).eq("status", "processing").lt("claimed_at", stale).execute()
    db.table(DELIVERIES).update({"status": "failed"}).eq("status", "sending").lt("claimed_at", stale).execute()

    events = (db.table(EVENTS).select("*").eq("status", "pending").lte("available_at", _iso(now))
              .order("id").limit(BATCH).execute().data)
    for event in events:
        claimed = (db.table(EVENTS).update({"status": "processing", "claimed_at": _iso(now)})
                   .eq("id", event["id"]).eq("status", "pending").execute().data)
        if not claimed:
            continue  # another worker has it
        stats["queued"] += fan_out(db, cfg, event, now)
        db.table(EVENTS).update({"status": "done"}).eq("id", event["id"]).execute()
        stats["events"] += 1

    due = (db.table(DELIVERIES).select("*").in_("status", ["pending", "failed"]).lte("next_attempt_at", _iso(now))
           .order("id").limit(BATCH).execute().data)
    event_cache = {}
    for delivery in due:
        claimed = (db.table(DELIVERIES)
                   .update({"status": "sending", "attempts": delivery["attempts"] + 1, "claimed_at": _iso(now)})
                   .eq("id", delivery["id"]).eq("status", delivery["status"]).eq("attempts", delivery["attempts"])
                   .execute().data)
        if not claimed:
            continue
        event_id = delivery["event_id"]
        if event_id not in event_cache:
            rows = db.table(EVENTS).select("*").eq("id", event_id).limit(1).execute().data
            event_cache[event_id] = rows[0] if rows else None
        if not event_cache[event_id]:
            continue
        stats[deliver(db, cfg, claimed[0], event_cache[event_id], now)] += 1
    return stats


def send_test(db, cfg, user_id, email, channel, endpoint=None):
    """Settings page "send test": a clearly labelled message, sent now, reported honestly."""
    title = "Test notification"
    body = "This is a test from your notification settings. No emergency was reported."
    now = datetime.now(timezone.utc)
    if channel == "email":
        provider_id = send_email(cfg, email, title, body, "security_alerts", "/account/notifications", now,
                                 idempotency_key=f"esrms-test-{user_id}-{int(now.timestamp())}")
        return {"accepted": 1, "provider_id": provider_id}
    query = db.table(SUBSCRIPTIONS).select("*").eq("user_id", user_id)
    if endpoint:
        query = query.eq("endpoint", endpoint)
    accepted = 0
    errors = []
    for sub in query.execute().data:
        try:
            send_push(cfg, sub, title, body, "security_alerts", "/account/notifications", "esrms-test")
            accepted += 1
        except SubscriptionGone as exc:
            db.table(SUBSCRIPTIONS).delete().eq("id", sub["id"]).execute()
            errors.append(str(exc))
        except DeliveryError as exc:
            errors.append(str(exc))
    return {"accepted": accepted, "errors": errors}


def run_forever(app, stop=None):
    """Poll the outbox. Started by main.py; also usable from scripts/deliver_notifications.py."""
    from app.supabase_client import get_supabase_admin

    interval = app.config["NOTIFICATION_POLL_SECONDS"]
    db = None
    while not (stop and stop.is_set()):
        pause = interval
        try:
            with app.app_context():
                db = db or get_supabase_admin()
                process_once(db, app.config)
        except Exception as exc:
            db = None
            code = getattr(exc, "code", "") or ""
            if code in ("PGRST205", "PGRST204", "42P01", "42703"):
                log.warning("Notification delivery is waiting for supabase/migrations/006_delivery_recovery_sessions.sql.")
                pause = 60
            else:
                log.warning("Notification delivery pass failed: %s", exc)
                pause = max(interval, 15)
        (stop.wait if stop else time.sleep)(pause)


def start_worker(app):
    if not app.config.get("SUPABASE_SERVICE_ROLE_KEY") or not app.config.get("NOTIFICATION_WORKER", True):
        return None
    thread = threading.Thread(target=run_forever, args=(app,), name="esrms-notifications", daemon=True)
    thread.start()
    log.info("Notification delivery worker started (email: %s, push: %s).",
             "on" if email_configured(app.config) else "not configured",
             "on" if push_configured(app.config) else "not configured")
    return thread
