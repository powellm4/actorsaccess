"""Tests for the per-run Apply Anyway confirmation email."""
import os
from unittest.mock import patch, MagicMock

from src import override_email


def _outcome(**overrides) -> dict:
    base = {
        "issue_number": 42, "project_name": "My Show", "role_name": "Lead Detective",
        "platform": "aa", "mode": "paid", "outcome": "applied",
        "detail": "Submitted successfully via override",
    }
    base.update(overrides)
    return base


def test_build_subject_single_outcome_includes_platform_mode_and_label():
    subject = override_email.build_subject([_outcome()], platform="aa", mode="paid")
    assert "Apply Anyway" in subject
    assert "Actors Access" in subject
    assert "paid" in subject
    assert "Applied successfully" in subject
    assert "My Show" in subject
    assert "Lead Detective" in subject


def test_build_subject_multiple_outcomes_summarizes_counts():
    outcomes = [
        _outcome(outcome="applied"),
        _outcome(outcome="failed"),
        _outcome(outcome="not_found"),
    ]
    subject = override_email.build_subject(outcomes, platform="backstage", mode="unpaid")
    assert "Backstage" in subject
    assert "unpaid" in subject
    assert "3 processed" in subject
    assert "1 applied" in subject
    assert "1 failed" in subject
    assert "1 not found" in subject


def test_build_html_renders_each_outcome_with_distinct_styling_and_issue_link():
    outcomes = [
        _outcome(outcome="applied"),
        _outcome(outcome="failed", detail="browser timeout"),
    ]
    html = override_email.build_html(outcomes, platform="aa", mode="paid", repo="me/aa-overrides")
    # Each outcome rendered
    assert "Applied successfully" in html
    assert "Failed" in html
    # Failed detail surfaced
    assert "browser timeout" in html
    # Header includes platform + mode loud and clear
    assert "APPLY ANYWAY" in html
    assert "ACTORS ACCESS" in html
    assert "PAID" in html
    # GitHub issue link wired up
    assert "https://github.com/me/aa-overrides/issues/42" in html
    # Summary line shows applied count
    assert "1 of 2 applied" in html


def test_send_returns_false_when_outcomes_empty():
    assert override_email.send_override_results_email([], platform="aa", mode="paid") is False


def test_send_returns_false_when_smtp_credentials_missing():
    with patch.dict(os.environ, {}, clear=True):
        assert override_email.send_override_results_email(
            [_outcome()], platform="aa", mode="paid",
        ) is False


def test_send_dispatches_via_smtp_with_apply_anyway_subject():
    import email as email_pkg

    env = {
        "GMAIL_APP_PASSWORD": "pw",
        "DIGEST_SENDER_EMAIL": "me@example.com",
    }
    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp

    with patch.dict(os.environ, env, clear=True), \
         patch("src.override_email.smtplib.SMTP_SSL", return_value=fake_smtp) as smtp_ctor:
        sent = override_email.send_override_results_email(
            [_outcome()], platform="aa", mode="paid", repo="me/aa-overrides",
        )

    assert sent is True
    smtp_ctor.assert_called_once_with("smtp.gmail.com", 465)
    fake_smtp.login.assert_called_once_with("me@example.com", "pw")
    sendmail_args = fake_smtp.sendmail.call_args
    assert sendmail_args.args[0] == "me@example.com"
    assert sendmail_args.args[1] == ["me@example.com"]

    from email.header import decode_header, make_header
    msg = email_pkg.message_from_string(sendmail_args.args[2])
    subject = str(make_header(decode_header(msg["Subject"])))
    assert "Apply Anyway" in subject
    assert "Actors Access" in subject
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    html_body = html_part.get_payload(decode=True).decode("utf-8")
    assert "APPLY ANYWAY" in html_body
    assert "My Show" in html_body


def test_send_honors_override_notify_email_override():
    env = {
        "GMAIL_APP_PASSWORD": "pw",
        "DIGEST_SENDER_EMAIL": "sender@example.com",
        "OVERRIDE_NOTIFY_EMAIL": "alerts+aa@example.com",
    }
    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp

    with patch.dict(os.environ, env, clear=True), \
         patch("src.override_email.smtplib.SMTP_SSL", return_value=fake_smtp):
        override_email.send_override_results_email(
            [_outcome()], platform="aa", mode="paid",
        )

    sendmail_args = fake_smtp.sendmail.call_args
    assert sendmail_args.args[1] == ["alerts+aa@example.com"]


def test_send_does_not_raise_when_smtp_fails():
    env = {
        "GMAIL_APP_PASSWORD": "pw",
        "DIGEST_SENDER_EMAIL": "me@example.com",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("src.override_email.smtplib.SMTP_SSL", side_effect=RuntimeError("boom")):
        # Must swallow exceptions — a failed notification email must never
        # break the run that just successfully applied a role.
        assert override_email.send_override_results_email(
            [_outcome()], platform="aa", mode="paid",
        ) is False
