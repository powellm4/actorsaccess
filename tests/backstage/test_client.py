"""Tests for BackstageClient.submit_for_role, focusing on the prepare_only path."""
from unittest.mock import patch

import pytest

from src.backstage.client import BackstageClient, APPLICATION_URL, BASE_URL


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


# fetch_role_detail: Cloudflare retry behaviour
# A transient 403 JS challenge on the first request should not flag the role —
# we retry once with a longer backoff. Persistent challenges still return None.

VALID_DETAIL_HTML = (
    '<html><head></head><body>'
    '<script>window.__data = {"id": 12345, "legacy_id": 999, '
    '"roles": [{"id": 1, "role_name": "Lead"}]};</script>'
    '</body></html>'
)
CLOUDFLARE_403 = {"_error": True, "code": 403, "cloudflare": True}


def test_fetch_role_detail_retries_after_cloudflare_403(client):
    """Transient Cloudflare 403 on attempt 1 → retry → success on attempt 2."""
    responses = [CLOUDFLARE_403, VALID_DETAIL_HTML]
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append(url)
        return responses[len(calls) - 1]

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_role_detail("/casting/foo/bar/")

    assert len(calls) == 2
    assert result == {"id": 12345, "legacy_id": 999, "roles": [{"id": 1, "role_name": "Lead"}]}


def test_fetch_role_detail_gives_up_after_two_cloudflare_403s(client):
    """Two consecutive Cloudflare 403s → return None (flag the role)."""
    with patch.object(client, "_request", return_value=CLOUDFLARE_403), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_role_detail("/casting/foo/bar/")

    assert result is None


def test_fetch_role_detail_retries_on_inline_cloudflare_challenge_page(client):
    """200 OK with a Cloudflare 'Just a moment...' body is also retried."""
    cf_page = '<html><head><title>Just a moment...</title></head><body></body></html>'
    responses = [cf_page, VALID_DETAIL_HTML]
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append(url)
        return responses[len(calls) - 1]

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_role_detail("/casting/foo/bar/")

    assert len(calls) == 2
    assert isinstance(result, dict) and result.get("id") == 12345


def test_fetch_saved_searches_returns_list_on_success(client):
    """A successful fetch returns the list of saved searches verbatim."""
    searches = [{"id": 1, "name": "acting"}, {"id": 2, "name": "unpaid"}]
    with patch.object(client, "_request", return_value=searches), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_saved_searches()

    assert result == searches


def test_fetch_saved_searches_returns_none_on_request_failure(client):
    """A failed request (None) is reported as None, NOT an empty list — so the
    caller doesn't mistake a fetch failure for 'no saved searches exist'."""
    with patch.object(client, "_request", return_value=None), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_saved_searches()

    assert result is None


def test_fetch_saved_searches_retries_after_cloudflare_403(client):
    """Transient Cloudflare 403 on attempt 1 → retry → success on attempt 2."""
    responses = [CLOUDFLARE_403, [{"id": 9, "name": "unpaid"}]]
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append(url)
        return responses[len(calls) - 1]

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_saved_searches()

    assert len(calls) == 2
    assert result == [{"id": 9, "name": "unpaid"}]


def test_fetch_saved_searches_returns_none_after_persistent_cloudflare(client):
    """Two consecutive Cloudflare 403s → return None (transient block, not missing)."""
    with patch.object(client, "_request", return_value=CLOUDFLARE_403), \
         patch("src.backstage.client._random_delay"):
        result = client.fetch_saved_searches()

    assert result is None


def test_request_returns_cloudflare_marker_for_403_challenge(client):
    """_request should surface a 403 'Just a moment...' as a structured error."""
    import urllib.error
    import io

    cf_body = b'<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>'
    err = urllib.error.HTTPError(
        url=f"{BASE_URL}/casting/foo/", code=403, msg="Forbidden",
        hdrs=None, fp=io.BytesIO(cf_body),
    )

    with patch.object(client._opener, "open", side_effect=err):
        result = client._request(f"{BASE_URL}/casting/foo/")

    assert result == {"_error": True, "code": 403, "cloudflare": True}
