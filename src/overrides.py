# src/overrides.py
"""GitHub Issues client for the Apply Anyway override flow.

The digest email renders an "Apply anyway" link beside each role in the
Passed and Needs Attention sections. The link opens a pre-filled new-issue
URL in a separate private repo (e.g. powellm4/aa-overrides). On the next
run, the bot reads open issues with the configured label, queues them
locally, applies the roles directly, then comments + closes the issues.

This module only handles the GitHub side: URL building, body parsing, and
REST calls. Queueing + apply orchestration lives in src/main.py.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
ALLOWED_PLATFORMS = {"aa", "backstage", "cn"}
ALLOWED_MODES = {"paid", "unpaid"}
REQUIRED_FIELDS = ("project_name", "role_name", "platform", "mode")


@dataclass
class OverrideRequest:
    issue_number: int
    project_name: str
    role_name: str
    platform: str
    mode: str


# --- URL builder (used by digest.py) ---

def build_override_url(
    repo: str, label: str, project_name: str, role_name: str,
    platform: str, mode: str,
) -> str:
    """Build a GitHub new-issue URL with title/body/label pre-filled.

    Clicking it opens the GitHub issue creation page; the user just hits
    "Submit new issue" to queue the override.

    Uses `www.github.com` rather than `github.com` so iOS Universal Links
    don't hijack the tap into the GitHub mobile app (which drops the
    prefilled query params and lands on its home screen). `www.github.com`
    redirects to `github.com` on the web layer, so desktop / Android /
    iOS-without-the-app all still land on the prefilled issue page.
    """
    body = (
        f"project_name: {project_name}\n"
        f"role_name: {role_name}\n"
        f"platform: {platform}\n"
        f"mode: {mode}\n"
    )
    params = urllib.parse.urlencode({
        "labels": label,
        "title": f"Apply anyway: {role_name} @ {project_name}",
        "body": body,
    })
    return f"https://www.github.com/{repo}/issues/new?{params}"


# --- Issue body parser ---

def parse_issue_body(body: str) -> dict | None:
    """Pull the four required fields out of a free-form issue body.

    Format expected (ignores any other lines):
        project_name: ...
        role_name: ...
        platform: aa | backstage | cn
        mode: paid | unpaid

    Returns None on missing/invalid fields so the caller can comment
    "couldn't parse" and close the issue.
    """
    if not body or not body.strip():
        return None

    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip().lower()
        if key not in REQUIRED_FIELDS:
            continue
        value = raw_value.strip().strip('"').strip("'").strip()
        if value:
            fields.setdefault(key, value)

    if not all(k in fields for k in REQUIRED_FIELDS):
        return None
    if fields["platform"] not in ALLOWED_PLATFORMS:
        return None
    if fields["mode"] not in ALLOWED_MODES:
        return None
    return fields


# --- GitHub REST calls (urllib so we don't pull in `requests`) ---

def _request(method: str, url: str, token: str, payload: dict | None = None):
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "actorsaccess-bot",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def _github_get(path: str, token: str):
    return _request("GET", f"{GITHUB_API}{path}", token)


def _github_post(path: str, payload: dict, token: str):
    return _request("POST", f"{GITHUB_API}{path}", token, payload)


def _github_patch(path: str, payload: dict, token: str):
    return _request("PATCH", f"{GITHUB_API}{path}", token, payload)


# --- Public API ---

def fetch_pending(repo: str, label: str, token: str) -> list[OverrideRequest]:
    """Open issues with the override label, parsed and validated. Malformed
    issues are silently skipped — use fetch_pending_with_errors() if you
    need their issue numbers to comment back."""
    ok, _ = fetch_pending_with_errors(repo, label, token)
    return ok


def fetch_pending_with_errors(
    repo: str, label: str, token: str,
) -> tuple[list[OverrideRequest], list[int]]:
    """Returns (parsed_overrides, malformed_issue_numbers)."""
    path = f"/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=100"
    issues = _github_get(path, token) or []
    parsed: list[OverrideRequest] = []
    malformed: list[int] = []
    for issue in issues:
        # GitHub API returns PRs in the issues list too — skip them.
        if "pull_request" in issue:
            continue
        body = issue.get("body") or ""
        fields = parse_issue_body(body)
        if not fields:
            malformed.append(issue["number"])
            continue
        parsed.append(OverrideRequest(
            issue_number=issue["number"],
            project_name=fields["project_name"],
            role_name=fields["role_name"],
            platform=fields["platform"],
            mode=fields["mode"],
        ))
    return parsed, malformed


def comment_and_close(repo: str, issue_number: int, comment: str, token: str) -> None:
    """Post a comment, then close the issue. Errors are logged, not raised —
    we never want a digest/run to abort because GitHub burped."""
    try:
        _github_post(f"/repos/{repo}/issues/{issue_number}/comments", {"body": comment}, token)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.warning(f"[OVERRIDE] Failed to post comment on #{issue_number}: {e}")
    try:
        _github_patch(f"/repos/{repo}/issues/{issue_number}", {"state": "closed"}, token)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.warning(f"[OVERRIDE] Failed to close #{issue_number}: {e}")


# --- Cross-platform run orchestration helpers ---
#
# Each platform's main.py calls load_run_config() at startup, then (if it
# returned non-None) ingest_issues() to queue pending requests, then loops
# pending overrides for that platform with its own platform-specific applier.


def load_run_config(cfg: dict) -> tuple[dict | None, str | None]:
    """Return (overrides_cfg, token) for this run, or (None, None) if the
    override flow is disabled (missing config or OVERRIDE_GITHUB_TOKEN).

    Logs a warning when the config is partial (repo or label missing) so
    misconfigurations are visible without crashing the run.
    """
    overrides_cfg = cfg.get("overrides")
    token = os.environ.get("OVERRIDE_GITHUB_TOKEN")
    if not overrides_cfg or not token:
        return None, None
    if not overrides_cfg.get("repo") or not overrides_cfg.get("label"):
        logger.warning(
            "[OVERRIDE] config.overrides missing 'repo' or 'label' — "
            "skipping override processing"
        )
        return None, None
    return overrides_cfg, token


def ingest_issues(overrides_cfg: dict, token: str, db) -> None:
    """Pull open GitHub override issues and queue valid ones into the local DB.

    Valid issues are left OPEN until the apply path closes them with the
    outcome — closing on ingest would leave the user with no signal beyond
    "queued," which they reported as confusing. We post a one-time
    acknowledgement comment on first queue, then stay quiet on subsequent
    runs (gated by add_pending_override's return value).

    Malformed issues still get a parse-error comment + close — they're
    terminal and won't ever produce an outcome to communicate.
    """
    try:
        ensure_label_exists(overrides_cfg["repo"], overrides_cfg["label"], token)
    except Exception as e:
        logger.warning(f"[OVERRIDE] Could not ensure label exists: {e}")

    try:
        ok, malformed = fetch_pending_with_errors(
            overrides_cfg["repo"], overrides_cfg["label"], token,
        )
    except Exception as e:
        logger.error(f"[OVERRIDE] Failed to fetch override issues: {e}")
        return

    for req in ok:
        is_new = db.add_pending_override(
            issue_number=req.issue_number,
            project_name=req.project_name,
            role_name=req.role_name,
            platform=req.platform,
            mode=req.mode,
        )
        # Only acknowledge the first time we see this override — every
        # platform's run calls ingest, so re-commenting would spam the issue.
        if is_new:
            try:
                _github_post(
                    f"/repos/{overrides_cfg['repo']}/issues/{req.issue_number}/comments",
                    {"body": (
                        "Queued — will apply on the next run for this platform. "
                        "I'll comment again with the outcome and close this issue once it's processed."
                    )},
                    token,
                )
            except (urllib.error.URLError, urllib.error.HTTPError) as e:
                logger.warning(
                    f"[OVERRIDE] Failed to post queued-ack on #{req.issue_number}: {e}"
                )

    for issue_num in malformed:
        comment_and_close(
            overrides_cfg["repo"], issue_num,
            "Could not parse override request. Expected fields:\n"
            "`project_name`, `role_name`, `platform` (aa|backstage|cn), `mode` (paid|unpaid).",
            token,
        )


def ensure_label_exists(repo: str, label: str, token: str, color: str = "ff6f00") -> None:
    """Create the label if it doesn't exist yet. Idempotent — swallows
    422 (already exists) silently."""
    payload = {"name": label, "color": color, "description": "Force-apply this role on the next run."}
    try:
        _github_post(f"/repos/{repo}/labels", payload, token)
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return  # already exists
        logger.warning(f"[OVERRIDE] Failed to ensure label '{label}': {e}")
    except urllib.error.URLError as e:
        logger.warning(f"[OVERRIDE] Failed to ensure label '{label}': {e}")
