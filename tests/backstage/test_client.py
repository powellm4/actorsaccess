"""Tests for BackstageClient.submit_for_role, focusing on the prepare_only path."""
from unittest.mock import patch

import pytest

from src.backstage.client import BackstageClient, APPLICATION_URL


@pytest.fixture
def client():
    return BackstageClient()


def test_submit_for_role_prepare_only_returns_prepared_dict(client):
    """prepare_only=True creates the draft and returns before the final PUT."""
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append((url, data, method))
        if url == APPLICATION_URL:
            return {"id": 12345}
        return None

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=999, prepare_only=True)

    assert result == {"_prepared": True, "app_id": 12345}
    # Exactly one call: the draft creation. No PUT.
    assert len(calls) == 1
    assert calls[0][0] == APPLICATION_URL
    assert calls[0][2] is None  # POST, not PUT


def test_submit_for_role_prepare_only_attaches_media_but_skips_put(client):
    """prepare_only with media_ids posts media shares but still stops before step 4."""
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append((url, data, method))
        if url == APPLICATION_URL:
            return {"id": 77}
        if url.endswith(f"/talent_application/async/77/"):
            # attach_media fetches existing shares on the draft
            return {"shared_assets": []}
        if "medialocker/async/shares" in url:
            # Successful media attach returns a list
            return [{"ok": 1}]
        return None

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=42, media_ids=[101], prepare_only=True)

    assert result == {"_prepared": True, "app_id": 77}
    # Must NOT include a PUT to the application URL
    puts = [c for c in calls if c[2] == "PUT"]
    assert puts == []


def test_submit_for_role_prepare_only_draft_creation_fails(client):
    """If step 1 returns None, prepare_only returns None — no draft to prepare."""
    with patch.object(client, "_request", return_value=None), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=5, prepare_only=True)

    assert result is None


def test_submit_for_role_prepare_only_draft_rejected(client):
    """If step 1 is rejected with a structured error, prepare_only passes the _rejected shape through."""
    with patch.object(client, "_request",
                      return_value={"_error": True, "code": 400,
                                    "detail": {"non_field_errors": ["already applied"]}}), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=5, prepare_only=True)

    assert isinstance(result, dict)
    assert result.get("_rejected") is True


def test_submit_for_role_regular_path_still_submits(client):
    """prepare_only=False (default) must still run the final PUT."""
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append((url, data, method))
        if url == APPLICATION_URL:
            return {"id": 88}
        if method == "PUT":
            return {"id": 88, "applicant_status": "C"}
        return None

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=10)

    puts = [c for c in calls if c[2] == "PUT"]
    assert len(puts) == 1
    assert puts[0][1] == {"applicant_status": "C"}
    assert isinstance(result, dict) and result.get("id") == 88
