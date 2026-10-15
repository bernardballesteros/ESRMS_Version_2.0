"""Connection settings for the shared ESRMS Supabase project.

Only the PUBLIC anon key belongs here. It is safe in a mobile app because every table
it can touch is protected by Row Level Security. Never put SUPABASE_SERVICE_ROLE_KEY
in this app: it bypasses RLS and would expose every incident and citizen.

Lookup order: environment variables (desktop testing), then src/config.json (bundled into the APK).
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_anon_key: str
    emergency_hotline: str = "911"
    # Mobile number that an ESRMS operator actually reads SMS on. Empty = the citizen picks the recipient.
    sos_sms_number: str = ""
    # How long one GPS acquisition (SOS screen opened, Retry GPS pressed) waits for a fresh fix.
    gps_timeout_seconds: int = 30


def _looks_like_service_key(key):
    """Supabase JWT keys carry their role in the payload. Refuse anything that isn't anon."""
    import base64
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:
        return key.startswith("sb_secret_")
    return role not in (None, "anon")


def load():
    data = {}
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    url = os.environ.get("ESRMS_SUPABASE_URL") or data.get("supabase_url", "")
    key = os.environ.get("ESRMS_SUPABASE_ANON_KEY") or data.get("supabase_anon_key", "")
    hotline = os.environ.get("ESRMS_HOTLINE") or data.get("emergency_hotline") or "911"
    if not url or not key:
        raise ConfigError("Supabase URL / anon key missing. Copy src/config.example.json to src/config.json.")
    if _looks_like_service_key(key):
        raise ConfigError("config.json holds a privileged Supabase key. Use the anon (public) key only.")
    sms = os.environ.get("ESRMS_SOS_SMS_NUMBER") or data.get("sos_sms_number") or ""
    try:
        gps_timeout = int(os.environ.get("ESRMS_GPS_TIMEOUT") or data.get("gps_timeout_seconds") or 30)
    except (TypeError, ValueError):
        gps_timeout = 30
    return Config(url.rstrip("/"), key, hotline, sms, min(max(gps_timeout, 5), 300))
