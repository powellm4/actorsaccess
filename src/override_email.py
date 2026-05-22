# src/override_email.py
"""Per-run Apply Anyway confirmation email.

Sent at the end of each run that processed at least one GitHub override,
separate from the daily digest. The point is a strong real-time signal
that the override flow actually fired — the user shouldn't have to wait
for the next digest (or eyeball GitHub) to know whether their
"apply anyway" issue went through.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


PLATFORM_LABELS = {
    "aa": "Actors Access",
    "cn": "Casting Networks",
    "backstage": "Backstage",
}

_OUTCOME_STYLE = {
    "applied": ("Applied successfully", "#2e7d32", "#e8f5e9"),
    "failed": ("Failed", "#b71c1c", "#ffebee"),
    "not_found": ("Role no longer visible", "#666666", "#f5f5f5"),
}


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform.upper())


def build_subject(outcomes: list[dict], platform: str, mode: str) -> str:
    total = len(outcomes)
    applied = sum(1 for o in outcomes if o.get("outcome") == "applied")
    failed = sum(1 for o in outcomes if o.get("outcome") == "failed")
    not_found = sum(1 for o in outcomes if o.get("outcome") == "not_found")
    label = _platform_label(platform)

    if total == 1:
        o = outcomes[0]
        outcome_text = _OUTCOME_STYLE.get(o.get("outcome", ""), (o.get("outcome", ""), "", ""))[0]
        return (
            f"Apply Anyway [{label} · {mode}] — {outcome_text}: "
            f"{o.get('project_name', '')} / {o.get('role_name', '')}"
        )

    bits = [f"{applied} applied"]
    if failed:
        bits.append(f"{failed} failed")
    if not_found:
        bits.append(f"{not_found} not found")
    return f"Apply Anyway [{label} · {mode}] — {total} processed ({', '.join(bits)})"


def build_html(outcomes: list[dict], platform: str, mode: str, repo: str | None = None) -> str:
    """Build the HTML body for the Apply Anyway results email."""
    label = _platform_label(platform)
    applied = sum(1 for o in outcomes if o.get("outcome") == "applied")
    total = len(outcomes)
    summary = f"{applied} of {total} applied"

    cards = []
    for o in outcomes:
        outcome = o.get("outcome", "")
        outcome_text, accent, bg = _OUTCOME_STYLE.get(
            outcome, (outcome.title() or "Unknown", "#444", "#fafafa")
        )
        project_name = o.get("project_name", "")
        role_name = o.get("role_name", "")
        detail = o.get("detail", "")
        issue_number = o.get("issue_number")

        issue_link = ""
        if repo and issue_number:
            issue_link = (
                f' <a href="https://github.com/{repo}/issues/{issue_number}" '
                f'style="color:#1565c0;font-size:12px;">(issue #{issue_number})</a>'
            )

        detail_line = ""
        if detail and outcome != "applied":
            detail_line = (
                f'<br><span style="color:#555;font-size:13px;">{detail}</span>'
            )

        cards.append(
            f'<div style="background:{bg};border-left:4px solid {accent};'
            f'padding:12px 14px;border-radius:4px;margin-bottom:10px;">'
            f'<div style="font-size:12px;font-weight:bold;color:{accent};'
            f'letter-spacing:0.5px;text-transform:uppercase;">{outcome_text}</div>'
            f'<div style="margin-top:4px;font-size:15px;">'
            f'<strong>{project_name}</strong> — <strong>{role_name}</strong>'
            f'{issue_link}</div>'
            f'{detail_line}'
            f'</div>'
        )

    body = "".join(cards)
    generated = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;padding:20px;">
<div style="background:#ff6f00;color:white;padding:14px 18px;border-radius:6px 6px 0 0;">
<div style="font-size:11px;letter-spacing:1.5px;font-weight:bold;opacity:0.9;">APPLY ANYWAY · {label.upper()} · {mode.upper()}</div>
<h1 style="margin:4px 0 0 0;font-size:20px;color:white;">{summary}</h1>
</div>
<div style="background:#fff8e1;border:1px solid #ffb74d;border-top:none;border-radius:0 0 6px 6px;padding:14px;">
{body}
</div>
<p style="color:#888;font-size:12px;margin-top:14px;">Sent {generated} — this is a per-run notification, separate from the daily digest.</p>
</body>
</html>"""


def send_override_results_email(
    outcomes: list[dict],
    platform: str,
    mode: str,
    repo: str | None = None,
) -> bool:
    """Send a confirmation email for a batch of processed overrides.

    Returns True if an email was sent, False otherwise (empty batch or
    missing credentials). Never raises — email failure must not break the
    run that just successfully applied an override.
    """
    if not outcomes:
        return False

    password = os.environ.get("GMAIL_APP_PASSWORD")
    sender = os.environ.get("DIGEST_SENDER_EMAIL")
    if not password or not sender:
        logger.warning(
            "Apply Anyway email skipped — GMAIL_APP_PASSWORD or "
            "DIGEST_SENDER_EMAIL not set (would have sent %d outcome(s))",
            len(outcomes),
        )
        return False

    recipient = (os.environ.get("OVERRIDE_NOTIFY_EMAIL") or sender).strip()

    html = build_html(outcomes, platform=platform, mode=mode, repo=repo)
    subject = build_subject(outcomes, platform=platform, mode=mode)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        logger.info(
            "Apply Anyway email sent: platform=%s mode=%s count=%d",
            platform, mode, len(outcomes),
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send Apply Anyway email: {e}")
        return False
