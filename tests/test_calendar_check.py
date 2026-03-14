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
        {"summary": "Callback - Netflix"},
        {"summary": "Self-tape deadline"},
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
