# GitHub Release Asset for SQLite DB

## Problem

Multiple GitHub Actions workflows (AA, CN, digest) race to commit `data/applied.db` via git. Binary files can't merge, causing data loss when workflows overlap.

## Solution

Store `applied.db` as a GitHub Release asset on a permanent release (tag `db-storage`). Each workflow downloads it at start, runs, then re-uploads. No git commits of the DB.

## Implementation

### One-time setup

Create a GitHub release tagged `db-storage` with the current `applied.db` as an asset.

### Workflow changes

All 3 workflows (`auto-apply.yml`, `cn-auto-apply.yml`, `daily-digest.yml`):

1. **Before run step:** Download DB from release
   ```
   gh release download db-storage --pattern applied.db --dir data/ --clobber
   ```

2. **After run step:** Upload DB back to release
   ```
   gh release upload db-storage data/applied.db --clobber
   ```

3. **Remove** the "Commit updated database" step entirely.

4. **Add concurrency group** to all 3 workflows:
   ```yaml
   concurrency:
     group: db-access
     cancel-in-progress: false
   ```
   This queues workflows instead of running them in parallel, eliminating race conditions.

### Git cleanup

- Add `data/applied.db` to `.gitignore`
- Remove `data/applied.db` from git tracking

### Local development

No change — local runs (CN) continue using `data/applied.db` directly. The release asset is only for GitHub Actions.

## Behavior

| Scenario | Before | After |
|----------|--------|-------|
| Two workflows run simultaneously | Binary merge conflict, possible data loss | Queued via concurrency group, no conflict |
| Workflow fails mid-run | DB commit step fails, data lost | DB not uploaded, previous version preserved |
| Local development | Works with local DB | No change |
