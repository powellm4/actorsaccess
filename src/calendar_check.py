# src/calendar_check.py
"""Google Calendar integration for checking date availability."""

import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

_service_cache = None
_calendar_id_cache = {}


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
        return _service_cache
    except Exception as e:
        logger.warning(f"Failed to build calendar service: {e}")
        return None


def _resolve_calendar_ids(service, calendar_names: list[str]) -> list[str]:
    """Resolve calendar names to their IDs."""
    cache_key = tuple(sorted(calendar_names))
    if cache_key in _calendar_id_cache:
        return _calendar_id_cache[cache_key]

    try:
        cal_list = service.calendarList().list().execute()
        name_to_id = {cal["summary"]: cal["id"] for cal in cal_list.get("items", [])}
        ids = []
        for name in calendar_names:
            if name in name_to_id:
                ids.append(name_to_id[name])
            else:
                logger.warning(f"Calendar '{name}' not found — skipping")
        _calendar_id_cache[cache_key] = ids
        return ids
    except Exception as e:
        logger.warning(f"Failed to list calendars: {e}")
        return []


def check_availability(
    start_date: str, end_date: str, calendar_names: list[str],
) -> tuple[bool, list[str]]:
    """Check if a date range is free on the specified calendars.

    Args:
        start_date: ISO date string (e.g., "2026-04-05")
        end_date: ISO date string (e.g., "2026-04-12")
        calendar_names: List of calendar names to check (e.g., ["Acting", "Travel"])

    Returns:
        Tuple of (is_available, conflicting_event_names).
        Returns (True, []) if calendar service is unavailable.
    """
    service = get_calendar_service()
    if service is None:
        return True, []

    calendar_ids = _resolve_calendar_ids(service, calendar_names)
    if not calendar_ids:
        return True, []

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

    return len(conflicts) == 0, conflicts
