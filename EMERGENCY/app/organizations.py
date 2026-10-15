"""Organization-based dispatch rules: which registered organization BASE suits an emergency, and how near it is.

Free of Flask/Supabase so they can be unit tested (tests/test_organizations.py).

An organization (EMS, fire station, police station, barangay response center, hospital, other) is one row of
the hospitals table with a fixed base latitude/longitude. Nothing here knows about individual responders:
the only distance ever measured is incident location -> organization base.
"""
from app.dispatch import valid_coordinates
from app.hospital import RESPONSE_TYPES, distance_km  # noqa: F401  (RESPONSE_TYPES re-exported)

ORG_TYPES = {"ems": "EMS", "fire": "Fire", "police": "Police", "barangay": "Barangay", "hospital": "Hospital", "other": "Other"}
BASE_LABELS = {"ems": "EMS Base", "fire": "Fire Station", "police": "Police Station",
               "barangay": "Barangay Response Center", "hospital": "Hospital", "other": "Other Organization"}
# The stored availability values are the hospital ones; dispatch reads them as the organization's status.
AVAILABILITY = {"ready": "Available", "limited": "Busy", "full": "Unavailable", "offline": "Offline"}
AVAILABLE, BUSY = "ready", "limited"

# Response request sent to each kind of organization. Hospitals get a patient report from Hospital Signal instead.
SIGNAL_FOR = {"ems": "medical_response", "fire": "fire_response", "police": "police_response",
              "barangay": "barangay_response", "other": "response_request"}
MESSAGE_LIMIT = 500

# (label, words looked for in the emergency type, organization types to recommend, types offered as support).
# An organization type is its capability: an EMS base answers medical calls, a fire station fires, and so on.
RULES = (
    ("Fire", ("fire", "smoke", "hazmat", "explosion", "gas leak", "burning"), ("fire",), ("police", "ems", "barangay")),
    ("Traffic accident", ("accident", "collision", "crash", "road", "traffic", "vehic"), ("ems", "police"), ("fire", "barangay")),
    ("Crime / security", ("crime", "security", "robbery", "theft", "assault", "violence", "shooting", "stabbing", "police"),
     ("police",), ("barangay", "ems")),
    ("Disaster", ("flood", "earthquake", "landslide", "typhoon", "storm", "disaster", "evacuat"),
     ("barangay", "ems", "fire", "police", "hospital"), ()),
    ("Medical emergency", ("medical", "ems", "unconscious", "chest", "injur", "bleed", "breath", "stroke", "heart", "sick", "seizure"),
     ("ems",), ("barangay", "police")),
)
DEFAULT_RULE = ("Emergency", (), ("ems", "police", "fire", "barangay"), ())


def rule_for(emergency_type):
    """The rule matching an incident's type. The category before ':' decides first, then the whole text."""
    text = (emergency_type or "").lower()
    for part in (text.split(":", 1)[0], text):
        for rule in RULES:
            if any(word in part for word in rule[1]):
                return rule
    return DEFAULT_RULE


def recommend(incident, organizations):
    """Rank organization bases for one incident.

    Returns {state, category, primary, support, items, others}:
      items  - verified, active, suitable organizations that are Available or Busy, best first. Available ones come
               before Busy ones; within each, recommended types before support types, then nearest base first.
               The first item is marked recommended only if it is Available.
      others - every other active organization, for manual selection, each with why it was not recommended.
      state  - 'none_registered' | 'none_available' | 'ok'
    """
    label, _, primary, support = rule_for(incident.get("emergency_type"))
    located = valid_coordinates(incident.get("latitude"), incident.get("longitude"))
    items, others = [], []
    for org in organizations:
        if org.get("is_active") is False:
            continue
        kind = org.get("org_type") or "hospital"
        distance = (distance_km(incident["latitude"], incident["longitude"], org.get("latitude"), org.get("longitude"))
                    if located else None)
        entry = {"org": org, "distance_km": distance, "role": "primary" if kind in primary else "support" if kind in support else None}
        has_base = valid_coordinates(org.get("latitude"), org.get("longitude"))
        why = None
        if org.get("verified") is False:
            why = "Not verified yet"
        elif entry["role"] is None:
            why = f"{ORG_TYPES.get(kind, kind)} is not a usual responder for this type of emergency"
        elif org.get("availability") not in (AVAILABLE, BUSY):
            why = AVAILABILITY.get(org.get("availability"), "Unavailable")
        elif not has_base:
            why = "No base location recorded"
        if why:
            others.append({**entry, "note": why})
        else:
            items.append(entry)
    far = float("inf")
    by_distance = lambda e: (e["distance_km"] is None, e["distance_km"] if e["distance_km"] is not None else far,  # noqa: E731
                             (e["org"].get("name") or "").lower())
    items.sort(key=lambda e: (e["org"].get("availability") != AVAILABLE, e["role"] != "primary", *by_distance(e)))
    others.sort(key=by_distance)
    for rank, entry in enumerate(items):
        org = entry["org"]
        kind = ORG_TYPES.get(org.get("org_type") or "hospital", "")
        available = org.get("availability") == AVAILABLE
        entry["recommended"] = rank == 0 and available
        if not available:
            entry["reason"] = "Busy: not recommended first"
        elif not located:
            entry["reason"] = f"Suitable {kind} organization (incident has no GPS location, distance unknown)"
        elif entry["recommended"]:
            entry["reason"] = f"Nearest suitable {kind} organization"
        else:
            entry["reason"] = f"{kind} support" if entry["role"] == "support" else f"Suitable {kind} organization"
    state = ("none_registered" if not any(o.get("is_active") is not False for o in organizations)
             else "ok" if any(e["org"].get("availability") == AVAILABLE for e in items) else "none_available")
    return {"state": state, "category": label, "primary": list(primary), "support": list(support),
            "located": located, "items": items, "others": others}


def validate_request(payload, incident_priority=None):
    """(priority, message, client_request_id) for a response request, or raise ValidationError."""
    from app.dispatch import ValidationError
    errors = {}
    priority = payload.get("priority", incident_priority or 2)
    try:
        priority = int(priority)
        if not 1 <= priority <= 4:
            raise ValueError
    except (TypeError, ValueError):
        errors["priority"] = "Choose a priority."
    message = payload.get("message")
    message = message.strip() if isinstance(message, str) else ""
    if len(message) > MESSAGE_LIMIT:
        errors["message"] = f"Keep the message to {MESSAGE_LIMIT} characters or fewer."
    if errors:
        raise ValidationError(errors)
    request_id = payload.get("client_request_id")
    return priority, message or None, request_id if isinstance(request_id, str) and len(request_id) == 36 else None
