# Daily Digest & Multi-Role Submission

**Date:** 2026-03-12
**Status:** Approved

## Problem

The actor cannot verify whether the AI is selecting the right roles. There is no visibility into what roles were considered and rejected, or why. Additionally, the system only submits for one role per project even when two roles are both strong fits.

## Solution

Two changes:

1. **Daily digest email** — a nightly HTML email summarizing every application and rejection from the day, with AI reasoning and links to each role.
2. **Multi-role submission** — allow the AI to select up to 2 roles per project when both are genuinely strong fits.

## Design

### 1. Database Changes

**New table: `rejected_roles`**

| Column            | Type      | Description                                |
|-------------------|-----------|--------------------------------------------|
| id                | INTEGER   | Primary key                                |
| project_name      | TEXT      | Name of the project                        |
| project_url       | TEXT      | Link back to the project/role on AA/CN     |
| role_name         | TEXT      | Name of the rejected role                  |
| role_description  | TEXT      | Role description text                      |
| rejection_reason  | TEXT      | AI's reason for not selecting this role     |
| run_id            | INTEGER   | Foreign key to run_history                 |
| platform          | TEXT      | 'aa' or 'cn'                               |
| rejected_at       | TIMESTAMP | When the rejection was recorded            |

**Uniqueness:** `UNIQUE(role_name, project_name, platform)` to prevent duplicate rejections across runs when the same listing appears on multiple days.

**New column on `applied_roles`:** `project_url TEXT DEFAULT ''` — URL to the project listing. Added via `ALTER TABLE ... ADD COLUMN` migration pattern (same as existing schema migrations in `database.py`).

**New method on `Database`:**
- `record_rejection(project_name, project_url, role_name, role_description, rejection_reason, run_id, platform)` — upserts into `rejected_roles` using `ON CONFLICT(role_name, project_name, platform) DO UPDATE SET rejection_reason, run_id, rejected_at` so the digest always shows the most recent AI reasoning
- `record_application()` — add optional `project_url` parameter with default `""`

### 2. Multi-Role Submission

**`role_selector.py` changes:**

- Rename `select_best_role` → `select_best_roles`
- Returns `tuple[list[tuple[dict, str]], dict[str, str]]`:
  - First element: list of (selected_role, reason) pairs (1 or 2 items), or empty list if AI skips
  - Second element: dict mapping rejected role names to rejection reasons
- Increase `max_tokens` from 200 to 500 to accommodate multi-role reasoning
- Updated AI prompt:

> Pick the ONE role that is the best fit. If a second role is also a genuinely strong fit, return it too — but only if both are truly well-matched. Don't force a second pick.
>
> If NONE of the roles are a good fit, respond with SKIP on the first line and explain why.
>
> Respond with this exact format:
> SELECTED: 2 - reason for picking role 2
> SELECTED: 3 - reason for picking role 3
> REJECTED: 1 - reason for rejecting role 1
> REJECTED: 4 - reason for rejecting role 4

- **Parsing:** Look for lines starting with `SELECTED:` or `REJECTED:` and extract role number + reason via regex. This structured format avoids ambiguity in the current free-text parsing.
- **Fallback:** If the AI response contains no `SELECTED:` lines (malformed output), fall back to selecting the first candidate with reason "AI returned unparseable response, defaulted to first". All other candidates get rejected with the same reason.
- When only 1 candidate exists, return it directly (no API call, same as current behavior)
- When API key is missing, fall back to first role only (same as current behavior)
- When AI returns SKIP: return empty selected list, all candidates go to rejections dict

**`main.py` and `cn/main.py` changes:**

- Call `select_best_roles` instead of `select_best_role`
- Receive both selected list and rejections dict
- Loop over selected list, submit for each role
- Record each application in DB with its own `unique_id`
- Pass `run_id` through to rejection recording (already available in the run loop scope)
- After submissions, record all rejected candidates via `db.record_rejection()`

### 3. Rejected Role Tracking

Captured in both `main.py` and `cn/main.py` after AI selection:

- **Multi-candidate projects:** non-selected candidates written to `rejected_roles` with per-role rejection reason from the structured AI response
- **AI skips project (returns SKIP):** all candidates written to `rejected_roles` with the skip reason
- **Single-candidate projects where the role is selected:** no rejections to record (no choice was made)

**Out of scope:** Roles eliminated by hard filters (`role_matches`, `_is_background`, etc.) are not tracked in `rejected_roles`. These are deterministic and don't need review — the digest focuses on AI decisions.

### 4. Project URL Plumbing

- **AA:** `scrape_projects()` already returns `project["url"]` as a relative path. Store as full URL: `https://breakdownexpress.com{project["url"]}` (or similar base).
- **CN:** Each role dict has a `role["url"]` which is a per-role URL. Store role-level URLs since they're more useful for linking directly to the listing.
- Both `main.py` files pass the URL through to `db.record_application()` and `db.record_rejection()`

### 5. Daily Digest Email

**New module: `src/digest.py`**

Responsibilities:
1. Open `data/applied.db`
2. Query `applied_roles` and `rejected_roles` for the last 24 hours (using `applied_at >= datetime('now', '-24 hours')` in UTC — this aligns with the 8pm PT daily cadence)
3. Group results by project
4. Render HTML email with per-project sections:
   - Project name (linked to URL)
   - **Applied roles:** role name, description snippet, AI reasoning
   - **Rejected candidates:** role name, description snippet, rejection reason, link
5. Footer: total projects, roles applied, roles rejected, any failed runs
6. Send via SendGrid API to `REDACTED`
7. If no applications today, send a short "No applications today" email

**Error handling:** If SendGrid API fails, log the error and exit with code 0 (don't fail the workflow — it's informational, not critical).

**New workflow: `.github/workflows/daily-digest.yml`**

- Schedule: `0 3 * * *` (8pm PT = 3am UTC)
- Steps: checkout → Python setup → install deps (sendgrid) → run `python -m src.digest`
- Secrets: `SENDGRID_API_KEY`
- No DB commit step needed (read-only)

**SendGrid setup:**
- Verified sender: `REDACTED` (Single Sender Verification)
- API key stored as `SENDGRID_API_KEY` GitHub Actions secret

### 6. Edge Cases

- **Failed submission for one of two selected roles:** The successfully submitted role is recorded in `applied_roles`. The failed one is not recorded anywhere (same as current behavior for single-role failures). On the next run, the project will be skipped because one role is already in the DB. This is acceptable — the digest will show what was applied, and the actor can manually submit for the other role via the link.
- **No config changes needed:** Multi-role behavior is entirely AI-driven, not configurable. No changes to `config.yaml` or `cn_config.yaml`.

## Files Changed

| File | Change |
|------|--------|
| `src/database.py` | New table, new columns, new methods |
| `src/role_selector.py` | `select_best_roles`, updated prompt, structured response parsing |
| `src/main.py` | Multi-role loop, rejection tracking, URL plumbing, run_id threading |
| `src/cn/main.py` | Same changes as main.py |
| `src/digest.py` | **New** — digest builder and sender |
| `.github/workflows/daily-digest.yml` | **New** — daily digest workflow |
| `requirements.txt` | Add `sendgrid` |
