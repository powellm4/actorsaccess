# src/main.py
import argparse
import logging
import os
import sys
import time

import schedule

from src.config import load_config, ConfigError
from src.database import Database
from src.browser import ActorsAccessBrowser
from src.filters import role_matches, project_matches
from src.role_selector import select_best_role

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


def run_once(cfg: dict, db: Database, dry_run: bool = False):
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
    """
    run_id = db.start_run()
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

        # Navigate and filter
        filters = cfg["filters"]
        if not browser.navigate_to_breakdowns(filters.get("region", "")):
            raise RuntimeError("Failed to navigate to breakdowns")

        browser.apply_filters(filters)

        # Process breakdowns page by page
        total_pages = browser.get_total_pages()
        max_pages = cfg.get("max_pages", 5)  # Don't crawl all 46 pages by default
        max_subs = cfg.get("max_submissions")
        pages_to_process = min(total_pages, max_pages)
        logger.info(f"Processing {pages_to_process} of {total_pages} pages")

        if dry_run:
            print("\n=== DRY RUN — no submissions will be made ===\n")

        hit_limit = False
        for page_num in range(1, pages_to_process + 1):
            if hit_limit:
                break
            if page_num > 1:
                # Navigate back to breakdowns list and go to next page
                browser.navigate_to_breakdowns(filters.get("region", ""))
                browser.apply_filters(filters)
                if not browser.go_to_page(page_num):
                    break

            # Scrape project listings on this page
            projects = browser.scrape_projects()

            for project in projects:
                # Skip projects we've already submitted to
                if project["already_submitted"]:
                    logger.debug(f"Skipping already-submitted project: {project['project_name']}")
                    continue

                # Skip excluded project types (e.g., theater)
                proj_ok, proj_reason = project_matches(project)
                if not proj_ok:
                    if dry_run:
                        print(f"\n[SKIP PROJECT - {proj_reason}] {project['project_name']}")
                    else:
                        logger.info(f"Skipping project: {project['project_name']} ({proj_reason})")
                    continue

                # Navigate into the project to see individual roles
                roles = browser.scrape_roles_on_project(project["url"])
                roles_found += len(roles)

                # Filter roles and collect candidates
                candidates = []
                for role in roles:
                    unique_id = f"{project['breakdown_id']}_{role['role_id']}"

                    if db.is_applied(unique_id):
                        roles_skipped += 1
                        continue

                    matches, skip_reason = role_matches(role)
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

                    candidates.append(role)

                if not candidates:
                    continue

                # Check submission cap
                if max_subs and roles_applied >= max_subs:
                    logger.info(f"Reached max submissions ({max_subs}), stopping")
                    hit_limit = True
                    break

                # Pick the best role (AI selection if multiple candidates)
                best, ai_reason = select_best_role(candidates, project["project_name"])
                unique_id = f"{project['breakdown_id']}_{best['role_id']}"

                # In dry run, show what was picked and what was passed over
                if dry_run:
                    if len(candidates) > 1:
                        for role in candidates:
                            if role is best:
                                _print_role_decision("SUBMIT (AI pick)", project["project_name"], role)
                            else:
                                _print_role_decision("SKIP", project["project_name"], role, "not best fit")
                        print(f"  AI reason: {ai_reason}")
                    else:
                        _print_role_decision("SUBMIT", project["project_name"], best)
                    roles_applied += 1
                    continue

                success = browser.submit_for_role(
                    best, project["project_name"], cfg["submission"]
                )
                if success:
                    db.record_application(
                        unique_id, project["project_name"], best["role_name"],
                        role_description=best.get("description", ""),
                        ai_reason=ai_reason,
                        candidates_considered=len(candidates),
                    )
                    roles_applied += 1
                else:
                    logger.warning(
                        f"Skipping failed submission: {best['role_name']}"
                    )

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
    parser = argparse.ArgumentParser(description="ActorsAccess Auto-Apply Tool")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser")
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

    try:
        cfg = load_config(args.config)
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
        logger.info("Running single pass..." + (" (dry run)" if args.dry_run else ""))
        run_once(cfg, db, dry_run=args.dry_run)
    else:
        interval = cfg["schedule"]["interval_hours"]
        logger.info(f"Starting scheduler — running every {interval} hours")
        run_once(cfg, db)  # Run immediately on start
        schedule.every(interval).hours.do(run_once, cfg, db)
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")

    db.close()


if __name__ == "__main__":
    main()
