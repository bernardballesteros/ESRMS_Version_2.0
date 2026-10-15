"""Fresh-fix rules. The 'device' here is a scripted stand-in so each platform behaviour can be replayed;
the app itself only ever uses the real geolocator."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from esrms import gps

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)


def pos(age, lat=7.07, lng=125.61, accuracy=12.0, **kw):
    return SimpleNamespace(latitude=lat, longitude=lng, accuracy=accuracy, mocked=False,
                           timestamp=NOW - age if isinstance(age, timedelta) else age, **kw)


class Device:
    """Answers each location request with the next scripted item; time only moves when the app waits."""

    def __init__(self, *answers):
        self.answers, self.requests, self.t = list(answers), 0, 0.0

    async def request(self, seconds_left):
        self.requests += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(self, seconds):
        self.t += max(seconds, 0.01)

    def run(self, timeout=30, **kw):
        return asyncio.run(gps.acquire(self.request, timeout, clock=lambda: self.t, utcnow=lambda: NOW,
                                       sleep=self.sleep, **kw))


def test_fresh_fix_is_returned_with_accuracy_and_time():
    fresh, stale, reason = Device(pos(timedelta(seconds=3))).run()
    assert (fresh.latitude, fresh.longitude, fresh.accuracy_m) == (7.07, 125.61, 12.0)
    assert fresh.fixed_at == NOW - timedelta(seconds=3) and fresh.is_current(NOW) and not fresh.cached
    assert stale is None and reason is None


def test_732_day_old_answer_is_never_the_current_location():
    device = Device(pos(timedelta(days=732)))
    fresh, stale, reason = device.run(timeout=30)
    assert fresh is None and reason == "stale"
    assert stale.cached and not stale.is_current(NOW) and stale.freshness(NOW) == "OLD, 732 days ago"
    assert device.requests > 5          # it kept asking the device until the timeout, not just once


def test_stale_answer_then_fresh_fix_is_accepted():
    device = Device(pos(timedelta(days=732)), pos(timedelta(minutes=9)), pos(timedelta(seconds=1), lat=7.08))
    fresh, stale, reason = device.run()
    assert fresh.latitude == 7.08 and reason is None and device.requests == 3
    assert stale.fixed_at == NOW - timedelta(minutes=9)     # newest of the rejected ones


def test_no_answer_within_timeout_is_a_timeout_not_a_guess():
    device = Device(Exception("Location request timed out."))
    assert device.run(timeout=10) == (None, None, "timeout")
    assert device.t >= 10


def test_location_off_or_denied_stops_at_once():
    device = Device(Exception("Location services are disabled. Enable them in the device or system settings."))
    assert device.run() == (None, None, "off") and device.requests == 1
    assert Device(Exception("Location permission denied: x.")).run()[2] == "denied"


def test_request_that_never_answers_is_abandoned():
    async def hangs(seconds_left):
        await asyncio.sleep(3600)

    assert asyncio.run(gps.acquire(hangs, 0.05)) == (None, None, "timeout")


def test_unknown_or_future_fix_time_is_not_fresh():
    assert Device(pos(None)).run(timeout=4)[0] is None
    assert Device(pos(NOW + timedelta(hours=3))).run(timeout=4)[0] is None


def test_invalid_coordinates_are_rejected():
    assert Device(pos(timedelta(seconds=1), lat=0, lng=0)).run(timeout=4) == (None, None, "failed")


def test_windows_plugin_seconds_as_milliseconds_timestamp():
    # What this PC's location API returned on 2026-10-04: today's time in seconds read as milliseconds.
    broken = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=NOW.timestamp())
    assert broken.year == 1970
    assert Device(pos(broken)).run(timeout=4)[0] is None                      # other platforms: stale
    fresh, _, _ = Device(pos(broken)).run(timeout=4, seconds_as_millis=True)  # Windows: rescaled
    assert abs((fresh.fixed_at - NOW).total_seconds()) < 1
