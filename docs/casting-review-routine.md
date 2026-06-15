# Casting digest review routine

A recurring **Claude routine** that reads the daily casting digest emails this app sends,
sanity-checks **which roles got submitted to and why**, catches **missing submission
requirements** and recurring failure patterns, then **emails you the findings** and
**opens draft PRs** for concrete, low-risk improvements.

It runs entirely inside Claude (Desktop/web "Routines") using your connected **Gmail** and
**GitHub** — no GitHub Actions workflow, secrets, or extra API cost on this repo. This file
is the source of truth for the routine; if you tweak the prompt, update it here too.

---

## What it reviews

The digest is built by `src/digest.py` and emailed daily (paid up to 6×/day, unpaid 1×/day)
with stable, searchable subjects:

- `Casting Digest (Paid) — <Mon DD, YYYY>`
- `Casting Digest (UNPAID) — <Mon DD, YYYY>`

Each email already carries the "why" behind every decision:

| Section | Key fields | What to check |
| --- | --- | --- |
| **Applied** | `ai_reason`, `candidates_considered`, `submission_note` | Was this a good fit? Is the reason sound? Was a custom note required and supplied? |
| **Passed** | `rejection_reason` (bucketed: gender/age/build/ethnicity/travel-pay/skill/missing-materials/other) | Any clearly strong role wrongly passed (false negative)? |
| **Needs Attention / Calendar Conflicts** | `flag_reason`, `suggested_note` | Recurring blockers (missing self-tape, conflicts)? |
| **Apply-Anyway results** + **run summary** | per-platform `roles_found/applied/skipped`, `error_message` | Platform failures or zero-result runs? |

For deeper history the routine can open the attached `submissions-archive.html` or, if
`ARCHIVE_SITE_URL` is configured, the live archive site (every applied/drafted/flagged/
passed role to date).

---

## System map — where to propose fixes

When the routine identifies an improvement, these are the files to edit (draft PR only):

- **Actor profile, hard disqualifiers, travel-pay thresholds** — `src/role_selector.py`
  - `ACTOR_PROFILE` (lines 49–76) — physical type, skills, languages, union, exclusions.
  - Travel-pay thresholds (`check_travel_pay` at line 251; tiers `{short:250, medium:500,
    fly:1000}` at line 331).
  - Disqualifier prompt blocks (the `{ACTOR_PROFILE}`-bearing prompts at ~478, 597, 883).
  - `analyze_submission_requirements` (line 1000) — how required materials/notes are judged.
- **Project/role filters & accepted role types** — `src/filters.py`
  - `project_matches` (268), `role_matches` (303), `is_lead_or_supporting` (194),
    `ACCEPTED_ROLE_TYPES` (126).
- **Per-platform / per-mode config** — `config.yaml`, `config_unpaid.yaml`,
  `cn_config.yaml`, `cn_config_unpaid.yaml`, `backstage_config.yaml`,
  `backstage_config_unpaid.yaml` (regions, gender, age range, union, paying-only,
  submission materials, default note).

The actor profile (for reference while judging fit): appears 25 / plays 17–30, male,
White/Latino, 6'0", athletic, brown hair, clean-shaven, bilingual English/Spanish (American
English accent only — Spanish is the exception), improv (UCB/Groundlings), salsa, guitar,
sings, LA-based with reliable transport, non-union, no demo reel yet (still apply), no
VO/UGC/background/theatre.

---

## Setup

1. In Claude (Desktop or web) open **Routines → Create routine**.
2. **Connectors:** enable **Gmail** (read) and **GitHub** (write access to
   `powellm4/actorsaccess`).
3. **Schedule:** daily, late evening (≈30–60 min after the last digest cron — the paid
   digest's final run is `0 6 * * *` UTC, i.e. 11pm PT; an early-morning local run also
   works).
4. **Prompt:** paste the block below verbatim.
5. **Test before trusting it:** run it once manually with the connectors on and confirm it
   finds the latest digest, sends a coherent findings email, and (if there's an obvious
   issue) opens a *draft* PR touching only an allowed file. Then leave it scheduled.
6. To pause/edit later: Routines → select this routine → edit or disable.

---

## Routine prompt (paste verbatim)

```
You are reviewing the daily "casting digest" emails produced by the actorsaccess
auto-apply bot. Your job: judge how well it applied to roles today, catch missing
submission requirements, and propose concrete low-risk improvements.

STEP 1 — GATHER
- In Gmail, search: subject:"Casting Digest" newer_than:1d
- Read every matching email body (both "Casting Digest (Paid)" and
  "Casting Digest (UNPAID)"). If a digest links a submissions archive
  (ARCHIVE_SITE_URL) or attaches submissions-archive.html, open it for fuller
  history when you need context.
- If there are no digests in the last day, send a one-line "no digests today"
  email and stop.

STEP 2 — ANALYZE (the actor: appears 25 / plays 17-30, male, White/Latino, 6'0",
athletic, brown hair, clean-shaven, bilingual English/Spanish [American English
accent only; Spanish is the exception], improv/salsa/guitar/sings, LA-based,
non-union, no demo reel yet so still applies for roles requesting one; no
VO/UGC/background/theatre).
  a) Submission accuracy ("roles submitted to and why"): for each APPLIED role,
     judge whether the ai_reason is sound and the role actually fits the profile
     (age, height, build, ethnicity/look, union, gender, language/accent, travel
     pay). Flag poor-fit submissions.
  b) False negatives: for PASSED roles, flag any that clearly fit but were
     rejected; name the rejection bucket and why it looks wrong.
  c) Missing submission requirements: flag APPLIED or FLAGGED roles where required
     materials weren't satisfied (self-tape, reel, sides, a specifically requested
     note, SAG status). Prioritize RECURRING patterns (e.g. many flags for missing
     self-tape) over one-offs.
  d) Reliability: note platform run errors or zero-result runs in the summary.

STEP 3 — EMAIL THE FINDINGS
- Send me (the account owner) an email, subject "Casting Review — <today's date>".
- Keep it concise: top issues, recurring patterns, and recommended changes with
  the specific file each maps to (see the system map). Group by accuracy /
  missing-requirements / reliability. If nothing is actionable, say so in one line.

STEP 4 — PROPOSE CHANGES (draft PR, only when concrete and low-risk)
- Repo: powellm4/actorsaccess, base branch: master. Open changes as DRAFT pull
  requests. One PR per distinct improvement; link each PR in the email.
- Where to edit:
  * Actor profile / disqualifiers / travel-pay thresholds / submission-requirement
    logic -> src/role_selector.py (ACTOR_PROFILE ~L49-76; thresholds ~L331;
    analyze_submission_requirements ~L1000).
  * Project/role filters & accepted role types -> src/filters.py
    (project_matches, role_matches, is_lead_or_supporting, ACCEPTED_ROLE_TYPES).
  * Search/submission settings -> config.yaml, config_unpaid.yaml, cn_config.yaml,
    cn_config_unpaid.yaml, backstage_config.yaml, backstage_config_unpaid.yaml.
- GUARDRAILS: draft PRs only; never merge. Never edit .github/workflows/**,
  secrets, or the database. If a change is ambiguous, large, or would alter
  shared/production behavior, describe it in the email instead of opening a PR.
```

---

## Notes & future enhancements

- The routine reads the rendered HTML email, so it depends on the digest layout staying
  roughly stable. If parsing ever gets flaky, consider adding a small machine-readable
  summary block to the digest (mirroring how `src/shadow_report.render_digest_block`
  injects a block) so the routine has structured input.
- A self-contained GitHub Action that reads `data/applied.db` directly was considered as an
  alternative path but intentionally deferred in favor of this built-in routine.
