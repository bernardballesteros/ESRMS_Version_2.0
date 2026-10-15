"""Language table, theme palettes and the Account Settings values. No network."""
import asyncio
import re
import tomllib
from pathlib import Path

from fake_supabase import FakeSupabase

import esrms
from esrms import i18n
from esrms.backend import Backend
from esrms.config import Config
from esrms.ui import settings
from esrms.ui import theme as t

ROOT = Path(__file__).resolve().parent.parent


def test_every_selectable_language_is_completely_translated():
    english = i18n.STRINGS["en"]
    for code, (name, offered, _) in i18n.LANGUAGES.items():
        if not offered:
            assert not i18n.selectable(code)
            continue
        strings = i18n.STRINGS[code]
        assert set(strings) == set(english), f"{name}: missing {set(english) - set(strings)}"
        for key, text in strings.items():
            assert text.strip() and re.findall(r"\{(\w+)\}", text) == re.findall(r"\{(\w+)\}", english[key]), key


def test_translation_lookup_and_fallbacks():
    assert i18n.tr("set.title", "fil") == "Mga Setting ng Account" and i18n.tr("set.title") == "Account Settings"
    assert i18n.tr("dash.pending", "fil", n=2).startswith("2 SOS ANG HINDI PA NAIPADALA")
    assert i18n.tr("set.title", "ceb") == "Account Settings"            # untranslated language falls back
    assert i18n.tr("no.such.key", "fil") == "no.such.key"
    assert i18n.normalize("ceb") == "en" and i18n.normalize(None) == "en" and i18n.normalize("fil") == "fil"


def test_shown_version_matches_the_build_configuration():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert esrms.__version__ == project["version"]


def test_theme_palettes_define_the_same_colours_and_switch():
    assert set(t.PALETTES["dark"]) == set(t.PALETTES["light"])
    try:
        assert t.apply("light") == "light" and t.BG == "#F3F6FA" and t.accent("cyan") == t.CYAN == "#0E7490"
        assert t.label("x").color == t.TEXT == "#0F172A"          # helpers follow the active palette
        assert t.apply("nonsense") == "dark"
    finally:
        t.apply("dark")
    assert t.BG == "#05090F" and t.label("x").color == "#E6EDF7"


def test_member_since_is_real_or_unavailable():
    assert settings.member_since("2025-04-15T03:20:00Z") == "Apr 15, 2025"
    assert settings.member_since("2025-04-14T17:00:00+00:00") == "Apr 15, 2025"     # Philippine date
    assert settings.member_since(None) is None and settings.member_since("garbage") is None


def test_account_info_comes_from_the_signed_in_account():
    fake = FakeSupabase()
    juan = fake.add_user("juan@example.ph", "OldPass#123", full_name="Juan")
    fake.add_user("maria@example.ph", "x", full_name="Maria")
    fake.users[juan]["user_metadata"]["role"] = "organization"     # self-edited metadata must not win
    b = Backend(Config("https://example.supabase.co", "anon-key"), transport=fake.transport())
    asyncio.run(b.sign_in("juan@example.ph", "OldPass#123"))
    info = asyncio.run(b.account_info())
    assert info == {"user_id": juan, "is_anonymous": False, "role": "citizen", "created_at": "2025-04-15T03:20:00Z"}
    del fake.rows[juan]["role"]                                    # row without a role: fall back to sign-up data
    assert asyncio.run(b.account_info())["role"] == "organization"
