# tests/test_overrides.py
"""Tests for the GitHub Issues override client (Apply Anyway flow)."""
from unittest.mock import patch, MagicMock

import pytest

from src.overrides import (
    OverrideRequest,
    build_override_url,
    parse_issue_body,
)


# --- parse_issue_body ---

def test_parse_issue_body_happy_path():
    body = (
        "project_name: Acme - Untitled Pilot\n"
        "role_name: Lead Detective\n"
        "platform: aa\n"
        "mode: paid\n"
    )
    parsed = parse_issue_body(body)
    assert parsed == {
        "project_name": "Acme - Untitled Pilot",
        "role_name": "Lead Detective",
        "platform": "aa",
        "mode": "paid",
    }


def test_parse_issue_body_handles_extra_text():
    """Body may have a comment line above the structured fields — just
    pull out the four keys we care about and ignore the rest."""
    body = (
        "Apply this role on the next run.\n\n"
        "project_name: Acme\n"
        "role_name: Lead\n"
        "platform: backstage\n"
        "mode: unpaid\n"
        "\nThanks!\n"
    )
    parsed = parse_issue_body(body)
    assert parsed["project_name"] == "Acme"
    assert parsed["platform"] == "backstage"
    assert parsed["mode"] == "unpaid"


def test_parse_issue_body_returns_none_when_required_field_missing():
    body = "project_name: Acme\nrole_name: Lead\nplatform: aa\n"  # no mode
    assert parse_issue_body(body) is None


def test_parse_issue_body_returns_none_when_blank():
    assert parse_issue_body("") is None
    assert parse_issue_body("   \n\n  ") is None


def test_parse_issue_body_strips_whitespace_and_quotes():
    body = (
        'project_name:  "Acme - Spaces "\n'
        "role_name: 'Lead Role'\n"
        "platform: aa\n"
        "mode: paid\n"
    )
    parsed = parse_issue_body(body)
    assert parsed["project_name"] == "Acme - Spaces"
    assert parsed["role_name"] == "Lead Role"


def test_parse_issue_body_rejects_unknown_platform():
    body = "project_name: P\nrole_name: R\nplatform: tinder\nmode: paid\n"
    assert parse_issue_body(body) is None


def test_parse_issue_body_rejects_unknown_mode():
    body = "project_name: P\nrole_name: R\nplatform: aa\nmode: vibes\n"
    assert parse_issue_body(body) is None


# --- build_override_url ---

def test_build_override_url_includes_label_and_encoded_body():
    url = build_override_url(
        repo="powellm4/aa-overrides",
        label="apply-anyway",
        project_name="Acme - Untitled",
        role_name="Lead / Detective",
        platform="aa",
        mode="paid",
    )
    # www.github.com (not bare github.com) so iOS Universal Links don't
    # hijack the link into the GitHub mobile app.
    assert url.startswith("https://www.github.com/powellm4/aa-overrides/issues/new?")
    # Both the label and the body fields show up in URL-encoded form.
    assert "labels=apply-anyway" in url
    assert "title=" in url
    assert "body=" in url
    # Spaces and slashes get encoded — verify a couple of fragments.
    assert "Acme" in url
    # The body contains the four key/value lines URL-encoded.
    assert "project_name" in url
    assert "role_name" in url
    assert "platform" in url
    assert "mode" in url


def test_build_override_url_round_trips_through_parse():
    """URL → encoded body → parse should round-trip cleanly."""
    from urllib.parse import urlparse, parse_qs

    url = build_override_url(
        repo="r/o", label="apply-anyway",
        project_name="My Project: A Story",
        role_name="Lead — Hero",
        platform="backstage", mode="unpaid",
    )
    qs = parse_qs(urlparse(url).query)
    body_text = qs["body"][0]
    parsed = parse_issue_body(body_text)
    assert parsed["project_name"] == "My Project: A Story"
    assert parsed["role_name"] == "Lead — Hero"
    assert parsed["platform"] == "backstage"
    assert parsed["mode"] == "unpaid"


# --- fetch_pending (uses urllib; we patch the http call) ---

def test_fetch_pending_parses_issues_and_skips_malformed():
    from src import overrides

    fake_issues = [
        {
            "number": 1,
            "body": (
                "project_name: P1\nrole_name: R1\nplatform: aa\nmode: paid\n"
            ),
        },
        {
            "number": 2,
            "body": "totally wrong content",  # malformed → skipped
        },
        {
            "number": 3,
            "body": (
                "project_name: P3\nrole_name: R3\nplatform: aa\nmode: unpaid\n"
            ),
        },
    ]

    with patch.object(overrides, "_github_get", return_value=fake_issues):
        results = overrides.fetch_pending(repo="o/r", label="apply-anyway", token="tok")

    assert len(results) == 2
    assert isinstance(results[0], OverrideRequest)
    assert results[0].issue_number == 1
    assert results[0].project_name == "P1"
    assert results[1].issue_number == 3
    assert results[1].mode == "unpaid"


def test_fetch_pending_returns_malformed_separately():
    """Caller needs to know which issues were malformed so it can comment+close them."""
    from src import overrides

    fake_issues = [
        {"number": 5, "body": "garbage"},
        {"number": 6, "body": "project_name: P\nrole_name: R\nplatform: aa\nmode: paid\n"},
    ]
    with patch.object(overrides, "_github_get", return_value=fake_issues):
        ok, malformed = overrides.fetch_pending_with_errors(
            repo="o/r", label="apply-anyway", token="tok",
        )
    assert [r.issue_number for r in ok] == [6]
    assert malformed == [5]


# --- comment_and_close (smoke test only — verifies it makes the right calls) ---

def test_comment_and_close_posts_then_closes():
    from src import overrides

    with patch.object(overrides, "_github_post") as post, \
         patch.object(overrides, "_github_patch") as patch_call:
        overrides.comment_and_close(
            repo="o/r", issue_number=42,
            comment="Applied successfully", token="tok",
        )
    post.assert_called_once()
    patch_call.assert_called_once()
    # Comment endpoint
    assert "issues/42/comments" in post.call_args[0][0]
    # Close endpoint
    assert "issues/42" in patch_call.call_args[0][0]
    assert patch_call.call_args[0][1] == {"state": "closed"}
