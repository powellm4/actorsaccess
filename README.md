# ActorsAccess Auto-Apply

Automated tool that logs into [Actors Access](https://actorsaccess.com), scrapes casting breakdowns, filters roles by fit, uses AI to pick the best role per project, and submits. Uses Playwright for browser automation and Claude Haiku for intelligent role selection.

## Setup Instructions

### Prerequisites

- Python 3.10+ (developed on 3.12)
- Git
- Internet connection

### Step-by-step setup

```bash
# 1. Clone the repo
git clone https://github.com/powellm4/actorsaccess.git
cd actorsaccess

# 2. Create and activate a virtual environment
python -m venv venv

# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium

# 5. Set environment variables
#    (credentials and API key are NOT stored in the repo)

# On Windows (permanent):
setx AA_USERNAME "your_actorsaccess_username"
setx AA_PASSWORD "your_password"
setx ANTHROPIC_API_KEY "sk-ant-..."
# Then restart your terminal for setx to take effect

# On macOS/Linux, add to ~/.bashrc or ~/.zshrc:
export AA_USERNAME="your_actorsaccess_username"
export AA_PASSWORD="your_password"
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Note:** The username is your Actors Access username, NOT your email.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AA_USERNAME` | Yes | Actors Access username |
| `AA_PASSWORD` | Yes | Actors Access password |
| `ANTHROPIC_API_KEY` | No | Anthropic API key for AI role selection. Without it, defaults to first matching role per project. Get one at [console.anthropic.com](https://console.anthropic.com) |

### Config settings

All other settings are in `config.yaml` (committed to the repo). Edit as needed:

| Setting | Description |
|---------|-------------|
| `filters.region` | Region to search. Options: Los Angeles, New York, Chicago, San Francisco / NorCal, Central Atlantic, Midwest, New England, North Central, Northwest, Pacific, Rocky Mountains, South Central, Southeast, Vancouver, Toronto |
| `filters.union_status` | `all`, `union`, `non-union`, `fit_for_me`, `union_fit`, `nonunion_fit` |
| `filters.paying_only` | Only show paying roles |
| `filters.exclude_reality_tv` | Skip reality TV listings |
| `submission.headshot_index` | Which headshot to use (0 = first) |
| `submission.include_media` | Attach demo reel/media |
| `submission.include_size_card` | Attach size card |
| `max_pages` | Pages of breakdowns per run (~25 projects/page) |
| `schedule.interval_hours` | How often to repeat (scheduler mode) |
| `browser.headless` | `true` = no visible browser window |

## Usage

### Continuous scheduler (recommended for autonomous operation)

Runs immediately, then repeats every `interval_hours`:

```bash
python -m src.main
```

### Single run

```bash
python -m src.main --once
```

### Dry run (preview only, no submissions)

```bash
python -m src.main --once --dry-run
```

Prints `[SUBMIT]`, `[SUBMIT (AI pick)]`, or `[SKIP - reason]` for each role without actually submitting.

### With visible browser (for debugging)

```bash
python -m src.main --once --headed
```

### Limit pages processed

```bash
python -m src.main --once --max-pages 1
```

## How it works

1. **Login** — Logs into Actors Access with credentials from env vars
2. **Navigate** — Goes to the Breakdowns page, selects region, applies filters (union status, etc.)
3. **Scrape projects** — Collects project listings page by page (~25 per page)
4. **Filter projects** — Skips theater/musical projects entirely
5. **Scrape roles** — For each remaining project, navigates in and scrapes individual roles
6. **Filter roles** — Three-layer filtering:
   - **Fit for me** — Skips roles the site doesn't highlight as a demographic match (the site uses your profile's age, gender, ethnicity to highlight fitting roles in yellow)
   - **Background filter** — Skips roles with "BACKGROUND", "BG", or "Extra" in the name or description
   - **Already applied** — Skips roles tracked in the local SQLite database
7. **AI role selection** — If multiple roles pass filters on the same project, sends descriptions to Claude Haiku to pick the single best fit based on actor profile (only 1 submission per project). Falls back to first role if no API key is set.
8. **Submit** — Opens the submission modal, selects headshot, attaches size card/media, and submits
9. **Record** — Logs the submission to SQLite so it won't resubmit, and logs to `logs/auto_apply.log`

### Actor profile (used by AI role selection)

The AI selects roles optimized for:
- Appears 25 years old, male, white, 6'0", 185 lbs, athletic build
- Type: Leading man, comedic/charming
- Strengths: Charisma-driven roles, protagonists, romantic leads, comedy, wit
- Best fit: Confident, driven, likable, or funny characters

To change the actor profile, edit `ACTOR_PROFILE` in `src/role_selector.py`.

## File structure

```
actorsaccess/
  src/
    main.py            # Entry point, CLI args, scheduler, main loop
    browser.py         # Playwright browser automation (login, scrape, submit)
    config.py          # Config loading and defaults
    database.py        # SQLite tracking of applied roles
    filters.py         # Role/project filtering (fit-for-me, background, theater)
    role_selector.py   # AI-powered best role selection via Claude Haiku
  config.yaml          # Settings (committed — no credentials)
  config.example.yaml  # Template config
  requirements.txt     # Python dependencies
  data/applied.db      # SQLite DB tracking submissions (git-ignored)
  logs/                # Log files (git-ignored)
  tests/               # pytest tests
```

## Running as a background process on Windows

To keep it running 24/7 without a terminal window:

```bash
# Option 1: Use pythonw (no console window)
pythonw -m src.main

# Option 2: Use Windows Task Scheduler
# Create a task that runs: python -m src.main --once
# Set it to repeat every 4 hours
# Set "Start in" to the project directory
```

## Running tests

```bash
python -m pytest tests/ -v
```
