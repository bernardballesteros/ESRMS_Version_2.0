"""Rules for the account settings pages: profile, agency, password and notifications.

Free of Flask/Supabase so they can be unit tested (tests/test_account.py).
"""
import hashlib
import re
import secrets

from app.dispatch import PHONE_RE, ValidationError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ORI_RE = re.compile(r"^[A-Z0-9-]{1,9}$")

AGENCY_TYPES = {
    "psap-primary": "Primary 911 PSAP Dispatch Facility",
    "psap-secondary": "Secondary PSAP / Fire-Rescue Control",
    "regional-e911": "County Emergency Management Agency (EMA)",
    "law-dispatch": "Municipal Law Enforcement Comms Desk",
    "tri-services": "Consolidated Tri-Services Joint Dispatch",
}

ROLE_LABELS = {
    "admin": "System Administrator",
    "organization": "Organization / 911 Dispatcher",
    "responder": "Responder",
    "citizen": "Citizen",
}

# Same minimums the registration forms and the admin portal enforce.
MIN_PASSWORD = {"organization": 16, "responder": 12, "admin": 12}
DEFAULT_MIN_PASSWORD = 8
MAX_PASSWORD = 72  # bcrypt limit used by Supabase Auth

# Notification categories -> (label, description). Order is the display order.
NOTIFICATION_CATEGORIES = {
    "emergency_incidents": ("Emergency Incidents", "Critical and high-priority (P1/P2) incident alerts"),
    "new_reports": ("New Reports", "New emergency reports submitted from the Citizen App"),
    "dispatch_updates": ("Dispatch & Assignments", "Incidents dispatched, units en route or on scene"),
    "status_updates": ("Status Updates", "Incidents acknowledged, resolved or cancelled"),
    "system_announcements": ("System Announcements", "Newly published announcements and advisories"),
    "security_alerts": ("Security Alerts", "Password, MFA and sign-in changes on your account"),
}
DELIVERY_FIELDS = ("browser_enabled", "email_enabled")
PREFERENCE_FIELDS = tuple(NOTIFICATION_CATEGORIES) + DELIVERY_FIELDS
# Settings -> Display: percent of the browser's normal text size. Everything is sized in rem, so it all follows.
DISPLAY_SCALES = (80, 90, 100, 110, 125)
DEFAULT_DISPLAY_SCALE = 90
DEFAULT_PREFERENCES = {**{k: True for k in NOTIFICATION_CATEGORIES}, "browser_enabled": False, "email_enabled": False}

SECURITY_EVENTS = {
    "password_changed": "Password changed",
    "mfa_enabled": "Multi-factor authentication enabled",
    "mfa_disabled": "Multi-factor authentication disabled",
    "sessions_revoked": "Other sessions signed out",
    "email_change_requested": "Email address change requested",
    "mfa_factor_added": "Authenticator added",
    "mfa_factor_removed": "Authenticator removed",
    "recovery_codes_generated": "New MFA recovery codes generated",
    "recovery_code_used": "MFA recovery code used; authenticators were removed",
    "mfa_reset_requested": "Administrator MFA reset requested",
    "mfa_reset_by_admin": "MFA reset by an administrator",
    "session_revoked": "A device was signed out",
    "all_sessions_revoked": "All devices signed out",
}

AGENCY_STATUSES = {"pending": "Pending verification", "verified": "Verified", "suspended": "Suspended"}

# No 0/O or 1/I, so a code can be read out over the phone.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_COUNT = 10
MAX_FACTORS = 3

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def min_password(role):
    return MIN_PASSWORD.get(role, DEFAULT_MIN_PASSWORD)


def _text(payload, name, limit, errors):
    value = payload.get(name)
    value = " ".join(value.split()) if isinstance(value, str) else ""
    if len(value) > limit:
        errors[name] = f"Must be {limit} characters or fewer."
    return value


def _multiline(payload, name, limit, errors):
    value = payload.get(name)
    value = value.strip() if isinstance(value, str) else ""
    if len(value) > limit:
        errors[name] = f"Must be {limit} characters or fewer."
    return value


def _phone(value, name, errors, required=False, label="Phone number"):
    if not value:
        if required:
            errors[name] = f"{label} is required."
    elif not PHONE_RE.match(value) or sum(ch.isdigit() for ch in value) < 7:
        errors[name] = "Use at least 7 digits; spaces, dashes and parentheses are allowed, optionally starting with +."


def _email(value, name, errors, required=False, label="Email address"):
    if not value:
        if required:
            errors[name] = f"{label} is required."
    elif len(value) > 254 or not EMAIL_RE.match(value):
        errors[name] = "Enter a valid email address."


def validate_profile(payload):
    """Editable profile fields only. Role, agency and account status are never read from the payload."""
    errors = {}
    full_name = _text(payload, "full_name", 120, errors)
    email = _text(payload, "email", 254, errors).lower()
    phone = _text(payload, "phone", 32, errors)
    if not full_name:
        errors["full_name"] = "Full name is required."
    _email(email, "email", errors, required=True)
    if "phone" not in errors:
        _phone(phone, "phone", errors)
    if errors:
        raise ValidationError(errors)
    return {"full_name": full_name, "email": email, "phone": phone or None}


def validate_agency(payload):
    """Editable agency fields. The agency code (ORI) is fixed at registration and not accepted here."""
    errors = {}
    name = _text(payload, "agency_name", 120, errors)
    agency_type = _text(payload, "agency_type", 40, errors)
    jurisdiction = _text(payload, "jurisdiction", 120, errors)
    phone = _text(payload, "psap_phone", 32, errors)
    email = _text(payload, "agency_email", 254, errors).lower()
    address = _multiline(payload, "agency_address", 300, errors)
    description = _multiline(payload, "agency_description", 500, errors)
    if not name:
        errors["agency_name"] = "Agency name is required."
    if agency_type not in AGENCY_TYPES:
        errors["agency_type"] = "Choose an agency type."
    if not jurisdiction:
        errors["jurisdiction"] = "Jurisdiction is required."
    if "psap_phone" not in errors:
        _phone(phone, "psap_phone", errors, required=True, label="Contact number")
    _email(email, "agency_email", errors, required=True, label="Agency email")
    if not address:
        errors["agency_address"] = "Office address is required."
    if errors:
        raise ValidationError(errors)
    return {
        "agency_name": name,
        "agency_type": agency_type,
        "jurisdiction": jurisdiction,
        "psap_phone": phone,
        "agency_email": email,
        "agency_address": address,
        "agency_description": description or None,
    }


def password_requirements(role):
    """(key, label) pairs shown on the Security page; password_problems() returns the unmet keys."""
    return [
        ("length", f"Minimum {min_password(role)} characters"),
        ("upper", "At least 1 uppercase letter"),
        ("lower", "At least 1 lowercase letter"),
        ("digit", "At least 1 number"),
        ("special", "At least 1 special character"),
    ]


def password_problems(password, role):
    problems = []
    if len(password) < min_password(role):
        problems.append("length")
    if not any(c.isupper() for c in password):
        problems.append("upper")
    if not any(c.islower() for c in password):
        problems.append("lower")
    if not any(c.isdigit() for c in password):
        problems.append("digit")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        problems.append("special")
    return problems


def validate_password_change(payload, role):
    errors = {}
    current = payload.get("current_password")
    new = payload.get("new_password")
    confirm = payload.get("confirm_password")
    current = current if isinstance(current, str) else ""
    new = new if isinstance(new, str) else ""
    if not current:
        errors["current_password"] = "Enter your current password."
    if not new:
        errors["new_password"] = "Enter a new password."
    elif len(new) > MAX_PASSWORD:
        errors["new_password"] = f"Must be {MAX_PASSWORD} characters or fewer."
    elif password_problems(new, role):
        errors["new_password"] = "The new password does not meet all the requirements."
    elif new == current:
        errors["new_password"] = "The new password must be different from the current one."
    if new and confirm != new:
        errors["confirm_password"] = "Passwords do not match."
    if errors:
        raise ValidationError(errors)
    return current, new


def new_code(groups):
    """Random code in groups of four, e.g. K7QM-2XHD. 20 bits per group."""
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(groups))


def normalize_code(code):
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def hash_code(user_id, code):
    """What is stored instead of a recovery or reset code. Bound to the account it was issued for."""
    return hashlib.sha256(f"{user_id}:{normalize_code(code)}".encode()).hexdigest()


def generate_recovery_codes():
    return [new_code(4) for _ in range(RECOVERY_CODE_COUNT)]   # 80 bits each


def clean_factor_name(name):
    name = " ".join(name.split())[:40] if isinstance(name, str) else ""
    return name or "Authenticator"


def validate_agency_admin_fields(payload):
    """Extra fields only a system administrator may set on an agency."""
    errors = {}
    code = payload.get("ori_code")
    code = code.strip().upper() if isinstance(code, str) else ""
    if code and not ORI_RE.match(code):
        errors["ori_code"] = "Use up to 9 letters, digits or dashes."
    status = payload.get("status")
    if status not in AGENCY_STATUSES:
        errors["status"] = "Choose a status."
    if errors:
        raise ValidationError(errors)
    return {"ori_code": code or None, "status": status}


def validate_push_subscription(payload):
    """A PushSubscription.toJSON() from the browser. Only https push-service endpoints are accepted."""
    keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    endpoint, p256dh, auth = payload.get("endpoint"), keys.get("p256dh"), keys.get("auth")
    ok = (isinstance(endpoint, str) and endpoint.startswith("https://") and 10 <= len(endpoint) <= 1000
          and isinstance(p256dh, str) and 10 <= len(p256dh) <= 200
          and isinstance(auth, str) and 10 <= len(auth) <= 100)
    if not ok:
        raise ValidationError({"subscription": "This browser sent an invalid push subscription."})
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


def valid_totp_code(code):
    return isinstance(code, str) and len(code) == 6 and code.isdigit()


def validate_display_scale(payload):
    """One of the offered sizes, as a real number: "110" or 110.0 are not accepted."""
    scale = payload.get("display_scale")
    if isinstance(scale, bool) or not isinstance(scale, int) or scale not in DISPLAY_SCALES:
        raise ValidationError({"display_scale": "Choose one of the listed sizes."})
    return scale


def display_scale_from_row(row):
    """The saved size, or the default for anyone who has not chosen one (or whose row holds something else)."""
    scale = (row or {}).get("display_scale")
    return scale if scale in DISPLAY_SCALES else DEFAULT_DISPLAY_SCALE


def validate_preferences(payload):
    """Every preference must be present and a real boolean, so a partial post can't silently flip one."""
    errors = {k: "Choose on or off." for k in PREFERENCE_FIELDS if not isinstance(payload.get(k), bool)}
    if errors:
        raise ValidationError(errors)
    return {k: payload[k] for k in PREFERENCE_FIELDS}


def preferences_from_row(row):
    """Stored preferences, with the defaults for anyone who has not saved yet."""
    row = row or {}
    return {k: row[k] if isinstance(row.get(k), bool) else DEFAULT_PREFERENCES[k] for k in PREFERENCE_FIELDS}


def sniff_image(data):
    """MIME type from the file's own bytes; the browser-supplied type is not trusted."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data):
    if not data:
        raise ValidationError({"file": "Choose an image to upload."})
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationError({"file": "The image must be 2 MB or smaller."})
    mime = sniff_image(data)
    if mime not in IMAGE_TYPES:
        raise ValidationError({"file": "Use a PNG, JPEG or WebP image."})
    return mime, IMAGE_TYPES[mime]


def initials(name):
    parts = (name or "User").split()
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()
