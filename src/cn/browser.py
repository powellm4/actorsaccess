# src/cn/browser.py
import logging
import random
import re
import time
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

BASE_URL = "https://app.castingnetworks.com"
LOGIN_URL = f"{BASE_URL}/login/"


def _random_delay(min_sec: float = 2.0, max_sec: float = 8.0):
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"Waiting {delay:.1f}s")
    time.sleep(delay)


class CastingNetworksBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self.page = self.context.new_page()
        self.page.add_init_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined})')
        self.page.set_default_timeout(30000)
        logger.info("Browser started")

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Browser closed")

    def login(self, email: str, password: str) -> bool:
        try:
            logger.info("Navigating to Casting Networks login")
            self.page.goto(LOGIN_URL)
            _random_delay(3, 5)

            self.page.fill("input#email", email)
            _random_delay(0.5, 1.5)
            self.page.fill("input#password", password)
            _random_delay(0.5, 1.5)

            self.page.click('button[type="submit"]')
            _random_delay(5, 8)

            body_text = self.page.inner_text("body")
            if "Casting Billboard" in body_text:
                logger.info("Login successful")
                return True

            logger.error("Login may have failed — no post-login indicator found")
            return False
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False

    def scrape_roles(self) -> list[dict]:
        """Scrape all role cards from the current billboard page.

        Returns list of dicts with keys: project_id, role_id, role_name,
        project_name, project_type, role_type, age_range, gender, pay,
        union, description, url.
        """
        roles = []
        try:
            role_links = self.page.query_selector_all(
                '[data-qa-id="casting-billboard-role-name-link"]'
            )
            logger.info(f"Found {len(role_links)} role links on page")

            for link in role_links:
                href = link.get_attribute("href") or ""
                role_name = link.inner_text().strip()

                match = re.search(r"/project/(\d+)/role/(\d+)", href)
                if not match:
                    continue
                project_id = match.group(1)
                role_id = match.group(2)

                card_data = self.page.evaluate('''(linkEl) => {
                    const get = (container, qaId) => {
                        const el = container.querySelector('[data-qa-id="' + qaId + '"]');
                        return el ? el.innerText.trim() : "";
                    };

                    // Walk up to find the role-level container
                    // (contains this role's description, type, age, gender)
                    let roleCard = linkEl;
                    for (let i = 0; i < 15; i++) {
                        if (!roleCard.parentElement) break;
                        roleCard = roleCard.parentElement;
                        if (roleCard.querySelector('[data-qa-id="casting-billboard-role-description"]')) break;
                    }

                    // Walk up further to find the project-level container
                    let projectCard = roleCard;
                    for (let i = 0; i < 15; i++) {
                        if (!projectCard.parentElement) break;
                        projectCard = projectCard.parentElement;
                        if (projectCard.querySelector('[data-qa-id="casting-billboard-project-name-link"]')) break;
                    }

                    return {
                        projectName: get(projectCard, "casting-billboard-project-name-link"),
                        projectType: get(projectCard, "casting-billboard-project-type"),
                        roleType: get(roleCard, "casting-billboard-role-header-type-name"),
                        ageRange: get(roleCard, "casting-billboard-role-header-age-range"),
                        gender: get(roleCard, "casting-billboard-role-header-gender-appearance"),
                        pay: get(roleCard, "casting-billboard-role-header-rate-summary"),
                        union: get(roleCard, "casting-billboard-role-header-union") || get(projectCard, "casting-billboard-project-union"),
                        description: get(roleCard, "casting-billboard-role-description"),
                        submissionDate: get(roleCard, "casting-billboard-role-header-submission-due-date"),
                    };
                }''', link)

                roles.append({
                    "project_id": project_id,
                    "role_id": role_id,
                    "role_name": role_name,
                    "project_name": card_data.get("projectName", ""),
                    "project_type": card_data.get("projectType", ""),
                    "role_type": card_data.get("roleType", ""),
                    "age_range": card_data.get("ageRange", ""),
                    "gender": card_data.get("gender", ""),
                    "pay": card_data.get("pay", ""),
                    "union": card_data.get("union", ""),
                    "description": card_data.get("description", ""),
                    "submission_date": card_data.get("submissionDate", ""),
                    "url": href,
                })

            logger.info(f"Scraped {len(roles)} roles from page")
        except Exception as e:
            logger.error(f"Failed to scrape roles: {e}")

        return roles

    def navigate_to_billboard(self) -> bool:
        """Navigate back to the Casting Billboard page."""
        try:
            self.page.goto(f"{BASE_URL}/talent/casting-billboard")
            _random_delay(3, 5)
            self.page.wait_for_selector(
                '[data-qa-id="casting-billboard-role-name-link"]',
                timeout=30000,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to billboard: {e}")
            return False

    def get_total_pages(self) -> int:
        """Get total pages from the pagination select dropdown."""
        try:
            # Pagination is a <select> with <option> elements like "Page 1 of 200"
            options = self.page.query_selector_all('option')
            for opt in reversed(options):
                text = opt.inner_text().strip()
                match = re.search(r"Page \d+ of (\d+)", text)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        return 1

    def go_to_page(self, page_num: int) -> bool:
        """Navigate to a page by selecting from the pagination dropdown."""
        try:
            # Find the select that contains "Page X of Y" options
            selects = self.page.query_selector_all("select")
            for select in selects:
                options = select.query_selector_all("option")
                for opt in options:
                    text = opt.inner_text().strip()
                    if text.startswith(f"Page {page_num} of"):
                        value = opt.get_attribute("value") or ""
                        # Grab a role name before pagination to detect change
                        old_first = self.page.query_selector(
                            '[data-qa-id="casting-billboard-role-name-link"]'
                        )
                        old_text = old_first.inner_text() if old_first else ""
                        select.select_option(value=value)
                        _random_delay(3, 5)
                        # Wait for roles to change (new page loaded)
                        for _ in range(10):
                            new_first = self.page.query_selector(
                                '[data-qa-id="casting-billboard-role-name-link"]'
                            )
                            new_text = new_first.inner_text() if new_first else ""
                            if new_text and new_text != old_text:
                                break
                            time.sleep(1)
                        logger.info(f"Navigated to page {page_num}")
                        return True
            logger.error(f"Page {page_num} option not found")
            return False
        except Exception as e:
            logger.error(f"Failed to go to page {page_num}: {e}")
            return False

    def submit_for_role(self, role: dict, submission_config: dict) -> bool:
        """Navigate to role detail, then submit.

        Flow: role detail page -> click Submit -> customize-submission page
        -> photos pre-selected -> optional note -> click Submit.
        """
        try:
            url = role["url"]
            if not url.startswith("http"):
                url = BASE_URL + url

            logger.info(f"Submitting for: {role['project_name']} — {role['role_name']}")

            self.page.goto(url)
            _random_delay(2, 4)

            submit_link = self.page.query_selector('[data-qa-id="casting-billboard-submit-role"]')
            if not submit_link:
                logger.error("Submit link not found on role detail page")
                return False

            # Check if already submitted
            submit_text = submit_link.inner_text().strip()
            if submit_text == "Submitted":
                logger.info(f"Already submitted for: {role['role_name']}")
                return False

            submit_link.click()
            _random_delay(3, 5)

            # Fill phone number (required field that blocks submit)
            phone = submission_config.get("phone", "3104567890")
            country_select = self.page.query_selector("select")
            if country_select:
                self.page.select_option("select", value="US")
                _random_delay(0.3, 0.8)
            phone_input = self.page.query_selector("input#phone")
            if phone_input:
                phone_input.fill(phone)
                _random_delay(0.3, 0.8)

            # Fill note if configured
            note = submission_config.get("default_note", "")
            if note:
                note_field = self.page.query_selector("textarea#submissionNote")
                if note_field:
                    note_field.fill(note)
                    _random_delay(0.5, 1)

            # Click somewhere neutral to trigger validation
            self.page.click("textarea#submissionNote")
            _random_delay(1, 2)

            # Click the final Submit button (last one on page)
            submit_buttons = self.page.query_selector_all('button:has-text("Submit")')
            final_submit = None
            for btn in submit_buttons:
                text = btn.inner_text().strip()
                if text == "Submit":
                    final_submit = btn

            if not final_submit:
                logger.error("Final submit button not found")
                return False

            if final_submit.evaluate("el => el.disabled"):
                logger.error("Submit button is disabled — missing required field")
                return False

            final_submit.click()
            _random_delay(2, 4)

            # Handle confirmation dialog: "Are you sure you're ready to send?"
            confirm_btn = self.page.query_selector('button:has-text("Yes, Send Response")')
            if confirm_btn:
                confirm_btn.click()
                _random_delay(3, 5)

            logger.info(f"Submitted for: {role['role_name']}")
            return True

        except Exception as e:
            logger.error(f"Failed to submit for {role['role_name']}: {e}")
            return False
