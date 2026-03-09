# src/cn/main.py
import argparse
import logging
import os
import sys
from collections import defaultdict

from src.cn.config import load_cn_config, CnConfigError
from src.cn.browser import CastingNetworksBrowser
from src.database import Database
from src.filters import _is_background
from src.role_selector import select_best_role

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


def _is_cn_background(role: dict) -> bool:
    """Check if a CN role is background based on role_type or name/description."""
    role_type = role.get("role_type", "").lower()
    if "background" in role_type or "extra" in role_type:
        return True
    return _is_background(role)


def run_once(cfg: dict, db: Database, dry_run: bool = False):
    run_id = db.start_run(platform="cn")
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

            page_roles = browser.scrape_roles()
            roles_found += len(page_roles)

            # Group roles by project
            projects = defaultdict(list)
            for role in page_roles:
                projects[role["project_id"]].append(role)

            for project_id, proj_roles in projects.items():
                project_name = proj_roles[0]["project_name"]

                candidates = []
                for role in proj_roles:
                    unique_id = f"cn_{project_id}_{role['role_id']}"

                    if db.is_applied(unique_id):
                        roles_skipped += 1
                        continue

                    if _is_cn_background(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "background role")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (background)")
                        continue

                    candidates.append(role)

                if not candidates:
                    continue

                if max_subs and roles_applied >= max_subs:
                    break

                best, ai_reason = select_best_role(candidates, project_name)
                unique_id = f"cn_{best['project_id']}_{best['role_id']}"

                if dry_run:
                    if len(candidates) > 1:
                        for role in candidates:
                            if role is best:
                                _print_role_decision("SUBMIT (AI pick)", project_name, role)
                            else:
                                _print_role_decision("SKIP", project_name, role, "not best fit")
                        print(f"  AI reason: {ai_reason}")
                    else:
                        _print_role_decision("SUBMIT", project_name, best)
                    roles_applied += 1
                    continue

                success = browser.submit_for_role(best, cfg["submission"])
                if success:
                    db.record_application(
                        unique_id, project_name, best["role_name"],
                        role_description=best.get("description", ""),
                        ai_reason=ai_reason,
                        candidates_considered=len(candidates),
                        platform="cn",
                    )
                    roles_applied += 1
                else:
                    logger.warning(f"Skipping failed submission: {best['role_name']}")

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
