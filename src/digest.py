# src/digest.py
"""Daily digest email — summarizes applications and rejections from the last 24 hours."""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from src.database import Database

logger = logging.getLogger("digest")


def gather_digest_data(db: Database) -> dict:
    """Query the database for the last 24 hours of activity."""
    return {
        "applications": db.get_daily_applications(),
        "rejections": db.get_daily_rejections(),
        "flagged": db.get_daily_flagged(),
        "runs": db.get_daily_run_summary(),
    }


def build_digest_html(data: dict) -> str:
    """Build an HTML email body from digest data."""
    applications = data["applications"]
    rejections = data["rejections"]
    flagged = data.get("flagged", [])
    runs = data["runs"]

    if not applications and not rejections and not flagged:
        return _empty_digest_html(runs)

    # Build flagged roles section (shown at top)
    flagged_section = ""
    if flagged:
        flagged_section = '<div style="margin-bottom:24px;">\n'
        flagged_section += '<h2 style="color:#4a148c;margin-bottom:12px;">Needs Your Attention</h2>\n'
        for item in flagged:
            platform_badge = _platform_badge(item.get("platform", "aa"))
            desc = item.get("role_description") or ""
            project_url = item.get("project_url", "")
            role_label = f'<a href="{project_url}" style="color:#4a148c;text-decoration:underline;">{item["role_name"]}</a>' if project_url else item["role_name"]
            flagged_section += f'<div style="background:#ede7f6;border-left:4px solid #7c4dff;padding:12px;border-radius:4px;margin-bottom:8px;">\n'
            flagged_section += f'{platform_badge} <strong>{item["project_name"]}</strong> — <strong>{role_label}</strong>'
            flagged_section += f'<br><span style="color:#4a148c;"><strong>Needed:</strong> {item.get("flag_reason", "Unknown")}</span>'
            if desc:
                flagged_section += f'<br><span style="color:#555;">{desc}</span>'
            flagged_section += '\n</div>\n'
        flagged_section += '</div>\n'

    # Group by project
    projects = defaultdict(lambda: {"applied": [], "rejected": []})
    for app in applications:
        projects[app["project_name"]]["applied"].append(app)
    for rej in rejections:
        projects[rej["project_name"]]["rejected"].append(rej)

    # Build HTML
    sections = []
    for project_name, roles in sorted(projects.items()):
        project_url = ""
        if roles["applied"]:
            project_url = roles["applied"][0].get("project_url", "")
        elif roles["rejected"]:
            project_url = roles["rejected"][0].get("project_url", "")

        header = f'<a href="{project_url}">{project_name}</a>' if project_url else project_name

        section = f'<div style="margin-bottom:24px;border:1px solid #ddd;border-radius:8px;padding:16px;">\n'
        section += f'<h2 style="margin-top:0;color:#333;">{header}</h2>\n'

        # Applied roles
        if roles["applied"]:
            for app in roles["applied"]:
                platform_badge = _platform_badge(app.get("platform", "aa"))
                desc = app.get("role_description") or ""
                section += f'<div style="background:#e8f5e9;padding:12px;border-radius:4px;margin-bottom:8px;">\n'
                app_url = app.get("project_url", "")
                role_label = f'<a href="{app_url}" style="color:#2e7d32;text-decoration:underline;">{app["role_name"]}</a>' if app_url else app["role_name"]
                section += f'<strong style="color:#2e7d32;">APPLIED</strong> {platform_badge} — <strong>{role_label}</strong>'
                if app.get("candidates_considered", 1) > 1:
                    section += f' <em>(chosen from {app["candidates_considered"]} candidates)</em>'
                section += f'<br><span style="color:#555;">{desc}</span>' if desc else ""
                section += f'<br><strong>Reason:</strong> {app.get("ai_reason", "N/A")}'
                note = app.get("submission_note", "")
                if note:
                    section += f'<br><strong>Note submitted:</strong> <em>{note}</em>'
                else:
                    section += f'<br><span style="color:#888;">No specific submission info requested</span>'
                section += '\n</div>\n'

        # Rejected roles
        if roles["rejected"]:
            for rej in roles["rejected"]:
                platform_badge = _platform_badge(rej.get("platform", "aa"))
                desc = rej.get("role_description") or ""
                rej_url = rej.get("project_url", "")
                role_label = f'<a href="{rej_url}" style="color:#e65100;text-decoration:underline;">{rej["role_name"]}</a>' if rej_url else rej["role_name"]
                section += f'<div style="background:#fff3e0;padding:12px;border-radius:4px;margin-bottom:8px;">\n'
                section += f'<strong style="color:#e65100;">PASSED</strong> {platform_badge} — <strong>{role_label}</strong>'
                section += f'<br><span style="color:#555;">{desc}</span>' if desc else ""
                section += f'<br><strong>Reason:</strong> {rej.get("rejection_reason", "N/A")}'
                section += '\n</div>\n'

        section += '</div>\n'
        sections.append(section)

    # Footer stats
    total_applied = len(applications)
    total_rejected = len(rejections)
    total_projects = len(projects)
    failed_runs = [r for r in runs if r.get("status") == "error"]

    footer = f'<div style="margin-top:24px;padding:16px;background:#f5f5f5;border-radius:8px;">\n'
    total_flagged = len(flagged)
    footer += f'<strong>Summary:</strong> {total_applied} roles applied, {total_rejected} roles passed'
    if total_flagged:
        footer += f', {total_flagged} flagged for review'
    footer += f', {total_projects} projects evaluated<br>\n'
    footer += f'<strong>Runs:</strong> {len(runs)} total'
    if failed_runs:
        footer += f' ({len(failed_runs)} failed)'
        for fr in failed_runs:
            footer += f'<br><span style="color:red;">Failed ({fr.get("platform","?")}): {fr.get("error_message","unknown")}</span>'
    footer += '\n</div>\n'

    body = flagged_section + "\n".join(sections) + footer
    return _wrap_html(body)


def _empty_digest_html(runs: list[dict]) -> str:
    """Build HTML for a day with no applications."""
    content = '<div style="padding:24px;text-align:center;color:#666;">\n'
    content += '<h2>No applications today</h2>\n'
    content += f'<p>{len(runs)} automation run(s) completed — no new roles matched.</p>\n'
    failed = [r for r in runs if r.get("status") == "error"]
    if failed:
        content += '<p style="color:red;">Some runs failed:</p>\n'
        for fr in failed:
            content += f'<p style="color:red;">{fr.get("platform","?")}: {fr.get("error_message","unknown")}</p>\n'
    content += '</div>\n'
    return _wrap_html(content)


def _platform_badge(platform: str) -> str:
    color = "#1565c0" if platform == "aa" else "#6a1b9a"
    label = "AA" if platform == "aa" else "CN"
    return f'<span style="background:{color};color:white;padding:2px 6px;border-radius:3px;font-size:12px;">{label}</span>'


def _wrap_html(body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
<h1 style="border-bottom:2px solid #333;padding-bottom:8px;">Daily Casting Digest</h1>
<p style="color:#666;">Generated {datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %I:%M %p")} UTC</p>
{body}
</body>
</html>"""


def send_email(html: str):
    """Send the digest email via Gmail SMTP."""
    import smtplib
    from email.mime.text import MIMEText

    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        logger.error("GMAIL_APP_PASSWORD not set — cannot send digest")
        return

    sender = "REDACTED"
    msg = MIMEText(html, "html")
    msg["Subject"] = f"Casting Digest — {datetime.now(tz=timezone.utc).strftime('%B %d, %Y')}"
    msg["From"] = sender
    msg["To"] = sender

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, sender, msg.as_string())
        logger.info("Digest email sent via Gmail")
    except Exception as e:
        logger.error(f"Failed to send digest email: {e}")


def main():
    """Entry point for the digest workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    db = Database("data/applied.db")
    try:
        data = gather_digest_data(db)
        html = build_digest_html(data)
        send_email(html)
        db.record_digest_sent()
    finally:
        db.close()


if __name__ == "__main__":
    main()
