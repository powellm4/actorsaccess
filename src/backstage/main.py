# src/backstage/main.py
"""Backstage.com auto-apply pipeline.

Uses direct HTTP API calls (no browser automation).
"""

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

from src import overrides as overrides_mod
from src.backstage.config import load_backstage_config, BackstageConfigError
from src.backstage.client import BackstageClient
from src.database import Database
from src.override_email import send_override_results_email
from src.calendar_check import check_availability, parse_shoot_dates, parse_all_dates
from src.filters import _is_background, _is_court_tv, _is_family_casting, _is_ugc, _is_unpaid, _COURT_TV_PATTERN, extract_role_type_marker, is_lead_or_supporting, project_has_female_cast
from src.role_selector import (
    TRANSIENT_REJECTION_PREFIX,
    analyze_submission_requirements,
    answer_prescreen_questions,
    check_travel_pay,
    generate_cover_letter,
    select_best_roles,
)
from src.shadow import clear_run_context, flush_pending_shadows, set_run_context

logger = logging.getLogger("backstage")

# Production types to skip (actor doesn't do theater/musical)
_SKIP_PROD_TYPES = {"theater", "theatre", "musical"}

# Hard-coded cover letter note attached to every Backstage submission where
# a self-tape / video prescreen is detected. Casting reads this in the
# submission note and knows to ask for a tape if they're interested.
SELFTAPE_COVER_LETTER = (
    "If you think I would be a good fit for this role looks wise, "
    "I would be happy to submit a self tape. Let me know."
)


def setup_logging(log_config: dict):
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "logs/backstage_auto_apply.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _print_role_decision(tag: str, project_name: str, role: dict, reason: str = ""):
    label = f"[{tag}]" if not reason else f"[{tag} - {reason}]"
    print(f"\n{label} Project: {project_name} | Role: {role['role_name']}")
    if role.get("description"):
        desc = role["description"]
        if len(desc) > 200:
            desc = desc[:200] + "..."
        print(f"  {desc}")


def _normalize_role(api_role: dict, production: dict) -> dict:
    """Convert Backstage API role format to the shared format expected
    by role_selector.py and filters.py.

    The role description includes production-level context (project description,
    shoot location/dates) so the AI can make informed decisions about travel,
    visa requirements, etc.
    """
    # Build enriched description: role description + production context
    role_desc = api_role.get("description", "")
    prod_desc = production.get("description", "")
    prod_info = production.get("production_info", "")
    rate_display = api_role.get("rate_display", "")
    total_pay_display = api_role.get("total_pay_display", "")
    pay_str = " / ".join(p for p in (rate_display, total_pay_display) if p)
    parts = [role_desc]
    if pay_str:
        parts.append(f"PAY: {pay_str}")
    if prod_desc:
        parts.append(f"PROJECT DESCRIPTION: {prod_desc}")
    if prod_info:
        parts.append(f"SHOOT INFO: {prod_info}")
    enriched_desc = "\n".join(p for p in parts if p)

    return {
        "role_id": api_role["id"],
        "role_name": api_role.get("name", "Unknown"),
        "role_type": api_role.get("role_type_display", ""),
        "age_range": api_role.get("age_range_display", ""),
        "gender": api_role.get("gender_display", ""),
        "pay": pay_str,
        "description": enriched_desc,
        "url": api_role.get("absolute_url") or api_role.get("url", ""),
        # Backstage-specific fields
        "has_submission": api_role.get("has_submission", False),
        "prescreen_type": api_role.get("prescreen_type", ""),
        "prescreen_message": api_role.get("prescreen_message", ""),
        "project_id": production.get("id"),
        "project_name": production.get("title", "Unknown"),
        "project_type": ", ".join(
            pt.get("name", "") if isinstance(pt, dict) else str(pt)
            for pt in production.get("production_types", [])
        ),
        "union": production.get("union_status_display", ""),
    }


def _is_backstage_background(role: dict) -> bool:
    """Check if a Backstage role is background based on role_type or patterns."""
    role_type = role.get("role_type", "").lower()
    if "background" in role_type or "extra" in role_type or "bg" == role_type:
        return True
    return _is_background(role)


def _is_theater(production: dict) -> bool:
    """Check if a production is theater/musical (actor doesn't do these)."""
    prod_types = production.get("production_types", [])
    for pt in prod_types:
        # production_types can be strings or dicts with a "name" key
        name = pt.get("name", "") if isinstance(pt, dict) else str(pt)
        if name.lower() in _SKIP_PROD_TYPES:
            return True
    return False


def _is_expired(production: dict) -> bool:
    """Check if a production's casting has expired."""
    expires = production.get("expires", "")
    if not expires:
        return False
    try:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return datetime.now(exp_dt.tzinfo) > exp_dt
    except (ValueError, TypeError):
        return False


def _parse_shoot_info(production: dict) -> str | None:
    """Extract shoot date info from production_info field."""
    info = production.get("production_info", "")
    if not info:
        return None
    return info


def _apply_backstage_override(
    cfg: dict, db: Database, client: BackstageClient, override: dict,
    overrides_cfg: dict, token: str, dry_run: bool,
):
    """Force-apply a single Backstage override. Bypasses filters and AI
    selection — the user explicitly asked for this role to go through.

    Backstage's submit_for_role takes a numeric role_id rather than a URL,
    so we fetch the role detail page using the stored URL and pick the
    nested role whose name matches.
    """
    project_name = override["project_name"]
    role_name = override["role_name"]
    issue_num = override["issue_number"]
    mode = override["mode"]

    def _finish(outcome: str, detail: str, comment: str) -> dict:
        db.record_override_outcome(
            issue_number=issue_num, project_name=project_name, role_name=role_name,
            platform="backstage", mode=mode, outcome=outcome, detail=detail,
        )
        db.clear_pending_override(project_name, role_name, "backstage", mode)
        overrides_mod.comment_and_close(overrides_cfg["repo"], issue_num, comment, token)
        return {
            "issue_number": issue_num, "project_name": project_name,
            "role_name": role_name, "platform": "backstage", "mode": mode,
            "outcome": outcome, "detail": detail,
        }

    role_url = db.get_known_project_url(role_name, project_name, "backstage")
    if not role_url:
        prior = db.find_application_by_name(role_name, project_name, "backstage")
        if prior:
            logger.info(
                f"[OVERRIDE] {project_name} — {role_name} was already applied on "
                f"{prior['applied_at']} ({prior['mode']} mode). Closing override."
            )
            return _finish(
                "already_applied",
                f"Applied on {prior['applied_at']} ({prior['mode']} mode).",
                f"This role was already applied on {prior['applied_at']} ({prior['mode']} mode) — no further action needed.",
            )
        logger.warning(f"[OVERRIDE] No role URL on file for {project_name} — {role_name}")
        return _finish(
            "failed", "Role URL not on file in db.",
            "Failed: no role URL on file. The role may have been removed before it was queued.",
        )

    try:
        production = client.fetch_role_detail(role_url)
    except Exception as e:
        logger.error(f"[OVERRIDE] Backstage role-detail fetch raised: {e}")
        return _finish("failed", f"role-detail fetch raised: {e}", f"Failed: could not load role detail page ({e}).")

    if not production:
        return _finish(
            "not_found", "Role detail unavailable (page gone or Cloudflare blocked).",
            "Role detail page no longer loads. (The casting call may have been pulled.)",
        )

    project_id = production.get("id")
    matched = next(
        (r for r in production.get("roles", []) if r.get("name") == role_name),
        None,
    )
    if not matched:
        return _finish(
            "not_found",
            f"Role '{role_name}' not on detail page roles list.",
            "Role no longer visible on the project page. (The role may have been removed.)",
        )

    role_id = matched.get("id")
    if role_id is None:
        return _finish(
            "failed", "Matched role has no id field.",
            "Failed: role found on page but had no usable id.",
        )

    unique_id = f"backstage_{project_id}_{role_id}"

    if db.is_applied(unique_id):
        db.delete_rejection(role_name, project_name, "backstage")
        db.delete_flagged(role_name, project_name, "backstage")
        return _finish(
            "applied", "Already applied — no action needed",
            "Already applied — no action needed.",
        )

    if dry_run:
        print(f"\n[OVERRIDE - DRY RUN] would re-apply: {project_name} — {role_name}")
        return None

    submission_cfg = cfg.get("submission", {})
    media_ids = list(submission_cfg.get("video_reel_ids", []))
    resume_id = submission_cfg.get("resume_id")
    if resume_id:
        media_ids.append(resume_id)

    try:
        result = client.submit_for_role(role_id, note="", media_ids=media_ids, answers=None)
    except Exception as e:
        logger.error(f"[OVERRIDE] Backstage submit raised on {project_name} / {role_name}: {e}")
        return _finish("failed", str(e), f"Failed to apply: {e}")

    if isinstance(result, dict) and result.get("_rejected"):
        reason = result.get("reason", "Unknown rejection")
        return _finish("failed", reason, f"Failed to apply: Backstage rejected the submission ({reason}).")

    if not result:
        return _finish(
            "failed", "submit_for_role returned None",
            "Failed to apply: submission did not complete.",
        )

    db.record_application(
        unique_id, project_name, role_name,
        role_description="",
        ai_reason="OVERRIDE: applied via Apply Anyway",
        candidates_considered=1, platform="backstage",
        project_url=role_url, submission_note="", mode=mode,
    )
    db.delete_rejection(role_name, project_name, "backstage")
    db.delete_flagged(role_name, project_name, "backstage")
    return _finish(
        "applied", "Submitted successfully via override",
        f"Applied successfully on **{project_name}** — *{role_name}*.",
    )


def process_backstage_overrides(
    cfg: dict, db: Database, client: BackstageClient,
    run_id: int, mode: str, dry_run: bool,
):
    """Pull GitHub override issues, queue them, then apply pending Backstage
    overrides for this mode. Called once per run, right after login.

    Non-Backstage-platform overrides are left in the queue for whichever run
    handles that platform.
    """
    overrides_cfg, token = overrides_mod.load_run_config(cfg)
    if not overrides_cfg:
        overrides_mod.report_unprocessable_pending(db, "backstage")
        return

    overrides_mod.ingest_issues(overrides_cfg, token, db)

    pending = [
        o for o in db.list_pending_overrides()
        if o["platform"] == "backstage" and o["mode"] == mode
    ]
    if not pending:
        return

    logger.info(f"[OVERRIDE] Processing {len(pending)} Backstage override(s) for mode={mode}")
    outcomes: list[dict] = []
    for override in pending:
        result = _apply_backstage_override(cfg, db, client, override, overrides_cfg, token, dry_run)
        if result:
            outcomes.append(result)

    if outcomes and not dry_run:
        send_override_results_email(
            outcomes, platform="backstage", mode=mode, repo=overrides_cfg.get("repo"),
        )


def run_once(cfg: dict, db: Database, dry_run: bool = False, mode: str = "paid"):
    run_id = db.start_run(platform="backstage", mode=mode)
    set_run_context(platform="backstage", mode=mode, run_id=run_id)
    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
    logger.info(f"[RUN] Started Backstage run_id={run_id}, mode={mode}, calendar_ids={len(cal_ids)} configured")
    roles_found = 0
    roles_applied = 0
    roles_skipped = 0
    roles_filtered = 0

    client = BackstageClient()

    try:
        creds = cfg["credentials"]
        if not client.login(creds["email"], creds["password"]):
            raise RuntimeError("Backstage login failed")

        # Apply Anyway: pull queued GitHub overrides and force-apply them
        # before the normal scrape. Backstage's submit goes through a direct
        # role_id, so this doesn't need the listings to surface the role first.
        process_backstage_overrides(cfg, db, client, run_id, mode, dry_run)

        max_pages = cfg.get("max_pages", 5)
        max_subs = cfg.get("max_submissions")
        saved_search_name = cfg.get("saved_search", "acting")

        # Find the saved search to use its filters
        saved_search = None
        saved_searches = client.fetch_saved_searches()
        for ss in saved_searches:
            if ss.get("name", "").lower() == saved_search_name.lower():
                saved_search = ss
                logger.info(f"Using saved search '{ss['name']}' (id={ss['id']})")
                break
        if not saved_search:
            # Unpaid mode must not silently fall back to the default (paid)
            # filters — that would submit to paid roles under unpaid flag.
            if mode == "unpaid":
                raise RuntimeError(
                    f"Unpaid mode: saved search '{saved_search_name}' not found on Backstage. "
                    f"Create it (LA + unpaid + Lead/Supporting) before running unpaid mode."
                )
            logger.warning(f"Saved search '{saved_search_name}' not found, using default filters")

        if dry_run:
            print("\n=== DRY RUN — no submissions will be made ===\n")

        empty_pages = 0

        for page_num in range(1, max_pages + 1):
            if max_subs and roles_applied >= max_subs:
                logger.info(f"Reached max submissions ({max_subs}), stopping")
                break

            logger.info(f"Fetching page {page_num}...")
            data = client.fetch_listings(page=page_num, size=20, saved_search=saved_search)
            productions = data.get("items", [])
            backstage_applied = set(data.get("roles_applied_for", []))

            if not productions:
                logger.info(f"No productions on page {page_num}, stopping")
                break

            page_applied_before = roles_applied

            for production in productions:
                project_name = production.get("title", "Unknown")
                project_id = production.get("id")

                # Skip theater/musical productions
                if _is_theater(production):
                    prod_types = ", ".join(production.get("production_types", []))
                    roles_filtered += len(production.get("roles", []))
                    if dry_run:
                        for r in production.get("roles", []):
                            role = _normalize_role(r, production)
                            _print_role_decision("SKIP", project_name, role, f"theater/musical ({prod_types})")
                    else:
                        logger.info(f"Filtered out project: {project_name} ({prod_types})")
                    continue

                # Skip court TV projects
                if _COURT_TV_PATTERN.search(project_name):
                    roles_filtered += len(production.get("roles", []))
                    logger.info(f"Filtered out project: {project_name} (court TV)")
                    continue

                # Skip expired
                if _is_expired(production):
                    roles_filtered += len(production.get("roles", []))
                    logger.info(f"Filtered out project: {project_name} (expired)")
                    continue


                # Parse shoot dates for calendar check
                shoot_info = _parse_shoot_info(production)
                shoot_dates = parse_shoot_dates(shoot_info) if shoot_info else None
                confirmed_dates = None
                skip_project = False

                if shoot_dates and cal_ids:
                    start, end = shoot_dates
                    logger.info(f"[CALENDAR] Shoot dates for {project_name}: {start} to {end}")
                    available, conflicts = check_availability(start, end, cal_ids)
                    if not available:
                        conflict_list = ", ".join(conflicts[:5])
                        flag_reason = f"Calendar conflict: {conflict_list}"
                        cal_proj_url = production.get("url", "")
                        if not cal_proj_url.startswith("http"):
                            cal_proj_url = f"https://www.backstage.com{cal_proj_url}"
                        for r in production.get("roles", []):
                            role = _normalize_role(r, production)
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=cal_proj_url,
                                role_name=role["role_name"],
                                role_description=role["description"],
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="backstage",
                            )
                        if dry_run:
                            for r in production.get("roles", []):
                                role = _normalize_role(r, production)
                                _print_role_decision("FLAGGED", project_name, role, flag_reason)
                        skip_project = True
                    else:
                        confirmed_dates = f"{start} to {end}"

                if skip_project:
                    continue

                # Normalize all roles up front so project_has_female_cast()
                # sees the full structured gender field across the cast.
                all_roles = [
                    _normalize_role(ar, production) for ar in production.get("roles", [])
                ]

                # Process roles within the production
                candidates = []
                for role in all_roles:
                    roles_found += 1
                    unique_id = f"backstage_{project_id}_{role['role_id']}"

                    # Skip already applied (via Backstage's tracking or our DB)
                    if role["has_submission"] or role["role_id"] in backstage_applied or db.is_applied(unique_id):
                        roles_skipped += 1
                        continue

                    # Skip already rejected by AI
                    if db.is_rejected(role["role_name"], project_name, "backstage"):
                        roles_skipped += 1
                        continue

                    if _is_backstage_background(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "background role")
                        else:
                            logger.info(f"Filtered: {project_name} — {role['role_name']} (background)")
                        continue

                    if mode == "unpaid":
                        # Trust the "unpaid" saved search for the pay filter.
                        # The pay/rate text field can contain rate strings even
                        # when the role is categorized unpaid on the site, so
                        # our text _is_unpaid() check would reject things it
                        # shouldn't. Saved search is the source of truth.
                        # Require Lead/Principal/Series Regular unless the project
                        # has a female on the cast — then any role type is OK.
                        has_female = project_has_female_cast(all_roles, role, "backstage")
                        lead_ok, lead_reason = is_lead_or_supporting(
                            role, "backstage", role.get("project_type", ""),
                            has_female_cast=has_female,
                        )
                        if not lead_ok:
                            roles_filtered += 1
                            if dry_run:
                                _print_role_decision("SKIP", project_name, role, lead_reason)
                            else:
                                logger.info(f"Filtered (unpaid role-type): {project_name} — {role['role_name']} ({lead_reason})")
                            continue
                        if has_female:
                            logger.info(
                                f"Unpaid bypass (female cast): {project_name} — {role['role_name']}"
                            )
                    else:
                        if _is_unpaid(role):
                            roles_filtered += 1
                            if dry_run:
                                _print_role_decision("SKIP", project_name, role, "unpaid")
                            else:
                                logger.info(f"Filtered: {project_name} — {role['role_name']} (unpaid)")
                            continue

                    if _is_ugc(project_name, role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "UGC")
                        else:
                            logger.info(f"Filtered: {project_name} — {role['role_name']} (UGC)")
                        continue

                    if _is_court_tv(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "court TV")
                        else:
                            logger.info(f"Filtered: {project_name} — {role['role_name']} (court TV)")
                        continue

                    if _is_family_casting(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "requires real family/group unit")
                        else:
                            logger.info(f"Filtered: {project_name} — {role['role_name']} (real family/group unit)")
                        continue

                    candidates.append(role)

                if not candidates:
                    continue

                if max_subs and roles_applied >= max_subs:
                    break

                # AI role selection
                selected, rejections = select_best_roles(candidates, project_name, mode=mode)
                logger.info(
                    f"[AI] Selection for {project_name}: "
                    f"selected={[s[0]['role_name'] for s in selected]}, "
                    f"rejected={list(rejections.keys())}"
                )

                prod_url = production.get("url", "")
                project_url = prod_url if prod_url.startswith("http") else f"https://www.backstage.com{prod_url}"

                # Record rejections (skip transient AI failures so they retry next run)
                for role in candidates:
                    if role["role_name"] in rejections:
                        reason = rejections[role["role_name"]]
                        if reason.startswith(TRANSIENT_REJECTION_PREFIX):
                            logger.info(
                                f"[TRANSIENT] Re-queuing for next run: "
                                f"{project_name} — {role['role_name']} ({reason})"
                            )
                            continue
                        role_url = role.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://www.backstage.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=reason,
                            run_id=run_id,
                            platform="backstage",
                            mode=mode,
                        )

                if not selected:
                    ai_reason = next(iter(rejections.values()), "AI skipped")
                    logger.info(f"AI skipped project: {project_name} — {ai_reason}")
                    if dry_run:
                        for role in candidates:
                            _print_role_decision("SKIP", project_name, role, f"AI: {ai_reason}")
                    continue

                for best, ai_reason in selected:
                    unique_id = f"backstage_{best['project_id']}_{best['role_id']}"

                    # Programmatic travel pay check (overrides AI)
                    # Include pay field since Backstage stores pay as structured metadata
                    pay_text = best.get("pay", "")
                    tp_ok, tp_reason = check_travel_pay(
                        project_name,
                        f"{best.get('description', '')} Pay: {pay_text}" if pay_text else best.get("description", ""),
                        production.get("project_notes", ""),
                        mode=mode,
                    )
                    if not tp_ok:
                        logger.info(f"[TRAVEL PAY] Skipping {best['role_name']} on {project_name}: {tp_reason}")
                        role_url = best.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://www.backstage.com{role_url}"
                        # LEAD/PRINCIPAL roles at fly-to locations are flagged for
                        # manual review rather than hard-rejected — a career-defining
                        # role may be worth the travel cost even below the threshold.
                        _rtype = (best.get("role_type") or "").upper() or (extract_role_type_marker(best.get("description", "")) or "")
                        if _rtype in ("LEAD", "PRINCIPAL", "SERIES REGULAR") and "fly-to" in (tp_reason or ""):
                            flag_reason = f"LEAD role below fly-to threshold — {tp_reason}. Apply manually if it's a strong type match."
                            logger.info(f"[TRAVEL PAY] Flagging LEAD {best['role_name']} on {project_name} for review")
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=role_url or project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="backstage",
                                mode=mode,
                            )
                        else:
                            db.record_rejection(
                                project_name=project_name,
                                project_url=role_url or project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                rejection_reason=tp_reason,
                                run_id=run_id,
                                platform="backstage",
                                mode=mode,
                            )
                        continue

                    # Fetch role detail page for full data (prescreen, attachments, etc.)
                    role_url = best.get("url", "") or prod_url
                    detail_enriched = False
                    if role_url:
                        logger.info(f"Fetching detail page for {best['role_name']}...")
                        detail = client.fetch_role_detail(role_url)
                        if detail:
                            # Find our role in the detail data and enrich
                            for detail_role in detail.get("roles", []):
                                if detail_role.get("id") == best["role_id"]:
                                    best["prescreen_type"] = detail_role.get("prescreen_type", "")
                                    best["prescreen_message"] = detail_role.get("prescreen_message", "")
                                    best["prescreen_questions"] = detail_role.get("prescreen_questions", [])
                                    best["submission_requires"] = detail_role.get("submission_requires", []) or []
                                    best["submission_requires_display"] = detail_role.get("submission_requires_display", "") or ""
                                    # Use the full description from detail if longer
                                    detail_desc = detail_role.get("description", "")
                                    if len(detail_desc) > len(best.get("description", "")):
                                        best["description"] = detail_desc
                                    detail_enriched = True
                                    break

                    # If we couldn't enrich the role from the detail page, we don't
                    # know the prescreen state — flag rather than submit blind.
                    if not detail_enriched:
                        flag_reason = "Could not fetch role detail — prescreen state unknown"
                        logger.warning(
                            f"[DETAIL] Enrichment failed for {best['role_name']} at {role_url} — flagging"
                        )
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=flag_reason,
                            run_id=run_id,
                            platform="backstage",
                            mode=mode,
                        )
                        if dry_run:
                            _print_role_decision("FLAGGED", project_name, best, flag_reason)
                        continue

                    # Backstage submission_requires codes: "0"=Headshot, "1"=Video Reel,
                    # "4"=Resume, "5"=Cover Letter. Headshot/reel/resume auto-attach
                    # from media_ids. Cover letter ("5") routes into prepare_only: we
                    # create the draft, attach media, answer prescreen, and stop before
                    # the final PUT. The user lands in Backstage, pastes/edits the AI
                    # cover letter from the digest, and submits manually.
                    submission_requires = best.get("submission_requires", []) or []
                    prepare_only = "5" in submission_requires
                    if prepare_only:
                        logger.info(
                            f"[REQUIRES] {best['role_name']}: cover letter required "
                            f"({best.get('submission_requires_display','')}) — preparing draft only"
                        )

                    # Check for prescreen/self-tape requirements
                    # Backstage roles can have:
                    #   prescreen_type: "V" = video, "T" = text, "N" = none
                    #   prescreen_message: free-text instructions (self-tape, etc.)
                    #   prescreen_questions: list of applicant questions (YES_NO, SHORT_ANSWER)
                    prescreen_type = best.get("prescreen_type", "")
                    prescreen_msg = best.get("prescreen_message", "")
                    prescreen_questions = best.get("prescreen_questions", [])
                    msg_preview = repr(prescreen_msg[:50]) if prescreen_msg else "''"
                    logger.info(
                        f"[PRESCREEN] {best['role_name']}: type={prescreen_type!r}, "
                        f"msg={msg_preview}, questions={len(prescreen_questions)}, enriched={detail_enriched}"
                    )
                    # Video prescreen / free-text self-tape: submit anyway and
                    # attach the cover-letter note so casting knows to ask for a tape.
                    selftape_detected = False
                    if prescreen_type == "V" or prescreen_msg:
                        selftape_detected = True
                        logger.info(
                            f"[SELFTAPE] {best['role_name']}: prescreen requests self-tape "
                            f"— will submit anyway with cover letter"
                        )

                    # Check calendar for dates mentioned in prescreen questions
                    # so the AI can answer callback/audition availability questions.
                    unavailable_dates: str | None = None
                    if prescreen_questions and cal_ids:
                        all_q_text = " ".join(q.get("text", "") for q in prescreen_questions)
                        q_date_ranges = parse_all_dates(all_q_text)
                        extra_confirmed: list[str] = []
                        extra_unavailable: list[str] = []
                        for q_start, q_end in q_date_ranges:
                            if confirmed_dates and q_start >= confirmed_dates.split(" to ")[0] and q_end <= confirmed_dates.split(" to ")[-1]:
                                continue
                            available, conflicts = check_availability(q_start, q_end, cal_ids)
                            date_label = q_start if q_start == q_end else f"{q_start} to {q_end}"
                            if available:
                                extra_confirmed.append(date_label)
                            else:
                                extra_unavailable.append(date_label)
                        if extra_confirmed:
                            if confirmed_dates:
                                confirmed_dates = confirmed_dates + ", " + ", ".join(extra_confirmed)
                            else:
                                confirmed_dates = ", ".join(extra_confirmed)
                            logger.info(f"[CALENDAR] Expanded confirmed_dates with prescreen question dates: {confirmed_dates}")
                        if extra_unavailable:
                            unavailable_dates = ", ".join(extra_unavailable)
                            logger.info(f"[CALENDAR] Unavailable dates from prescreen questions: {unavailable_dates}")

                    prescreen_answers: list[dict] | None = None
                    if prescreen_questions:
                        ai_result = answer_prescreen_questions(
                            prescreen_questions,
                            best,
                            project_name,
                            confirmed_dates=confirmed_dates,
                            unavailable_dates=unavailable_dates,
                        )
                        if ai_result.get("needs_input"):
                            logger.info(
                                f"[PRESCREEN] {best['role_name']}: AI couldn't answer any "
                                f"applicant questions ({ai_result['needs_input']}) "
                                f"— submitting anyway without answers"
                            )
                            selftape_detected = True
                            prescreen_answers = None
                        elif ai_result.get("partial"):
                            prescreen_answers = ai_result.get("answers") or []
                            logger.info(
                                f"[PRESCREEN] AI partially answered questions for {best['role_name']} "
                                f"({ai_result.get('unanswered_reason')}) "
                                f"— submitting partial answers, unanswered stay pending"
                            )
                        else:
                            prescreen_answers = ai_result.get("answers") or []
                            logger.info(
                                f"[PRESCREEN] AI answered {len(prescreen_answers)} question(s) for {best['role_name']}"
                            )

                    # Also check description for self-tape submission requests
                    desc_lower = best.get("description", "").lower()
                    submit_phrases = [
                        "submit a self-tape", "submit your self-tape",
                        "send a self-tape", "send your self-tape",
                        "provide a self-tape", "upload a self-tape",
                        "self-tape required", "self tape required",
                    ]
                    if any(sp in desc_lower for sp in submit_phrases):
                        selftape_detected = True
                        logger.info(
                            f"[SELFTAPE] {best['role_name']}: description requests self-tape "
                            f"— will submit anyway with cover letter"
                        )

                    # Analyze submission requirements
                    has_media = bool(cfg.get("submission", {}).get("video_reel_ids"))
                    analysis = analyze_submission_requirements(
                        best, project_name, best.get("description", ""),
                        confirmed_dates=confirmed_dates, mode=mode, has_media=has_media,
                    )
                    logger.info(f"[ANALYSIS] {best['role_name']}: action={analysis['action']}, note={analysis.get('note', 'N/A')}")

                    if analysis["action"] == "NEEDS_INPUT":
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=analysis["needs_input_reason"],
                            run_id=run_id,
                            platform="backstage",
                            mode=mode,
                        )
                        if dry_run:
                            _print_role_decision("FLAGGED", project_name, best, analysis["needs_input_reason"])
                        continue

                    # Build the submission note. Priority when composing:
                    #   1. Cover letter (if self-tape detected)
                    #   2. AI-generated casting-note (if analysis said SUBMIT_WITH_NOTE)
                    # Prescreen Q&A is NOT stuffed into the note anymore — it
                    # goes through the real prescreen endpoint via the answers
                    # kwarg on submit_for_role.
                    note_parts: list[str] = []
                    if selftape_detected:
                        logger.info(f"[SELFTAPE] Attaching cover letter note for {best['role_name']}")
                        note_parts.append(SELFTAPE_COVER_LETTER)
                    if analysis["action"] == "SUBMIT_WITH_NOTE" and analysis.get("note"):
                        note_parts.append(analysis["note"])
                    note = "\n\n".join(note_parts)

                    # For prepare_only roles, generate a suggested cover letter the
                    # user can paste into Backstage. Only on the non-dry-run path to
                    # avoid a live AI call during dry runs.
                    suggested_cover_letter = ""
                    if prepare_only and not dry_run:
                        suggested_cover_letter = generate_cover_letter(best, project_name)

                    if dry_run:
                        if prepare_only:
                            tag = "DRAFT-ONLY (cover letter)"
                        else:
                            tag = "SUBMIT (AI pick)" if len(candidates) > 1 else "SUBMIT"
                        _print_role_decision(tag, project_name, best)
                        if len(candidates) > 1:
                            print(f"  AI reason: {ai_reason}")
                        if analysis["action"] == "SUBMIT_WITH_NOTE":
                            print(f"  Note: {analysis['note']}")
                        if prescreen_answers:
                            print(f"  Prescreen answers: {prescreen_answers}")
                        if not prepare_only:
                            roles_applied += 1
                        continue

                    submission_cfg = cfg.get("submission", {})
                    media_ids = list(submission_cfg.get("video_reel_ids", []))
                    resume_id = submission_cfg.get("resume_id")
                    if resume_id:
                        media_ids.append(resume_id)

                    # submit_for_role handles the full flow: create draft →
                    # attach media → POST prescreen answers (if any) → PUT
                    # to submit. Prescreen answers go to the real endpoint
                    # now; see client._submit_prescreen_answers.
                    submit_note = "" if prepare_only else note
                    logger.info(
                        f"[SUBMIT] Attempting: {project_name} — {best['role_name']} "
                        f"(id={unique_id}), media={media_ids}, "
                        f"note_len={len(submit_note)}, prescreen_answers={len(prescreen_answers) if prescreen_answers else 0}, "
                        f"prepare_only={prepare_only}"
                    )
                    result = client.submit_for_role(
                        best["role_id"],
                        note=submit_note,
                        media_ids=media_ids,
                        answers=prescreen_answers,
                        prepare_only=prepare_only,
                    )

                    role_url = best.get("url", "")
                    if role_url and not role_url.startswith("http"):
                        role_url = f"https://www.backstage.com{role_url}"

                    if prepare_only:
                        display_suffix = best.get("submission_requires_display", "")
                        if isinstance(result, dict) and result.get("_prepared"):
                            app_id = result["app_id"]
                            flag_reason = (
                                f"Cover letter required ({display_suffix}) "
                                f"— draft ready in Backstage"
                            )
                            logger.info(
                                f"[PREPARE-ONLY] Draft {app_id} ready for {best['role_name']} on {project_name}"
                            )
                            # Record applied first so is_applied dedupes future runs
                            # even if the flagged insert later fails.
                            db.record_application(
                                unique_id, project_name, best["role_name"],
                                role_description=best.get("description", ""),
                                ai_reason=ai_reason,
                                candidates_considered=len(candidates),
                                platform="backstage",
                                project_url=role_url,
                                submission_note=suggested_cover_letter,
                                mode=mode,
                                status="draft",
                            )
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=role_url or project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="backstage",
                                mode=mode,
                                suggested_note=suggested_cover_letter,
                                draft_app_id=app_id,
                            )
                        else:
                            if isinstance(result, dict) and result.get("_rejected"):
                                reason = result.get("reason", "draft rejected")
                                logger.warning(
                                    f"[PREPARE-ONLY] REJECTED by Backstage for {best['role_name']}: {reason}"
                                )
                                flag_reason = (
                                    f"Cover letter required ({display_suffix}) "
                                    f"— draft rejected by Backstage ({reason}), submit manually"
                                )
                            else:
                                logger.warning(
                                    f"[PREPARE-ONLY] Draft preparation failed for {best['role_name']}"
                                )
                                flag_reason = (
                                    f"Cover letter required ({display_suffix}) "
                                    f"— draft preparation failed, submit manually"
                                )
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="backstage",
                                mode=mode,
                                suggested_note=suggested_cover_letter,
                            )
                        continue

                    if isinstance(result, dict) and result.get("_rejected"):
                        reason = result.get("reason", "Unknown rejection")
                        logger.warning(f"[SUBMIT] REJECTED by Backstage: {best['role_name']} on {project_name} — {reason}")
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=f"Backstage rejected: {reason}",
                            run_id=run_id,
                            platform="backstage",
                            mode=mode,
                        )
                    elif result:
                        logger.info(f"[SUBMIT] SUCCESS: {best['role_name']} on {project_name}")
                        db.record_application(
                            unique_id, project_name, best["role_name"],
                            role_description=best.get("description", ""),
                            ai_reason=ai_reason,
                            candidates_considered=len(candidates),
                            platform="backstage",
                            project_url=role_url,
                            submission_note=note,
                            mode=mode,
                        )
                        roles_applied += 1
                    else:
                        logger.warning(f"[SUBMIT] FAILED: {best['role_name']} on {project_name}")

                # Print rejected roles in dry run
                if dry_run and rejections:
                    for role in candidates:
                        if role["role_name"] in rejections:
                            _print_role_decision("SKIP", project_name, role, rejections[role["role_name"]])

            # Track consecutive pages with no submissions
            if roles_applied > page_applied_before:
                empty_pages = 0
            else:
                empty_pages += 1
                if empty_pages >= 3:
                    logger.info("3 consecutive pages with no new submissions, stopping")
                    break

        db.complete_run(run_id, roles_found, roles_applied, roles_skipped)

        summary = (
            f"Run complete: {roles_applied} {'would apply' if dry_run else 'applied'}, "
            f"{roles_filtered} filtered out, {roles_skipped} already applied, "
            f"{roles_found} total roles found"
        )
        if dry_run:
            print(f"\n=== {summary} ===")
        logger.info(summary)

    except Exception as e:
        logger.error(f"Run failed: {e}")
        db.fail_run(run_id, str(e))
        raise

    finally:
        try:
            flush_pending_shadows(timeout=60)
        except Exception as flush_err:
            logger.warning(f"[SHADOW] flush_pending_shadows failed: {flush_err}")
        clear_run_context()


def main():
    parser = argparse.ArgumentParser(description="Backstage Auto-Apply Tool")
    parser.add_argument("--config", default=None, help="Path to config file (default: backstage_config.yaml, or backstage_config_unpaid.yaml when --mode unpaid)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument(
        "--mode", choices=["paid", "unpaid"], default="paid",
        help="paid (default) or unpaid (LA-local Lead/Supporting only)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without submitting")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to process")
    parser.add_argument("--max-submissions", type=int, default=None, help="Max roles to submit for")
    args = parser.parse_args()

    config_path = args.config
    if config_path is None:
        config_path = "backstage_config_unpaid.yaml" if args.mode == "unpaid" else "backstage_config.yaml"

    try:
        cfg = load_backstage_config(config_path)
    except BackstageConfigError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    if args.max_pages is not None:
        cfg["max_pages"] = args.max_pages
    if args.max_submissions is not None:
        cfg["max_submissions"] = args.max_submissions

    setup_logging(cfg["logging"])

    os.makedirs("data", exist_ok=True)
    db = Database("data/applied.db")

    if args.dry_run:
        args.once = True

    if args.once:
        logger.info(f"Running single pass (mode={args.mode})" + (" (dry run)" if args.dry_run else ""))
        run_once(cfg, db, dry_run=args.dry_run, mode=args.mode)
    else:
        import schedule as sched
        interval = 4
        logger.info(f"Starting scheduler (mode={args.mode}) — running every {interval} hours")
        run_once(cfg, db, mode=args.mode)
        sched.every(interval).hours.do(run_once, cfg, db, mode=args.mode)
        try:
            while True:
                import time
                sched.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")

    db.close()


if __name__ == "__main__":
    main()
