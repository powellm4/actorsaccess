# tests/test_filters.py
"""Tests for role filtering — focused on the unpaid female-cast bypass."""

from src.filters import (
    _gender_indicates_female,
    is_lead_or_supporting,
    project_has_female_cast,
)


def _cn_role(role_name="Bob", role_type="Day Player", gender="Male", description=""):
    return {
        "role_name": role_name,
        "role_type": role_type,
        "gender": gender,
        "description": description,
    }


def _aa_role(role_name="Bob", description=""):
    return {"role_name": role_name, "description": description}


# ---------------------------------------------------------------------------
# _gender_indicates_female
# ---------------------------------------------------------------------------


def test_gender_indicates_female_basic_female():
    assert _gender_indicates_female("Female") is True


def test_gender_indicates_female_mixed():
    assert _gender_indicates_female("Female and Male") is True


def test_gender_indicates_female_trans():
    assert _gender_indicates_female("Trans Female") is True


def test_gender_indicates_female_rejects_male():
    assert _gender_indicates_female("Male") is False


def test_gender_indicates_female_rejects_all_genders():
    # "All Genders" is too ambiguous about who actually shows up.
    assert _gender_indicates_female("All Genders") is False


def test_gender_indicates_female_rejects_nonbinary():
    assert _gender_indicates_female("Non-binary") is False


def test_gender_indicates_female_rejects_empty():
    assert _gender_indicates_female("") is False


def test_gender_indicates_female_handles_none():
    assert _gender_indicates_female(None) is False


# ---------------------------------------------------------------------------
# project_has_female_cast — CN / Backstage (structured gender field)
# ---------------------------------------------------------------------------


def test_cn_female_peer_role_returns_true():
    male_role = _cn_role(role_name="Bob", gender="Male")
    female_role = _cn_role(role_name="Alice", gender="Female")
    assert project_has_female_cast([male_role, female_role], male_role, "cn") is True


def test_cn_all_male_returns_false():
    a = _cn_role(role_name="Bob", gender="Male")
    b = _cn_role(role_name="Carl", gender="Male")
    assert project_has_female_cast([a, b], a, "cn") is False


def test_cn_self_only_returns_false():
    # Identity exclusion: the role being evaluated doesn't count as cast.
    only = _cn_role(role_name="Bob", gender="Male")
    assert project_has_female_cast([only], only, "cn") is False


def test_cn_self_female_only_returns_false():
    # Even if the only role is female, it's the current role — no "other" cast.
    only = _cn_role(role_name="Alice", gender="Female")
    assert project_has_female_cast([only], only, "cn") is False


def test_cn_all_genders_does_not_trigger():
    a = _cn_role(role_name="Bob", gender="Male")
    b = _cn_role(role_name="Other", gender="All Genders")
    assert project_has_female_cast([a, b], a, "cn") is False


def test_cn_mixed_field_triggers():
    a = _cn_role(role_name="Bob", gender="Male")
    b = _cn_role(role_name="Other", gender="Female and Male")
    assert project_has_female_cast([a, b], a, "cn") is True


def test_backstage_female_peer_returns_true():
    # Backstage uses the same structured "gender" field after normalization.
    male_role = {"role_name": "Bob", "gender": "Male", "description": ""}
    female_role = {"role_name": "Alice", "gender": "Female", "description": ""}
    assert project_has_female_cast([male_role, female_role], male_role, "backstage") is True


def test_backstage_all_male_returns_false():
    a = {"role_name": "Bob", "gender": "Male", "description": ""}
    b = {"role_name": "Carl", "gender": "Male", "description": ""}
    assert project_has_female_cast([a, b], a, "backstage") is False


# ---------------------------------------------------------------------------
# project_has_female_cast — AA (description text scan, ALL-CAPS only)
# ---------------------------------------------------------------------------


def test_aa_allcaps_female_in_peer_description_returns_true():
    bob = _aa_role(role_name="BOB", description="MALE, 30s. A grizzled cop.")
    alice = _aa_role(role_name="ALICE", description="FEMALE, 20s. Bob's partner.")
    assert project_has_female_cast([bob, alice], bob, "aa") is True


def test_aa_lowercase_female_in_prose_returns_false():
    # "his female cousin" in male role backstory should NOT trigger.
    bob = _aa_role(role_name="BOB", description="MALE, 30s. A grizzled cop.")
    carl = _aa_role(role_name="CARL", description="MALE, 40s. Loyal to his female cousin.")
    assert project_has_female_cast([bob, carl], bob, "aa") is False


def test_aa_single_role_female_self_returns_false():
    only = _aa_role(role_name="ALICE", description="FEMALE, 20s.")
    assert project_has_female_cast([only], only, "aa") is False


def test_aa_woman_marker_triggers():
    bob = _aa_role(role_name="BOB", description="MALE, 30s.")
    other = _aa_role(role_name="THE WOMAN", description="WOMAN, mysterious.")
    assert project_has_female_cast([bob, other], bob, "aa") is True


# ---------------------------------------------------------------------------
# is_lead_or_supporting — has_female_cast bypass
# ---------------------------------------------------------------------------


def test_is_lead_or_supporting_rejects_day_player_by_default():
    role = _cn_role(role_type="Day Player")
    ok, _ = is_lead_or_supporting(role, "cn")
    assert ok is False


def test_is_lead_or_supporting_accepts_day_player_when_female_cast_true():
    role = _cn_role(role_type="Day Player")
    ok, _ = is_lead_or_supporting(role, "cn", has_female_cast=True)
    assert ok is True


def test_is_lead_or_supporting_still_accepts_lead_with_bypass():
    role = _cn_role(role_type="Lead")
    ok, _ = is_lead_or_supporting(role, "cn", has_female_cast=True)
    assert ok is True


def test_is_lead_or_supporting_aa_day_player_marker_accepted_with_bypass():
    role = _aa_role(description="DAY PLAYER. Male, 30s, brief courtroom scene.")
    ok, _ = is_lead_or_supporting(role, "aa", has_female_cast=True)
    assert ok is True


def test_is_lead_or_supporting_aa_day_player_marker_rejected_without_bypass():
    role = _aa_role(description="DAY PLAYER. Male, 30s, brief courtroom scene.")
    ok, _ = is_lead_or_supporting(role, "aa")
    assert ok is False
