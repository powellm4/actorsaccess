# Prepare-Only Draft Feature — Follow-up Audit (2026-04-27)

Feature shipped: commits 62fd0d4 / 8d35425 / efd6c23 on master, 2026-04-24.

---

## Tooling constraint — why logs could not be read

The `gh` CLI is **not installed** in this Claude Code session, and the GitHub
MCP tool set does not include Actions API endpoints (no `list_workflow_runs`,
`get_workflow_run_logs`, or `download_artifact`).  The local git proxy
(`127.0.0.1:33459`) accepts only git operations and returns "Invalid path
format" for REST API calls.

**Consequence:** individual run logs could not be grepped for the
`[PREPARE-ONLY]` / `Generated cover letter` markers.  Everything below is
derived from source inspection and the `db-storage` release metadata.

---

## What is confirmed

### Workflows are running

The `db-storage` release asset `applied.db` was last uploaded at
**2026-04-27T16:53:32Z** (today) by `github-actions[bot]`, confirming the
workflow is executing normally.

**Estimated run count since 2026-04-24:**

| Workflow | Schedule | Est. runs since 2026-04-24 |
|---|---|---|
| `auto-apply.yml` (paid) | 6×/day at 15,18,21,00,03,06 UTC | ~20 |
| `auto-apply-unpaid.yml` (unpaid) | 1×/day at 16 UTC | ~4 |
| **Total** | | **~24** |

### Feature code is present and correct

All expected log markers are confirmed in source at the right call sites:

| Marker | File:line | Fires when |
|---|---|---|
| `[REQUIRES] <role>: cover letter required … — preparing draft only` | `src/backstage/main.py:468` | `"5" in submission_requires` |
| `[PREPARE-ONLY] Draft <id> ready for <role> on <project>` | `src/backstage/main.py:665` | Draft POST succeeded, `result["_prepared"]` is true |
| `[PREPARE-ONLY] REJECTED by Backstage for <role>: <reason>` | `src/backstage/main.py:696` | Backstage returns a rejection on the draft POST |
| `[PREPARE-ONLY] Draft preparation failed for <role>` | `src/backstage/main.py:704` | Any other non-success result |
| `Generated cover letter for <role> on <project> (<N> chars)` | `src/role_selector.py:994` | AI call succeeded and `_validate_note` passed |
| `Cover letter generation returned empty text for <role> on <project>` | `src/role_selector.py:984` | Claude returned blank |
| `Cover letter failed validation for <role> on <project>` | `src/role_selector.py:991` | `_validate_note` blocked the output |
| `ANTHROPIC_API_KEY not set — cannot generate cover letter` | `src/role_selector.py:934` | Env var missing (should not happen in prod) |

`ANTHROPIC_API_KEY` is set as a repo secret and injected in both workflows'
`Run Backstage auto-apply` step — the credential-missing path should never
fire in production.

---

## What could not be determined

- **Zero or more drafts prepared?** — unknown; requires grepping run logs.
- **Which role/project names hit the cover-letter path?** — unknown.
- **Cover-letter char counts?** — unknown.

---

## How to check from your own terminal

```bash
# List the 20 most recent paid-mode runs
gh run list --workflow=auto-apply.yml --limit 20

# List the most recent unpaid runs
gh run list --workflow=auto-apply-unpaid.yml --limit 10

# Stream the full log for a specific run and grep for all prepare-only markers
gh run view <RUN_ID> --log | grep -E \
  "\[PREPARE-ONLY\]|Generated cover letter|Cover letter failed|returned empty text|ANTHROPIC_API_KEY not set"

# Or download the log artifact directly (retained 90 days)
gh run download <RUN_ID> --name "run-logs-<RUN_NUMBER>" --dir /tmp/logs
grep -rE "\[PREPARE-ONLY\]|Generated cover letter|Cover letter failed|returned empty text" /tmp/logs/
```

Artifact names follow the pattern:
- `run-logs-{run_number}` (paid)
- `run-logs-unpaid-{run_number}` (unpaid)

---

## Possible reasons zero drafts prepared (if that turns out to be the case)

1. **No cover-letter-required roles surfaced yet** — Backstage casting
   directors don't always set `submission_requires = ["5"]`; it depends on
   the project.  The prepare-only path only fires when `"5"` is explicitly
   present in that field for the winning role.
2. **Winning roles always have a non-cover-letter requirement** — roles that
   require only headshot/reel/resume (codes 0/1/4) go through the normal
   submit path; code 5 is the only one that routes to `prepare_only`.
3. **No Backstage roles matched at all** during a given run** — if fitness
   scoring filtered all candidates out, `submit_for_role` is never called.
4. **AI cover letter generation failed silently** — if Claude returned empty
   or `_validate_note` rejected the output, `suggested_cover_letter` would be
   `""` but the draft would still be prepared (the cover letter is advisory,
   not a gate).
