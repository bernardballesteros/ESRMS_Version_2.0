from datetime import datetime, timedelta, timezone

from esrms import announcements as ann

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)


def iso(delta_h):
    return (NOW + timedelta(hours=delta_h)).isoformat()


def row(id, **kw):
    base = dict(id=id, title=id, message="m", category="general", priority="medium", is_pinned=False,
                published_at=iso(-1), expires_at=None)
    base.update(kw)
    return base


def test_future_and_expired_hidden():
    rows = [row("live"), row("future", published_at=iso(2)), row("expired", expires_at=iso(-0.5)),
            row("nopub", published_at=None)]
    assert [r["id"] for r in ann.visible(rows, NOW)] == ["live"]


def test_sort_pinned_then_priority_then_newest():
    rows = [
        row("old-critical", priority="critical", published_at=iso(-5)),
        row("new-low", priority="low", published_at=iso(-0.1)),
        row("pinned-low", priority="low", is_pinned=True, published_at=iso(-9)),
        row("new-critical", priority="critical", published_at=iso(-0.2)),
    ]
    assert [r["id"] for r in ann.visible(rows, NOW)] == ["pinned-low", "new-critical", "old-critical", "new-low"]


def test_category_styles_cover_dispatcher_categories():
    # Same five categories as EMERGENCY/app/announcements.py CATEGORIES.
    for cat in ("emergency", "important", "advisory", "general", "maintenance"):
        assert ann.style({"category": cat})[0] == cat.upper()


def test_urgent():
    assert ann.is_urgent({"category": "emergency", "priority": "low"})
    assert ann.is_urgent({"category": "general", "priority": "high"})
    assert not ann.is_urgent({"category": "general", "priority": "medium"})
