# src/backstage/client.py
"""HTTP client for Backstage.com REST API.

Uses urllib (not requests) because Cloudflare blocks the requests library's
TLS fingerprint. Python's built-in urllib uses a different SSL stack that
passes Cloudflare's checks.
"""

import json
import logging
import random
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from http.cookiejar import CookieJar

logger = logging.getLogger(__name__)

BASE_URL = "https://www.backstage.com"
LOGIN_URL = f"{BASE_URL}/accounts/async/login/"
CASTING_URL = f"{BASE_URL}/casting/async/"
APPLICATION_URL = f"{BASE_URL}/talent_application/async/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) "
    "Gecko/20100101 Firefox/134.0"
)


def _random_delay(min_sec: float = 1.0, max_sec: float = 3.0):
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Waiting {delay:.1f}s")
    time.sleep(delay)


class BackstageClient:
    def __init__(self):
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._csrftoken = ""
        self._logged_in = False

    def _request(self, url: str, data: dict | None = None, method: str | None = None) -> dict | str | None:
        """Make an HTTP request with cookie management.

        If data is provided, defaults to POST (unless method is specified).
        Returns parsed JSON dict for JSON responses, raw string for HTML,
        or None on error.
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": f"{BASE_URL}/casting/",
        }

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-Requested-With"] = "XMLHttpRequest"
            if self._csrftoken:
                headers["X-CSRFToken"] = self._csrftoken

        kwargs = {"url": url, "data": body, "headers": headers}
        if method:
            kwargs["method"] = method
        req = urllib.request.Request(**kwargs)

        try:
            resp = self._opener.open(req, timeout=15)
            content_type = resp.headers.get("Content-Type", "")

            # Update CSRF token from cookies
            for cookie in self._cookie_jar:
                if cookie.name == "csrftoken":
                    self._csrftoken = cookie.value

            response_body = resp.read().decode("utf-8")

            if "json" in content_type:
                return json.loads(response_body)
            return response_body

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            logger.error(f"HTTP {e.code} for {url}: {body}")
            # Return structured error for 400s so callers can inspect the reason
            if e.code == 400:
                try:
                    error_data = json.loads(body)
                    return {"_error": True, "code": 400, "detail": error_data}
                except (json.JSONDecodeError, ValueError):
                    pass
            return None
        except Exception as e:
            logger.error(f"Request error for {url}: {e}")
            return None

    def login(self, email: str, password: str) -> bool:
        """Log in via API and establish session cookies."""
        data = self._request(LOGIN_URL, data={"username": email, "password": password})
        if isinstance(data, dict) and data.get("id"):
            logger.info(f"Logged in as {data.get('full_name', email)}")
            logger.info(f"Member status: {data.get('member_status')}, subscription: {data.get('subscription_state')}")
            self._logged_in = True
            return True
        logger.error(f"Login failed: {data}")
        return False

    def fetch_saved_searches(self) -> list[dict]:
        """Fetch the user's saved searches."""
        data = self._request(f"{BASE_URL}/casting/async/saved-search/")
        if isinstance(data, list):
            return data
        logger.error(f"Failed to fetch saved searches: {data}")
        return []

    def fetch_listings(self, page: int = 1, size: int = 20, saved_search: dict | None = None) -> dict:
        """Fetch casting call listings from the async API.

        If saved_search is provided, uses its search_params as the query
        filters (matching the user's configured search on Backstage).
        Otherwise falls back to default params.
        """
        if saved_search:
            params = dict(saved_search.get("search_params", {}))
            params["page"] = page
            params["size"] = size
            # search_params may contain lists (pt, role_type, etc.)
            # Need to build query string manually for repeated keys
            query_parts = []
            for key, val in params.items():
                if val is None or val == "" or val == []:
                    continue
                if isinstance(val, list):
                    for item in val:
                        query_parts.append(f"{key}={urllib.parse.quote_plus(str(item))}")
                elif isinstance(val, bool):
                    query_parts.append(f"{key}={str(val).lower()}")
                else:
                    query_parts.append(f"{key}={urllib.parse.quote_plus(str(val))}")
            url = f"{CASTING_URL}?{'&'.join(query_parts)}"
        else:
            params = urllib.parse.urlencode({
                "page": page,
                "size": size,
                "sort_by": "newest",
                "view": "production",
                "gender": "M",
                "min_age": 18,
                "max_age": 32,
                "compensation_type": "paid",
                "job_type": "acting",
            })
            url = f"{CASTING_URL}?{params}"

        _random_delay(1, 2)
        data = self._request(url)
        if isinstance(data, dict):
            return data
        logger.error(f"Failed to fetch listings page {page}")
        return {"items": [], "counts": {}}

    def fetch_role_detail(self, role_url: str) -> dict | None:
        """Fetch role detail page and extract embedded JSON with full role data.

        The detail page contains a JSON blob with prescreen_type, prescreen_message,
        full description, attachments, and other fields not in the listing API.

        Returns the full production dict (with nested roles), or None on failure.
        """
        full_url = role_url if role_url.startswith("http") else f"{BASE_URL}{role_url}"
        if not full_url.endswith("/"):
            full_url += "/"

        _random_delay(1, 2)
        html = self._request(full_url)
        if not isinstance(html, str):
            logger.warning(f"Role detail returned non-HTML for {full_url}")
            return None

        if "cloudflare" in html[:500].lower() or "blocked" in html[:500].lower():
            logger.warning(f"Cloudflare blocked role detail page: {full_url}")
            return None

        # Find the embedded JSON: {"id": <casting_call_id>, ... "roles": [...]}
        match = re.search(r'\{"id":\s*\d+,\s*"(?:legacy_id|status)"', html)
        if not match:
            logger.warning(f"No embedded JSON found on {full_url}")
            return None

        # Parse balanced braces from match start
        text = html[match.start():]
        depth = 0
        end = 0
        for i, c in enumerate(text[:50000]):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            if depth == 0:
                end = i + 1
                break

        try:
            return json.loads(text[:end])
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse role detail JSON: {e}")
            return None

    def attach_media(self, app_id: int, media_ids: list[int]) -> bool:
        """Attach media (video reels, etc.) to a draft application.

        Uses POST /medialocker/async/shares/ with an array of share objects.
        Each share links a medialocker asset to the application.

        Returns True if all media were attached successfully.
        """
        if not media_ids:
            return True

        # Get existing shared_assets to determine ordering offset
        existing = self._request(f"{APPLICATION_URL}{app_id}/")
        existing_count = len(existing.get("shared_assets", [])) if isinstance(existing, dict) else 0
        existing_ids = set()
        if isinstance(existing, dict):
            existing_ids = {a["asset"]["id"] for a in existing.get("shared_assets", []) if "asset" in a}

        # Filter out media already attached
        new_ids = [mid for mid in media_ids if mid not in existing_ids]
        if not new_ids:
            logger.info(f"All {len(media_ids)} media already attached to app {app_id}")
            return True

        shares = [
            {
                "asset_id": mid,
                "object_id": app_id,
                "content_type": "applicant",
                "is_primary": False,
                "featured": False,
                "ordering": existing_count + i,
            }
            for i, mid in enumerate(new_ids)
        ]

        _random_delay(1, 2)
        result = self._request(f"{BASE_URL}/medialocker/async/shares/", data=shares)
        if isinstance(result, list):
            logger.info(f"Attached {len(new_ids)} media to app {app_id}: {new_ids}")
            return True
        logger.warning(f"Failed to attach media to app {app_id}: {result}")
        return False

    def submit_prescreen(self, role_id: int, answers: list[dict]) -> dict | None:
        """Submit applicant prescreen answers for a role.

        Endpoint: POST /talent_application/async/prescreen/{role_id}/
        Body: list of answer dicts, each:
            {"question_id": <int>, "selected_answer_id": <int or null>, "answer_text": <str or null>}

        Backstage treats prescreen submission as the entire application for roles
        that have applicant questions — no separate draft/PUT step is needed.

        Returns the response dict on success (HTTP 201), or {"_rejected": True, "reason": ...}
        on a 400 rejection, or None on other failure.
        """
        url = f"{APPLICATION_URL}prescreen/{role_id}/"
        _random_delay(1, 3)
        result = self._request(url, data=answers)
        if isinstance(result, dict) and result.get("_error"):
            detail = result.get("detail", {})
            errors = detail.get("non_field_errors", []) if isinstance(detail, dict) else []
            reason = "; ".join(errors) if errors else str(detail)
            logger.error(f"Prescreen submission rejected for role {role_id}: {reason}")
            return {"_rejected": True, "reason": reason}
        if result is None:
            logger.error(f"Prescreen submission failed for role {role_id}")
            return None
        if isinstance(result, dict):
            logger.info(f"Prescreen submission accepted for role {role_id}")
            return result
        # Empty / non-JSON 2xx body — also treat as success
        logger.info(f"Prescreen submission accepted (non-JSON response) for role {role_id}")
        return {"ok": True}

    def submit_for_role(self, role_id: int, note: str = "", media_ids: list[int] | None = None) -> dict | None:
        """Submit an application for a role.

        Three-step process:
        1. POST /talent_application/async/ with {role: id} → creates draft
        2. Attach video reels via /medialocker/async/shares/ (if media_ids provided)
        3. PUT /talent_application/async/{app_id}/ with {applicant_status: "C"} → submits
        """
        # Step 1: Create draft
        _random_delay(1, 3)
        app = self._request(APPLICATION_URL, data={"role": role_id})
        if isinstance(app, dict) and app.get("_error"):
            detail = app.get("detail", {})
            errors = detail.get("non_field_errors", [])
            reason = "; ".join(errors) if errors else str(detail)
            logger.error(f"Draft creation rejected for role {role_id}: {reason}")
            return {"_rejected": True, "reason": reason}
        if not isinstance(app, dict) or not app.get("id"):
            logger.error(f"Draft creation failed for role {role_id}: {app}")
            return None

        app_id = app["id"]
        logger.info(f"Created draft application {app_id} for role {role_id}")

        # Step 2: Attach video reels
        if media_ids:
            logger.info(f"Attaching {len(media_ids)} video reels to app {app_id}: {media_ids}")
            if not self.attach_media(app_id, media_ids):
                logger.warning(f"Failed to attach media to app {app_id}, submitting without videos")
        else:
            logger.info(f"No media_ids provided for app {app_id}, skipping video attachment")

        # Step 3: Submit via PUT (change status from Draft "I" to Submitted "C")
        _random_delay(2, 4)
        submit_data = {"applicant_status": "C"}
        if note:
            submit_data["note"] = note

        submit_url = f"{APPLICATION_URL}{app_id}/"
        result = self._request(submit_url, data=submit_data, method="PUT")
        if isinstance(result, dict) and result.get("_error"):
            detail = result.get("detail", {})
            errors = detail.get("non_field_errors", [])
            reason = "; ".join(errors) if errors else str(detail)
            logger.error(f"Submission rejected for app {app_id}: {reason}")
            return {"_rejected": True, "reason": reason}
        if isinstance(result, dict) and not result.get("_error"):
            logger.info(f"Application {app_id} submitted successfully")
            return result
        logger.error(f"Submission failed for app {app_id}: {result}")
        return None
