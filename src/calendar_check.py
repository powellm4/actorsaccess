# src/calendar_check.py
"""Google Calendar integration for checking date availability."""

import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

_service_cache = None


def get_calendar_service():
    """Build and return a Google Calendar API service using service account credentials.

    Reads base64-encoded service account JSON from GOOGLE_CALENDAR_SA_KEY env var.
    Returns None if the env var is not set or credentials are invalid.
    """
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    sa_key_b64 = os.environ.get("GOOGLE_CALENDAR_SA_KEY")
    if not sa_key_b64:
        logger.info("GOOGLE_CALENDAR_SA_KEY not set — calendar check disabled")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_info = json.loads(base64.b64decode(sa_key_b64))
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/calendar.readonly"]
        )
        _service_cache = build("calendar", "v3", credentials=creds)
        logger.info("[CALENDAR] Calendar service initialized successfully")
        return _service_cache
    except Exception as e:
        logger.warning(f"Failed to build calendar service: {e}")
        return None


def check_availability(
    start_date: str, end_date: str, calendar_ids: list[str],
) -> tuple[bool, list[str]]:
    """Check if a date range is free on the specified calendars.

    Args:
        start_date: ISO date string (e.g., "2026-04-05")
        end_date: ISO date string (e.g., "2026-04-12")
        calendar_ids: List of Google Calendar IDs to check.

    Returns:
        Tuple of (is_available, conflicting_event_names).
        Returns (True, []) if calendar service is unavailable.
    """
    service = get_calendar_service()
    if service is None:
        logger.warning("[CALENDAR] Calendar service unavailable — defaulting to 'available' (dates NOT checked)")
        return True, []

    if not calendar_ids:
        logger.warning("[CALENDAR] No calendar_ids configured — defaulting to 'available' (dates NOT checked)")
        return True, []

    logger.info(f"[CALENDAR] Checking {start_date} to {end_date} against {len(calendar_ids)} calendar(s)")
    conflicts = []
    time_min = f"{start_date}T00:00:00Z"
    time_max = f"{end_date}T23:59:59Z"

    try:
        for cal_id in calendar_ids:
            events_result = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            for event in events_result.get("items", []):
                conflicts.append(event.get("summary", "Untitled event"))
    except Exception as e:
        logger.warning(f"Calendar API error: {e}")
        return True, []

    if conflicts:
        logger.info(f"[CALENDAR] CONFLICT — {len(conflicts)} event(s): {', '.join(conflicts[:5])}")
    else:
        logger.info(f"[CALENDAR] AVAILABLE — no conflicts found for {start_date} to {end_date}")
    return len(conflicts) == 0, conflicts


def get_busy_dates(
    start_date: str, end_date: str, calendar_ids: list[str],
) -> list[str]:
    """Return a list of dates within the range that have calendar events.

    Args:
        start_date: ISO date string (e.g., "2026-03-26")
        end_date: ISO date string (e.g., "2026-04-02")
        calendar_ids: List of Google Calendar IDs to check.

    Returns:
        List of ISO date strings that have events (e.g., ["2026-03-28"]).
        Returns [] if calendar service is unavailable.
    """
    from datetime import date, timedelta

    service = get_calendar_service()
    if service is None or not calendar_ids:
        return []

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total_days = (end - start).days + 1

    busy_dates = set()
    time_min = f"{start_date}T00:00:00Z"
    time_max = f"{end_date}T23:59:59Z"

    try:
        for cal_id in calendar_ids:
            events_result = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            for event in events_result.get("items", []):
                event_start = event.get("start", {})
                event_end = event.get("end", {})
                # Expand multi-day events into all constituent dates
                if "date" in event_start:
                    # All-day events: 'date' is inclusive start, end 'date' is exclusive
                    ev_start = date.fromisoformat(event_start["date"])
                    ev_end = date.fromisoformat(event_end.get("date", event_start["date"]))
                    # Google Calendar all-day event end is exclusive, so don't subtract 1
                    current = ev_start
                    while current < ev_end:
                        busy_dates.add(current.isoformat())
                        current += timedelta(days=1)
                elif "dateTime" in event_start:
                    # Timed events: extract date from start and end
                    ev_start = date.fromisoformat(event_start["dateTime"][:10])
                    ev_end_str = event_end.get("dateTime", event_start["dateTime"])[:10]
                    ev_end = date.fromisoformat(ev_end_str)
                    current = ev_start
                    while current <= ev_end:
                        busy_dates.add(current.isoformat())
                        current += timedelta(days=1)
    except Exception as e:
        logger.warning(f"[CALENDAR] API error getting busy dates: {e}")
        return []

    # Calculate free dates
    all_dates = {(start + timedelta(days=i)).isoformat() for i in range(total_days)}
    free_dates = sorted(all_dates - busy_dates)
    busy_sorted = sorted(busy_dates & all_dates)

    logger.info(
        f"[CALENDAR] Date breakdown for {start_date} to {end_date}: "
        f"{len(busy_sorted)} busy ({', '.join(busy_sorted)}), "
        f"{len(free_dates)} free ({', '.join(free_dates)})"
    )
    return busy_sorted


def parse_shoot_dates(text: str) -> tuple[str, str] | None:
    """Extract shoot dates from project notes or breakdown text.

    Handles AA-style formats like:
    - "Shoot Dates: April 12 - 25, 2026"
    - "April 7-18, 2026"
    - "AVAILABILITY BETWEEN APRIL 12-25, 2026"
    - "from April 7-18, 2026"

    Returns (start_iso, end_iso) or None if no shoot dates found.
    """
    import re
    from datetime import datetime

    current_year = datetime.now().year
    logger.info(f"[CALENDAR] parse_shoot_dates input (first 200 chars): {text[:200]}")

    # Pattern: "Month Day - Day, Year" (e.g., "April 12-25, 2026" or "April 12 - 25, 2026")
    match = re.search(
        r"(\w+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s*(\d{4})",
        text,
    )
    if match:
        month_str, start_day, end_day, year = match.groups()
        try:
            start = datetime.strptime(f"{month_str} {start_day} {year}", "%B %d %Y")
            end = datetime.strptime(f"{month_str} {end_day} {year}", "%B %d %Y")
            result = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            logger.info(f"[CALENDAR] Parsed shoot dates (pattern 1): {result[0]} to {result[1]}")
            return result
        except ValueError as e:
            logger.warning(f"[CALENDAR] Pattern 1 matched but date parsing failed: {e} (groups={match.groups()})")

    # Pattern: "Month Day - Month Day, Year" (e.g., "March 28 - April 5, 2026")
    match = re.search(
        r"(\w+)\s+(\d{1,2})\s*[-–]\s*(\w+)\s+(\d{1,2}),?\s*(\d{4})",
        text,
    )
    if match:
        m1, d1, m2, d2, year = match.groups()
        try:
            start = datetime.strptime(f"{m1} {d1} {year}", "%B %d %Y")
            end = datetime.strptime(f"{m2} {d2} {year}", "%B %d %Y")
            result = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            logger.info(f"[CALENDAR] Parsed shoot dates (pattern 2): {result[0]} to {result[1]}")
            return result
        except ValueError as e:
            logger.warning(f"[CALENDAR] Pattern 2 matched but date parsing failed: {e} (groups={match.groups()})")

    # Pattern 3: "Month Day-Day" or "Month Day - Month Day" range without year
    # (e.g., "March 30-April 3 in L.A.", "May 23-25 in LA")
    # Check BEFORE single-date pattern so ranges aren't misread as singles
    match = re.search(
        r"(\w+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–]\s*(?:(\w+)\.?\s+)?(\d{1,2})(?:st|nd|rd|th)?",
        text,
    )
    if match:
        m1, d1, m2, d2 = match.groups()
        m1 = m1.rstrip(".")
        if m2:
            m2 = m2.rstrip(".")
        else:
            m2 = m1  # same month range
        try:
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    start = datetime.strptime(f"{m1} {d1} {current_year}", fmt)
                    end = datetime.strptime(f"{m2} {d2} {current_year}", fmt)
                    result = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
                    logger.info(f"[CALENDAR] Parsed shoot dates (pattern 3 - range no year): {result[0]} to {result[1]}")
                    return result
                except ValueError:
                    continue
        except ValueError:
            pass

    # Pattern 4: "Month Day" single date without year (e.g., "Shoots April 15 in LA")
    # Also handles abbreviated months (Apr., Mar., etc.)
    match = re.search(
        r"(\w+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*[-–])",
        text,
    )
    if match:
        month_str, day = match.groups()
        month_str = month_str.rstrip(".")
        try:
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    dt = datetime.strptime(f"{month_str} {day} {current_year}", fmt)
                    result = dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d")
                    logger.info(f"[CALENDAR] Parsed shoot dates (pattern 4 - single date): {result[0]}")
                    return result
                except ValueError:
                    continue
        except ValueError:
            pass

    if re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', text):
        logger.info("[CALENDAR] Month name found but no specific dates — skipping calendar check (role will proceed)")
    else:
        logger.info("[CALENDAR] No shoot dates found in project_notes")
    return None


def parse_all_dates(text: str) -> list[tuple[str, str]]:
    """Extract all date ranges from text, returning every match (not just the first).

    Uses the same 4 patterns as parse_shoot_dates but collects all non-overlapping matches.
    Returns a list of (start_iso, end_iso) tuples.
    """
    import re
    from datetime import datetime

    current_year = datetime.now().year
    results: list[tuple[str, str]] = []
    seen_spans: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(s <= start < e or s < end <= e for s, e in seen_spans)

    patterns = [
        (r"(\w+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s*(\d{4})", "range_same_month_year"),
        (r"(\w+)\s+(\d{1,2})\s*[-–]\s*(\w+)\s+(\d{1,2}),?\s*(\d{4})", "range_cross_month_year"),
        (r"(\w+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–]\s*(?:(\w+)\.?\s+)?(\d{1,2})(?:st|nd|rd|th)?", "range_no_year"),
        (r"(\w+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*[-–])", "single_no_year"),
    ]

    for pat, kind in patterns:
        for m in re.finditer(pat, text):
            if _overlaps(m.start(), m.end()):
                continue
            try:
                if kind == "range_same_month_year":
                    month_str, d1, d2, year = m.groups()
                    start = datetime.strptime(f"{month_str} {d1} {year}", "%B %d %Y")
                    end = datetime.strptime(f"{month_str} {d2} {year}", "%B %d %Y")
                elif kind == "range_cross_month_year":
                    m1, d1, m2, d2, year = m.groups()
                    start = datetime.strptime(f"{m1} {d1} {year}", "%B %d %Y")
                    end = datetime.strptime(f"{m2} {d2} {year}", "%B %d %Y")
                elif kind == "range_no_year":
                    m1, d1, m2, d2 = m.groups()
                    m1 = m1.rstrip(".")
                    m2 = (m2 or m1).rstrip(".")
                    start = end = None
                    for fmt in ("%B %d %Y", "%b %d %Y"):
                        try:
                            start = datetime.strptime(f"{m1} {d1} {current_year}", fmt)
                            end = datetime.strptime(f"{m2} {d2} {current_year}", fmt)
                            break
                        except ValueError:
                            continue
                    if not start or not end:
                        continue
                elif kind == "single_no_year":
                    month_str = m.group(1).rstrip(".")
                    day = m.group(2)
                    start = end = None
                    for fmt in ("%B %d %Y", "%b %d %Y"):
                        try:
                            start = end = datetime.strptime(f"{month_str} {day} {current_year}", fmt)
                            break
                        except ValueError:
                            continue
                    if not start:
                        continue
                else:
                    continue

                pair = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
                if pair not in results:
                    results.append(pair)
                    seen_spans.append((m.start(), m.end()))
            except ValueError:
                continue

    return results


def parse_work_dates(submission_date: str) -> tuple[str, str] | None:
    """Extract work dates from a submission_date string.

    Handles formats like:
    - "Work Mar 29 - Mar 30"
    - "Submissions Due ... | Work Mar 29 - Mar 30 | Posted ..."
    - "Work Mar 29 - Apr 5, 2026"
    - "Work Mar 29, 2026"

    Returns (start_iso, end_iso) or None if no work dates found.
    """
    import re
    from datetime import datetime
    logger.info(f"[CALENDAR] parse_work_dates input: {submission_date[:200]}")

    match = re.search(
        r"Work\s+(\w+ \d+(?:,?\s*\d{4})?)\s*(?:-\s*(\w+ \d+(?:,?\s*\d{4})?))?",
        submission_date,
    )
    if not match:
        logger.info("[CALENDAR] No 'Work ...' pattern found in submission_date")
        return None

    raw_start = match.group(1).strip().rstrip(",")
    raw_end = (match.group(2) or "").strip().rstrip(",")
    current_year = datetime.now().year

    def _parse(raw: str) -> str | None:
        if not raw:
            return None
        for fmt in ("%b %d %Y", "%b %d, %Y", "%b %d"):
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.year == 1900:
                    dt = dt.replace(year=current_year)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        logger.warning(f"[CALENDAR] Could not parse work date: '{raw}'")
        return None

    start = _parse(raw_start)
    if not start:
        logger.warning(f"[CALENDAR] Failed to parse start work date from: '{raw_start}'")
        return None
    end = _parse(raw_end) if raw_end else start
    logger.info(f"[CALENDAR] Parsed work dates: {start} to {end}")
    return start, end


def check_work_date_conflicts(
    role: dict, calendar_ids: list[str],
) -> tuple[bool, list[str]]:
    """Check if a role's work dates conflict with calendar events.

    Args:
        role: Role dict (must have 'submission_date' key).
        calendar_ids: List of Google Calendar IDs to check.

    Returns:
        (True, conflicts) if there is a conflict,
        (False, []) if no conflict or no work dates found.
    """
    dates = parse_work_dates(role.get("submission_date", ""))
    if not dates:
        logger.info("[CALENDAR] No work dates found for role, skipping calendar check")
        return False, []

    start, end = dates
    available, conflicts = check_availability(start, end, calendar_ids)
    if not available:
        logger.info(f"[CALENDAR] CONFLICT for work dates {start} to {end}: {', '.join(conflicts[:5])}")
        return True, conflicts
    logger.info(f"[CALENDAR] No conflict for work dates {start} to {end}")
    return False, []
