# src/main.py
import argparse
import logging
import os
import sys
import time

import re

import schedule

from src import overrides as overrides_mod
from src.config import load_config, ConfigError
from src.database import Database
from src.browser import ActorsAccessBrowser
from src.override_email import send_override_results_email
from src.filters import role_matches, project_matches, is_sag_only, is_lead_or_supporting, project_has_female_cast
from src.role_selector import (
    TRANSIENT_REJECTION_PREFIX,
    analyze_submission_requirements,
    check_partial_availability,
    check_travel_pay,
    select_best_roles,
)
from src.shadow import clear_run_context, flush_pending_shadows, set_run_context
from src.calendar_check import parse_shoot_dates, check_availability, get_busy_dates

logger = logging.getLogger("actorsaccess")


def setup_logging(log_config: dict):
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "logs/auto_apply.log")

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
    """Print a formatted role decision line for dry-run output."""
    label = f"[{tag}]" if not reason else f"[{tag} - {reason}]"
    print(f"\n{label} Project: {project_name} | Role: {role['role_name']}")
    if role.get("description"):
        desc = role["description"]
        if len(desc) > 200:
            desc = desc[:200] + "..."
        print(f"  {desc}")


def _apply_aa_override(
    cfg: dict, db: Database, browser, override: dict,
    overrides_cfg: dict, token: str, dry_run: bool,
):
    """Force-apply a single AA-platform override role. Bypasses all of the
    normal filters / AI selection / travel-pay / calendar checks — the user
    explicitly asked for this role to go through."""
    project_name = override["project_name"]
    role_name = override["role_name"]
    issue_num = override["issue_number"]
    mode = override["mode"]

    def _finish(outcome: str, detail: str, comment: str) -> dict:
        db.record_override_outcome(
            issue_number=issue_num, project_name=project_name, role_name=role_name,
            platform="aa", mode=mode, outcome=outcome, detail=detail,
        )
        db.clear_pending_override(project_name, role_name, "aa", mode)
        overrides_mod.comment_and_close(overrides_cfg["repo"], issue_num, comment, token)
        return {
            "issue_number": issue_num, "project_name": project_name,
            "role_name": role_name, "platform": "aa", "mode": mode,
            "outcome": outcome, "detail": detail,
        }

    project_url = db.get_known_project_url(role_name, project_name, "aa")
    if not project_url:
        logger.warning(f"[OVERRIDE] No project URL on file for {project_name} — {role_name}")
        return _finish(
            "failed",
            "Project URL not on file in db.",
            (
                f"Failed to apply: **{project_name}** — *{role_name}* was not found in the bot's "
                f"project database. This usually means the breakdown expired or was removed before "
                f"it was scraped.\n\n"
                f"**To apply manually:** search for \"{project_name}\" on Actors Access and submit "
                f"directly for the \"{role_name}\" role. If the project is no longer listed, the "
                f"breakdown has likely closed."
            ),
        )

    if dry_run:
        print(f"\n[OVERRIDE - DRY RUN] would re-apply: {project_name} — {role_name}")
        # Don't dequeue in dry-run so the real run still picks it up.
        return None

    try:
        roles, _ = browser.scrape_roles_on_project(project_url)
    except Exception as e:
        logger.error(f"[OVERRIDE] Scrape failed for {project_url}: {e}")
        return _finish("failed", f"Scrape failed: {e}", f"Failed to scrape project page: {e}")

    matched = next((r for r in roles if r["role_name"] == role_name), None)
    if not matched:
        return _finish(
            "not_found",
            "Role no longer visible on project page",
            "Role no longer visible on the project page. (The breakdown may have been pulled or the role removed.)",
        )

    bid_match = re.search(r"breakdown=(\d+)", project_url)
    breakdown_id = bid_match.group(1) if bid_match else f"override{issue_num}"
    unique_id = f"{breakdown_id}_{matched['role_id']}"

    if db.is_applied(unique_id):
        db.delete_rejection(role_name, project_name, "aa")
        db.delete_flagged(role_name, project_name, "aa")
        return _finish(
            "applied",
            "Already applied — no action needed",
            "Already applied — no action needed.",
        )

    sub_cfg = cfg["submission"].copy()
    # Apply Anyway = plain submit, no special note (per the brainstorm decision
    # for needs-attention overrides).
    sub_cfg["default_note"] = ""

    try:
        result = browser.submit_for_role(matched, project_name, sub_cfg)
        err_detail = "" if result else "browser.submit_for_role returned False"
    except Exception as e:
        logger.error(f"[OVERRIDE] Submit raised on {project_name} / {role_name}: {e}")
        result = False
        err_detail = str(e)

    if result:
        db.record_application(
            unique_id, project_name, role_name,
            role_description=matched.get("description", ""),
            ai_reason="OVERRIDE: applied via Apply Anyway",
            candidates_considered=1, platform="aa",
            project_url=project_url, submission_note="", mode=mode,
        )
        db.delete_rejection(role_name, project_name, "aa")
        db.delete_flagged(role_name, project_name, "aa")
        return _finish(
            "applied",
            "Submitted successfully via override",
            f"Applied successfully on **{project_name}** — *{role_name}*.",
        )
    return _finish(
        "failed",
        err_detail,
        f"Failed to apply: {err_detail}",
    )


def process_aa_overrides(
    cfg: dict, db: Database, browser, run_id: int, mode: str, dry_run: bool,
):
    """Pull GitHub override issues, queue them, then apply pending AA
    overrides for this mode. Called once per run, right after login.

    Non-AA-platform overrides are left in the queue for whichever run
    handles that platform (no-op here)."""
    overrides_cfg, token = overrides_mod.load_run_config(cfg)
    if not overrides_cfg:
        overrides_mod.report_unprocessable_pending(db, "aa")
        return

    overrides_mod.ingest_issues(overrides_cfg, token, db)

    pending = [
        o for o in db.list_pending_overrides()
        if o["platform"] == "aa" and o["mode"] == mode
    ]
    if not pending:
        return

    logger.info(f"[OVERRIDE] Processing {len(pending)} AA override(s) for mode={mode}")
    outcomes: list[dict] = []
    for override in pending:
        result = _apply_aa_override(cfg, db, browser, override, overrides_cfg, token, dry_run)
        if result:
            outcomes.append(result)

    if outcomes and not dry_run:
        send_override_results_email(
            outcomes, platform="aa", mode=mode, repo=overrides_cfg.get("repo"),
        )


def run_once(cfg: dict, db: Database, dry_run: bool = False, mode: str = "paid"):
    """Execute a single scrape-and-apply run.

    Flow:
    1. Login
    2. Navigate to breakdowns, apply filters
    3. For each page of results:
       - Scrape project listings
       - Skip projects already fully submitted
       - For each new project: navigate to it, scrape roles, filter, submit
    4. Record everything in the database

    If dry_run is True, print what would be submitted without actually submitting.
    In unpaid mode, only roles explicitly marked Lead/Supporting/Principal/
    Series Regular/Recurring and shooting in LA are accepted.
    """
    run_id = db.start_run(platform="aa", mode=mode)
    set_run_context(platform="aa", mode=mode, run_id=run_id)
    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
    logger.info(f"[RUN] Started AA run_id={run_id}, mode={mode}, calendar_ids={len(cal_ids)} configured")
    roles_found = 0
    roles_applied = 0
    roles_skipped = 0
    roles_filtered = 0

    browser = ActorsAccessBrowser(headless=cfg["browser"]["headless"])

    try:
        browser.start()

        # Login (with one retry)
        creds = cfg["credentials"]
        if not browser.login(creds["username"], creds["password"]):
            logger.warning("Login failed, retrying once...")
            if not browser.login(creds["username"], creds["password"]):
                raise RuntimeError("Login failed after retry")

        # Apply Anyway: pull queued GitHub overrides and force-apply them
        # before the normal scrape. They use direct project-URL navigation,
        # so they don't depend on the breakdown filter pulling the project up.
        process_aa_overrides(cfg, db, browser, run_id, mode, dry_run)

        # Navigate and filter
        filters = cfg["filters"]
        max_pages = cfg.get("max_pages", 5)
        max_subs = cfg.get("max_submissions")

        # Support both single region (legacy) and multiple regions
        raw_regions = filters.get("regions") or [filters.get("region", "")]
        # Normalize to list of dicts: {"region": name, "max_pages": N}
        regions = []
        for r in raw_regions:
            if isinstance(r, str):
                regions.append({"region": r, "max_pages": max_pages})
            else:
                regions.append({"region": r["region"], "max_pages": r.get("max_pages", max_pages)})

        if dry_run:
            print("\n=== DRY RUN — no submissions will be made ===\n")

        hit_limit = False
        for region_cfg in regions:
            if hit_limit:
                break

            region = region_cfg["region"]
            region_max_pages = region_cfg["max_pages"]

            logger.info(f"[REGION] Processing region: {region} (max_pages={region_max_pages})")
            if dry_run:
                print(f"\n--- Region: {region} ---\n")

            if not browser.navigate_to_breakdowns(region):
                logger.warning(f"Failed to navigate to breakdowns for region: {region}, skipping")
                continue

            browser.apply_filters(filters)

            total_pages = browser.get_total_pages()
            pages_to_process = min(total_pages, region_max_pages)
            logger.info(f"Processing {pages_to_process} of {total_pages} pages for {region}")

            consecutive_seen = 0
            hit_old = False

            for page_num in range(1, pages_to_process + 1):
                if hit_limit or hit_old:
                    break
                if page_num > 1:
                    browser.navigate_to_breakdowns(region)
                    browser.apply_filters(filters)
                    if not browser.go_to_page(page_num):
                        break

                # Scrape project listings on this page
                projects = browser.scrape_projects()

                for project in projects:
                    # Check if we've already seen this project.
                    # Paid mode: use the site's "already_submitted" flag and
                    # DB dedup (mode-agnostic — matches historical behavior).
                    # Unpaid mode: only count DB rows tagged mode='unpaid'.
                    # Paid mode has already chewed through the LA page once,
                    # so the site flag + mode-agnostic DB check would trip
                    # the consecutive-seen guard almost immediately and cut
                    # the unpaid run off after one project.
                    if mode == "unpaid":
                        already_seen = db.has_seen_breakdown(
                            project["breakdown_id"], mode="unpaid"
                        )
                    else:
                        already_seen = project["already_submitted"] or db.has_seen_breakdown(
                            project["breakdown_id"]
                        )
                    if already_seen:
                        consecutive_seen += 1
                        logger.debug(
                            f"Already seen ({consecutive_seen}): {project['project_name']}"
                        )
                        if consecutive_seen >= 3:
                            logger.info(
                                f"[REGION] Hit 3 consecutive old listings in {region}, "
                                f"moving to next region"
                            )
                            hit_old = True
                            break
                        continue
                    consecutive_seen = 0

                    # Skip excluded project types (e.g., theater)
                    proj_ok, proj_reason = project_matches(project)
                    if not proj_ok:
                        if dry_run:
                            print(f"\n[SKIP PROJECT - {proj_reason}] {project['project_name']}")
                        else:
                            logger.info(f"Skipping project: {project['project_name']} ({proj_reason})")
                        continue

                    # Navigate into the project to see individual roles
                    roles, project_notes = browser.scrape_roles_on_project(project["url"])
                    roles_found += len(roles)

                    # Build full project URL for AA
                    project_url = f"https://actorsaccess.com{project['url']}" if project.get("url") else ""

                    # Check for SAG-only projects (actor is non-union)
                    if is_sag_only(project_notes):
                        logger.info(f"[FILTER] Skipping SAG-only project: {project['project_name']}")
                        if dry_run:
                            print(f"\n[SKIP PROJECT - SAG-AFTRA members only] {project['project_name']}")
                        continue

                    # Check shoot dates against calendar BEFORE filtering/AI
                    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
                    logger.info(f"[CALENDAR] project_notes for {project['project_name']} (length={len(project_notes)}): {project_notes[:200]}")
                    shoot_dates = parse_shoot_dates(project_notes)
                    partial_availability = None  # set if some days free, some busy
                    if shoot_dates:
                        start_date, end_date = shoot_dates
                        logger.info(f"[CALENDAR] Shoot dates found: {start_date} to {end_date}, checking calendar...")
                        dates_available, conflicts = check_availability(start_date, end_date, cal_ids)
                        if not dates_available:
                            # Get per-day breakdown to check partial availability
                            busy_dates = get_busy_dates(start_date, end_date, cal_ids)
                            from datetime import date, timedelta
                            start_d = date.fromisoformat(start_date)
                            end_d = date.fromisoformat(end_date)
                            total_days = (end_d - start_d).days + 1
                            free_days = total_days - len(busy_dates)

                            if free_days == 0:
                                # ALL days are busy — skip entirely
                                flag_reason = f"Calendar conflict with ALL shoot dates {start_date} to {end_date}: {', '.join(conflicts[:5])}"
                                logger.info(f"[CALENDAR] Skipping entire project: {project['project_name']} — {flag_reason}")
                                for role in roles:
                                    db.record_flagged_role(
                                        project_name=project["project_name"],
                                        project_url=project_url,
                                        role_name=role["role_name"],
                                        role_description=role.get("description", ""),
                                        flag_reason=flag_reason,
                                        run_id=run_id,
                                        platform="aa",
                                        mode=mode,
                                    )
                                if dry_run:
                                    for role in roles:
                                        _print_role_decision("FLAGGED", project["project_name"], role, flag_reason)
                                continue
                            else:
                                # Some days free — let AI decide per-role if partial availability works
                                all_dates_set = {(start_d + timedelta(days=i)).isoformat() for i in range(total_days)}
                                free_dates = sorted(all_dates_set - set(busy_dates))
                                partial_availability = {
                                    "shoot_range": f"{start_date} to {end_date}",
                                    "busy_dates": busy_dates,
                                    "free_dates": free_dates,
                                    "free_count": free_days,
                                    "total_days": total_days,
                                }
                                logger.info(
                                    f"[CALENDAR] Partial availability for {project['project_name']}: "
                                    f"{free_days}/{total_days} days free — will let AI decide per-role"
                                )
                    else:
                        logger.info(f"[CALENDAR] No shoot dates parsed for {project['project_name']} — calendar check skipped")

                    # Log all scraped roles with fit_for_me status
                    for role in roles:
                        fit = role.get("fit_for_me", False)
                        logger.info(
                            f"Scraped role: {project['project_name']} — "
                            f"{role['role_name']} (fit_for_me={fit}, id={role.get('role_id', '?')})"
                        )

                    # Filter roles and collect candidates (skip already-applied roles)
                    candidates = []
                    for role in roles:
                        # Skip roles we've already applied for or rejected
                        unique_id = f"{project['breakdown_id']}_{role['role_id']}"
                        if db.is_applied(unique_id):
                            roles_skipped += 1
                            logger.info(
                                f"Already applied: {project['project_name']} — "
                                f"{role['role_name']} (id={unique_id})"
                            )
                            continue

                        if db.is_rejected(role["role_name"], project["project_name"], "aa"):
                            roles_skipped += 1
                            logger.info(
                                f"Already rejected: {project['project_name']} — "
                                f"{role['role_name']}"
                            )
                            continue

                        matches, skip_reason = role_matches(role, mode=mode)
                        if not matches:
                            roles_filtered += 1
                            if dry_run:
                                _print_role_decision("SKIP", project["project_name"], role, skip_reason)
                            else:
                                logger.info(
                                    f"Filtered out: {project['project_name']} — "
                                    f"{role['role_name']} ({skip_reason})"
                                )
                            continue

                        # Unpaid mode: require Lead/Principal/Series Regular
                        # marker in the description (AA has no structured role_type field),
                        # unless the project has a female on the cast — then any role type
                        # is accepted.
                        if mode == "unpaid":
                            has_female = project_has_female_cast(roles, role, "aa")
                            lead_ok, lead_reason = is_lead_or_supporting(
                                role, "aa", project.get("project_type", ""),
                                has_female_cast=has_female,
                            )
                            if not lead_ok:
                                roles_filtered += 1
                                if dry_run:
                                    _print_role_decision("SKIP", project["project_name"], role, lead_reason)
                                else:
                                    logger.info(
                                        f"Filtered out (unpaid role-type): {project['project_name']} — "
                                        f"{role['role_name']} ({lead_reason})"
                                    )
                                continue
                            if has_female:
                                logger.info(
                                    f"Unpaid bypass (female cast): {project['project_name']} — "
                                    f"{role['role_name']}"
                                )

                        # Enrich description with project context so AI
                        # can apply travel pay rules
                        if project_notes:
                            role["description"] = (
                                role.get("description", "") + "\n"
                                f"PROJECT NOTES: {project_notes[:2000]}"
                            )

                        candidates.append(role)

                    logger.info(
                        f"Candidates for {project['project_name']}: "
                        f"{[c['role_name'] for c in candidates]} "
                        f"(from {len(roles)} scraped, {roles_skipped} already applied, {roles_filtered} filtered)"
                    )

                    if not candidates:
                        continue

                    # Check submission cap
                    if max_subs and roles_applied >= max_subs:
                        logger.info(f"Reached max submissions ({max_subs}), stopping")
                        hit_limit = True
                        break

                    # Pick the best role(s) (AI selection if multiple candidates)
                    selected, rejections = select_best_roles(candidates, project["project_name"], mode=mode)
                    logger.info(
                        f"AI selection for {project['project_name']}: "
                        f"selected={[s[0]['role_name'] for s in selected]}, "
                        f"rejected={list(rejections.keys())}"
                    )

                    # Record rejections (skip transient AI failures so they retry next run)
                    for role in candidates:
                        if role["role_name"] in rejections:
                            reason = rejections[role["role_name"]]
                            if reason.startswith(TRANSIENT_REJECTION_PREFIX):
                                logger.info(
                                    f"[TRANSIENT] Re-queuing for next run: "
                                    f"{project['project_name']} — {role['role_name']} ({reason})"
                                )
                                continue
                            db.record_rejection(
                                project_name=project["project_name"],
                                project_url=project_url,
                                role_name=role["role_name"],
                                role_description=role.get("description", ""),
                                rejection_reason=reason,
                                run_id=run_id,
                                platform="aa",
                                mode=mode,
                            )

                    if not selected:
                        ai_reason = next(iter(rejections.values()), "AI skipped")
                        logger.info(f"AI skipped project: {project['project_name']} — {ai_reason}")
                        if dry_run:
                            for role in candidates:
                                _print_role_decision("SKIP", project["project_name"], role, f"AI: {ai_reason}")
                        continue

                    for best, ai_reason in selected:
                        unique_id = f"{project['breakdown_id']}_{best['role_id']}"

                        # Programmatic travel pay check (overrides AI)
                        tp_ok, tp_reason = check_travel_pay(
                            project["project_name"],
                            best.get("description", ""),
                            project_notes,
                            mode=mode,
                        )
                        if not tp_ok:
                            logger.info(f"[TRAVEL PAY] Skipping {best['role_name']} on {project['project_name']}: {tp_reason}")
                            db.record_rejection(
                                project_name=project["project_name"],
                                project_url=project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                rejection_reason=tp_reason,
                                run_id=run_id,
                                platform="aa",
                                mode=mode,
                            )
                            continue

                        # If partial availability, ask AI if this specific role works
                        if partial_availability:
                            pa_result = check_partial_availability(
                                best, project["project_name"], project_notes, partial_availability,
                            )
                            if not pa_result["proceed"]:
                                flag_reason = f"Calendar partial conflict — {pa_result['reason']}"
                                db.record_flagged_role(
                                    project_name=project["project_name"],
                                    project_url=project_url,
                                    role_name=best["role_name"],
                                    role_description=best.get("description", ""),
                                    flag_reason=flag_reason,
                                    run_id=run_id,
                                    platform="aa",
                                    mode=mode,
                                )
                                logger.info(f"[CALENDAR] Skipping {best['role_name']} — {flag_reason}")
                                if dry_run:
                                    _print_role_decision("FLAGGED", project["project_name"], best, flag_reason)
                                continue

                        # Analyze submission requirements
                        confirmed_dates = None
                        if shoot_dates:
                            if partial_availability:
                                # Only confirm the free dates
                                confirmed_dates = ", ".join(partial_availability["free_dates"])
                            else:
                                confirmed_dates = f"{shoot_dates[0]} to {shoot_dates[1]}"
                        has_media = bool(cfg.get("submission", {}).get("include_media", False))
                        analysis = analyze_submission_requirements(best, project["project_name"], project_notes, confirmed_dates=confirmed_dates, mode=mode, has_media=has_media)
                        logger.info(f"[ANALYSIS] {best['role_name']}: action={analysis['action']}, note={analysis.get('note', 'N/A')}")

                        if analysis["action"] == "NEEDS_INPUT":
                            db.record_flagged_role(
                                project_name=project["project_name"],
                                project_url=project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=analysis["needs_input_reason"],
                                run_id=run_id,
                                platform="aa",
                                mode=mode,
                            )
                            logger.info(f"Flagged for review: {best['role_name']} — {analysis['needs_input_reason']}")
                            if dry_run:
                                _print_role_decision("FLAGGED", project["project_name"], best, analysis["needs_input_reason"])
                            continue

                        if dry_run:
                            tag = "SUBMIT (AI pick)" if len(candidates) > 1 else "SUBMIT"
                            _print_role_decision(tag, project["project_name"], best)
                            if len(candidates) > 1:
                                print(f"  AI reason: {ai_reason}")
                            if analysis["action"] == "SUBMIT_WITH_NOTE":
                                print(f"  Note: {analysis['note']}")
                            roles_applied += 1
                            continue

                        sub_cfg = cfg["submission"].copy()
                        if analysis["action"] == "SUBMIT_WITH_NOTE":
                            sub_cfg["default_note"] = analysis["note"]

                        # Re-navigate to project page to get fresh DOM element for this role
                        # (previous submissions may have changed the page state)
                        fresh_roles, _ = browser.scrape_roles_on_project(project["url"])
                        fresh_match = next(
                            (r for r in fresh_roles if r["role_id"] == best["role_id"]),
                            None,
                        )
                        if fresh_match:
                            best["element"] = fresh_match["element"]
                        else:
                            logger.warning(f"[SUBMIT] Could not find fresh element for {best['role_name']}, skipping")
                            continue

                        logger.info(f"[SUBMIT] Attempting submission: {project['project_name']} — {best['role_name']} (id={unique_id})")
                        result = browser.submit_for_role(
                            best, project["project_name"], sub_cfg
                        )
                        if result:
                            db.record_application(
                                unique_id, project["project_name"], best["role_name"],
                                role_description=best.get("description", ""),
                                ai_reason=ai_reason,
                                candidates_considered=len(candidates),
                                project_url=project_url,
                                submission_note=analysis.get("note") or "",
                                mode=mode,
                            )
                            logger.info(f"[SUBMIT] SUCCESS: {best['role_name']} on {project['project_name']}")
                            roles_applied += 1
                        else:
                            logger.warning(
                                f"[SUBMIT] FAILED: {best['role_name']} on {project['project_name']}"
                            )

                    # Print rejected roles in dry run
                    if dry_run and rejections:
                        for role in candidates:
                            if role["role_name"] in rejections:
                                _print_role_decision("SKIP", project["project_name"], role, rejections[role["role_name"]])

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

    finally:
        browser.close()
        try:
            flush_pending_shadows(timeout=60)
        except Exception as flush_err:
            logger.warning(f"[SHADOW] flush_pending_shadows failed: {flush_err}")
        clear_run_context()


def main():
    parser = argparse.ArgumentParser(description="ActorsAccess Auto-Apply Tool")
    parser.add_argument("--config", default=None, help="Path to config file (default: config.yaml, or config_unpaid.yaml when --mode unpaid)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
    parser.add_argument(
        "--mode", choices=["paid", "unpaid"], default="paid",
        help="paid (default): normal paying-roles flow. unpaid: LA-local Lead/Supporting/Principal only.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be submitted without actually submitting",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Max pages of breakdowns to process (default: 5)",
    )
    parser.add_argument(
        "--max-submissions", type=int, default=None,
        help="Max number of roles to submit for per run",
    )
    args = parser.parse_args()

    # Pick config file based on mode unless explicitly overridden
    config_path = args.config
    if config_path is None:
        config_path = "config_unpaid.yaml" if args.mode == "unpaid" else "config.yaml"

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    if args.headed:
        cfg["browser"]["headless"] = False

    if args.max_pages is not None:
        cfg["max_pages"] = args.max_pages

    if args.max_submissions is not None:
        cfg["max_submissions"] = args.max_submissions

    setup_logging(cfg["logging"])

    os.makedirs("data", exist_ok=True)
    db = Database("data/applied.db")

    if args.dry_run:
        args.once = True  # dry-run implies --once

    if args.once:
        logger.info(f"Running single pass (mode={args.mode})" + (" (dry run)" if args.dry_run else ""))
        run_once(cfg, db, dry_run=args.dry_run, mode=args.mode)
    else:
        interval = cfg["schedule"]["interval_hours"]
        logger.info(f"Starting scheduler (mode={args.mode}) — running every {interval} hours")
        run_once(cfg, db, mode=args.mode)  # Run immediately on start
        schedule.every(interval).hours.do(run_once, cfg, db, mode=args.mode)
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")

    db.close()


if __name__ == "__main__":
    main()
