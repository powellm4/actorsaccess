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


def run_once(cfg: dict, db: Database):
    """Execute a single scrape-and-apply run.

    Flow:
    1. Login
    2. Navigate to breakdowns, apply filters
    3. For each page of results:
       - Scrape project listings
       - Skip projects already fully submitted
       - For each new project: navigate to it, scrape roles, submit for each
    4. Record everything in the database
    """
    run_id = db.start_run()
    roles_found = 0
    roles_applied = 0
    roles_skipped = 0

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
        pages_to_process = min(total_pages, max_pages)
        logger.info(f"Processing {pages_to_process} of {total_pages} pages")

        for page_num in range(1, pages_to_process + 1):
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

                # Navigate into the project to see individual roles
                roles = browser.scrape_roles_on_project(project["url"])
                roles_found += len(roles)

                for role in roles:
                    # Use breakdown_id + role_id as unique key
                    unique_id = f"{project['breakdown_id']}_{role['role_id']}"

                    if db.is_applied(unique_id):
                        roles_skipped += 1
                        continue

                    success = browser.submit_for_role(
                        role, project["project_name"], cfg["submission"]
                    )
                    if success:
                        db.record_application(
                            unique_id, project["project_name"], role["role_name"]
                        )
                        roles_applied += 1
                    else:
                        logger.warning(
                            f"Skipping failed submission: {role['role_name']}"
                        )

        db.complete_run(run_id, roles_found, roles_applied, roles_skipped)
        logger.info(
            f"Run complete: {roles_applied} applied, {roles_skipped} skipped, "
            f"{roles_found} total roles found"
        )

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
        "--max-pages", type=int, default=None,
        help="Max pages of breakdowns to process (default: 5)",
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

    setup_logging(cfg["logging"])

    os.makedirs("data", exist_ok=True)
    db = Database("data/applied.db")

    if args.once:
        logger.info("Running single pass...")
        run_once(cfg, db)
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
