import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
    # Server-only key for the admin portal's user management. Never expose it to the browser.
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    # Account roles allowed to create, edit, publish, archive and delete announcements.
    ANNOUNCEMENT_MANAGER_ROLES = {
        r.strip() for r in os.environ.get("ANNOUNCEMENT_MANAGER_ROLES", "admin,organization").split(",") if r.strip()
    }
    # Account roles allowed to use the Dispatch Center: update incidents and manage hotlines.
    DISPATCH_ROLES = {
        r.strip() for r in os.environ.get("DISPATCH_ROLES", "admin,organization").split(",") if r.strip()
    }
    # Account roles that administer an agency and may edit Agency Settings.
    AGENCY_ADMIN_ROLES = {
        r.strip() for r in os.environ.get("AGENCY_ADMIN_ROLES", "organization").split(",") if r.strip()
    }
    # Public Supabase Storage bucket for profile photos and agency logos (created by migration 005).
    MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "esrms-media")
    # Email delivery through Resend (https://resend.com). Server-side only.
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "")  # e.g. "ESRMS Alerts <alerts@your-verified-domain>"
    # Public address of this ESRMS server, used for links in emails. HTTPS in production.
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "")
    # Web push (VAPID). Generate with scripts/generate_vapid_keys.py. The private key never leaves the server.
    VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
    VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "")  # "mailto:you@example.com" or your https site
    # Background worker that sends queued email and push notifications.
    NOTIFICATION_WORKER = os.environ.get("NOTIFICATION_WORKER", "1") != "0"
    NOTIFICATION_POLL_SECONDS = max(2, int(os.environ.get("NOTIFICATION_POLL_SECONDS", "5")))
    # Analytics -> High-Risk Area Detection thresholds (method and defaults: app/risk.py). Unset keeps the default.
    RISK_CELL_KM = os.environ.get("RISK_CELL_KM")                        # size of an area, km (1)
    RISK_HIGH_RATIO = os.environ.get("RISK_HIGH_RATIO")                  # High: score >= this x the average area (1.5)
    RISK_MODERATE_RATIO = os.environ.get("RISK_MODERATE_RATIO")          # Moderate: score >= this x the average (1.0)
    RISK_HIGH_MIN_INCIDENTS = os.environ.get("RISK_HIGH_MIN_INCIDENTS")  # incidents an area needs to be High (3)
    RISK_MIN_INCIDENTS = os.environ.get("RISK_MIN_INCIDENTS")            # located incidents needed before any level is given (10)
    RISK_MIN_AREAS = os.environ.get("RISK_MIN_AREAS")                    # areas needed before any level is given (3)
    RISK_STABLE_PCT = os.environ.get("RISK_STABLE_PCT")                  # trend is Stable below this % change (10)
    RISK_COVERAGE_KM = os.environ.get("RISK_COVERAGE_KM")                # radius for nearby organization bases, km (5)
    # Public-safe copy of the risk areas for the Citizen App's Safety Radar (app/public_risk.py, migration 012).
    RISK_PUBLIC_ENABLED = os.environ.get("RISK_PUBLIC_ENABLED", "1") != "0"   # 0 = never publish to citizens
    RISK_PUBLIC_MAX_AGE_MIN = max(5, int(os.environ.get("RISK_PUBLIC_MAX_AGE_MIN", "60")))  # republish when older
    RISK_PUBLIC_MIN_INCIDENTS = max(2, int(os.environ.get("RISK_PUBLIC_MIN_INCIDENTS", "3")))  # smaller areas stay private
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # matches the 25MB upload limit shown on the forms
