# Daily Digest & Multi-Role Submission Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daily email digest of AI role decisions and allow submitting for up to 2 roles per project.

**Architecture:** Extend the SQLite database with a `rejected_roles` table and `project_url` column. Modify `role_selector.py` to return multiple selections with structured SELECTED/REJECTED parsing. Add `src/digest.py` module to query the DB and send HTML email via SendGrid. New GitHub Actions workflow triggers daily at 8pm PT.

**Tech Stack:** Python 3.12, SQLite, SendGrid (`sendgrid` PyPI package), GitHub Actions

**Spec:** `docs/superpowers/specs/2026-03-12-daily-digest-multi-role-design.md`

---

## Chunk 1: Database & Role Selector Changes

### Task 1: Add `rejected_roles` table and `project_url` column to database

**Files:**
- Modify: `src/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing tests for new DB schema and methods**

Add to `tests/test_database.py`:

```python
def test_rejected_roles_table_created(db):
    """DB should create rejected_roles table on init."""
    cursor = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "rejected_roles" in tables


def test_record_rejection(db):
    run_id = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://actorsaccess.com/projects/?breakdown=123",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age range too high",
        run_id=run_id,
        platform="aa",
    )
    cursor = db.conn.execute(
        "SELECT project_name, role_name, rejection_reason, platform FROM rejected_roles"
    )
    row = cursor.fetchone()
    assert row[0] == "Test Project"
    assert row[1] == "Villain"
    assert row[2] == "Age range too high"
    assert row[3] == "aa"


def test_record_rejection_upserts(db):
    """Second rejection for same role/project/platform should update reason."""
    run_id = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age range too high",
        run_id=run_id,
        platform="aa",
    )
    run_id2 = db.start_run()
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Not a leading man type",
        run_id=run_id2,
        platform="aa",
    )
    cursor = db.conn.execute("SELECT COUNT(*) FROM rejected_roles")
    assert cursor.fetchone()[0] == 1
    cursor = db.conn.execute("SELECT rejection_reason FROM rejected_roles")
    assert cursor.fetchone()[0] == "Not a leading man type"


def test_record_application_with_project_url(db):
    db.record_application(
        "role_url", "URL Project", "Lead",
        project_url="https://actorsaccess.com/projects/?breakdown=456",
    )
    cursor = db.conn.execute(
        "SELECT project_url FROM applied_roles WHERE role_id = ?", ("role_url",)
    )
    assert cursor.fetchone()[0] == "https://actorsaccess.com/projects/?breakdown=456"


def test_record_application_project_url_defaults_empty(db):
    db.record_application("role_nourl", "No URL Project", "Lead")
    cursor = db.conn.execute(
        "SELECT project_url FROM applied_roles WHERE role_id = ?", ("role_nourl",)
    )
    assert cursor.fetchone()[0] == ""


def test_get_daily_applications(db):
    """get_daily_applications should return today's applications."""
    db.record_application(
        "role_daily", "Daily Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    rows = db.get_daily_applications()
    assert len(rows) >= 1
    assert rows[0]["project_name"] == "Daily Project"


def test_get_daily_rejections(db):
    """get_daily_rejections should return today's rejections."""
    run_id = db.start_run()
    db.record_rejection(
        project_name="Daily Project",
        project_url="https://example.com",
        role_name="Side Character",
        role_description="A friend",
        rejection_reason="Not leading man",
        run_id=run_id,
        platform="aa",
    )
    rows = db.get_daily_rejections()
    assert len(rows) >= 1
    assert rows[0]["role_name"] == "Side Character"


def test_get_daily_run_summary(db):
    """get_daily_run_summary should return today's run stats."""
    run_id = db.start_run()
    db.complete_run(run_id, roles_found=10, roles_applied=3, roles_skipped=7)
    summary = db.get_daily_run_summary()
    assert len(summary) >= 1
    assert summary[0]["roles_applied"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_database.py -v`
Expected: Multiple FAILs — `record_rejection` not defined, `project_url` column missing, `get_daily_*` methods not defined.

- [ ] **Step 3: Implement database changes**

In `src/database.py`, add to `_create_tables`:

```python
CREATE TABLE IF NOT EXISTS rejected_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT,
    project_url TEXT DEFAULT '',
    role_name TEXT,
    role_description TEXT,
    rejection_reason TEXT,
    run_id INTEGER,
    platform TEXT DEFAULT 'aa',
    rejected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(role_name, project_name, platform)
);
```

Add migration for `project_url` on `applied_roles` (same pattern as existing migrations):

```python
try:
    self.conn.execute("ALTER TABLE applied_roles ADD COLUMN project_url TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass
```

Add `project_url` parameter to `record_application`:

```python
def record_application(
    self, role_id: str, project_name: str, role_name: str,
    role_description: str = "", ai_reason: str = "", candidates_considered: int = 1,
    platform: str = "aa", project_url: str = "",
):
    self.conn.execute(
        """INSERT OR IGNORE INTO applied_roles
           (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (role_id, project_name, role_name, role_description, ai_reason, candidates_considered, platform, project_url),
    )
    self.conn.commit()
```

Add `record_rejection` method:

```python
def record_rejection(
    self, project_name: str, project_url: str, role_name: str,
    role_description: str, rejection_reason: str, run_id: int, platform: str = "aa",
):
    self.conn.execute(
        """INSERT INTO rejected_roles
           (project_name, project_url, role_name, role_description, rejection_reason, run_id, platform)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(role_name, project_name, platform) DO UPDATE SET
               rejection_reason = excluded.rejection_reason,
               run_id = excluded.run_id,
               rejected_at = CURRENT_TIMESTAMP,
               project_url = excluded.project_url""",
        (project_name, project_url, role_name, role_description, rejection_reason, run_id, platform),
    )
    self.conn.commit()
```

Add digest query methods:

```python
def get_daily_applications(self) -> list[dict]:
    cursor = self.conn.execute(
        """SELECT project_name, role_name, role_description, ai_reason,
                  candidates_considered, platform, project_url, applied_at
           FROM applied_roles
           WHERE applied_at >= datetime('now', '-24 hours')
           ORDER BY applied_at DESC"""
    )
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def get_daily_rejections(self) -> list[dict]:
    cursor = self.conn.execute(
        """SELECT project_name, role_name, role_description, rejection_reason,
                  platform, project_url, rejected_at
           FROM rejected_roles
           WHERE rejected_at >= datetime('now', '-24 hours')
           ORDER BY rejected_at DESC"""
    )
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def get_daily_run_summary(self) -> list[dict]:
    cursor = self.conn.execute(
        """SELECT platform, status, roles_found, roles_applied, roles_skipped,
                  error_message, started_at, completed_at
           FROM run_history
           WHERE started_at >= datetime('now', '-24 hours')
           ORDER BY started_at DESC"""
    )
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_database.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat: add rejected_roles table, project_url column, and digest query methods"
```

---

### Task 2: Update `select_best_role` → `select_best_roles` with structured parsing

**Files:**
- Modify: `src/role_selector.py`
- Create: `tests/test_role_selector.py`

- [ ] **Step 1: Write failing tests for new `select_best_roles`**

Create `tests/test_role_selector.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_role_selector.py -v`
Expected: FAIL — `select_best_roles` not found (only `select_best_role` exists).

- [ ] **Step 3: Implement `select_best_roles`**

Replace the `select_best_role` function in `src/role_selector.py` with:

```python
def select_best_roles(roles: list[dict], project_name: str) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Pick the best role(s) from a list of matching roles.

    If only one role, returns it directly (no API call).
    If multiple roles, uses Claude Haiku to select 1-2 best fits.
    Falls back to the first role if the API call fails.

    Args:
        roles: List of role dicts that already passed filters.
        project_name: Name of the project (for context).

    Returns:
        Tuple of:
        - List of (role dict, reason string) for selected roles
        - Dict mapping rejected role names to rejection reasons
    """
    if len(roles) == 1:
        return [(roles[0], "only matching role")], {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set — defaulting to first role")
        return [(roles[0], "no API key, defaulted to first")], {}

    try:
        import anthropic  # noqa: E402 — imported here to stay optional

        client = anthropic.Anthropic(api_key=api_key)

        # Build role summaries for the prompt
        role_options = []
        for i, role in enumerate(roles):
            meta = []
            if role.get("role_type"):
                meta.append(role["role_type"])
            if role.get("age_range"):
                meta.append(f"Age: {role['age_range']}")
            if role.get("gender"):
                meta.append(role["gender"])
            if role.get("pay"):
                meta.append(role["pay"])
            meta_str = f" ({', '.join(meta)})" if meta else ""
            role_options.append(
                f"{i + 1}. {role['role_name']}{meta_str}: {role.get('description', 'No description')[:500]}"
            )

        prompt = f"""You are a casting assistant helping an actor decide which role(s) to submit for on a project.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}

AVAILABLE ROLES:
{chr(10).join(role_options)}

Pick the ONE role that is the best fit for this actor. If a second role is also a genuinely strong fit, return it too — but only if both are truly well-matched. Don't force a second pick.

Consider:
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead > supporting > day player)
4. Overall castability — would this actor realistically be considered?

If NONE of the roles are a good fit, respond with SKIP on the first line and explain why.

Respond with this exact format (one line per role):
SELECTED: <number> - <reason for picking this role>
REJECTED: <number> - <reason for rejecting this role>

Example for selecting one role out of three:
SELECTED: 2 - Best physical and type match for leading man
REJECTED: 1 - Age range 40-50 is too old
REJECTED: 3 - Background role, not a fit"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        return _parse_structured_response(text, roles, project_name)

    except Exception as e:
        logger.warning(f"AI role selection failed ({e}), defaulting to first role")
        rejections = {r["role_name"]: f"AI failed ({e}), defaulted to first" for r in roles[1:]}
        return [(roles[0], f"AI failed ({e}), defaulted to first")], rejections


def _parse_structured_response(
    text: str, roles: list[dict], project_name: str,
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Parse the structured SELECTED/REJECTED response from the AI."""
    lines = text.strip().split("\n")

    # Check for SKIP
    if lines[0].strip().upper().startswith("SKIP"):
        skip_reason = lines[0].split("-", 1)[1].strip() if "-" in lines[0] else lines[0].strip()
        rejections = {r["role_name"]: skip_reason for r in roles}
        logger.info(f"AI skipped project {project_name}: {skip_reason}")
        return [], rejections

    selected = []
    rejections = {}
    selected_re = re.compile(r"SELECTED:\s*(\d+)\s*-\s*(.*)", re.IGNORECASE)
    rejected_re = re.compile(r"REJECTED:\s*(\d+)\s*-\s*(.*)", re.IGNORECASE)

    for line in lines:
        m = selected_re.match(line.strip())
        if m:
            idx = int(m.group(1)) - 1  # Convert to 0-indexed
            reason = m.group(2).strip()
            if 0 <= idx < len(roles):
                selected.append((roles[idx], reason))
            continue
        m = rejected_re.match(line.strip())
        if m:
            idx = int(m.group(1)) - 1
            reason = m.group(2).strip()
            if 0 <= idx < len(roles):
                rejections[roles[idx]["role_name"]] = reason

    # Fallback: no SELECTED lines found
    if not selected:
        logger.warning(f"AI returned unparseable response for {project_name}: {text[:100]}")
        fallback_reason = "AI returned unparseable response, defaulted to first"
        rejections = {r["role_name"]: fallback_reason for r in roles[1:]}
        return [(roles[0], fallback_reason)], rejections

    # Fill in any roles not mentioned in rejections
    selected_names = {s[0]["role_name"] for s in selected}
    for role in roles:
        if role["role_name"] not in selected_names and role["role_name"] not in rejections:
            rejections[role["role_name"]] = "not mentioned by AI"

    # Cap at 2 selections per spec
    if len(selected) > 2:
        for extra_role, extra_reason in selected[2:]:
            rejections[extra_role["role_name"]] = f"Capped at 2 selections (was: {extra_reason})"
        selected = selected[:2]

    logger.info(f"AI selected {len(selected)} role(s) for {project_name}: {[s[0]['role_name'] for s in selected]}")
    return selected, rejections
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_role_selector.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/role_selector.py tests/test_role_selector.py
git commit -m "feat: select_best_roles with multi-role selection and structured parsing"
```

---

### Task 3: Update AA `main.py` for multi-role + rejection tracking

**Files:**
- Modify: `src/main.py`

- [ ] **Step 1: Update imports**

Change line 14 in `src/main.py`:

```python
# Old:
from src.role_selector import select_best_role, generate_submission_note
# New:
from src.role_selector import select_best_roles, generate_submission_note
```

- [ ] **Step 2: Update the role selection and submission block**

Replace the block in `run_once` (approximately lines 164-213) that handles AI selection and submission. The key changes:

1. Call `select_best_roles` instead of `select_best_role`
2. Build project URL from `project["url"]`
3. Loop over selected roles, submit each
4. Record rejections to DB

```python
                # Pick the best role(s) (AI selection if multiple candidates)
                selected, rejections = select_best_roles(candidates, project["project_name"])

                # Build full project URL for AA
                project_url = f"https://actorsaccess.com{project['url']}" if project.get("url") else ""

                # Record rejections
                for role in candidates:
                    if role["role_name"] in rejections:
                        db.record_rejection(
                            project_name=project["project_name"],
                            project_url=project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=rejections[role["role_name"]],
                            run_id=run_id,
                            platform="aa",
                        )

                if not selected:
                    ai_reason = next(iter(rejections.values()), "AI skipped")
                    logger.info(f"AI skipped project: {project['project_name']} — {ai_reason}")
                    if dry_run:
                        for role in candidates:
                            _print_role_decision("SKIP", project["project_name"], role, f"AI: {ai_reason}")
                    continue

                for best, ai_reason in selected:
                    unique_id = f"{project['breakdown_id']}_{best['role_id']}"

                    if dry_run:
                        tag = "SUBMIT (AI pick)" if len(candidates) > 1 else "SUBMIT"
                        _print_role_decision(tag, project["project_name"], best)
                        if len(candidates) > 1:
                            print(f"  AI reason: {ai_reason}")
                        custom_note = generate_submission_note(best, project["project_name"])
                        if custom_note:
                            print(f"  Note: {custom_note}")
                        roles_applied += 1
                        continue

                    # Generate custom note if the role asks for one
                    sub_cfg = cfg["submission"].copy()
                    custom_note = generate_submission_note(best, project["project_name"])
                    if custom_note:
                        sub_cfg["default_note"] = custom_note

                    success = browser.submit_for_role(
                        best, project["project_name"], sub_cfg
                    )
                    if success:
                        db.record_application(
                            unique_id, project["project_name"], best["role_name"],
                            role_description=best.get("description", ""),
                            ai_reason=ai_reason,
                            candidates_considered=len(candidates),
                            project_url=project_url,
                        )
                        roles_applied += 1
                    else:
                        logger.warning(
                            f"Skipping failed submission: {best['role_name']}"
                        )

                # Print rejected roles in dry run
                if dry_run and rejections:
                    for role in candidates:
                        if role["role_name"] in rejections:
                            _print_role_decision("SKIP", project["project_name"], role, rejections[role["role_name"]])
```

- [ ] **Step 3: Run existing tests to verify nothing is broken**

Run: `python -m pytest tests/ -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat: AA main.py multi-role submission and rejection tracking"
```

---

### Task 4: Update CN `main.py` for multi-role + rejection tracking

**Files:**
- Modify: `src/cn/main.py`

- [ ] **Step 1: Update imports**

Change line 14 in `src/cn/main.py`:

```python
# Old:
from src.role_selector import select_best_role, generate_submission_note
# New:
from src.role_selector import select_best_roles, generate_submission_note
```

- [ ] **Step 2: Update the role selection and submission block**

Replace the block in `run_once` (approximately lines 180-224) that handles AI selection and submission. The changes mirror AA's `main.py`:

```python
                selected, rejections = select_best_roles(candidates, project_name)

                # Build project URL for CN (use first candidate's URL)
                project_url = candidates[0].get("url", "")
                if project_url and not project_url.startswith("http"):
                    project_url = f"https://app.castingnetworks.com{project_url}"

                # Record rejections
                for role in candidates:
                    if role["role_name"] in rejections:
                        role_url = role.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_rejection(
                            project_name=project_name,
                            project_url=role_url or project_url,
                            role_name=role["role_name"],
                            role_description=role.get("description", ""),
                            rejection_reason=rejections[role["role_name"]],
                            run_id=run_id,
                            platform="cn",
                        )

                if not selected:
                    ai_reason = next(iter(rejections.values()), "AI skipped")
                    logger.info(f"AI skipped project: {project_name} — {ai_reason}")
                    if dry_run:
                        for role in candidates:
                            _print_role_decision("SKIP", project_name, role, f"AI: {ai_reason}")
                    continue

                for best, ai_reason in selected:
                    unique_id = f"cn_{best['project_id']}_{best['role_id']}"

                    if dry_run:
                        tag = "SUBMIT (AI pick)" if len(candidates) > 1 else "SUBMIT"
                        _print_role_decision(tag, project_name, best)
                        if len(candidates) > 1:
                            print(f"  AI reason: {ai_reason}")
                        custom_note = generate_submission_note(best, project_name)
                        if custom_note:
                            print(f"  Note: {custom_note}")
                        roles_applied += 1
                        continue

                    sub_cfg = cfg["submission"].copy()
                    custom_note = generate_submission_note(best, project_name)
                    if custom_note:
                        sub_cfg["default_note"] = custom_note

                    success = browser.submit_for_role(best, sub_cfg)
                    if success:
                        role_url = best.get("url", "")
                        if role_url and not role_url.startswith("http"):
                            role_url = f"https://app.castingnetworks.com{role_url}"
                        db.record_application(
                            unique_id, project_name, best["role_name"],
                            role_description=best.get("description", ""),
                            ai_reason=ai_reason,
                            candidates_considered=len(candidates),
                            platform="cn",
                            project_url=role_url,
                        )
                        roles_applied += 1
                    else:
                        logger.warning(f"Skipping failed submission: {best['role_name']}")

                # Print rejected roles in dry run
                if dry_run and rejections:
                    for role in candidates:
                        if role["role_name"] in rejections:
                            _print_role_decision("SKIP", project_name, role, rejections[role["role_name"]])
```

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add src/cn/main.py
git commit -m "feat: CN main.py multi-role submission and rejection tracking"
```

---

## Chunk 2: Daily Digest Email & Workflow

### Task 5: Create `src/digest.py` — email builder and sender

**Files:**
- Create: `src/digest.py`
- Create: `tests/test_digest.py`

- [ ] **Step 1: Write failing tests for digest query and HTML rendering**

Create `tests/test_digest.py`:

```python
# tests/test_digest.py
"""Tests for the daily digest email builder.

Tests the data gathering and HTML rendering. Does NOT test SendGrid sending.
"""
import os
from unittest.mock import patch, MagicMock

import pytest

from src.database import Database
from src.digest import build_digest_html, gather_digest_data


@pytest.fixture
def db(tmp_path):
    db_path = os.path.join(str(tmp_path), "test.db")
    return Database(db_path)


def test_gather_digest_data_empty(db):
    """Empty DB should return empty data."""
    data = gather_digest_data(db)
    assert data["applications"] == []
    assert data["rejections"] == []
    assert data["runs"] == []


def test_gather_digest_data_with_records(db):
    """Should gather today's applications and rejections."""
    run_id = db.start_run()
    db.record_application(
        "role_1", "Test Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age too high",
        run_id=run_id,
        platform="aa",
    )
    db.complete_run(run_id, roles_found=5, roles_applied=1, roles_skipped=2)

    data = gather_digest_data(db)
    assert len(data["applications"]) == 1
    assert len(data["rejections"]) == 1
    assert len(data["runs"]) == 1


def test_build_digest_html_with_data(db):
    """Should produce HTML with project sections."""
    run_id = db.start_run()
    db.record_application(
        "role_1", "Test Project", "Lead",
        ai_reason="Best fit", project_url="https://example.com",
    )
    db.record_rejection(
        project_name="Test Project",
        project_url="https://example.com",
        role_name="Villain",
        role_description="The bad guy",
        rejection_reason="Age too high",
        run_id=run_id,
        platform="aa",
    )
    db.complete_run(run_id, roles_found=5, roles_applied=1, roles_skipped=2)

    data = gather_digest_data(db)
    html = build_digest_html(data)

    assert "Test Project" in html
    assert "Lead" in html
    assert "Best fit" in html
    assert "Villain" in html
    assert "Age too high" in html
    assert "https://example.com" in html


def test_build_digest_html_empty():
    """Empty data should produce 'no applications' message."""
    data = {"applications": [], "rejections": [], "runs": []}
    html = build_digest_html(data)
    assert "No applications" in html or "no applications" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL — `src.digest` module not found.

- [ ] **Step 3: Implement `src/digest.py`**

```python
# src/digest.py
"""Daily digest email — summarizes applications and rejections from the last 24 hours."""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from src.database import Database

logger = logging.getLogger("digest")


def gather_digest_data(db: Database) -> dict:
    """Query the database for the last 24 hours of activity."""
    return {
        "applications": db.get_daily_applications(),
        "rejections": db.get_daily_rejections(),
        "runs": db.get_daily_run_summary(),
    }


def build_digest_html(data: dict) -> str:
    """Build an HTML email body from digest data."""
    applications = data["applications"]
    rejections = data["rejections"]
    runs = data["runs"]

    if not applications and not rejections:
        return _empty_digest_html(runs)

    # Group by project
    projects = defaultdict(lambda: {"applied": [], "rejected": []})
    for app in applications:
        projects[app["project_name"]]["applied"].append(app)
    for rej in rejections:
        projects[rej["project_name"]]["rejected"].append(rej)

    # Build HTML
    sections = []
    for project_name, roles in sorted(projects.items()):
        project_url = ""
        if roles["applied"]:
            project_url = roles["applied"][0].get("project_url", "")
        elif roles["rejected"]:
            project_url = roles["rejected"][0].get("project_url", "")

        header = f'<a href="{project_url}">{project_name}</a>' if project_url else project_name

        section = f'<div style="margin-bottom:24px;border:1px solid #ddd;border-radius:8px;padding:16px;">\n'
        section += f'<h2 style="margin-top:0;color:#333;">{header}</h2>\n'

        # Applied roles
        if roles["applied"]:
            for app in roles["applied"]:
                platform_badge = _platform_badge(app.get("platform", "aa"))
                desc = (app.get("role_description") or "")[:200]
                section += f'<div style="background:#e8f5e9;padding:12px;border-radius:4px;margin-bottom:8px;">\n'
                section += f'<strong style="color:#2e7d32;">APPLIED</strong> {platform_badge} — <strong>{app["role_name"]}</strong>'
                if app.get("candidates_considered", 1) > 1:
                    section += f' <em>(chosen from {app["candidates_considered"]} candidates)</em>'
                section += f'<br><span style="color:#555;">{desc}</span>' if desc else ""
                section += f'<br><strong>Reason:</strong> {app.get("ai_reason", "N/A")}'
                section += '\n</div>\n'

        # Rejected roles
        if roles["rejected"]:
            for rej in roles["rejected"]:
                platform_badge = _platform_badge(rej.get("platform", "aa"))
                desc = (rej.get("role_description") or "")[:200]
                rej_url = rej.get("project_url", "")
                role_link = f' — <a href="{rej_url}">view listing</a>' if rej_url else ""
                section += f'<div style="background:#fff3e0;padding:12px;border-radius:4px;margin-bottom:8px;">\n'
                section += f'<strong style="color:#e65100;">PASSED</strong> {platform_badge} — <strong>{rej["role_name"]}</strong>{role_link}'
                section += f'<br><span style="color:#555;">{desc}</span>' if desc else ""
                section += f'<br><strong>Reason:</strong> {rej.get("rejection_reason", "N/A")}'
                section += '\n</div>\n'

        section += '</div>\n'
        sections.append(section)

    # Footer stats
    total_applied = len(applications)
    total_rejected = len(rejections)
    total_projects = len(projects)
    failed_runs = [r for r in runs if r.get("status") == "error"]

    footer = f'<div style="margin-top:24px;padding:16px;background:#f5f5f5;border-radius:8px;">\n'
    footer += f'<strong>Summary:</strong> {total_applied} roles applied, {total_rejected} roles passed, {total_projects} projects evaluated<br>\n'
    footer += f'<strong>Runs:</strong> {len(runs)} total'
    if failed_runs:
        footer += f' ({len(failed_runs)} failed)'
        for fr in failed_runs:
            footer += f'<br><span style="color:red;">Failed ({fr.get("platform","?")}): {fr.get("error_message","unknown")}</span>'
    footer += '\n</div>\n'

    body = "\n".join(sections) + footer
    return _wrap_html(body)


def _empty_digest_html(runs: list[dict]) -> str:
    """Build HTML for a day with no applications."""
    content = '<div style="padding:24px;text-align:center;color:#666;">\n'
    content += '<h2>No applications today</h2>\n'
    content += f'<p>{len(runs)} automation run(s) completed — no new roles matched.</p>\n'
    failed = [r for r in runs if r.get("status") == "error"]
    if failed:
        content += '<p style="color:red;">Some runs failed:</p>\n'
        for fr in failed:
            content += f'<p style="color:red;">{fr.get("platform","?")}: {fr.get("error_message","unknown")}</p>\n'
    content += '</div>\n'
    return _wrap_html(content)


def _platform_badge(platform: str) -> str:
    color = "#1565c0" if platform == "aa" else "#6a1b9a"
    label = "AA" if platform == "aa" else "CN"
    return f'<span style="background:{color};color:white;padding:2px 6px;border-radius:3px;font-size:12px;">{label}</span>'


def _wrap_html(body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
<h1 style="border-bottom:2px solid #333;padding-bottom:8px;">Daily Casting Digest</h1>
<p style="color:#666;">Generated {datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %I:%M %p")} UTC</p>
{body}
</body>
</html>"""


def send_email(html: str):
    """Send the digest email via SendGrid."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set — cannot send digest")
        return

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Email, To, Content

        message = Mail(
            from_email=Email("REDACTED"),
            to_emails=To("REDACTED"),
            subject=f"Casting Digest — {datetime.now(tz=timezone.utc).strftime('%B %d, %Y')}",
            html_content=Content("text/html", html),
        )

        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"Digest email sent (status {response.status_code})")

    except Exception as e:
        logger.error(f"Failed to send digest email: {e}")


def main():
    """Entry point for the digest workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    db = Database("data/applied.db")
    try:
        data = gather_digest_data(db)
        html = build_digest_html(data)
        send_email(html)
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_digest.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digest.py tests/test_digest.py
git commit -m "feat: daily digest email builder and sender"
```

---

### Task 6: Add SendGrid to requirements and create GitHub Actions workflow

**Files:**
- Modify: `requirements.txt`
- Create: `.github/workflows/daily-digest.yml`

- [ ] **Step 1: Add `sendgrid` to requirements.txt**

Append to `requirements.txt`:

```
sendgrid>=6.11.0
```

- [ ] **Step 2: Create the daily digest workflow**

Create `.github/workflows/daily-digest.yml`:

```yaml
name: Daily Digest

on:
  schedule:
    # 8pm PT = 3am UTC
    - cron: "0 3 * * *"
  workflow_dispatch: # Allow manual trigger

jobs:
  send-digest:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Send daily digest
        env:
          SENDGRID_API_KEY: ${{ secrets.SENDGRID_API_KEY }}
        run: python -m src.digest
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt .github/workflows/daily-digest.yml
git commit -m "feat: add daily digest workflow and sendgrid dependency"
```

---

### Task 7: Add SendGrid API key to GitHub Actions secrets

**Files:** None (GitHub settings only)

- [ ] **Step 1: Add the secret**

Run: `gh secret set SENDGRID_API_KEY`

Enter the SendGrid API key when prompted. (The user has this key.)

- [ ] **Step 2: Verify the secret exists**

Run: `gh secret list`

Expected: `SENDGRID_API_KEY` appears in the list.

- [ ] **Step 3: Commit** — N/A (no code change)

---

### Task 8: Run full test suite and verify

**Files:** None

- [ ] **Step 1: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS.

- [ ] **Step 2: Run AA dry-run locally to verify multi-role flow**

Run: `python -m src.main --once --dry-run --max-pages 1`
Expected: Output shows SUBMIT/SKIP decisions. No crashes.

- [ ] **Step 3: Trigger digest workflow manually**

Run: `gh workflow run "Daily Digest"`

Check: `gh run list --workflow="Daily Digest" --limit=1`

Expected: Workflow completes successfully. Check inbox for digest email.
