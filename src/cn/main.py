# src/cn/main.py
import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

from src import overrides as overrides_mod
from src.cn.config import load_cn_config, CnConfigError
from src.cn.browser import CastingNetworksBrowser
from src.database import Database
from src.override_email import send_override_results_email
from src.calendar_check import check_work_date_conflicts, parse_work_dates, check_availability, get_busy_dates
from src.filters import _is_background, _is_court_tv, _is_ugc, _is_unpaid, _is_voiceover, _COURT_TV_PATTERN, is_lead_or_supporting, project_has_female_cast
from src.role_selector import (
    TRANSIENT_REJECTION_PREFIX,
    analyze_submission_requirements,
    check_travel_pay,
    select_best_roles,
)
from src.shadow import clear_run_context, flush_pending_shadows, set_run_context

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


def _apply_cn_override(
    cfg: dict, db: Database, browser: CastingNetworksBrowser, override: dict,
    overrides_cfg: dict, token: str, dry_run: bool,
):
    """Force-apply a single CN-platform override. Bypasses filters and AI
    selection — the user explicitly asked for this role to go through.

    CN's submit_for_role navigates to role['url'] directly, so we only need
    the stored role URL plus enough of the role dict to satisfy the browser
    API and the dedup check.
    """
    project_name = override["project_name"]
    role_name = override["role_name"]
    issue_num = override["issue_number"]
    mode = override["mode"]

    def _finish(outcome: str, detail: str, comment: str) -> dict:
        db.record_override_outcome(
            issue_number=issue_num, project_name=project_name, role_name=role_name,
            platform="cn", mode=mode, outcome=outcome, detail=detail,
        )
        db.clear_pending_override(project_name, role_name, "cn", mode)
        overrides_mod.comment_and_close(overrides_cfg["repo"], issue_num, comment, token)
        return {
            "issue_number": issue_num, "project_name": project_name,
            "role_name": role_name, "platform": "cn", "mode": mode,
            "outcome": outcome, "detail": detail,
        }

    role_url = db.get_known_project_url(role_name, project_name, "cn")
    if not role_url:
        logger.warning(f"[OVERRIDE] No role URL on file for {project_name} — {role_name}")
        return _finish(
            "failed", "Role URL not on file in db.",
            "Failed: no role URL on file. The role may have been removed before it was queued.",
        )

    # CN role URLs look like https://app.castingnetworks.com/project/{pid}/role/{rid}/...
    id_match = re.search(r"/project/(\d+)/role/(\d+)", role_url)
    if not id_match:
        return _finish(
            "failed", f"Could not parse project/role id from URL: {role_url}",
            f"Failed: stored role URL didn't match CN's expected /project/{{id}}/role/{{id}}/ format.",
        )
    project_id, role_id = id_match.group(1), id_match.group(2)
    unique_id = f"cn_{project_id}_{role_id}"

    if db.is_applied(unique_id):
        db.delete_rejection(role_name, project_name, "cn")
        db.delete_flagged(role_name, project_name, "cn")
        return _finish(
            "applied", "Already applied — no action needed",
            "Already applied — no action needed.",
        )

    if dry_run:
        print(f"\n[OVERRIDE - DRY RUN] would re-apply: {project_name} — {role_name}")
        return None

    sub_cfg = cfg["submission"].copy()
    sub_cfg["default_note"] = ""
    role = {
        "url": role_url,
        "role_name": role_name,
        "project_name": project_name,
        "role_id": role_id,
        "project_id": project_id,
    }

    try:
        result = browser.submit_for_role(role, sub_cfg)
        err_detail = "" if result else "browser.submit_for_role returned False"
    except Exception as e:
        logger.error(f"[OVERRIDE] CN submit raised on {project_name} / {role_name}: {e}")
        result = False
        err_detail = str(e)

    if result:
        db.record_application(
            unique_id, project_name, role_name,
            role_description="",
            ai_reason="OVERRIDE: applied via Apply Anyway",
            candidates_considered=1, platform="cn",
            project_url=role_url, submission_note="", mode=mode,
        )
        db.delete_rejection(role_name, project_name, "cn")
        db.delete_flagged(role_name, project_name, "cn")
        return _finish(
            "applied", "Submitted successfully via override",
            f"Applied successfully on **{project_name}** — *{role_name}*.",
        )
    return _finish("failed", err_detail, f"Failed to apply: {err_detail}")


def process_cn_overrides(
    cfg: dict, db: Database, browser: CastingNetworksBrowser,
    run_id: int, mode: str, dry_run: bool,
):
    """Pull GitHub override issues, queue them, then apply pending CN
    overrides for this mode. Called once per run, right after login.

    Non-CN-platform overrides are left in the queue for whichever run
    handles that platform.
    """
    overrides_cfg, token = overrides_mod.load_run_config(cfg)
    if not overrides_cfg:
        return

    overrides_mod.ingest_issues(overrides_cfg, token, db)

    pending = [
        o for o in db.list_pending_overrides()
        if o["platform"] == "cn" and o["mode"] == mode
    ]
    if not pending:
        return

    logger.info(f"[OVERRIDE] Processing {len(pending)} CN override(s) for mode={mode}")
    outcomes: list[dict] = []
    for override in pending:
        result = _apply_cn_override(cfg, db, browser, override, overrides_cfg, token, dry_run)
        if result:
            outcomes.append(result)

    if outcomes and not dry_run:
        send_override_results_email(
            outcomes, platform="cn", mode=mode, repo=overrides_cfg.get("repo"),
        )


def run_once(cfg: dict, db: Database, dry_run: bool = False, mode: str = "paid"):
    run_id = db.start_run(platform="cn", mode=mode)
    set_run_context(platform="cn", mode=mode, run_id=run_id)
    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
    logger.info(f"[RUN] Started CN run_id={run_id}, mode={mode}, calendar_ids={len(cal_ids)} configured")
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

        # Apply Anyway: pull queued GitHub overrides and force-apply them
        # before the normal scrape. CN's submit_for_role navigates directly
        # to role URLs, so this doesn't depend on the billboard listing.
        process_cn_overrides(cfg, db, browser, run_id, mode, dry_run)

        # Optional saved search on CN. Paid mode typically leaves this empty
        # and lets the user's default saved search auto-load. Unpaid mode
        # sets it to "unpaid" in cn_config_unpaid.yaml so CN filters the
        # billboard to unpaid roles before we scrape.
        saved_search = cfg.get("saved_search") or None
        if saved_search:
            if not browser.click_saved_search(saved_search):
                if mode == "unpaid":
                    # Fail closed in unpaid mode: if the saved search can't
                    # be applied we'd end up scraping paid roles under the
                    # unpaid flag, which is worse than doing nothing.
                    raise RuntimeError(
                        f"Unpaid mode: could not apply CN saved search {saved_search!r}. "
                        f"Verify it exists and is spelled correctly on castingnetworks.com."
                    )
                logger.warning(f"Could not apply saved search {saved_search!r}, continuing with default")

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
                # Navigate back to billboard (submissions navigate away).
                # Re-apply the saved search on each return so we don't fall
                # back to the default (e.g. "usa 2") for the next page.
                if not browser.navigate_to_billboard(saved_search=saved_search):
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

                    if mode == "unpaid":
                        # Trust the "unpaid" saved search for the pay filter.
                        # CN populates role["pay"] with rate strings even when
                        # the Pay Preferences category is "unpaid", so our text
                        # _is_unpaid() check rejects everything it shouldn't.
                        # Just enforce the role-type whitelist (CN's saved
                        # search only filters at the coarse principal/background
                        # level, so we still need to narrow to Lead/Principal/
                        # Series Regular here) — unless the project has a
                        # female on the cast, in which case any role type is OK.
                        has_female = project_has_female_cast(proj_roles, role, "cn")
                        lead_ok, lead_reason = is_lead_or_supporting(
                            role, "cn", role.get("project_type", ""),
                            has_female_cast=has_female,
                        )
                        if not lead_ok:
                            roles_filtered += 1
                            if dry_run:
                                _print_role_decision("SKIP", project_name, role, lead_reason)
                            else:
                                logger.info(f"Filtered out (unpaid role-type): {project_name} — {role['role_name']} ({lead_reason})")
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
                                logger.info(f"Filtered out: {project_name} — {role['role_name']} (unpaid)")
                            continue

                    if _is_voiceover(role):
                        roles_filtered += 1
                        if dry_run:
                            _print_role_decision("SKIP", project_name, role, "voiceover")
                        else:
                            logger.info(f"Filtered out: {project_name} — {role['role_name']} (voiceover)")
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

                    # Enrich description with pay and project context so AI
                    # can apply travel pay rules
                    extra_context = []
                    if role.get("pay"):
                        extra_context.append(f"PAY: {role['pay']}")
                    if role.get("location"):
                        extra_context.append(f"SHOOT LOCATION: {role['location']}")
                    if role.get("project_type"):
                        extra_context.append(f"PROJECT TYPE: {role['project_type']}")
                    if extra_context:
                        role["description"] = (
                            role.get("description", "") + "\n" + "\n".join(extra_context)
                        )

                    candidates.append(role)

                if not candidates:
                    continue

                if max_subs and roles_applied >= max_subs:
                    break

                selected, rejections = select_best_roles(candidates, project_name, mode=mode)
                logger.info(
                    f"[AI] Selection for {project_name}: "
                    f"selected={[s[0]['role_name'] for s in selected]}, "
                    f"rejected={list(rejections.keys())}"
                )

                # Build project URL for CN (use first candidate's URL)
                project_url = candidates[0].get("url", "")
                if project_url and not project_url.startswith("http"):
                    project_url = f"https://app.castingnetworks.com{project_url}"

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
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=reason,
                            run_id=run_id,
                            platform="cn",
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
                    unique_id = f"cn_{best['project_id']}_{best['role_id']}"

                    # Programmatic travel pay check (overrides AI)
                    tp_ok, tp_reason = check_travel_pay(
                        project_name,
                        best.get("description", ""),
                        f"{best.get('submission_date', '')} {best.get('location', '')}",
                        mode=mode,
                    )
                    if not tp_ok:
                        logger.info(f"[TRAVEL PAY] Skipping {best['role_name']} on {project_name}: {tp_reason}")
                        role_url = best.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=best["role_name"],
                            role_description=best.get("description", ""),
                            rejection_reason=tp_reason,
                            run_id=run_id,
                            platform="cn",
                            mode=mode,
                        )
                        continue

                    # Analyze submission requirements
                    # Scrape submission instructions from the role detail page
                    role_instructions = ""
                    if not dry_run:
                        role_instructions = browser.scrape_role_instructions(best)
                        if role_instructions:
                            logger.info(f"Found submission instructions for {best['role_name']}: {role_instructions[:200]}")

                    # Check calendar for work date conflicts FIRST (before self-tape check).
                    # CN "Work" ranges describe a window — the actual shoot is usually
                    # 1 day inside it. Only skip if EVERY day in the window is busy;
                    # if any day is free, submit and let the AI note the free dates.
                    cal_ids = cfg.get("google_calendar", {}).get("calendar_ids", [])
                    work_dates = parse_work_dates(best.get("submission_date", ""))
                    confirmed_dates = None
                    if work_dates:
                        start, end = work_dates
                        logger.info(f"[CALENDAR] Work dates found for {best['role_name']}: {start} to {end}")
                        available, conflicts = check_availability(start, end, cal_ids)
                        if not available:
                            from datetime import date as _date, timedelta as _td
                            busy_dates = get_busy_dates(start, end, cal_ids)
                            start_d = _date.fromisoformat(start)
                            end_d = _date.fromisoformat(end)
                            total_days = (end_d - start_d).days + 1
                            free_days = total_days - len(busy_dates)
                            if free_days == 0:
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
                                    mode=mode,
                                )
                                logger.info(f"Skipping {best['role_name']} — {flag_reason}")
                                if dry_run:
                                    _print_role_decision("FLAGGED", project_name, best, flag_reason)
                                continue
                            # Partial availability — submit anyway and tell the AI which dates are free.
                            all_dates = {(start_d + _td(days=i)).isoformat() for i in range(total_days)}
                            free_dates = sorted(all_dates - set(busy_dates))
                            confirmed_dates = ", ".join(free_dates)
                            logger.info(
                                f"[CALENDAR] Partial availability for {best['role_name']}: "
                                f"{free_days}/{total_days} days free ({confirmed_dates}) — submitting"
                            )
                        else:
                            confirmed_dates = f"{start} to {end}"
                    else:
                        logger.info(f"[CALENDAR] No work dates parsed for {best['role_name']}")

                    # Note: we used to flag and skip self-tape/video-audition requests here.
                    # We now submit anyway — casting can ask for a tape later if interested.

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
                                mode=mode,
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
                            mode=mode,
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

    finally:
        browser.close()
        try:
            flush_pending_shadows(timeout=60)
        except Exception as flush_err:
            logger.warning(f"[SHADOW] flush_pending_shadows failed: {flush_err}")
        clear_run_context()


def main():
    parser = argparse.ArgumentParser(description="Casting Networks Auto-Apply Tool")
    parser.add_argument("--config", default=None, help="Path to CN config file (default: cn_config.yaml, or cn_config_unpaid.yaml when --mode unpaid)")
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
        config_path = "cn_config_unpaid.yaml" if args.mode == "unpaid" else "cn_config.yaml"

    try:
        cfg = load_cn_config(config_path)
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
                sched.run_pending()
                import time
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")

    db.close()


if __name__ == "__main__":
    main()
