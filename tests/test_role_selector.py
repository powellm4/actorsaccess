# tests/test_role_selector.py
"""Tests for the role selector's parsing and selection logic.

These tests mock the Anthropic API to test parsing without making real API calls.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from src.role_selector import select_best_roles


SAMPLE_ROLES = [
    {"role_name": "Jake", "role_type": "Lead", "age_range": "25-30", "gender": "Male", "description": "Confident protagonist"},
    {"role_name": "Officer Dan", "role_type": "Supporting", "age_range": "40-50", "gender": "Male", "description": "Grizzled veteran cop"},
    {"role_name": "Tommy", "role_type": "Lead", "age_range": "22-28", "gender": "Male", "description": "Charming con artist"},
]


def _make_mock_anthropic(response_text: str):
    """Create a mock anthropic module with a preset response."""
    mock_module = MagicMock()
    mock_client = MagicMock()
    mock_module.Anthropic.return_value = mock_client
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = response_text
    mock_response.content = [mock_content]
    mock_client.messages.create.return_value = mock_response
    return mock_module, mock_client


def _make_mock_anthropic_error():
    """Create a mock anthropic module that raises on create."""
    mock_module = MagicMock()
    mock_client = MagicMock()
    mock_module.Anthropic.return_value = mock_client
    mock_client.messages.create.side_effect = Exception("API timeout")
    return mock_module, mock_client


def test_single_role_returns_directly():
    """Single candidate should return without API call."""
    roles = [SAMPLE_ROLES[0]]
    selected, rejections = select_best_roles(roles, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert selected[0][1] == "only matching role"
    assert rejections == {}


def test_no_api_key_falls_back_to_first():
    """Missing API key should return first role."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")
    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "no API key" in selected[0][1]
    assert rejections == {}


def test_single_selection_parsed():
    """AI selecting one role should parse correctly."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Best physical and type match\nREJECTED: 2 - Age range too high for actor\nREJECTED: 3 - Similar to role 1 but less prominent"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "Best physical and type match" in selected[0][1]
    assert "Officer Dan" in rejections
    assert "Tommy" in rejections


def test_double_selection_parsed():
    """AI selecting two roles should parse both."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Great leading man fit\nSELECTED: 3 - Also a strong charming type\nREJECTED: 2 - Age range 40-50 is too old"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 2
    assert selected[0][0]["role_name"] == "Jake"
    assert selected[1][0]["role_name"] == "Tommy"
    assert "Officer Dan" in rejections


def test_skip_returns_empty_selected():
    """AI returning SKIP should return empty selected list."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SKIP - All roles require age 40+ which doesn't match actor profile"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 0
    assert len(rejections) == 3


def test_malformed_response_falls_back_to_first():
    """Unparseable AI response should fall back to first role."""
    mock_anthropic, _ = _make_mock_anthropic(
        "I think role 1 is the best choice because..."
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "unparseable" in selected[0][1].lower()
    assert len(rejections) == 2


def test_api_failure_falls_back_to_first():
    """API exception should fall back to first role."""
    mock_anthropic, _ = _make_mock_anthropic_error()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 1
    assert selected[0][0]["role_name"] == "Jake"
    assert "API timeout" in selected[0][1]


def test_three_selections_capped_at_two():
    """AI returning 3 SELECTED lines should cap at 2."""
    mock_anthropic, _ = _make_mock_anthropic(
        "SELECTED: 1 - Great fit\nSELECTED: 2 - Also good\nSELECTED: 3 - Third pick"
    )
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            selected, rejections = select_best_roles(SAMPLE_ROLES, "Test Project")

    assert len(selected) == 2
    assert "Tommy" in rejections
