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
| project_url       | TEXT      | Link back to the project on AA/CN          |
| role_name         | TEXT      | Name of the rejected role                  |
| role_description  | TEXT      | Role description text                      |
| rejection_reason  | TEXT      | AI's reason for not selecting this role     |
| run_id            | INTEGER   | Foreign key to run_history                 |
| platform          | TEXT      | 'aa' or 'cn'                               |
| rejected_at       | TIMESTAMP | When the rejection was recorded            |

**New column on `applied_roles`:** `project_url TEXT` — URL to the project listing.

**New method on `Database`:** `record_rejection(...)` — inserts into `rejected_roles`.

### 2. Multi-Role Submission

**`role_selector.py` changes:**

- Rename `select_best_role` → `select_best_roles`
- Returns `list[tuple[dict, str]]` — list of (role, reason) pairs, 1 or 2 items
- Updated AI prompt:

> Pick the ONE role that is the best fit. If a second role is also a genuinely strong fit, return it too — but only if both are truly well-matched. Don't force a second pick.
>
> Respond with:
> - Line 1: chosen role number(s), comma-separated (e.g., "2" or "1,3")
> - Line 2+: reasoning for each chosen role, and brief reason each unchosen role was passed over

- When only 1 candidate exists, return it directly (no API call, same as current behavior)
- When API key is missing, fall back to first role only (same as current behavior)
- Return `None` (empty list) when AI returns 0 (skip project)

**`main.py` and `cn/main.py` changes:**

- Call `select_best_roles` instead of `select_best_role`
- Loop over returned list, submit for each selected role
- Record each application in DB with its own `unique_id`
- After submissions, record all non-selected candidates in `rejected_roles`

### 3. Rejected Role Tracking

Captured in both `main.py` and `cn/main.py` after AI selection:

- **Multi-candidate projects:** non-selected candidates written to `rejected_roles` with per-role rejection reason from AI response
- **AI skips project (returns 0):** all candidates written to `rejected_roles` with the skip reason
- **Single-candidate projects:** no rejections to record (no choice was made)

The AI prompt explicitly asks for rejection reasoning for each non-selected role.

### 4. Project URL Plumbing

Both platforms already scrape `project["url"]` — it just isn't persisted. Changes:

- `record_application()` accepts and stores `project_url`
- `record_rejection()` accepts and stores `project_url`
- Both `main.py` files pass `project["url"]` through to database calls

### 5. Daily Digest Email

**New module: `src/digest.py`**

Responsibilities:
1. Open `data/applied.db`
2. Query `applied_roles` and `rejected_roles` for today (both platforms)
3. Group results by project
4. Render HTML email with per-project sections:
   - Project name (linked to URL)
   - **Applied roles:** role name, description snippet, AI reasoning
   - **Rejected candidates:** role name, description snippet, rejection reason, link
5. Footer: total projects, roles applied, roles rejected, failed runs
6. Send via SendGrid API to `REDACTED`
7. If no applications today, send a short "No applications today" email

**New workflow: `.github/workflows/daily-digest.yml`**

- Schedule: `0 3 * * *` (8pm PT = 3am UTC)
- Steps: checkout → Python setup → install deps (sendgrid) → run `python -m src.digest`
- Secrets: `SENDGRID_API_KEY`
- No DB commit step needed (read-only)

**SendGrid setup:**
- Verified sender: `REDACTED` (Single Sender Verification)
- API key stored as `SENDGRID_API_KEY` GitHub Actions secret

## Files Changed

| File | Change |
|------|--------|
| `src/database.py` | New table, new columns, new methods |
| `src/role_selector.py` | `select_best_roles`, updated prompt |
| `src/main.py` | Multi-role loop, rejection tracking, URL plumbing |
| `src/cn/main.py` | Same changes as main.py |
| `src/digest.py` | **New** — digest builder and sender |
| `.github/workflows/daily-digest.yml` | **New** — daily digest workflow |
| `requirements.txt` | Add `sendgrid` |
