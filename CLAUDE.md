# Working notes for Claude

## Workflow preferences

- After pushing a branch, open the required draft PR, mark it ready for review, and **squash-merge it into `master` immediately** without asking — the user doesn't want to babysit pull requests for straightforward changes. Default branch is `master`, not `main`.
- Only pause for explicit approval before merging if the change is risky, ambiguous, or touches shared/production infrastructure (workflows, secrets, DB schema, archive publishing, etc.).
