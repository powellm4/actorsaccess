# One-click "Apply anyway" worker

A tiny Cloudflare Worker that turns the digest's **Apply anyway** button into a
single tap. Without it, the button opens GitHub's pre-filled new-issue page and
you still have to press **Submit new issue**. With it, one tap creates the
override issue server-side and shows a "Queued ✓" page — the bot then picks the
issue up on the next run exactly as before.

## How it works

The digest signs each button's URL with an HMAC (shared `OVERRIDE_SIGNING_SECRET`)
and points it at this worker. The worker verifies the signature, creates the
issue in the override repo using a stored fine-grained PAT, and returns a
confirmation page. The signature means only links the digest generated can queue
a role — nobody can forge new ones.

The created issue is byte-for-byte the same as the old two-click flow produced
(`project_name` / `role_name` / `platform` / `mode` body + `apply-anyway`
label), so nothing downstream changes.

## One-time deploy

Prereqs: a free [Cloudflare account](https://dash.cloudflare.com/sign-up) and
`npm i -g wrangler` (then `wrangler login`).

1. **Pick a signing secret** (any long random string), e.g.:
   ```bash
   openssl rand -hex 32
   ```
   You'll set this in two places below, and they must match.

2. **From this `worker/` directory, set the worker's secrets:**
   ```bash
   cd worker
   wrangler secret put GITHUB_TOKEN     # paste the OVERRIDE_GITHUB_TOKEN PAT (Issues: R/W on aa-overrides)
   wrangler secret put SIGNING_SECRET   # paste the random string from step 1
   ```
   (`OVERRIDE_REPO` / `OVERRIDE_LABEL` are already set as plain vars in
   `wrangler.toml` — edit them there if your repo/label differ.)

3. **Deploy:**
   ```bash
   wrangler deploy
   ```
   Note the URL it prints, e.g. `https://aa-apply.<your-subdomain>.workers.dev`.

4. **Tell the digest where the worker lives.** In `config.yaml`, under
   `overrides:`, add:
   ```yaml
   overrides:
     repo: "powellm4/aa-overrides"
     label: "apply-anyway"
     apply_url: "https://aa-apply.<your-subdomain>.workers.dev/apply"
   ```

5. **Give the digest the same signing secret.** Set `OVERRIDE_SIGNING_SECRET` to
   the step-1 value:
   - Locally: export it in your shell / `.env`.
   - In CI: add `OVERRIDE_SIGNING_SECRET` as a GitHub Actions repo secret (the
     digest steps in both workflows already pass it through).

That's it. The next digest's buttons become one-click. If `apply_url` or
`OVERRIDE_SIGNING_SECRET` is missing, the digest automatically falls back to the
old two-click GitHub link, so it degrades safely.

## Notes

- **Prefetch:** a single GET creates the issue, so an aggressive email scanner
  that follows links could queue a role you passed on. Low harm — the bot
  no-ops if it's already applied, and any queue shows up in the digest's
  "Awaiting Processing" section. (The worker ignores non-GET methods, which
  blocks the HEAD-style prefetchers.)
- **Duplicates:** tapping a button twice creates two issues, same as the old
  flow; the bot closes the one it queued and the duplicate can be closed by
  hand.
