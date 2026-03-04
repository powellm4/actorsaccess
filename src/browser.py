# src/browser.py
import logging
import random
import time
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

BASE_URL = "https://actorsaccess.com"


def _random_delay(min_sec: float = 2.0, max_sec: float = 8.0):
    """Sleep for a random duration to appear human-like."""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Waiting {delay:.1f}s")
    time.sleep(delay)


class ActorsAccessBrowser:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(30000)
        logger.info("Browser started")

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Browser closed")

    def login(self, username: str, password: str) -> bool:
        """Log into Actors Access. Returns True on success, False on failure."""
        try:
            logger.info("Navigating to Actors Access login page")
            self.page.goto(BASE_URL)
            _random_delay(1, 3)

            # PLACEHOLDER SELECTORS — update after manual site inspection
            self.page.fill('input[name="username"], input[id="username"], input[type="text"]', username)
            _random_delay(0.5, 1.5)
            self.page.fill('input[name="password"], input[id="password"], input[type="password"]', password)
            _random_delay(0.5, 1.5)

            # Click login button — PLACEHOLDER SELECTOR
            self.page.click('button[type="submit"], input[type="submit"], .login-button, #login-btn')
            _random_delay(2, 4)

            # Verify login succeeded — PLACEHOLDER SELECTOR
            if self.page.url != BASE_URL or self.page.query_selector('.logout, .my-account, .dashboard'):
                logger.info("Login successful")
                return True
            else:
                logger.error("Login may have failed — no post-login indicator found")
                return False

        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False

    def navigate_to_breakdowns(self, region: str) -> bool:
        """Navigate to the Breakdowns page and select a region."""
        logger.info(f"Navigating to breakdowns for region: {region}")
        try:
            # Click Breakdowns menu — PLACEHOLDER SELECTOR
            self.page.click('a:has-text("Breakdowns"), [href*="projects"]')
            _random_delay(1, 3)

            # Select region if specified — PLACEHOLDER SELECTOR
            if region:
                self.page.click(f'a:has-text("{region}"), option:has-text("{region}")')
                _random_delay(2, 4)

            logger.info("Breakdowns page loaded")
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to breakdowns: {e}")
            return False

    def apply_filters(self, filters: dict) -> bool:
        """Apply search filters on the breakdowns page."""
        logger.info(f"Applying filters: {filters}")
        try:
            # All selectors below are PLACEHOLDERS — update after manual inspection

            gender = filters.get("gender", "any")
            if gender != "any":
                self.page.select_option('select[name="gender"]', label=gender)
                _random_delay(0.5, 1)

            age_range = filters.get("age_range")
            if age_range:
                self.page.fill('input[name="age_min"]', str(age_range[0]))
                self.page.fill('input[name="age_max"]', str(age_range[1]))
                _random_delay(0.5, 1)

            union = filters.get("union_status", "any")
            if union != "any":
                self.page.select_option('select[name="union"]', label=union)
                _random_delay(0.5, 1)

            if filters.get("paying_only"):
                self.page.check('input[name="paying_only"], input#paying_only')
                _random_delay(0.3, 0.8)

            if filters.get("exclude_reality_tv"):
                self.page.check('input[name="exclude_reality"], input#exclude_reality')
                _random_delay(0.3, 0.8)

            # Click search/apply button — PLACEHOLDER SELECTOR
            self.page.click('button:has-text("Search"), input[value="Search"]')
            _random_delay(2, 5)

            logger.info("Filters applied")
            return True
        except Exception as e:
            logger.error(f"Failed to apply filters: {e}")
            return False

    def scrape_roles(self) -> list[dict]:
        """Scrape all visible roles from the current breakdowns page.

        Returns a list of dicts with keys: role_id, project_name, role_name, element.
        The 'element' key holds a Playwright ElementHandle for clicking into the role.
        """
        roles = []
        try:
            # PLACEHOLDER SELECTORS — must be updated after inspecting site HTML
            project_elements = self.page.query_selector_all('.project-listing, .breakdown-item, tr.project')

            for project_el in project_elements:
                project_name = project_el.query_selector('.project-title, .title, td.title')
                if not project_name:
                    continue
                project_name_text = project_name.inner_text().strip()

                # Each project may have multiple roles
                role_elements = project_el.query_selector_all('.role, .character, tr.role')
                for role_el in role_elements:
                    role_name_el = role_el.query_selector('.role-name, .character-name, td.role')
                    if not role_name_el:
                        continue

                    role_name_text = role_name_el.inner_text().strip()
                    # Generate a unique-ish ID from project + role name
                    role_id = f"{project_name_text}::{role_name_text}".lower().replace(" ", "_")

                    roles.append({
                        "role_id": role_id,
                        "project_name": project_name_text,
                        "role_name": role_name_text,
                        "element": role_el,
                    })

            logger.info(f"Found {len(roles)} roles on page")
        except Exception as e:
            logger.error(f"Failed to scrape roles: {e}")

        return roles

    def submit_for_role(self, role: dict, submission_config: dict) -> bool:
        """Click into a role and submit application.

        Args:
            role: Dict from scrape_roles() with role_id, project_name, role_name, element
            submission_config: Dict with headshot_index, include_media, include_size_card, default_note

        Returns True on successful submission, False on failure.
        """
        try:
            logger.info(f"Submitting for: {role['project_name']} — {role['role_name']}")

            # Click the role to open submission dialog — PLACEHOLDER
            role["element"].click()
            _random_delay(2, 4)

            # Select headshot by index — PLACEHOLDER SELECTOR
            headshot_index = submission_config.get("headshot_index", 0)
            headshots = self.page.query_selector_all('.headshot-option, .photo-select img')
            if headshots and headshot_index < len(headshots):
                headshots[headshot_index].click()
                _random_delay(0.5, 1)

            # Include media if configured — PLACEHOLDER SELECTOR
            if submission_config.get("include_media"):
                media_checkbox = self.page.query_selector('input[name="include_media"], .media-checkbox')
                if media_checkbox and not media_checkbox.is_checked():
                    media_checkbox.check()
                    _random_delay(0.3, 0.8)

            # Include size card if configured — PLACEHOLDER SELECTOR
            if submission_config.get("include_size_card"):
                size_card = self.page.query_selector('input[name="size_card"], .size-card-checkbox')
                if size_card and not size_card.is_checked():
                    size_card.check()
                    _random_delay(0.3, 0.8)

            # Add note if configured — PLACEHOLDER SELECTOR
            note = submission_config.get("default_note", "")
            if note:
                note_field = self.page.query_selector('textarea[name="note"], .casting-note textarea')
                if note_field:
                    note_field.fill(note)
                    _random_delay(0.5, 1)

            # Click submit — PLACEHOLDER SELECTOR
            self.page.click('button:has-text("Submit"), input[value="Submit"], .submit-btn')
            _random_delay(2, 4)

            logger.info(f"Submitted for: {role['role_name']}")
            return True

        except Exception as e:
            logger.error(f"Failed to submit for {role['role_name']}: {e}")
            return False
