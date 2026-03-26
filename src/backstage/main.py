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
from src.filters import _is_background, _is_court_tv, _is_ugc, _is_unpaid, _COURT_TV_PATTERN
from src.role_selector import select_best_roles, analyze_submission_requirements

logger = logging.getLogger("backstage")

# Production types to skip (actor doesn't do theater/musical)
_SKIP_PROD_TYPES = {"theater", "theatre", "musical"}


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
    parts = [role_desc]
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


def run_once(cfg: dict, db: Database, dry_run: bool = False):
    run_id = db.start_run(platform="backstage")
    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
    logger.info(f"[RUN] Started Backstage run_id={run_id}, calendar_ids={len(cal_ids)} configured")
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
                selected, rejections = select_best_roles(candidates, project_name)
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

                    # Fetch role detail page for full data (prescreen, attachments, etc.)
                    role_url = best.get("url", "")
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
                                    # Use the full description from detail if longer
                                    detail_desc = detail_role.get("description", "")
                                    if len(detail_desc) > len(best.get("description", "")):
                                        best["description"] = detail_desc
                                    break

                    # Check for prescreen/self-tape requirements
                    # Backstage roles can have:
                    #   prescreen_type: "V" = video, "T" = text, "N" = none
                    #   prescreen_message: free-text instructions (self-tape, etc.)
                    #   prescreen_questions: list of applicant questions (YES_NO, SHORT_ANSWER)
                    prescreen_type = best.get("prescreen_type", "")
                    prescreen_msg = best.get("prescreen_message", "")
                    prescreen_questions = best.get("prescreen_questions", [])
                    if prescreen_type == "V" or prescreen_msg or prescreen_questions:
                        if prescreen_questions:
                            q_texts = [q.get("text", "") for q in prescreen_questions[:3]]
                            flag_reason = f"Applicant questions: {'; '.join(q_texts)}"
                        elif prescreen_msg:
                            flag_reason = f"Prescreen required: {prescreen_msg[:100]}"
                        else:
                            flag_reason = "Video prescreen required"
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=flag_reason,
                            run_id=run_id,
                            platform="backstage",
                        )
                        if dry_run:
                            _print_role_decision("FLAGGED", project_name, best, flag_reason)
                        continue

                    # Also check description for self-tape submission requests
                    desc_lower = best.get("description", "").lower()
                    submit_phrases = [
                        "submit a self-tape", "submit your self-tape",
                        "send a self-tape", "send your self-tape",
                        "provide a self-tape", "upload a self-tape",
                        "self-tape required", "self tape required",
                    ]
                    if any(sp in desc_lower for sp in submit_phrases):
                        flag_reason = "Self-tape submission requested in description"
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=flag_reason,
                            run_id=run_id,
                            platform="backstage",
                        )
                        if dry_run:
                            _print_role_decision("FLAGGED", project_name, best, flag_reason)
                        continue

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
                        roles_applied += 1
                        continue

                    # Actually submit
                    note = analysis.get("note", "") if analysis["action"] == "SUBMIT_WITH_NOTE" else ""
                    logger.info(f"[SUBMIT] Attempting: {project_name} — {best['role_name']} (id={unique_id})")
                    result = client.submit_for_role(best["role_id"], note=note)

                    if result:
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
    parser.add_argument("--config", default="backstage_config.yaml", help="Path to config file")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview without submitting")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to process")
    parser.add_argument("--max-submissions", type=int, default=None, help="Max roles to submit for")
    args = parser.parse_args()

    try:
        cfg = load_backstage_config(args.config)
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
        logger.info("Running single pass..." + (" (dry run)" if args.dry_run else ""))
        run_once(cfg, db, dry_run=args.dry_run)
    else:
        import schedule as sched
        interval = 4
        logger.info(f"Starting scheduler — running every {interval} hours")
        run_once(cfg, db)
        sched.every(interval).hours.do(run_once, cfg, db)
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
