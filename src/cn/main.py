# src/cn/main.py
import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

from src.cn.config import load_cn_config, CnConfigError
from src.cn.browser import CastingNetworksBrowser
from src.database import Database
from src.calendar_check import check_work_date_conflicts, parse_work_dates, check_availability
from src.filters import _is_background, _is_court_tv, _is_ugc, _is_unpaid, _is_voiceover, _COURT_TV_PATTERN
from src.role_selector import select_best_roles, analyze_submission_requirements

logger = logging.getLogger("castingnetworks")


def setup_logging(log_config: dict):
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file", "logs/cn_auto_apply.log")
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


def _is_past_deadline(role: dict) -> bool:
    """Check if a role's submission deadline has passed."""
    date_text = role.get("submission_date", "")
    if not date_text:
        return False
    # "DUE TODAY" is still valid
    if "DUE TODAY" in date_text.upper():
        return False
    # Already submitted roles are handled elsewhere
    if date_text.startswith("Submitted"):
        return False
    # Parse "Submissions Due Apr 29, 2026, 9:00 PM PDT"
    match = re.search(r"Submissions Due (\w+ \d+, \d{4}, \d+:\d+ [AP]M)", date_text)
    if not match:
        return False
    try:
        deadline = datetime.strptime(match.group(1), "%b %d, %Y, %I:%M %p")
        return datetime.now() > deadline
    except ValueError:
        return False


def _is_cn_background(role: dict) -> bool:
    """Check if a CN role is background based on role_type or name/description."""
    role_type = role.get("role_type", "").lower()
    if "background" in role_type or "extra" in role_type:
        return True
    return _is_background(role)


def run_once(cfg: dict, db: Database, dry_run: bool = False):
    run_id = db.start_run(platform="cn")
    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
    logger.info(f"[RUN] Started CN run_id={run_id}, calendar_ids={len(cal_ids)} configured")
    roles_found = 0
    roles_applied = 0
    roles_skipped = 0
    roles_filtered = 0

    browser = CastingNetworksBrowser(headless=cfg["browser"]["headless"])

    try:
        browser.start()

        creds = cfg["credentials"]
        if not browser.login(creds["email"], creds["password"]):
            logger.warning("Login failed, retrying once...")
            if not browser.login(creds["email"], creds["password"]):
                raise RuntimeError("Login failed after retry")

        max_pages = cfg.get("max_pages", 5)
        total_pages = browser.get_total_pages()
        pages_to_process = min(total_pages, max_pages)
        logger.info(f"Processing {pages_to_process} of {total_pages} pages")

        if dry_run:
            print("\n=== DRY RUN — no submissions will be made ===\n")

        max_subs = cfg.get("max_submissions")
        empty_pages = 0  # consecutive pages with no submissions

        for page_num in range(1, pages_to_process + 1):
            if max_subs and roles_applied >= max_subs:
                logger.info(f"Reached max submissions ({max_subs}), stopping")
                break

            if page_num > 1:
                # Navigate back to billboard (submissions navigate away)
                if not browser.navigate_to_billboard():
                    break
                if not browser.go_to_page(page_num):
                    break

            page_applied_before = roles_applied
            page_roles = browser.scrape_roles()
            roles_found += len(page_roles)

            # Group roles by project
            projects = defaultdict(list)
            for role in page_roles:
                projects[role["project_id"]].append(role)

            for project_id, proj_roles in projects.items():
                project_name = proj_roles[0]["project_name"]

                # Skip court TV projects
                if _COURT_TV_PATTERN.search(project_name):
                    roles_filtered += len(proj_roles)
                    if dry_run:
                        for role in proj_roles:
                            _print_role_decision("SKIP", project_name, role, "court TV project")
                    else:
                        logger.info(f"Filtered out project: {project_name} (court TV)")
                    continue

                candidates = []
                for role in proj_roles:
                    # Skip roles we've already applied for or that CN shows as submitted
                    unique_id = f"cn_{project_id}_{role['role_id']}"
                    sub_date = role.get("submission_date", "")
                    if db.is_applied(unique_id) or sub_date.startswith("Submitted"):
                        roles_skipped += 1
                        continue

                    # Skip roles already rejected by AI (don't re-evaluate or re-record)
                    if db.is_rejected(role["role_name"], project_name, "cn"):
                        roles_skipped += 1
                        continue

                    if _is_cn_background(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "background role")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (background)")
                        continue

                    if _is_unpaid(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "unpaid")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (unpaid)")
                        continue

                    if _is_ugc(project_name, role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "UGC")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (UGC)")
                        continue

                    if _is_court_tv(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "court TV")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (court TV)")
                        continue

                    if _is_past_deadline(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "past deadline")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (past deadline)")
                        continue

                    candidates.append(role)

                if not candidates:
                    continue

                if max_subs and roles_applied >= max_subs:
                    break

                selected, rejections = select_best_roles(candidates, project_name)
                logger.info(
                    f"[AI] Selection for {project_name}: "
                    f"selected={[s[0]['role_name'] for s in selected]}, "
                    f"rejected={list(rejections.keys())}"
                )

                # Build project URL for CN (use first candidate's URL)
                project_url = candidates[0].get("url", "")
                if project_url and not project_url.startswith("http"):
                    project_url = f"https://app.castingnetworks.com{project_url}"

                # Record rejections
                for role in candidates:
                    if role["role_name"] in rejections:
                        role_url = role.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=rejections[role["role_name"]],
                            run_id=run_id,
                            platform="cn",
                        )

                if not selected:
                    ai_reason = next(iter(rejections.values()), "AI skipped")
                    logger.info(f"AI skipped project: {project_name} — {ai_reason}")
                    if dry_run:
                        for role in candidates:
                            _print_role_decision("SKIP", project_name, role, f"AI: {ai_reason}")
                    continue

                for best, ai_reason in selected:
                    unique_id = f"cn_{best['project_id']}_{best['role_id']}"

                    # Analyze submission requirements
                    # Scrape submission instructions from the role detail page
                    role_instructions = ""
                    if not dry_run:
                        role_instructions = browser.scrape_role_instructions(best)
                        if role_instructions:
                            logger.info(f"Found submission instructions for {best['role_name']}: {role_instructions[:200]}")

                    # Check calendar for work date conflicts FIRST (before self-tape check)
                    # If busy, skip entirely — don't bother flagging for self-tape
                    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
                    work_dates = parse_work_dates(best.get("submission_date", ""))
                    confirmed_dates = None
                    if work_dates:
                        start, end = work_dates
                        logger.info(f"[CALENDAR] Work dates found for {best['role_name']}: {start} to {end}")
                        available, conflicts = check_availability(start, end, cal_ids)
                        if not available:
                            conflict_list = ", ".join(conflicts[:5])
                            flag_reason = f"Calendar conflict with work dates: {conflict_list}"
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="cn",
                            )
                            logger.info(f"Skipping {best['role_name']} — {flag_reason}")
                            if dry_run:
                                _print_role_decision("FLAGGED", project_name, best, flag_reason)
                            continue
                        confirmed_dates = f"{start} to {end}"
                    else:
                        logger.info(f"[CALENDAR] No work dates parsed for {best['role_name']}")

                    # Check for self-tape/video audition requests
                    # Only flag if instructions ask to SUBMIT a self-tape now,
                    # not if they just describe the audition format
                    selftape_keywords = ["self-tape", "self tape", "selftape", "slate"]
                    instructions_lower = role_instructions.lower()
                    format_phrases = [
                        "self-taped audition", "self-taped, online audition",
                        "self tape audition", "self-taped online audition",
                        "audition instructions will be sent",
                    ]
                    is_format_description = any(fp in instructions_lower for fp in format_phrases)
                    if not is_format_description and any(kw in instructions_lower for kw in selftape_keywords):
                        flag_reason = "Self-tape/video audition requested"
                        logger.info(f"Flagging {best['role_name']} — {flag_reason}")
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=flag_reason,
                            run_id=run_id,
                            platform="cn",
                        )
                        if dry_run:
                            _print_role_decision("FLAGGED", project_name, best, flag_reason)
                        continue

                    analysis = analyze_submission_requirements(best, project_name, role_instructions, confirmed_dates=confirmed_dates)
                    logger.info(f"[ANALYSIS] {best['role_name']}: action={analysis['action']}, note={analysis.get('note', 'N/A')}")

                    # Legacy check: work date conflicts for roles without parsed dates
                    if not work_dates and analysis["action"] in ("SUBMIT", "SUBMIT_WITH_NOTE"):
                        logger.info(f"[CALENDAR] Running legacy work date check for {best['role_name']}")
                        has_conflict, conflicts = check_work_date_conflicts(
                            best, cal_ids
                        )
                        if has_conflict:
                            conflict_list = ", ".join(conflicts[:5])
                            flag_reason = f"Calendar conflict with work dates: {conflict_list}"
                            db.record_flagged_role(
                                project_name=project_name,
                                project_url=project_url,
                                role_name=best["role_name"],
                                role_description=best.get("description", ""),
                                flag_reason=flag_reason,
                                run_id=run_id,
                                platform="cn",
                            )
                            logger.info(f"Skipping {best['role_name']} — {flag_reason}")
                            if dry_run:
                                _print_role_decision("FLAGGED", project_name, best, flag_reason)
                            continue

                    if analysis["action"] == "NEEDS_INPUT":
                        db.record_flagged_role(
                            project_name=project_name,
                            project_url=project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            flag_reason=analysis["needs_input_reason"],
                            run_id=run_id,
                            platform="cn",
                        )
                        logger.info(f"Flagged for review: {best['role_name']} — {analysis['needs_input_reason']}")
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

                    sub_cfg = cfg["submission"].copy()
                    if analysis["action"] == "SUBMIT_WITH_NOTE":
                        sub_cfg["default_note"] = analysis["note"]

                    logger.info(f"[SUBMIT] Attempting submission: {project_name} — {best['role_name']} (id={unique_id})")
                    success = browser.submit_for_role(best, sub_cfg)
                    if success:
                        logger.info(f"[SUBMIT] SUCCESS: {best['role_name']} on {project_name}")
                        role_url = best.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_application(
                            unique_id, project_name, best["role_name"],
                            role_description=best.get("description", ""),
                            ai_reason=ai_reason,
                            candidates_considered=len(candidates),
                            platform="cn",
                            project_url=role_url,
                            submission_note=analysis.get("note") or "",
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

    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description="Casting Networks Auto-Apply Tool")
    parser.add_argument("--config", default="cn_config.yaml", help="Path to CN config file")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview without submitting")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to process")
    parser.add_argument("--max-submissions", type=int, default=None, help="Max roles to submit for")
    args = parser.parse_args()

    try:
        cfg = load_cn_config(args.config)
    except CnConfigError as e:
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
                sched.run_pending()
                import time
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")

    db.close()


if __name__ == "__main__":
    main()
