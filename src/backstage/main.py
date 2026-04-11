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

from src.backstage.config import load_backstage_config, BackstageConfigError
from src.backstage.client import BackstageClient
from src.database import Database
from src.calendar_check import check_availability, parse_shoot_dates
from src.filters import _is_background, _is_court_tv, _is_ugc, _is_unpaid, _COURT_TV_PATTERN, is_lead_or_supporting
from src.role_selector import (
    select_best_roles,
    analyze_submission_requirements,
    answer_prescreen_questions,
    check_travel_pay,
)

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
    rate_display = api_role.get("rate_display") or api_role.get("total_pay_display", "")
    parts = [role_desc]
    if rate_display:
        parts.append(f"PAY: {rate_display}")
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
        "pay": api_role.get("rate_display") or api_role.get("total_pay_display", ""),
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


def run_once(cfg: dict, db: Database, dry_run: bool = False, mode: str = "paid"):
    run_id = db.start_run(platform="backstage", mode=mode)
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

                # Process roles within the production
                candidates = []
                for api_role in production.get("roles", []):
                    role = _normalize_role(api_role, production)
                    roles_found += 1
                    unique_id = f"backstage_{project_id}_{role['role_id']}"

                    # Skip already applied (via Backstage's tracking or our DB)
                    if role["has_submission"] or api_role["id"] in backstage_applied or db.is_applied(unique_id):
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
                        # Still require Lead/Supporting/Principal/Series Regular/Recurring.
                        lead_ok, lead_reason = is_lead_or_supporting(role, "backstage")
                        if not lead_ok:
                            roles_filtered += 1
                            if dry_run:
                                _print_role_decision("SKIP", project_name, role, lead_reason)
                            else:
                                logger.info(f"Filtered (unpaid role-type): {project_name} — {role['role_name']} ({lead_reason})")
                            continue
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

                # Record rejections
                for role in candidates:
                    if role["role_name"] in rejections:
                        role_url = role.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://www.backstage.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=rejections[role["role_name"]],
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
                    # "4"=Resume, "5"=Cover Letter. We auto-attach headshot/reel/resume,
                    # but we don't generate cover letters — flag those for manual review.
                    submission_requires = best.get("submission_requires", []) or []
                    if "5" in submission_requires:
                        flag_reason = (
                            f"Cover letter required ({best.get('submission_requires_display', '')})"
                        )
                        logger.info(f"[REQUIRES] {best['role_name']}: {flag_reason}")
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

                    # Applicant questions (YES_NO / SHORT_ANSWER): try AI first.
                    # If the AI can answer all of them confidently we POST the
                    # answers via the prescreen endpoint. If even one is
                    # unanswerable (open-ended pitch, "favorite film", dates
                    # outside confirmed availability, etc.), we SUBMIT ANYWAY
                    # without the answers — same submit-anyway pattern as
                    # self-tape requests. Questions stay Pending in the
                    # Backstage UI and casting can follow up if interested.
                    prescreen_answers: list[dict] | None = None
                    if prescreen_questions:
                        ai_result = answer_prescreen_questions(
                            prescreen_questions,
                            best,
                            project_name,
                            confirmed_dates=confirmed_dates,
                        )
                        if ai_result.get("needs_input"):
                            logger.info(
                                f"[PRESCREEN] {best['role_name']}: AI couldn't answer all "
                                f"applicant questions ({ai_result['needs_input']}) "
                                f"— submitting anyway without answers"
                            )
                            # Treat this like the self-tape path — attach the
                            # cover letter note so casting knows to follow up.
                            selftape_detected = True
                            prescreen_answers = None
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
                    analysis = analyze_submission_requirements(
                        best, project_name, best.get("description", ""),
                        confirmed_dates=confirmed_dates,
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

                    if dry_run:
                        tag = "SUBMIT (AI pick)" if len(candidates) > 1 else "SUBMIT"
                        _print_role_decision(tag, project_name, best)
                        if len(candidates) > 1:
                            print(f"  AI reason: {ai_reason}")
                        if analysis["action"] == "SUBMIT_WITH_NOTE":
                            print(f"  Note: {analysis['note']}")
                        if prescreen_answers:
                            print(f"  Prescreen answers: {prescreen_answers}")
                        roles_applied += 1
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

                    submission_cfg = cfg.get("submission", {})
                    media_ids = list(submission_cfg.get("video_reel_ids", []))
                    resume_id = submission_cfg.get("resume_id")
                    if resume_id:
                        media_ids.append(resume_id)

                    # submit_for_role handles the full flow: create draft →
                    # attach media → POST prescreen answers (if any) → PUT
                    # to submit. Prescreen answers go to the real endpoint
                    # now; see client._submit_prescreen_answers.
                    logger.info(
                        f"[SUBMIT] Attempting: {project_name} — {best['role_name']} "
                        f"(id={unique_id}), media={media_ids}, "
                        f"note_len={len(note)}, prescreen_answers={len(prescreen_answers) if prescreen_answers else 0}"
                    )
                    result = client.submit_for_role(
                        best["role_id"],
                        note=note,
                        media_ids=media_ids,
                        answers=prescreen_answers,
                    )

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
                        role_url = best.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://www.backstage.com{role_url}"
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
