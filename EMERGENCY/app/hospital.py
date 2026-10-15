"""Hospital Signal and Hospital Portal rules: the patient report's statuses, who may move it
where, trauma bays, registration and validation.

Free of Flask/Supabase so they can be unit tested (tests/test_hospital.py, tests/test_hospital_portal.py).

A patient report is ONE row of hospital_signals from the dispatcher's Send to the archived
handoff. Its status is its own thing and never changes the incident's status:

    sent ──> acknowledged ──> accepted ──> arrived ──> handoff ──> completed
    (incoming)               (preparing)
      │            │              │
      └────────────┴──────────────┴──> unable     (hospital, reason required)
      └────────────┴──────────────┴──> cancelled  (dispatcher)

The hospital may skip the early steps (a patient can arrive before anyone acknowledged), but a
handoff can only start after arrival and only a started handoff can be completed. completed,
unable and cancelled are final: the report is then read-only. 'received' is the final status
used before the portal existed and is treated as completed.
"""
import math
import re
from datetime import datetime, timedelta, timezone

from app.dispatch import PHONE_RE, ValidationError, valid_coordinates

PHT = timezone(timedelta(hours=8), "PHT")

SIGNAL_TYPES = {
    "incoming_patient": "Incoming Patient",
    "emergency_admission": "Emergency Admission",
    "bed_request": "Bed Request",
    "critical_patient": "Critical Patient",
    "other": "Other",
}
# Sent from the Dispatch Center to a non-hospital organization (app/organizations.py). Same table, own workflow:
#     sent ──> acknowledged ──> accepted ──> responding ──> on_scene ──> completed
RESPONSE_TYPES = {
    "medical_response": "Medical Response Request",
    "fire_response": "Fire Response Request",
    "police_response": "Police Response Request",
    "barangay_response": "Barangay Response Request",
    "response_request": "Emergency Response Request",
}
ALL_SIGNAL_TYPES = {**SIGNAL_TYPES, **RESPONSE_TYPES}
GENDERS = {"male": "Male", "female": "Female", "other": "Other", "undisclosed": "Prefer not to say"}
# What the dispatcher reports seeing. A description for the hospital, not a diagnosis.
CONDITIONS = {
    "stable": "Stable",
    "serious": "Serious",
    "critical": "Critical",
    "unconscious": "Unconscious",
    "injured": "Injured",
    "other": "Other",
}
TRANSPORT_TYPES = {
    "ground_als": "Ground ambulance (ALS)",
    "ground_bls": "Ground ambulance (BLS)",
    "air": "Air ambulance",
    "private": "Private vehicle",
    "other": "Other",
}
TRIAGE = {1: "P1 · Critical", 2: "P2 · Urgent", 3: "P3 · Moderate", 4: "P4 · Minor"}

# Labels on the dispatcher's side.
STATUSES = {
    "sent": "Sent",
    "acknowledged": "Acknowledged",
    "accepted": "Accepted · Preparing",
    "arrived": "Patient Arrived",
    "handoff": "Handoff",
    "responding": "Responding",
    "on_scene": "On Scene",
    "completed": "Completed",
    "received": "Received",
    "unable": "Unable to Receive",
    "cancelled": "Cancelled",
}
# The same statuses as the receiving hospital reads them.
PORTAL_STATUSES = {**STATUSES, "sent": "Incoming", "accepted": "Preparing", "arrived": "Arrived", "received": "Completed"}
ACTIVE = ("sent", "acknowledged", "accepted", "arrived", "handoff", "responding", "on_scene")
# A new report is only a Hospital Signal. It becomes an Incoming Report once the hospital has acknowledged it
# (or taken any later step, which implies acknowledgement).
INCOMING = ("acknowledged", "accepted", "arrived", "handoff", "responding", "on_scene")
DONE = ("completed", "received")
FINAL = {"completed", "received", "unable", "cancelled"}
# Incident Report → Recent Signals: quick filters over the same reports.
ARCHIVE_FILTERS = {
    "critical": "Critical",
    "hospital_alerts": "Hospital Alerts",
    "incoming_patients": "Incoming Patients",
    "acknowledged": "Acknowledged",
    "completed": "Completed",
    "cancelled": "Cancelled",
}
# Signals that alert the hospital without announcing a patient already on the way.
ALERT_TYPES = ("emergency_admission", "bed_request", "other")

AVAILABILITY = {"ready": "Ready", "limited": "Limited", "full": "Full", "offline": "Offline"}
ORG_TYPE_KEYS = ("ems", "fire", "police", "barangay", "hospital", "other")
HOSPITAL_TYPES = {
    "government": "Government hospital",
    "private": "Private hospital",
    "specialty": "Specialty center",
    "clinic": "Clinic / infirmary",
    "military": "Military hospital",
    "other": "Other",
}
STAFF_ROLES = {
    "admin": "Hospital Administrator",
    "reception": "Medical Reception",
    "nurse": "ER Nurse",
    "physician": "Emergency Physician",
    "receiving": "Dispatcher / Receiving Staff",
}
BAY_KINDS = {"trauma": "Trauma bay", "er": "Emergency room", "other": "Other"}
BAY_STORED = {"ready": "Ready", "cleaning": "Cleaning"}
BAY_STATUSES = {**BAY_STORED, "preparing": "Preparing", "occupied": "Occupied"}
DEFAULT_BAYS = (("Trauma Bay 1", "trauma"), ("Trauma Bay 2", "trauma"), ("Trauma Bay 3", "trauma"),
                ("Emergency Room 1", "er"), ("Emergency Room 2", "er"))
REJECTION_REASONS = ("No available bed", "Facility unavailable", "Required service or specialist not available", "Other")

EVENTS = {
    "REPORT_RECEIVED": "Hospital notified · report received",
    "REPORT_ACKNOWLEDGED": "Hospital acknowledged",
    "BAY_PREPARATION_STARTED": "Bay preparation started",
    "TRAUMA_BAY_ASSIGNED": "Bay assigned",
    "TRAUMA_BAY_RELEASED": "Bay released",
    "PATIENT_ARRIVED": "Patient arrived",
    "HANDOFF_STARTED": "Handoff started",
    "HANDOFF_COMPLETED": "Handoff completed",
    "REPORT_ARCHIVED": "Report archived",
    "UNABLE_TO_RECEIVE": "Hospital unable to receive",
    "REPORT_CANCELLED": "Cancelled by dispatcher",
    "SIGNAL_ARCHIVED": "Moved to Signal History",
    "SIGNAL_RESTORED": "Restored to Hospital Signal",
    "REQUEST_ACCEPTED": "Request accepted",
    "ORG_RESPONDING": "Organization responding",
    "ORG_ON_SCENE": "On scene",
    "RESPONSE_COMPLETED": "Response completed",
}
# The shared events as they read on a response request to an organization.
RESPONSE_EVENTS = {
    "REPORT_RECEIVED": "Signal sent · organization notified",
    "REPORT_ACKNOWLEDGED": "Organization acknowledged",
    "UNABLE_TO_RECEIVE": "Organization unable to respond",
}

NOTES_LIMIT = 500
REASON_LIMIT = 300
HANDOFF_NOTES_LIMIT = 1000
MIN_PASSWORD = 12
ETA_PAST_GRACE = timedelta(minutes=10)
ETA_MAX_AHEAD = timedelta(days=2)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# action -> (who, statuses it may start from, resulting status, columns stamped with actor and time, event)
ACTIONS = {
    "acknowledge": ("hospital", ("sent",), "acknowledged", "acknowledged", "REPORT_ACKNOWLEDGED"),
    "accept": ("hospital", ("sent", "acknowledged"), "accepted", "accepted", "BAY_PREPARATION_STARTED"),
    "arrive": ("hospital", ("sent", "acknowledged", "accepted"), "arrived", "arrived", "PATIENT_ARRIVED"),
    "handoff": ("hospital", ("arrived",), "handoff", "handoff_started", "HANDOFF_STARTED"),
    "complete": ("hospital", ("handoff",), "completed", "completed", "HANDOFF_COMPLETED"),
    "unable": ("hospital", ("sent", "acknowledged", "accepted"), "unable", "rejected", "UNABLE_TO_RECEIVE"),
    "cancel": ("dispatcher", ("sent", "acknowledged", "accepted"), "cancelled", "cancelled", "REPORT_CANCELLED"),
}

# The organization may skip early steps, but only a response that has started can be completed.
RESPONSE_ACTIONS = {
    "acknowledge": ("hospital", ("sent",), "acknowledged", "acknowledged", "REPORT_ACKNOWLEDGED"),
    "accept": ("hospital", ("sent", "acknowledged"), "accepted", "accepted", "REQUEST_ACCEPTED"),
    "respond": ("hospital", ("sent", "acknowledged", "accepted"), "responding", "responding", "ORG_RESPONDING"),
    "on_scene": ("hospital", ("sent", "acknowledged", "accepted", "responding"), "on_scene", "on_scene", "ORG_ON_SCENE"),
    "complete": ("hospital", ("responding", "on_scene"), "completed", "completed", "RESPONSE_COMPLETED"),
    "unable": ACTIONS["unable"],
    "cancel": ACTIONS["cancel"],
}

# Optional EMS numbers: field -> (label, lowest, highest, whole numbers only)
VITALS = {
    "heart_rate": ("Heart rate", 0, 300, True),
    "bp_systolic": ("Systolic pressure", 0, 350, True),
    "bp_diastolic": ("Diastolic pressure", 0, 250, True),
    "spo2": ("SpO2", 0, 100, True),
    "temperature": ("Temperature", 20, 46, False),
    "respiratory_rate": ("Respiratory rate", 0, 120, True),
}


class TransitionError(Exception):
    """The requested action is not allowed from the report's current status."""


def signal_code(row):
    n = row.get("signal_number")
    return f"HS-{int(n):06d}" if n is not None else "HS-?"


def is_response(row):
    """A response request to an organization, as opposed to a patient report to a hospital."""
    return bool(row) and row.get("signal_type") in RESPONSE_TYPES


def actions_for(row):
    return RESPONSE_ACTIONS if is_response(row) else ACTIONS


def allowed_actions(status, side, row=None):
    """Actions `side` ('hospital' or 'dispatcher') may take on a report in `status`."""
    return [name for name, spec in actions_for(row).items() if spec[0] == side and status in spec[1]]


def is_critical(row):
    """Shown under Critical Priority: triaged P1, or reported critical / unconscious."""
    return (row.get("triage_priority") == 1 or row.get("patient_condition") in ("critical", "unconscious")
            or row.get("signal_type") == "critical_patient")


def _clean(value, limit, name, errors, required=None):
    value = " ".join(value.split()) if isinstance(value, str) else ""
    if len(value) > limit:
        errors[name] = f"Must be {limit} characters or fewer."
    elif required and not value:
        errors[name] = required
    return value


def transition(row, action, actor_id, side, data=None, now=None):
    """Column changes for `action`, or raise. The caller applies them only if the status is still row['status']."""
    spec = actions_for(row).get(action)
    response = is_response(row)
    if not spec or spec[0] != side:
        raise TransitionError("That action is not available.")
    _, sources, target, stamp, _ = spec
    current = row.get("status")
    labels = PORTAL_STATUSES if side == "hospital" else STATUSES
    if current not in sources:
        if current in FINAL:
            raise TransitionError("This report is archived and read-only.")
        if current == target:
            raise TransitionError(f"This report is already {labels[target].lower()}.")
        if action == "complete" and response:
            raise TransitionError("Mark the organization as responding or on scene before completing the response.")
        if action == "handoff":
            raise TransitionError("Mark the patient as arrived before starting the handoff.")
        if action == "complete":
            raise TransitionError("Start the handoff before completing it.")
        raise TransitionError(f"A report that is {labels.get(current, current).lower()} cannot be changed this way.")
    data = data if isinstance(data, dict) else {}
    when = (now or datetime.now(timezone.utc)).isoformat()
    changes = {"status": target, f"{stamp}_by": actor_id, f"{stamp}_at": when}
    errors = {}
    if action == "unable":
        changes["rejection_reason"] = _clean(data.get("reason"), REASON_LIMIT, "reason", errors,
                                             "Give a reason before marking a report as unable to receive.")
    if action == "complete" and not response:
        changes["receiving_staff"] = _clean(data.get("receiving_staff"), 120, "receiving_staff", errors,
                                            "Enter the nurse or doctor who received the patient.")
        notes = data.get("handoff_notes")
        notes = notes.strip() if isinstance(notes, str) else ""
        if len(notes) > HANDOFF_NOTES_LIMIT:
            errors["handoff_notes"] = f"Keep the notes to {HANDOFF_NOTES_LIMIT} characters or fewer."
        changes["handoff_notes"] = notes or None
    if action == "complete" and response:
        notes = data.get("handoff_notes")
        notes = notes.strip() if isinstance(notes, str) else ""
        if len(notes) > HANDOFF_NOTES_LIMIT:
            errors["handoff_notes"] = f"Keep the notes to {HANDOFF_NOTES_LIMIT} characters or fewer."
        changes["handoff_notes"] = notes or None
    if errors:
        raise ValidationError(errors)
    # Any later step implies the hospital has seen the report.
    if action != "acknowledge" and side == "hospital" and not row.get("acknowledged_at"):
        changes.update(acknowledged_by=actor_id, acknowledged_at=when)
    return changes


def _text(payload, name, limit, errors):
    value = payload.get(name)
    value = value.strip() if isinstance(value, str) else ""
    if len(value) > limit:
        errors[name] = f"Must be {limit} characters or fewer."
    return value


def parse_eta(date_text, time_text, now=None):
    """ETA as an aware datetime from the form's PHT date and time. Both empty -> None."""
    date_text, time_text = (date_text or "").strip(), (time_text or "").strip()
    if not date_text and not time_text:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date() if date_text else now.astimezone(PHT).date()
        clock = datetime.strptime(time_text, "%H:%M").time()
    except ValueError:
        raise ValidationError({"eta_time": "Enter the arrival time as HH:MM and a valid date."}) from None
    eta = datetime.combine(day, clock, PHT)
    if eta < now - ETA_PAST_GRACE:
        raise ValidationError({"eta_time": "The estimated arrival time is in the past."})
    if eta > now + ETA_MAX_AHEAD:
        raise ValidationError({"eta_date": "The estimated arrival is more than two days away. Check the date."})
    return eta


def _number(payload, name, low, high, whole, errors, label):
    value = payload.get(name)
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or not low <= number <= high or (whole and not number.is_integer()):
            raise ValueError
    except (TypeError, ValueError):
        errors[name] = f"{label} must be {'a whole number ' if whole else ''}between {low} and {high}, or left blank."
        return None
    return int(number) if whole else round(number, 1)


def validate_signal(payload, now=None):
    """Clean values for a new report. Hospital and incident ids are checked against the database by the caller."""
    errors = {}
    row = {
        "hospital_id": payload.get("hospital_id") if isinstance(payload.get("hospital_id"), str) else "",
        "incident_id": payload.get("incident_id") if isinstance(payload.get("incident_id"), str) and payload.get("incident_id") else None,
        "signal_type": payload.get("signal_type"),
        "patient_condition": payload.get("patient_condition"),
        "patient_name": _text(payload, "patient_name", 120, errors) or None,
        "additional_notes": _text(payload, "additional_notes", NOTES_LIMIT, errors) or None,
        "patient_gender": payload.get("patient_gender") or None,
        "patient_age": _number(payload, "patient_age", 0, 130, True, errors, "Age"),
        "medic_unit": _text(payload, "medic_unit", 40, errors) or None,
        "chief_complaint": _text(payload, "chief_complaint", 200, errors) or None,
        "transport_type": payload.get("transport_type") or None,
        "triage_priority": _number(payload, "triage_priority", 1, 4, True, errors, "Priority"),
    }
    if "patient_age" in errors:
        errors["patient_age"] = "Enter an age between 0 and 130, or leave it blank."
    for name, (label, low, high, whole) in VITALS.items():
        row[name] = _number(payload, name, low, high, whole, errors, label)
    if not row["hospital_id"]:
        errors["hospital_id"] = "Select the receiving hospital."
    if row["signal_type"] not in SIGNAL_TYPES:
        errors["signal_type"] = "Choose a signal type."
    if row["patient_condition"] not in CONDITIONS:
        errors["patient_condition"] = "Select the patient's condition."
    if row["patient_gender"] is not None and row["patient_gender"] not in GENDERS:
        errors["patient_gender"] = "Choose a listed option."
    if row["transport_type"] is not None and row["transport_type"] not in TRANSPORT_TYPES:
        errors["transport_type"] = "Choose a listed transport type."
    if row["bp_systolic"] is not None and row["bp_diastolic"] is not None and row["bp_diastolic"] > row["bp_systolic"]:
        errors["bp_diastolic"] = "Diastolic pressure cannot be higher than systolic."
    try:
        eta = parse_eta(payload.get("eta_date"), payload.get("eta_time"), now)
        row["eta"] = eta.astimezone(timezone.utc).isoformat() if eta else None
    except ValidationError as exc:
        errors.update(exc.errors)
    request_id = payload.get("client_request_id")
    row["client_request_id"] = request_id if isinstance(request_id, str) and len(request_id) == 36 else None
    if errors:
        raise ValidationError(errors)
    return row


def _phone(value, name, errors, message="Use digits, spaces, dashes or parentheses, optionally starting with +."):
    if value and (not PHONE_RE.match(value) or sum(ch.isdigit() for ch in value) < 3):
        errors[name] = message


def validate_hospital(payload):
    """A hospital as a dispatcher adds or edits it."""
    errors = {}
    name = " ".join(_text(payload, "name", 120, errors).split())
    address = _text(payload, "address", 300, errors)
    phone = _text(payload, "phone_number", 25, errors)
    availability = payload.get("availability") or "ready"
    if not name:
        errors["name"] = "Hospital name is required."
    _phone(phone, "phone_number", errors)
    if availability not in AVAILABILITY:
        errors["availability"] = "Choose an availability status."
    lat, lng = payload.get("latitude"), payload.get("longitude")
    lat = None if lat in (None, "") else lat
    lng = None if lng in (None, "") else lng
    if (lat is None) != (lng is None):
        errors["latitude"] = "Enter both latitude and longitude, or leave both blank."
    elif lat is not None and not valid_coordinates(lat, lng):
        errors["latitude"] = "Enter valid coordinates, for example 7.0731 and 125.6128."
    if errors:
        raise ValidationError(errors)
    row = {
        "name": name, "address": address or None, "phone_number": phone or None, "availability": availability,
        "latitude": float(lat) if lat is not None else None, "longitude": float(lng) if lng is not None else None,
        "is_active": payload.get("is_active") is not False,
    }
    # Organization fields are only touched when the form sent them (the hospital dialog does not).
    if "org_type" in payload:
        if payload["org_type"] not in ORG_TYPE_KEYS:
            raise ValidationError({"org_type": "Choose an organization type."})
        row["org_type"] = payload["org_type"]
    for field, limit in (("base_name", 120), ("city", 100), ("province", 100)):
        if field in payload:
            value = _clean(payload.get(field), limit, field, errors)
            row[field] = value or None
    if errors:
        raise ValidationError(errors)
    return row


def _hospital_profile(payload, errors):
    """Fields shared by registration and Settings > Hospital Profile."""
    row = {
        "name": _clean(payload.get("hospital_name"), 120, "hospital_name", errors, "Hospital name is required."),
        "address": _clean(payload.get("address"), 300, "address", errors, "Hospital address is required."),
        "city": _clean(payload.get("city"), 100, "city", errors, "City is required."),
        "province": _clean(payload.get("province"), 100, "province", errors, "Province is required."),
        "country": _clean(payload.get("country"), 100, "country", errors, "Country is required."),
        "phone_number": _clean(payload.get("contact_number"), 25, "contact_number", errors, "Contact number is required."),
        "official_email": _clean(payload.get("official_email"), 254, "official_email", errors, "Official email is required.").lower(),
        "ed_contact": _clean(payload.get("ed_contact"), 120, "ed_contact", errors, "Emergency Department contact is required."),
    }
    if "contact_number" not in errors:
        _phone(row["phone_number"], "contact_number", errors)
    if "official_email" not in errors and not EMAIL_RE.match(row["official_email"]):
        errors["official_email"] = "Enter a valid email address."
    return row


def validate_profile(payload):
    errors = {}
    row = _hospital_profile(payload, errors)
    if errors:
        raise ValidationError(errors)
    return row


def password_problem(password):
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password) and re.search(r"\d", password)):
        return "Include an uppercase letter, a lowercase letter and a number."
    return None


def validate_registration(form):
    """(hospital row, account) from the Register Your Hospital form, or raise with every problem found."""
    errors = {}
    hospital = _hospital_profile(form, errors)
    hospital["hospital_type"] = form.get("hospital_type")
    if hospital["hospital_type"] not in HOSPITAL_TYPES:
        errors["hospital_type"] = "Select the hospital type."
    hospital["license_number"] = _clean(form.get("license_number"), 60, "license_number", errors) or None
    account = {
        "full_name": _clean(form.get("admin_name"), 120, "admin_name", errors, "Administrator full name is required."),
        "position": _clean(form.get("position"), 80, "position", errors, "Position / role is required."),
        "email": _clean(form.get("email"), 254, "email", errors, "Email is required.").lower(),
        "password": form.get("password") or "",
    }
    if "email" not in errors and not EMAIL_RE.match(account["email"]):
        errors["email"] = "Enter a valid email address."
    problem = password_problem(account["password"])
    if problem:
        errors["password"] = problem
    elif account["password"] != (form.get("confirm_password") or ""):
        errors["confirm_password"] = "The passwords do not match."
    if not form.get("agree"):
        errors["agree"] = "You must agree to the Hospital Portal Terms and Privacy Policy."
    if errors:
        raise ValidationError(errors)
    return hospital, account


def validate_staff(payload, new_account=True):
    errors = {}
    row = {
        "email": _clean(payload.get("email"), 254, "email", errors, "Email is required.").lower(),
        "full_name": _clean(payload.get("full_name"), 120, "full_name", errors, "Full name is required." if new_account else None),
        "position": _clean(payload.get("position"), 80, "position", errors) or None,
        "staff_role": payload.get("staff_role"),
        "password": payload.get("password") or "",
    }
    if "email" not in errors and not EMAIL_RE.match(row["email"]):
        errors["email"] = "Enter a valid email address."
    if row["staff_role"] not in STAFF_ROLES:
        errors["staff_role"] = "Choose a role."
    if new_account:
        problem = password_problem(row["password"])
        if problem:
            errors["password"] = problem
    if errors:
        raise ValidationError(errors)
    return row


def validate_bay(payload):
    errors = {}
    name = _clean(payload.get("name"), 60, "name", errors, "Give the bay a name.")
    kind = payload.get("kind") or "trauma"
    if kind not in BAY_KINDS:
        errors["kind"] = "Choose a bay type."
    if errors:
        raise ValidationError(errors)
    return {"name": name, "kind": kind}


def bay_state(bay, report=None):
    """What staff see for a bay: Preparing or Occupied while a report holds it, otherwise what was last set."""
    if report and report.get("status") in ACTIVE:
        return "occupied" if report["status"] in ("arrived", "handoff") else "preparing"
    return bay.get("status") if bay.get("status") in BAY_STORED else "ready"


def parse_ts(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def day_start(now=None, days_back=0):
    """Start of the PHT day `days_back` days ago, as an aware datetime."""
    local = (now or datetime.now(timezone.utc)).astimezone(PHT)
    return datetime(local.year, local.month, local.day, tzinfo=PHT) - timedelta(days=days_back)


def dashboard_stats(active_rows, completed_today):
    """The six dashboard counters, from this hospital's active reports and today's completed count."""
    by = lambda *statuses: sum(1 for r in active_rows if r.get("status") in statuses)  # noqa: E731
    return {
        "incoming": by("acknowledged", "accepted"),
        "critical": sum(1 for r in active_rows if is_critical(r)),
        "preparing": by("accepted"),
        "arrived": by("arrived"),
        "pending_handoffs": by("arrived", "handoff"),
        "responding": by("responding"),
        "on_scene": by("on_scene"),
        "completed_today": completed_today,
    }


def sort_incoming(rows):
    """Critical first, then soonest ETA, then oldest report."""
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda r: (not is_critical(r), parse_ts(r.get("eta")) or far, r.get("sent_at") or ""))


def distance_km(lat1, lng1, lat2, lng2):
    """Straight-line distance, or None unless both points are real coordinates. Not a driving distance."""
    if not (valid_coordinates(lat1, lng1) and valid_coordinates(lat2, lng2)):
        return None
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(float(lng2) - float(lng1)) / 2) ** 2)
    return round(6371.0088 * 2 * math.asin(math.sqrt(a)), 1)
