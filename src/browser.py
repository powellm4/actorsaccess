# src/browser.py
import logging
import random
import time
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

logger = logging.getLogger(__name__)

BASE_URL = "https://actorsaccess.com"
BREAKDOWNS_URL = "https://actorsaccess.com/projects/"

# Region name -> value mapping for the region dropdown
REGIONS = {
    "Los Angeles": "5",
    "New York": "32",
    "Chicago": "4",
    "San Francisco / NorCal": "25",
    "Central Atlantic": "12",
    "Midwest": "19",
    "New England": "20",
    "North Central": "21",
    "Northwest": "11",
    "Pacific": "8",
    "Rocky Mountains": "10",
    "South Central": "7",
    "Southeast": "9",
    "Vancouver": "27",
    "Toronto": "26",
}

# Filter dropdown value mapping
FILTER_OPTIONS = {
    "all": "all breakdowns",
    "union": "union breakdowns",
    "non-union": "nonunion breakdowns",
    "fit_for_me": "breakdowns fit for me",
    "union_fit": "union breakdowns fit for me",
    "nonunion_fit": "nonunion breakdowns fit for me",
}


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
            logger.info("Navigating to Actors Access")
            self.page.goto(BASE_URL)
            _random_delay(1, 3)

            # The login form is hidden — click the "Login" link to reveal it
            self.page.click('a:has-text("Login")')
            _random_delay(1, 2)

            # Fill login form (uses username, NOT email)
            self.page.fill('input[name="username"]', username)
            _random_delay(0.5, 1.5)
            self.page.fill('input[name="password"]', password)
            _random_delay(0.5, 1.5)

            # Click the LOGIN button
            self.page.click('button[type="submit"]')
            _random_delay(3, 5)

            # Verify login succeeded — look for "Sign out" link on the page
            body_text = self.page.inner_text("body")
            if "Sign out" in body_text or "BREAKDOWNS" in body_text:
                logger.info("Login successful")
                return True

            # Check for error dialog
            if "Incorrect Username or Password" in body_text:
                logger.error("Login failed: incorrect username or password")
                return False

            logger.error("Login may have failed — no post-login indicator found")
            return False

        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False

    def navigate_to_breakdowns(self, region: str) -> bool:
        """Navigate to the Breakdowns page and select a region."""
        logger.info(f"Navigating to breakdowns for region: {region}")
        try:
            self.page.goto(BREAKDOWNS_URL)
            _random_delay(2, 4)

            # Select region using the dropdown (form auto-submits on change)
            if region:
                region_value = REGIONS.get(region, "5")
                self.page.select_option(
                    'form#changeRegion select[name="region"]',
                    value=region_value,
                )
                _random_delay(1, 2)
                # The region form auto-submits via onchange
                self.page.wait_for_load_state("networkidle")
                _random_delay(2, 4)

            logger.info("Breakdowns page loaded")
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to breakdowns: {e}")
            return False

    def apply_filters(self, filters: dict) -> bool:
        """Apply search filters on the breakdowns page.

        Available filters on the site:
        - filter dropdown: All/Union/Non-Union/Fit For Me variants
        - checkboxes: background_role, paying_role, kids_only, exclude_realitytv, exclude_real_people
        - search_by/search_for: text search
        """
        logger.info(f"Applying filters")
        try:
            # Set the filter dropdown (union status / fit for me)
            union = filters.get("union_status", "any")
            filter_value = FILTER_OPTIONS.get(union, "all breakdowns")
            self.page.select_option('select[name="filter"]', value=filter_value)
            _random_delay(0.5, 1)

            # Paying roles only checkbox
            if filters.get("paying_only"):
                checkbox = self.page.query_selector('input#paying_role')
                if checkbox and not checkbox.is_checked():
                    checkbox.check()
                    _random_delay(0.3, 0.8)

            # Exclude reality TV checkbox
            if filters.get("exclude_reality_tv"):
                checkbox = self.page.query_selector('input#exclude_realitytv')
                if checkbox and not checkbox.is_checked():
                    checkbox.check()
                    _random_delay(0.3, 0.8)

            # Click the Search button (it's an input[type="button"] with onclick)
            self.page.click('input[value="Search"]')
            _random_delay(2, 5)
            self.page.wait_for_load_state("networkidle")

            logger.info("Filters applied")
            return True
        except Exception as e:
            logger.error(f"Failed to apply filters: {e}")
            return False

    def get_total_pages(self) -> int:
        """Get the total number of pages of breakdowns."""
        try:
            page_select = self.page.query_selector('select[name="page"]')
            if page_select:
                options = page_select.query_selector_all("option")
                return len(options)
        except Exception:
            pass
        return 1

    def go_to_page(self, page_num: int) -> bool:
        """Navigate to a specific page of breakdowns."""
        try:
            self.page.select_option('select[name="page"]', value=str(page_num))
            _random_delay(1, 2)
            self.page.wait_for_load_state("networkidle")
            _random_delay(2, 4)
            return True
        except Exception as e:
            logger.error(f"Failed to go to page {page_num}: {e}")
            return False

    def scrape_projects(self) -> list[dict]:
        """Scrape all project listings from the current breakdowns page.

        Returns a list of dicts with keys: breakdown_id, project_name, url, already_submitted.
        """
        projects = []
        try:
            # Projects are in table.bd_list, each row is tr.element
            rows = self.page.query_selector_all("table.bd_list tr.element")

            for row in rows:
                # Check if already submitted (has checkmark image)
                submitted_cell = row.query_selector("td.submitted")
                already_submitted = False
                if submitted_cell:
                    check_img = submitted_cell.query_selector(
                        'img[alt="Submitted On Project"]'
                    )
                    already_submitted = check_img is not None

                # Get project title and link
                title_cell = row.query_selector("td.bd_title a")
                if not title_cell:
                    continue

                project_name = title_cell.inner_text().strip()
                href = title_cell.get_attribute("href") or ""

                # Extract breakdown ID from URL like /projects/?view=breakdowns&breakdown=885850&region=5
                breakdown_id = ""
                if "breakdown=" in href:
                    breakdown_id = href.split("breakdown=")[1].split("&")[0]

                # Get project type from the row (e.g., "Student Film", "Theater", "TV")
                type_cell = row.query_selector("td.bd_type")
                project_type = type_cell.inner_text().strip() if type_cell else ""

                projects.append(
                    {
                        "breakdown_id": breakdown_id,
                        "project_name": project_name,
                        "project_type": project_type,
                        "url": href,
                        "already_submitted": already_submitted,
                    }
                )

            logger.info(
                f"Found {len(projects)} projects on page "
                f"({sum(1 for p in projects if p['already_submitted'])} already submitted)"
            )
        except Exception as e:
            logger.error(f"Failed to scrape projects: {e}")

        return projects

    def scrape_roles_on_project(self, project_url: str) -> tuple[list[dict], str]:
        """Navigate to a project page and scrape individual roles.

        Returns a tuple of:
        - List of dicts with keys: role_id, role_name, element,
          fit_for_me, description.
        - Project-level notes/instructions (text before the first role).
        """
        roles = []
        project_notes = ""
        try:
            # Navigate to the project breakdown page
            if not project_url.startswith("http"):
                project_url = BASE_URL + project_url
            self.page.goto(project_url)
            _random_delay(2, 4)

            # Scrape project-level notes (text between header and first role)
            project_notes = self.page.evaluate("""() => {
                const firstRole = document.querySelector('a.breakdown-open-add-role');
                if (!firstRole) return '';
                // Find the container: try known classes, fall back to body
                const body = document.querySelector('.breakdown-body, .breakdownBody, #breakdown_body')
                    || firstRole.parentElement;
                if (!body) return '';
                let text = '';
                const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
                while (walker.nextNode()) {
                    const node = walker.currentNode;
                    if (node === firstRole || firstRole.contains(node)) break;
                    if (node.closest && node.closest('a.breakdown-open-add-role')) break;
                    if (node.nodeType === Node.TEXT_NODE) {
                        text += node.textContent;
                    }
                }
                return text.trim();
            }""") or ""

            # Roles are links with class 'breakdown-open-add-role'
            # onclick="selectPhoto(5273126,885850,this);"
            role_links = self.page.query_selector_all("a.breakdown-open-add-role")

            for link in role_links:
                role_name = link.inner_text().strip()
                name_attr = link.get_attribute("name") or ""
                # name is like "role_5273126" — extract the ID
                role_id = name_attr.replace("role_", "") if name_attr else ""

                # Check if the role is highlighted as "fit for me"
                # The site wraps fitting roles in <div class="role_fit_for_me">
                fit_for_me = self.page.evaluate(
                    """(el) => {
                        const parent = el.closest('.role_fit_for_me');
                        return parent !== null;
                    }""",
                    link,
                )

                # Grab the description text following the link
                desc_text = self.page.evaluate(
                    """(el) => {
                        let text = '';
                        let node = el.nextSibling;
                        while (node) {
                            if (node.nodeType === Node.TEXT_NODE) {
                                text += node.textContent;
                            } else if (node.nodeType === Node.ELEMENT_NODE) {
                                if (node.tagName === 'A' || node.tagName === 'HR'
                                    || node.tagName === 'DIV') break;
                                text += node.textContent;
                            }
                            node = node.nextSibling;
                        }
                        return text.trim();
                    }""",
                    link,
                )

                roles.append(
                    {
                        "role_id": role_id,
                        "role_name": role_name,
                        "element": link,
                        "fit_for_me": fit_for_me,
                        "description": desc_text,
                    }
                )

            logger.info(f"Found {len(roles)} roles on project page")
            if project_notes:
                logger.info(f"Project notes: {project_notes[:200]}")
        except Exception as e:
            logger.error(f"Failed to scrape roles: {e}")

        return roles, project_notes

    def submit_for_role(self, role: dict, project_name: str, submission_config: dict) -> bool | str:
        """Click a role link to open the submission modal and submit.

        The submission modal contains an iframe with:
        - Step 1: Photo selection (radio buttons input[name="photo_to_use"])
        - Step 2: Media selection ("Select All" checkbox to include videos)
        - Step 3: Size card checkbox (input#include_sc_checkbox_id)
        - Step 4: Note textarea (textarea#roleSubmissionNotes)
        - Submit button: a#add_to_cart with onclick="submitForm();"

        Returns:
            True if submitted successfully.
            False if submission failed.
            A string starting with "NEEDS_SELFTAPE:" if a self-tape is requested.
        """
        try:
            logger.info(f"Submitting for: {project_name} — {role['role_name']}")

            # Click the role link to open the submission modal
            role["element"].click()
            _random_delay(2, 4)

            # Wait for the modal iframe to load
            iframe_el = self.page.wait_for_selector("#roleSubmissionIframe", timeout=10000)
            if not iframe_el:
                logger.error("Submission iframe not found")
                return False

            frame = iframe_el.content_frame()
            if not frame:
                logger.error("Could not access iframe content")
                return False

            _random_delay(1, 3)

            # Check media instructions for self-tape requests
            media_instructions = frame.evaluate('''() => {
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    const text = el.innerText || '';
                    if (text.toLowerCase().includes('media instruction')) return text;
                }
                return '';
            }''')
            if media_instructions:
                mi_lower = media_instructions.lower()
                selftape_keywords = [
                    "self-tape", "self tape", "selftape",
                    "reading of the sides", "audition video",
                    "video audition", "record a",
                    "film yourself", "tape yourself",
                    "submit a video", "video of you",
                ]
                if any(kw in mi_lower for kw in selftape_keywords):
                    reason = media_instructions.strip()[:300]
                    logger.info(f"Self-tape requested for {role['role_name']}, skipping submission")
                    self._close_modal()
                    return f"NEEDS_SELFTAPE: {reason}"

            # Step 1: Select headshot (radio button by index)
            headshot_index = submission_config.get("headshot_index", 0)
            photo_radios = frame.query_selector_all('input[name="photo_to_use"]')
            if photo_radios and headshot_index < len(photo_radios):
                photo_radios[headshot_index].check()
                _random_delay(0.5, 1)
            elif photo_radios:
                # Default to first photo
                photo_radios[0].check()
                _random_delay(0.5, 1)

            # Step 2: Select all media (videos/self-tapes)
            checkboxes = frame.query_selector_all('input[type="checkbox"]')
            for cb in checkboxes:
                label = cb.evaluate('el => { const lbl = el.closest("label") || el.parentElement; return lbl ? lbl.innerText.trim() : ""; }')
                if "select all" in label.lower():
                    if not cb.is_checked():
                        cb.check()
                        _random_delay(0.5, 1)
                    logger.info("Selected all media")
                    break

            # Step 3: Size card checkbox
            if submission_config.get("include_size_card"):
                sc_checkbox = frame.query_selector("input#include_sc_checkbox_id")
                if sc_checkbox and not sc_checkbox.is_checked():
                    sc_checkbox.check()
                    _random_delay(0.3, 0.8)

            # Step 4: Note for casting
            note = submission_config.get("default_note", "")
            if note:
                note_field = frame.query_selector("textarea#roleSubmissionNotes")
                if note_field:
                    note_field.fill(note)
                    _random_delay(0.5, 1)

            # Click Submit button (inside the iframe)
            submit_btn = frame.query_selector("a#add_to_cart")
            if not submit_btn:
                logger.error("Submit button not found in iframe")
                self._close_modal()
                return False

            submit_btn.click()
            _random_delay(3, 5)

            logger.info(f"Submitted for: {role['role_name']}")
            return True

        except Exception as e:
            logger.error(f"Failed to submit for {role['role_name']}: {e}")
            self._close_modal()
            return False

    def _close_modal(self):
        """Close the submission modal if it's open."""
        try:
            close_btn = self.page.query_selector(
                "#roleSubmissionModal .close-medium-icon"
            )
            if close_btn:
                close_btn.click()
                _random_delay(0.5, 1)
        except Exception:
            pass
