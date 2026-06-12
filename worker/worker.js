// One-click "Apply anyway" worker.
//
// The digest email links each role's "Apply anyway" button here with signed
// query params. A single GET creates the override issue in the private repo
// server-side (using a stored fine-grained PAT), so the user never sees
// GitHub's "Submit new issue" step. The created issue is identical to the one
// the old two-click flow produced, so the bot's existing ingest path picks it
// up unchanged on the next run.
//
// Required bindings (see wrangler.toml + `wrangler secret put`):
//   OVERRIDE_REPO    var    e.g. "powellm4/aa-overrides"
//   OVERRIDE_LABEL   var    e.g. "apply-anyway"
//   GITHUB_TOKEN     secret fine-grained PAT, Issues: Read & Write on the repo
//   SIGNING_SECRET   secret shared with the digest (OVERRIDE_SIGNING_SECRET)

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/apply") {
      return htmlPage("Not found", false, null, 404);
    }
    // Only act on GET. Some link prefetchers use HEAD/OPTIONS; never create an
    // issue for those.
    if (request.method !== "GET") {
      return htmlPage("Method not allowed", false, null, 405);
    }

    const q = url.searchParams;
    const platform = q.get("platform") || "";
    const mode = q.get("mode") || "";
    const project = q.get("p") || "";
    const role = q.get("r") || "";
    const sig = q.get("sig") || "";

    if (!platform || !mode || !project || !role || !sig) {
      return htmlPage("This link is missing information and can't be used.", false);
    }

    const expected = await sign(env.SIGNING_SECRET, [platform, mode, project, role].join("\n"));
    if (!timingSafeEqual(expected, sig)) {
      return htmlPage("This link is invalid (signature mismatch).", false);
    }

    const title = `Apply anyway: ${role} @ ${project}`;
    const body =
      `project_name: ${project}\n` +
      `role_name: ${role}\n` +
      `platform: ${platform}\n` +
      `mode: ${mode}\n`;

    let resp;
    try {
      resp = await fetch(`https://api.github.com/repos/${env.OVERRIDE_REPO}/issues`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "actorsaccess-apply-worker",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ title, body, labels: [env.OVERRIDE_LABEL] }),
      });
    } catch (e) {
      return htmlPage(`Couldn't reach GitHub to queue this role. ${e}`, false);
    }

    if (!resp.ok) {
      const detail = await resp.text();
      return htmlPage(`GitHub rejected the request (HTTP ${resp.status}).\n${detail}`, false);
    }

    const issue = await resp.json();
    return htmlPage(
      `Queued — "${role}" on ${project} will be applied on the next ${mode} run.`,
      true,
      issue.html_url,
    );
  },
};

async function sign(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Constant-time compare of two hex strings.
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function htmlPage(message, ok, link, status = 200) {
  const accent = ok ? "#2e7d32" : "#b71c1c";
  const heading = ok ? "Queued ✓" : "Couldn't queue this role";
  const linkHtml = link
    ? `<p style="margin-top:16px;"><a href="${escapeHtml(link)}" style="color:#1565c0;">View the issue on GitHub</a></p>`
    : "";
  const html =
    `<!doctype html><html><head><meta charset="utf-8">` +
    `<meta name="viewport" content="width=device-width,initial-scale=1">` +
    `<meta name="robots" content="noindex">` +
    `<title>${escapeHtml(heading)}</title></head>` +
    `<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;` +
    `max-width:520px;margin:48px auto;padding:24px;text-align:center;">` +
    `<h1 style="color:${accent};font-size:22px;">${escapeHtml(heading)}</h1>` +
    `<p style="color:#444;white-space:pre-wrap;font-size:15px;">${escapeHtml(message)}</p>` +
    linkHtml +
    `</body></html>`;
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
