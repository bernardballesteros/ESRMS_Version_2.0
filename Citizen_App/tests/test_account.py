"""My Profile and Security & MFA: rules, and the backend calls against a fake Supabase that enforces
the real server's rules (tests/fake_supabase.py). No network, no real accounts."""
import asyncio

import pytest
from fake_supabase import GOOD_CODE, FakeSupabase

from esrms import account as rules
from esrms.backend import Backend, BackendError, Session
from esrms.config import Config

CFG = Config("https://example.supabase.co", "anon-key")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world():
    fake = FakeSupabase()
    juan = fake.add_user("juan@example.ph", "OldPass#123", full_name="Juan Dela Cruz", phone="0917 123 4567")
    maria = fake.add_user("maria@example.ph", "Maria#Pass9", full_name="Maria Santos")
    backend = Backend(CFG, transport=fake.transport())
    run(backend.sign_in("juan@example.ph", "OldPass#123"))
    return fake, backend, juan, maria


# ---------------------------------------------------------------- rules

def test_profile_validation_rejects_bad_input():
    with pytest.raises(rules.ValidationError) as exc:
        rules.validate_profile("  ", "not-an-email", "abc", "x" * 201)
    assert set(exc.value.errors) == {"full_name", "email", "phone", "address"}
    clean = rules.validate_profile("  Juan   Dela Cruz ", "Juan@Example.PH", "+63 912 345 6789", "Davao City")
    assert clean == {"full_name": "Juan Dela Cruz", "email": "juan@example.ph", "phone": "+63 912 345 6789",
                     "address": "Davao City"}
    assert rules.validate_profile("Juan", "j@x.ph", "", "")["phone"] == ""      # phone and address are optional


def test_password_checklist_and_change_rules():
    assert [ok for _, ok in rules.password_checks("abc")] == [False, False, True, False, False]
    assert all(ok for _, ok in rules.password_checks("NewPass#2026"))
    for args, field in ((("", "NewPass#2026", "NewPass#2026"), "current"), (("a", "weak", "weak"), "new"),
                        (("Same#Pass1", "Same#Pass1", "Same#Pass1"), "new"),
                        (("a", "NewPass#2026", "different"), "confirm")):
        with pytest.raises(rules.ValidationError) as exc:
            rules.validate_password_change(*args)
        assert field in exc.value.errors
    rules.validate_password_change("old", "NewPass#2026", "NewPass#2026")


def test_image_and_code_rules():
    assert rules.validate_image(PNG) == ("image/png", "png")
    for bad in (b"", b"GIF89a....", PNG + b"0" * rules.MAX_IMAGE_BYTES):
        with pytest.raises(rules.ValidationError):
            rules.validate_image(bad)
    assert rules.clean_code(" 123 456 ") == "123456"
    for bad in ("12345", "abcdef", ""):
        with pytest.raises(rules.ValidationError):
            rules.clean_code(bad)


def test_sign_in_methods_come_from_the_provider_list():
    assert rules.sign_in_methods(("email",)) == (["Email and password"], True, False)
    assert rules.sign_in_methods(("google",)) == (["Google"], False, True)
    assert rules.sign_in_methods(("email", "google"))[1:] == (True, False)
    assert rules.sign_in_methods((), is_anonymous=True)[1:] == (False, False)
    assert rules.initials("Juan Dela Cruz") == "JC" and rules.initials("", "maria@x.ph") == "MA"


# ---------------------------------------------------------------- profile

def test_profile_loads_the_signed_in_citizen_only(world):
    fake, b, juan, maria = world
    s = run(b.load_account())
    assert (s.user_id, s.email, s.metadata["full_name"], s.metadata["phone"]) == (
        juan, "juan@example.ph", "Juan Dela Cruz", "0917 123 4567")
    assert s.providers == ("email",)


def test_profile_save_persists_and_never_touches_another_citizen(world):
    fake, b, juan, maria = world
    assert run(b.update_profile("Juan D. Cruz", "+63 912 345 6789", "Davao City")) is True
    run(b.sign_out())
    run(b.sign_in("juan@example.ph", "OldPass#123"))          # leave and come back
    meta = run(b.load_account()).metadata
    assert (meta["full_name"], meta["phone"], meta["address"]) == ("Juan D. Cruz", "+63 912 345 6789", "Davao City")
    assert fake.rows[juan]["full_name"] == "Juan D. Cruz" and fake.rows[juan]["phone"] == "+63 912 345 6789"
    assert fake.users[maria]["user_metadata"]["full_name"] == "Maria Santos"
    assert fake.rows[maria]["full_name"] == "Maria Santos"


def test_email_change_waits_for_confirmation_and_rejects_taken_address(world):
    fake, b, juan, maria = world
    assert run(b.change_email("juan.new@example.ph")) is True          # pending, not changed
    assert b.session.email == "juan@example.ph" and b.session.new_email == "juan.new@example.ph"
    with pytest.raises(BackendError):
        run(b.change_email("maria@example.ph"))


def test_profile_network_error_is_not_a_save(world):
    fake, b, juan, _ = world
    fake.down = True
    with pytest.raises(BackendError) as exc:
        run(b.update_profile("X", "", ""))
    assert exc.value.kind == "network" and fake.users[juan]["user_metadata"]["full_name"] == "Juan Dela Cruz"


def test_avatar_upload_replace_remove_and_missing_policy(world):
    fake, b, juan, maria = world
    url = run(b.set_avatar(PNG, "image/png", "png"))
    assert f"/esrms-media/avatars/{juan}.png" in url and fake.objects[f"avatars/{juan}.png"] == PNG
    assert run(b.load_account()).metadata["avatar_url"] == url and fake.rows[juan]["avatar_url"] == url
    run(b.remove_avatar())
    assert "avatar_url" not in b.session.metadata and not fake.objects
    fake.storage_policies = False
    with pytest.raises(BackendError) as exc:
        run(b.set_avatar(PNG, "image/png", "png"))
    assert exc.value.kind == "not_configured" and "avatar_url" not in b.session.metadata


def test_incident_photo_upload_and_missing_policy(world):
    fake, b, juan, maria = world
    url = run(b.upload_incident_photo(PNG, "image/png", "png", "abc"))
    assert url.endswith(f"/public/esrms-media/incidents/{juan}/abc.png")
    assert fake.objects[f"incidents/{juan}/abc.png"] == PNG
    fake.storage_policies = False
    with pytest.raises(BackendError) as exc:
        run(b.upload_incident_photo(PNG, "image/png", "png", "abc"))
    assert exc.value.kind == "not_configured"


# ---------------------------------------------------------------- password

def test_password_change_needs_the_right_current_password(world):
    fake, b, juan, _ = world
    with pytest.raises(BackendError) as exc:
        run(b.change_password("wrong", "NewPass#2026"))
    assert exc.value.kind == "wrong_password" and fake.passwords[juan] == "OldPass#123"
    run(b.change_password("OldPass#123", "NewPass#2026"))
    assert fake.passwords[juan] == "NewPass#2026"
    run(b.sign_out())
    with pytest.raises(BackendError):
        run(b.sign_in("juan@example.ph", "OldPass#123"))
    assert run(b.sign_in("juan@example.ph", "NewPass#2026")).user_id == juan     # login still works


# ---------------------------------------------------------------- MFA

def test_mfa_is_enabled_only_after_the_server_verifies_a_code(world):
    fake, b, juan, _ = world
    factor = run(b.mfa_enroll())
    assert factor["totp"]["secret"] and factor["totp"]["uri"].startswith("otpauth://")
    assert run(b.load_account()).verified_totp == []                 # enrolling alone enables nothing
    with pytest.raises(BackendError):
        run(b.mfa_verify(factor["id"], "000000"))
    assert run(b.load_account()).verified_totp == []
    s = run(b.mfa_verify(factor["id"], GOOD_CODE))
    assert [f["id"] for f in s.verified_totp] == [factor["id"]] and s.aal == "aal2" and not s.needs_mfa


def test_next_sign_in_needs_the_code_and_removal_needs_it_too(world):
    fake, b, juan, _ = world
    factor = run(b.mfa_enroll())
    run(b.mfa_verify(factor["id"], GOOD_CODE))
    run(b.sign_out())
    s = run(b.sign_in("juan@example.ph", "OldPass#123"))
    assert s.needs_mfa and s.aal == "aal1"                           # password alone is not enough
    with pytest.raises(BackendError):
        run(b._account("DELETE", f"/auth/v1/factors/{factor['id']}", "remove"))   # server refuses at aal1
    with pytest.raises(BackendError):
        run(b.mfa_remove(factor["id"], "999999"))
    assert run(b.load_account()).verified_totp
    assert run(b.mfa_remove(factor["id"], GOOD_CODE)).verified_totp == []


def test_abandoned_setup_is_replaced_and_other_peoples_factors_are_unreachable(world):
    fake, b, juan, maria = world
    first = run(b.mfa_enroll())
    second = run(b.mfa_enroll())
    assert [f["id"] for f in fake.users[juan]["factors"]] == [second["id"]] and first["id"] != second["id"]
    other = Backend(CFG, transport=fake.transport())
    run(other.sign_in("maria@example.ph", "Maria#Pass9"))
    with pytest.raises(BackendError):
        run(other.mfa_verify(second["id"], GOOD_CODE))
    with pytest.raises(BackendError):
        run(other._account("DELETE", f"/auth/v1/factors/{second['id']}", "remove"))
    assert fake.users[juan]["factors"][0]["status"] == "unverified"


def test_session_reads_factors_and_aal_without_trusting_local_flags():
    s = Session("not-a-jwt", "r", 0, "u1", "c@x.ph", False, {},
                factors=({"id": "f1", "factor_type": "totp", "status": "verified"},))
    assert s.aal == "aal1" and s.needs_mfa
    assert not Session("x", "r", 0, "u1", None, False, {},
                       factors=({"id": "f1", "factor_type": "totp", "status": "unverified"},)).needs_mfa
