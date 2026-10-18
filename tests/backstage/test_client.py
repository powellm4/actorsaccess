"""Tests for BackstageClient.submit_for_role and finalize_draft."""
from unittest.mock import patch

import pytest

from src.backstage.client import BackstageClient, APPLICATION_URL, BASE_URL


@pytest.fixture
def client():
    return BackstageClient()


def test_finalize_draft_puts_note_and_returns_result(client):
    """finalize_draft runs the final PUT for an existing draft with the note."""
    calls = []

    def fake_request(url, data=None, method=None):
        calls.append((url, data, method))
        return {"id": 12345, "applicant_status": "C"}

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.finalize_draft(12345, note="Hello casting")

    assert calls == [(f"{APPLICATION_URL}12345/", {"applicant_status": "C", "note": "Hello casting"}, "PUT")]
    assert isinstance(result, dict) and result.get("id") == 12345


def test_finalize_draft_rejection_carries_app_id(client):
    """A rejected final PUT reports the reason AND the draft id, so the caller
    can surface the still-existing draft instead of losing it."""
    with patch.object(client, "_request",
                      return_value={"_error": True, "code": 400,
                                    "detail": {"non_field_errors": ["Not all role requirements met: Cover Letter"]}}), \
         patch("src.backstage.client._random_delay"):
        result = client.finalize_draft(77, note="x")

    assert result == {"_rejected": True, "reason": "Not all role requirements met: Cover Letter", "app_id": 77}


def test_submit_for_role_cover_letter_required_sends_note_as_cover_letter_too(client):
    """For cover-letter-required roles the text is sent under both the proven
    `note` key and a `cover_letter` key, so whichever field Backstage checks
    for the requirement is satisfied."""
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
        result = client.submit_for_role(role_id=10, note="Dear casting", cover_letter_required=True)

    puts = [c for c in calls if c[2] == "PUT"]
    assert len(puts) == 1
    assert puts[0][1] == {"applicant_status": "C", "note": "Dear casting", "cover_letter": "Dear casting"}
    assert isinstance(result, dict) and result.get("id") == 88


def test_submit_for_role_final_put_rejection_includes_draft_id(client):
    def fake_request(url, data=None, method=None):
        if url == APPLICATION_URL:
            return {"id": 5}
        if method == "PUT":
            return {"_error": True, "code": 400,
                    "detail": {"non_field_errors": ["Not all role requirements met: Cover Letter"]}}
        return None

    with patch.object(client, "_request", side_effect=fake_request), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=10, note="x", cover_letter_required=True)

    assert result["_rejected"] is True
    assert result["app_id"] == 5


def test_submit_for_role_draft_rejected(client):
    """If step 1 is rejected with a structured error, the _rejected shape is returned."""
    with patch.object(client, "_request",
                      return_value={"_error": True, "code": 400,
                                    "detail": {"non_field_errors": ["already applied"]}}), \
         patch("src.backstage.client._random_delay"):
        result = client.submit_for_role(role_id=5)

    assert isinstance(result, dict)
    assert result.get("_rejected") is True
    assert "app_id" not in result


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


def test_fetch_saved_searches_backs_off_through_three_attempts(client):
    """Two Cloudflare 403s then success: three attempts, each retry preceded
    by a longer back-off delay."""
    responses = [CLOUDFLARE_403, CLOUDFLARE_403, [{"id": 9, "name": "acting"}]]
    with patch.object(client, "_request", side_effect=responses) as req, \
         patch("src.backstage.client._random_delay") as delay:
        result = client.fetch_saved_searches()

    assert req.call_count == 3
    assert result == [{"id": 9, "name": "acting"}]
    assert [c.args for c in delay.call_args_list] == [(20, 40), (45, 75)]
