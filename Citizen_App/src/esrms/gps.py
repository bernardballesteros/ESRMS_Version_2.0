"""Getting a position that is actually current.

"Get current position" does not guarantee a new fix: the platform may hand back a stored position
with an old timestamp. acquire() therefore checks the fix time of every answer and keeps asking
until one is fresh or the time runs out. A stale answer is returned separately and never as the
current location.

No Flet or network code here; the rules are unit tested in tests/test_gps.py. Positioning itself
needs no internet: on Android the request goes to the phone's GNSS receiver.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from esrms.report import Location, valid_coordinates

log = logging.getLogger(__name__)

POLL_SECONDS = 2
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def fix_time(value, now, seconds_as_millis=False):
    """The platform's position timestamp as an aware datetime, or None if it can't be trusted.

    seconds_as_millis: geolocator's Windows plugin reports the fix time in seconds where
    milliseconds are expected, so every position looks like January 1970. Rescale those.
    """
    try:
        if isinstance(value, (int, float)):
            value = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            value = value.astimezone()  # naive = device local time
        if seconds_as_millis and value.year == 1970:
            value = EPOCH + (value - EPOCH) * 1000
    except (ValueError, OSError, OverflowError):
        return None
    return value if value <= now + timedelta(minutes=1) else None


def to_location(pos, now, seconds_as_millis=False):
    """A geolocator position as a Location, or None if it has no usable coordinates."""
    if pos is None or not valid_coordinates(pos.latitude, pos.longitude):
        return None
    accuracy = pos.accuracy if pos.accuracy else None  # 0.0 means "not reported"
    return Location(source="gps", latitude=float(pos.latitude), longitude=float(pos.longitude),
                    accuracy_m=accuracy, mocked=bool(pos.mocked),
                    fixed_at=fix_time(pos.timestamp, now, seconds_as_millis))


def _reason(exc):
    text = str(exc).lower()
    if "disabled" in text:
        return "off"
    if "denied" in text:
        return "denied"
    return "timeout" if "timed out" in text or "timeout" in text else "failed"


async def acquire(request, timeout, *, seconds_as_millis=False, clock=time.monotonic,
                  utcnow=lambda: datetime.now(timezone.utc), sleep=asyncio.sleep):
    """Ask the device for a position until a fresh one arrives or `timeout` seconds pass.

    request(seconds_left) performs one real location request. Returns (fresh, stale, reason):
    fresh is a Location whose fix time is current, else None; stale is the newest old position the
    device answered with (marked cached), if any; reason says why there is no fresh fix:
    'timeout' (no answer: poor reception), 'stale' (only stored positions came back),
    'off', 'denied' or 'failed'.
    """
    deadline = clock() + timeout
    stale, reason = None, "timeout"
    while True:
        left = deadline - clock()
        if left <= 0:
            break
        try:
            # The outer limit also ends a request that never answers, e.g. the app was sent to the background.
            pos = await asyncio.wait_for(request(left), left + 3)
        except asyncio.TimeoutError:
            reason = "timeout"
            break
        except Exception as exc:
            reason = _reason(exc)
            log.info("Location request failed: %s", exc)
            if reason in ("off", "denied"):
                break
        else:
            loc = to_location(pos, utcnow(), seconds_as_millis)
            if loc and loc.is_current(utcnow()):
                return loc, stale, None
            if loc:
                loc.cached = True
                if stale is None or (loc.fixed_at and (stale.fixed_at is None or loc.fixed_at > stale.fixed_at)):
                    stale = loc
                reason = "stale"
            else:
                reason = "failed"
        await sleep(min(POLL_SECONDS, max(0.0, deadline - clock())))
    return None, stale, reason
