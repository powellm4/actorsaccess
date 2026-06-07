# tests/test_calendar_check.py
"""Tests for Google Calendar availability checking."""

import os
from unittest.mock import patch, MagicMock

import pytest

import src.calendar_check as calendar_check_module
from src.calendar_check import get_calendar_service, check_availability


def _mock_events_list(items):
    """Create a mock calendar service that returns given items for events().list()."""
    mock_service = MagicMock()
    mock_events = MagicMock()
    mock_service.events.return_value = mock_events
    mock_list = MagicMock()
    mock_events.list.return_value = mock_list
    mock_list.execute.return_value = {"items": items}

    # Also mock calendarList for resolving calendar names
    mock_cal_list = MagicMock()
    mock_service.calendarList.return_value = mock_cal_list
    mock_cal_list_exec = MagicMock()
    mock_cal_list.list.return_value = mock_cal_list_exec
    mock_cal_list_exec.execute.return_value = {
        "items": [
            {"id": "acting-cal-id", "summary": "Acting"},
            {"id": "travel-cal-id", "summary": "Travel"},
        ]
    }
    return mock_service


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level caches between tests."""
    calendar_check_module._service_cache = None
    calendar_check_module._calendar_id_cache = {}
    yield
    calendar_check_module._service_cache = None
    calendar_check_module._calendar_id_cache = {}


def test_get_calendar_service_no_env_var_returns_none():
    """Missing GOOGLE_CALENDAR_SA_KEY should return None."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("GOOGLE_CALENDAR_SA_KEY", None)
        result = get_calendar_service()
    assert result is None


def test_check_availability_free():
    """No events in range should return (True, [])."""
    mock_service = _mock_events_list([])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-04-05", "2026-04-12", ["Acting", "Travel"]
        )
    assert available is True
    assert conflicts == []


def test_check_availability_conflict():
    """Events in range should return (False, [event names])."""
    mock_service = _mock_events_list([
        {
            "summary": "Callback - Netflix",
            "start": {"dateTime": "2026-04-07T10:00:00-04:00"},
            "end": {"dateTime": "2026-04-07T12:00:00-04:00"},
        },
        {
            "summary": "Self-tape deadline",
            "start": {"date": "2026-04-10"},
            "end": {"date": "2026-04-11"},
        },
    ])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-04-05", "2026-04-12", ["Acting"]
        )
    assert available is False
    assert conflicts == ["Callback - Netflix", "Self-tape deadline"]


def test_check_availability_no_service():
    """No calendar service should return (True, []) — graceful fallback."""
    with patch("src.calendar_check.get_calendar_service", return_value=None):
        available, conflicts = check_availability(
            "2026-04-05", "2026-04-12", ["Acting", "Travel"]
        )
    assert available is True
    assert conflicts == []


def test_check_availability_ignores_adjacent_all_day_event():
    """An all-day event on Jun 7 (end.date Jun 8 exclusive) must NOT conflict with a Jun 8 query.

    This is the bug that flagged BRIDGEVILLE / SUPER RUSH / COLD BREW / WORLD CUP /
    REEDS JEWELERS / Bound by duty in the 2026-06-07 paid digest — Google's API
    returned the adjacent all-day event because of UTC overlap, and the old code
    treated it as a conflict.
    """
    mock_service = _mock_events_list([
        {
            "summary": "Vertical",
            "start": {"date": "2026-06-07"},
            "end": {"date": "2026-06-08"},  # exclusive: covers Jun 7 only
        },
    ])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-06-08", "2026-06-08", ["Acting"]
        )
    assert available is True
    assert conflicts == []


def test_check_availability_flags_all_day_event_on_requested_date():
    """An all-day event on Jun 8 (end.date Jun 9) SHOULD conflict with a Jun 8 query."""
    mock_service = _mock_events_list([
        {
            "summary": "Vertical",
            "start": {"date": "2026-06-08"},
            "end": {"date": "2026-06-09"},
        },
    ])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-06-08", "2026-06-08", ["Acting"]
        )
    assert available is False
    assert conflicts == ["Vertical"]


def test_check_availability_flags_timed_event_on_requested_date():
    """A timed event on the requested day SHOULD conflict."""
    mock_service = _mock_events_list([
        {
            "summary": "Callback",
            "start": {"dateTime": "2026-06-08T14:00:00-04:00"},
            "end": {"dateTime": "2026-06-08T16:00:00-04:00"},
        },
    ])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-06-08", "2026-06-08", ["Acting"]
        )
    assert available is False
    assert conflicts == ["Callback"]


def test_check_availability_deduplicates_multi_day_events():
    """An event spanning multiple requested days should appear once, not per-day."""
    mock_service = _mock_events_list([
        {
            "summary": "Yosemite",
            "start": {"date": "2026-07-17"},
            "end": {"date": "2026-07-20"},  # covers 17/18/19
        },
    ])
    with patch("src.calendar_check.get_calendar_service", return_value=mock_service):
        available, conflicts = check_availability(
            "2026-07-17", "2026-07-19", ["Acting"]
        )
    assert available is False
    assert conflicts == ["Yosemite"]
