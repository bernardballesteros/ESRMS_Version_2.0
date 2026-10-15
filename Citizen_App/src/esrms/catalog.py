"""Emergency categories and incident types shown on the dashboard.

Edit this list to change what citizens can report. Each report is stored in
emergency_incidents.emergency_type as "<category label>: <incident type>",
which is what the dispatcher dashboard shows on its map and incident list.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    subtitle: str
    icon: str        # ft.Icons name
    color: str       # accent colour, see ui/theme.py
    badge: str       # small status pill on the dashboard card
    incident_types: tuple


CATEGORIES = (
    Category(
        key="medical",
        label="Medical & EMS",
        subtitle="Paramedics, cardiac, critical care",
        icon="MONITOR_HEART",
        color="red",
        badge="EMS",
        incident_types=(
            "Medical emergency",
            "Difficulty breathing",
            "Unconscious person",
            "Injury or accident",
            "Other medical emergency",
        ),
    ),
    Category(
        key="police",
        label="Police & Tactical",
        subtitle="Theft, violence, suspicious activity",
        icon="LOCAL_POLICE",
        color="blue",
        badge="DISPATCH 911",
        incident_types=(
            "Theft or robbery",
            "Violence or assault",
            "Suspicious activity",
            "Security emergency",
            "Other police emergency",
        ),
    ),
    Category(
        key="fire",
        label="Fire & Hazmat",
        subtitle="Structure fires, smoke, toxic gas",
        icon="LOCAL_FIRE_DEPARTMENT",
        color="amber",
        badge="FIRE",
        incident_types=(
            "Building fire",
            "Vehicle fire",
            "Smoke or suspected fire",
            "Hazardous material incident",
            "Other fire or hazardous-material emergency",
        ),
    ),
    Category(
        key="roads",
        label="Contacts & Roads",
        subtitle="Accidents, obstructions, roadside help",
        icon="ADD_ROAD",
        color="green",
        badge="ROADS",
        incident_types=(
            "Road accident",
            "Road obstruction",
            "Traffic hazard",
            "Roadside assistance request",
            "Other road-related emergency",
        ),
    ),
)

# Used when the citizen presses SOS without choosing a category first.
GENERAL_SOS = "General SOS"

BY_KEY = {c.key: c for c in CATEGORIES}


def emergency_type(category_key, incident_type):
    """The emergency_type text stored for a report (max 80 chars in the database)."""
    if category_key is None:
        return GENERAL_SOS
    category = BY_KEY[category_key]
    if incident_type not in category.incident_types:
        raise ValueError(f"Unknown incident type for {category.label}: {incident_type!r}")
    return f"{category.label}: {incident_type}"[:80]
