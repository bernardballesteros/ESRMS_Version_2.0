"""Rules for My Profile and Security & MFA. No Flet or network code; tested in tests/test_account.py.

The same limits as the dispatcher dashboard (EMERGENCY/app/account.py) so both accept the same
values. These checks are for quick feedback; Supabase Auth, Row Level Security and the storage
bucket's own limits are what actually enforce them.
"""
import re

NAME_MAX, PHONE_MAX, ADDRESS_MAX, EMAIL_MAX = 120, 32, 200, 254
MIN_PASSWORD, MAX_PASSWORD = 8, 72   # 72 = bcrypt limit used by Supabase Auth
MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
GOOGLE_SECURITY_URL = "https://myaccount.google.com/security"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{5,30}$")

PASSWORD_RULES = (  # (label, check)
    (f"At least {MIN_PASSWORD} characters", lambda p: len(p) >= MIN_PASSWORD),
    ("An uppercase letter", lambda p: any(c.isupper() for c in p)),
    ("A lowercase letter", lambda p: any(c.islower() for c in p)),
    ("A number", lambda p: any(c.isdigit() for c in p)),
    ("A symbol (for example ! ? # @)", lambda p: any(not c.isalnum() and not c.isspace() for c in p)),
)


class ValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def initials(name, email=""):
    words = (name or "").split()
    if words:
        return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
    return (email or "?")[:2].upper()


def validate_profile(full_name, email, phone, address):
    """Return the cleaned fields or raise ValidationError({field: message})."""
    errors = {}
    full_name = " ".join((full_name or "").split())
    email = (email or "").strip().lower()
    phone = " ".join((phone or "").split())
    address = " ".join((address or "").split())
    if not full_name:
        errors["full_name"] = "Full name is required."
    elif len(full_name) > NAME_MAX:
        errors["full_name"] = f"Must be {NAME_MAX} characters or fewer."
    if not email:
        errors["email"] = "Email address is required."
    elif len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."
    if phone and (len(phone) > PHONE_MAX or not PHONE_RE.match(phone) or sum(c.isdigit() for c in phone) < 7):
        errors["phone"] = "Enter a valid phone number, for example +63 912 345 6789."
    if len(address) > ADDRESS_MAX:
        errors["address"] = f"Must be {ADDRESS_MAX} characters or fewer."
    if errors:
        raise ValidationError(errors)
    return {"full_name": full_name, "email": email, "phone": phone, "address": address}


def password_checks(password):
    """[(label, satisfied)] for the requirement checklist."""
    return [(label, bool(password) and check(password)) for label, check in PASSWORD_RULES]


def validate_password_change(current, new, confirm):
    errors = {}
    current, new, confirm = current or "", new or "", confirm or ""
    if not current:
        errors["current"] = "Enter your current password."
    if not new:
        errors["new"] = "Enter a new password."
    elif len(new) > MAX_PASSWORD:
        errors["new"] = f"Must be {MAX_PASSWORD} characters or fewer."
    elif not all(ok for _, ok in password_checks(new)):
        errors["new"] = "The new password does not meet all the requirements."
    elif new == current:
        errors["new"] = "The new password must be different from the current one."
    if new and confirm != new:
        errors["confirm"] = "Passwords do not match."
    if errors:
        raise ValidationError(errors)


def clean_code(code):
    """A 6-digit authenticator code with spaces removed, or raise."""
    digits = "".join(ch for ch in (code or "") if not ch.isspace())
    if len(digits) != 6 or not digits.isdigit():
        raise ValidationError({"code": "Enter the 6-digit code from your authenticator app."})
    return digits


def sniff_image(data):
    """Type from the file's own bytes, not its name."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data):
    """(mime, extension) for an allowed picture, else ValidationError."""
    if not data:
        raise ValidationError({"file": "Choose a picture."})
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationError({"file": "The picture must be 2 MB or smaller."})
    mime = sniff_image(data)
    if mime not in IMAGE_TYPES:
        raise ValidationError({"file": "Use a PNG, JPEG or WebP picture."})
    return mime, IMAGE_TYPES[mime]


def sign_in_methods(providers, is_anonymous=False):
    """What Supabase Auth says this account signs in with: (labels, has_password, google_only)."""
    if is_anonymous:
        return ["Quick access (no account)"], False, False
    names = {"email": "Email and password", "google": "Google"}
    labels = [names.get(p, p.title()) for p in providers] or ["Email and password"]
    has_password = "email" in providers or not providers
    return labels, has_password, ("google" in providers and not has_password)
