# ActorsAccess Auto-Apply

Automated tool that logs into [Actors Access](https://actorsaccess.com), scrapes casting breakdowns, and submits for roles that match the user's profile. Uses Playwright for browser automation.

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

# 5. Create config from the example
cp config.example.yaml config.yaml
```

### Configure `config.yaml`

Edit `config.yaml` and fill in:

```yaml
credentials:
  username: "your_actorsaccess_username"  # NOT your email — your AA username
  password: "your_password"

filters:
  region: "Los Angeles"        # See REGIONS list in src/browser.py for options
  gender: "male"
  age_range: [25, 35]
  union_status: "nonunion_fit" # Options: all, union, non-union, fit_for_me, union_fit, nonunion_fit
  paying_only: false
  exclude_reality_tv: true

submission:
  headshot_index: 0            # Which headshot to use (0 = first)
  include_media: true          # Include demo reel/media
  include_size_card: true      # Include size card
  default_note: ""             # Optional note to casting directors

max_pages: 2                   # Pages of breakdowns to process per run (~25 projects/page)

schedule:
  interval_hours: 4            # How often to run (when using scheduler mode)

browser:
  headless: true               # true = no visible browser window

logging:
  level: "INFO"
  file: "logs/auto_apply.log"
```

**`union_status` options explained:**
| Value | Dropdown selection |
|-------|-------------------|
| `all` | All breakdowns |
| `union` | Union breakdowns |
| `non-union` | Non-union breakdowns |
| `fit_for_me` | Breakdowns fit for me |
| `union_fit` | Union breakdowns fit for me |
| `nonunion_fit` | Non-union breakdowns fit for me |

**Region options:** Los Angeles, New York, Chicago, San Francisco / NorCal, Central Atlantic, Midwest, New England, North Central, Northwest, Pacific, Rocky Mountains, South Central, Southeast, Vancouver, Toronto

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

Prints `[SUBMIT]` or `[SKIP - reason]` for each role without actually submitting.

### With visible browser (for debugging)

```bash
python -m src.main --once --headed
```

### Limit pages processed

```bash
python -m src.main --once --max-pages 1
```

## How it works

1. Logs into Actors Access with your credentials
2. Navigates to the Breakdowns page and applies your configured filters
3. Scrapes project listings page by page
4. For each project, scrapes individual roles
5. Skips roles the site does NOT highlight as "fit for me" (yellow background = match, white = skip)
6. Skips roles already submitted (tracked in `data/applied.db`)
7. For matching roles: opens the submission modal, selects headshot, attaches size card/media, and submits
8. Logs everything to `logs/auto_apply.log`

## File structure

```
actorsaccess/
  src/
    main.py       # Entry point, CLI args, scheduler
    browser.py    # Playwright browser automation (login, scrape, submit)
    config.py     # Config loading and defaults
    database.py   # SQLite tracking of applied roles
    filters.py    # Role filtering (fit-for-me check)
  config.yaml         # Your config (git-ignored)
  config.example.yaml # Template config
  data/applied.db     # SQLite DB tracking submissions (git-ignored)
  logs/               # Log files (git-ignored)
  tests/              # pytest tests
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
