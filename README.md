# ActorsAccess Auto-Apply

Automated casting submission tool for three platforms — [Actors Access](https://actorsaccess.com), [Casting Networks](https://castingnetworks.com), and [Backstage](https://backstage.com). Logs in, scrapes breakdowns, filters roles by fit, uses Claude Sonnet to pick the best role per project, and submits. Supports two modes (`paid`, `unpaid`) and emails a daily digest of activity.

## Platforms

| Platform   | Entry point                 | Automation                              | Paid saved search          | Unpaid saved search |
|------------|-----------------------------|-----------------------------------------|----------------------------|---------------------|
| Actors Access | `python -m src.main`       | Playwright (Chromium)                   | site filters via config    | site filters via config |
| Casting Networks | `python -m src.cn.main`  | Playwright (Chromium, must run headed)  | default view               | saved search named `"unpaid"` |
| Backstage  | `python -m src.backstage.main` | urllib REST client (no browser)         | default view               | saved search named `"unpaid"` |

## Prerequisites

- Python 3.10+ (developed on 3.12)
- Git
- Internet connection

## Setup on a new machine

```bash
# 1. Clone
git clone https://github.com/powellm4/actorsaccess.git
cd actorsaccess

# 2. Virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

# 3. Install Python deps
pip install -r requirements.txt

# 4. Install Playwright's Chromium (used by AA and CN)
playwright install chromium

# 5. Set ANTHROPIC_API_KEY (the only required env var)
# Windows (permanent — then restart terminal):
setx ANTHROPIC_API_KEY "sk-ant-..."
# macOS/Linux — add to ~/.bashrc or ~/.zshrc:
export ANTHROPIC_API_KEY="sk-ant-..."
```

That's it. Run `python -m src.main --once --dry-run` to confirm the AA flow works; then the same for `src.cn.main` and `src.backstage.main`.

### Environment variables

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `ANTHROPIC_API_KEY` | Yes (AI selection + note generation) | Claude API key. Without it, the tool falls back to the first matching role with no reasoning. Get one at [console.anthropic.com](https://console.anthropic.com). |
| `DEEPSEEK_API_KEY` | Optional | DeepSeek API key for the shadow-eval comparison feature. Without it, shadow mode is silently disabled and Claude operates alone. |
| `SHADOW_ENABLED` | Optional | Set to `0` to force-disable shadow eval even when `DEEPSEEK_API_KEY` is set. Default: enabled. |
| `GMAIL_APP_PASSWORD` | Only to send digest emails | Gmail App Password (not your main password). Used by `src/digest.py` SMTP sender. |
| `GOOGLE_CALENDAR_SA_KEY` | Optional | Base64-encoded Google service account JSON for calendar-conflict checks (`src/calendar_check.py`). |
| `AA_USERNAME` / `AA_PASSWORD` | Optional | Overrides credentials in `config.yaml` / `config_unpaid.yaml`. Not needed locally — the committed yaml already has them. |
| `CN_EMAIL` / `CN_PASSWORD` | Optional | Overrides credentials in `cn_config.yaml` / `cn_config_unpaid.yaml`. |
| `BACKSTAGE_EMAIL` / `BACKSTAGE_PASSWORD` | Optional | Overrides credentials in `backstage_config.yaml` / `backstage_config_unpaid.yaml`. |
| `OVERRIDE_GITHUB_TOKEN` | Optional (Apply Anyway) | Fine-grained PAT with **Issues: Read & Write** scope on the override repo configured in `overrides.repo`. Without it, "Apply anyway" buttons in the digest are omitted. (Same name in CI — GitHub Actions disallows secret names starting with `GITHUB_`.) |

Credentials for all three platforms are committed in the per-platform yaml files. The env vars only exist so GitHub Actions can inject secrets at runtime.

### Apply Anyway via GitHub Issues

Each Passed and Needs-Attention card in the digest carries an **Apply anyway** button. Clicking it opens a pre-filled GitHub issue in a separate private repo. On the next AA run the bot reads open issues with the configured label, force-applies the role (bypassing AI / travel-pay / calendar / needs-input checks), then comments and closes the issue. Outcomes show up in the next digest's "Manually Applied" section.

One-time setup:

1. **Create a private repo** for the overrides — e.g. `your-username/aa-overrides`. It only ever holds issues; no code.
2. **Create a fine-grained PAT** at [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens) with access to that single repo and the **Issues: Read & Write** permission. Save it locally as the `OVERRIDE_GITHUB_TOKEN` env var, and add the same name as a repo secret in GitHub Actions for the scheduled runs.
3. **Set `overrides.repo`** in `config.yaml` (and `config_unpaid.yaml` if you use that mode) to the new repo's full name. The default label is `apply-anyway` — the bot creates it on first use if it doesn't exist.

Comment the `overrides:` block out, or leave the env var unset, to disable the feature entirely; the digest will simply omit the buttons.

### Config files

Every platform has two yaml files — one per mode. All are committed.

| File | Platform | Mode |
|------|----------|------|
| `config.yaml` | Actors Access | paid |
| `config_unpaid.yaml` | Actors Access | unpaid |
| `cn_config.yaml` | Casting Networks | paid |
| `cn_config_unpaid.yaml` | Casting Networks | unpaid |
| `backstage_config.yaml` | Backstage | paid |
| `backstage_config_unpaid.yaml` | Backstage | unpaid |

Common knobs (names vary by platform): `filters.region`, `filters.union_status`, `filters.paying_only`, `filters.exclude_reality_tv`, `submission.headshot_index`, `submission.include_media`, `max_pages`, `browser.headless`. See `config.example.yaml` for the AA template.

## Usage

Each entry point takes the same flags:

| Flag | Effect |
|------|--------|
| `--once` | Single run (default is continuous scheduler on `schedule.interval_hours`) |
| `--mode paid` (default) / `--mode unpaid` | Picks the right config yaml automatically |
| `--dry-run` | Prints `[SUBMIT]` / `[SKIP - reason]` for each role without submitting |
| `--headed` | Shows the browser window (AA, CN only) |
| `--max-pages N` | Cap breakdown pages processed this run |
| `--config path/to/file.yaml` | Override config file path |

Examples:

```bash
# Paid single-run, all three platforms
python -m src.main --once
python -m src.cn.main --once
python -m src.backstage.main --once

# Unpaid mode — same commands, different flag
python -m src.main --once --mode unpaid
python -m src.cn.main --once --mode unpaid
python -m src.backstage.main --once --mode unpaid

# Debug a flow without submitting
python -m src.main --once --dry-run --headed --max-pages 1
```

### Digest email

```bash
python -m src.digest --mode paid     # "Casting Digest (Paid)" — blue accent
python -m src.digest --mode unpaid   # "Casting Digest (Extended)" — purple accent
```

Sends via Gmail SMTP using `GMAIL_APP_PASSWORD`. Recipient is hardcoded in `src/digest.py`. The `digest_history` table keeps the two streams disjoint so a paid digest won't re-email unpaid rows.

## How it works

1. **Login** — credentials from per-platform config yaml (or env var override).
2. **Saved search / filters** — AA uses config-driven site filters; CN and Backstage click the `"unpaid"` saved search in unpaid mode.
3. **Scrape** — project list → role list per project.
4. **Filter** — fit-for-me, background/extra rejection, theater/musical rejection, already-applied dedup (`applied.db`, scoped by `platform` + `mode`), `check_travel_pay()` guard in paid mode, role-type whitelist (Lead/Principal/Series Regular) in unpaid mode.
5. **AI selection** — Claude Sonnet picks one role per project from what remains; generates a submission note if the casting post asks for specific info.
6. **Submit** — attaches media per config, applies cover letter when required (Backstage self-tape).
7. **Record** — writes to `data/applied.db` with `platform` and `mode` columns.

Actor profile used by AI selection lives in `ACTOR_PROFILE` in `src/role_selector.py` — edit there to change it.

### Shadow evaluation (DeepSeek)

Every Claude call in `src/role_selector.py` is duplicated through DeepSeek (`deepseek-chat` + `deepseek-reasoner`) in background threads for comparison. Claude remains the production decision-maker — DeepSeek responses are observed only. Results land in the `shadow_comparisons` table of `data/applied.db` and are surfaced via a daily-digest summary block plus a full report at `/shadow/` on the archive site (per-call-site agreement rate, disagreement queue, cost/latency stats). The feature auto-disables if `DEEPSEEK_API_KEY` is unset or `SHADOW_ENABLED=0`.

## Deployment — GitHub Actions

Production runs in two workflows, both in `.github/workflows/`:

| Workflow | Schedule (UTC) | What it does |
|----------|----------------|--------------|
| `auto-apply.yml` | `0 15,18,21,0,3,6 * * *` (6×/day) | AA → CN → Backstage → paid digest |
| `auto-apply-unpaid.yml` | `0 16 * * *` (9am PT, 1×/day) | AA → CN → Backstage → unpaid digest |

Both share a `db-access` concurrency group so they don't clobber each other. CN runs under `xvfb` on the Linux runner since it blocks headless browsers.

### DB storage in CI

`data/applied.db` lives in the `db-storage` GitHub Release. Each workflow downloads it at start, updates it per step, and re-uploads at end. Local runs get a fresh SQLite (auto-created by `Database._create_tables()` on first connect) — the local DB is not production data.

### Required GitHub Secrets

Set under repo Settings → Secrets and variables → Actions:

`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GMAIL_APP_PASSWORD`, `GOOGLE_CALENDAR_SA_KEY`, `AA_USERNAME`, `AA_PASSWORD`, `CN_EMAIL`, `CN_PASSWORD`, `BACKSTAGE_EMAIL`, `BACKSTAGE_PASSWORD`.

### Archive site (live, public, GitHub Pages)

Every digest run also publishes a searchable archive of every submission to a public GitHub Pages site. Open the URL on any device, type a role/project/CD name in the search box, and rows filter live. Useful for answering *"did the bot apply to the JORDAN role I just got a self-tape invite for?"* without digging through Gmail.

**One-time setup:**

1. **Set the public URL as a variable** (so the digest email can link to it). Repo Settings → Secrets and variables → Actions → Variables tab → New variable:
   - Name: `ARCHIVE_SITE_URL`
   - Value: `https://<your-github-username>.github.io/actorsaccess/`
2. **Enable GitHub Pages.** Repo Settings → Pages → Source: *Deploy from a branch* → Branch: `gh-pages` (root). Save.
3. Trigger the `Auto Apply` workflow manually (Actions tab → Run workflow) and wait for the `Publish archive site` step. The site will be live within ~1 min.

Local CLI (for testing the renderer):
```bash
python -m src.archive --db data/applied.db --output dist/index.html
```

## File structure

```
actorsaccess/
  src/
    main.py              # AA entry point + scheduler
    browser.py           # AA Playwright automation
    config.py            # AA config loader (env var overrides)
    database.py          # SQLite (platform + mode aware)
    filters.py           # Shared role/project filters
    role_selector.py     # Claude Sonnet role selection + note generation
    digest.py            # Mode-aware Gmail digest sender
    archive.py           # Searchable submissions archive renderer (CLI + email)
    calendar_check.py    # Google Calendar conflict detection
    cn/
      main.py            # CN entry point
      browser.py         # CN Playwright automation
      config.py
    backstage/
      main.py            # Backstage entry point
      client.py          # urllib REST client (Cloudflare-safe)
      config.py
  config.yaml                   # AA paid
  config_unpaid.yaml            # AA unpaid
  cn_config.yaml                # CN paid
  cn_config_unpaid.yaml         # CN unpaid
  backstage_config.yaml         # Backstage paid
  backstage_config_unpaid.yaml  # Backstage unpaid
  config.example.yaml           # AA template
  requirements.txt
  .github/workflows/
    auto-apply.yml              # Paid, 6×/day
    auto-apply-unpaid.yml       # Unpaid, 1×/day
  .github/actions/
    publish-archive/            # Composite action: render → encrypt → push to gh-pages
  data/applied.db               # SQLite (git-ignored; lives in GH Release for CI)
  logs/                         # Log files (git-ignored)
  tests/
```

## Database

Auto-created on first connect. Key tables:

**applied_roles** — every submission
| Column | Description |
|--------|-------------|
| `role_id` | Unique `breakdown_id + role_id` |
| `platform` | `aa`, `cn`, or `backstage` |
| `mode` | `paid` or `unpaid` |
| `project_name`, `role_name`, `role_description` | Posting details |
| `ai_reason` | Claude's pick rationale (or "only matching role") |
| `candidates_considered` | How many roles were in the running |
| `applied_at` | Timestamp |

**run_history** — per-run stats (started/completed, counts, status, error_message).

**rejected_roles** — filtered-out roles tracked for digest.

**flagged_roles** — roles requiring manual review (e.g., calendar conflicts).

**digest_history** — last-sent timestamps per mode.

### Quick DB inspection

```bash
sqlite3 data/applied.db "SELECT applied_at, platform, mode, project_name, role_name, ai_reason FROM applied_roles ORDER BY applied_at DESC LIMIT 20"
sqlite3 data/applied.db "SELECT * FROM run_history ORDER BY started_at DESC LIMIT 10"
tail -f logs/auto_apply.log
```

## Tests

```bash
python -m pytest tests/ -v
```
