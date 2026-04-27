"""Searchable submission archive — renders every record from the DB as a single
self-contained HTML page that gets attached to each daily digest email.

Open the attachment on a phone, type a role/project/CD name in the search box
at the top, and rows filter live as you type. Useful for answering questions
like "did the bot ever apply to the Jordan Self Tapes role on AA?" without
needing to dig through Gmail.
"""

import html
from collections import Counter


_PLATFORM_LABELS = {"aa": "Actors Access", "cn": "Casting Networks", "backstage": "Backstage"}
_PLATFORM_BADGE_COLORS = {"aa": "#1565c0", "cn": "#6a1b9a", "backstage": "#e65100"}
_TYPE_BADGE_COLORS = {
    "applied": "#2e7d32",
    "draft": "#558b2f",
    "flagged": "#7c4dff",
    "rejected": "#e65100",
}
_TYPE_LABELS = {
    "applied": "APPLIED",
    "draft": "DRAFT",
    "flagged": "FLAGGED",
    "rejected": "PASSED",
}


def render_archive_html(records: list[dict], generated_at: str) -> str:
    """Render every submission record as a self-contained, searchable HTML page.

    `records` should be the output of `Database.get_all_submission_records()`.
    `generated_at` is a human-readable timestamp shown in the header.
    """
    type_counts = Counter(r.get("record_type", "?") for r in records)
    platform_counts = Counter(r.get("platform", "?") for r in records)

    rows_html = "\n".join(_render_row(r) for r in records)
    if not records:
        rows_html = (
            '<tr><td colspan="5" class="empty">'
            'No submission records yet. The archive will populate as the bot runs.'
            '</td></tr>'
        )

    summary_chips = []
    for kind in ("applied", "draft", "flagged", "rejected"):
        count = type_counts.get(kind, 0)
        if count:
            color = _TYPE_BADGE_COLORS[kind]
            label = _TYPE_LABELS[kind]
            summary_chips.append(
                f'<span class="chip" style="background:{color};">{label} {count}</span>'
            )
    for plat, count in sorted(platform_counts.items()):
        color = _PLATFORM_BADGE_COLORS.get(plat, "#666")
        label = _PLATFORM_LABELS.get(plat, plat.upper())
        summary_chips.append(
            f'<span class="chip" style="background:{color};">{html.escape(label)} {count}</span>'
        )
    summary_html = " ".join(summary_chips) or '<span class="chip" style="background:#999;">empty</span>'

    return _PAGE_TEMPLATE.format(
        generated_at=html.escape(generated_at),
        total=len(records),
        summary=summary_html,
        rows=rows_html,
    )


def _render_row(record: dict) -> str:
    record_type = record.get("record_type") or "applied"
    type_color = _TYPE_BADGE_COLORS.get(record_type, "#666")
    type_label = _TYPE_LABELS.get(record_type, record_type.upper())

    platform = record.get("platform") or "aa"
    platform_color = _PLATFORM_BADGE_COLORS.get(platform, "#666")
    platform_label = _PLATFORM_LABELS.get(platform, platform.upper())

    project_name = html.escape(record.get("project_name") or "(unknown project)")
    project_url = record.get("project_url") or ""
    if project_url:
        project_html = f'<a href="{html.escape(project_url, quote=True)}" target="_blank">{project_name}</a>'
    else:
        project_html = project_name

    role_name = html.escape(record.get("role_name") or "(unknown role)")
    description = (record.get("role_description") or "").strip()
    reason = (record.get("reason") or "").strip()
    note = (record.get("submission_note") or "").strip()
    mode = record.get("mode") or ""
    date_iso = record.get("date_iso") or ""

    details_blocks = []
    if reason:
        reason_label = "Flag reason" if record_type == "flagged" else (
            "Rejection reason" if record_type == "rejected" else "AI reason"
        )
        details_blocks.append(
            f'<div class="reason"><strong>{reason_label}:</strong> {html.escape(reason)}</div>'
        )
    if note:
        note_label = "Suggested cover letter" if record_type == "flagged" else "Submission note"
        details_blocks.append(
            f'<div class="note"><strong>{note_label}:</strong> {html.escape(note)}</div>'
        )
    if description:
        details_blocks.append(
            '<details><summary>Role description</summary>'
            f'<div class="desc">{html.escape(description)}</div>'
            '</details>'
        )
    details_html = "".join(details_blocks)

    mode_chip = f'<span class="mode">{html.escape(mode)}</span>' if mode else ""

    return f"""<tr class="record">
  <td class="date">{html.escape(date_iso[:19])}</td>
  <td><span class="badge" style="background:{type_color};">{type_label}</span></td>
  <td><span class="badge" style="background:{platform_color};">{html.escape(platform_label)}</span>{mode_chip}</td>
  <td class="project">{project_html}</td>
  <td class="role"><div class="rolename">{role_name}</div>{details_html}</td>
</tr>"""


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Submissions Archive</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 16px; background: #fafafa; color: #222; }}
  header {{ position: sticky; top: 0; background: #fafafa; padding-bottom: 12px;
           border-bottom: 1px solid #ddd; z-index: 10; margin-bottom: 16px; }}
  h1 {{ margin: 0 0 4px 0; font-size: 20px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 8px; }}
  .chip {{ display: inline-block; color: #fff; font-size: 12px; font-weight: 600;
          padding: 2px 8px; border-radius: 10px; margin-right: 4px; margin-bottom: 4px; }}
  #search {{ width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 16px;
            border: 1px solid #bbb; border-radius: 6px; margin-top: 8px; }}
  #count {{ font-size: 12px; color: #666; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #eee;
            vertical-align: top; font-size: 14px; }}
  th {{ background: #f0f0f0; font-size: 12px; text-transform: uppercase; color: #555; }}
  td.date {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
             white-space: nowrap; color: #555; }}
  td.project a {{ color: #1565c0; text-decoration: none; }}
  td.project a:hover {{ text-decoration: underline; }}
  .badge {{ display: inline-block; color: #fff; font-size: 11px; font-weight: 700;
           padding: 2px 6px; border-radius: 3px; }}
  .mode {{ display: inline-block; font-size: 11px; color: #555; margin-left: 6px;
          font-style: italic; }}
  .rolename {{ font-weight: 600; margin-bottom: 4px; }}
  .reason, .note {{ font-size: 13px; color: #444; margin-top: 4px; }}
  details {{ margin-top: 4px; }}
  summary {{ cursor: pointer; font-size: 12px; color: #888; }}
  .desc {{ white-space: pre-wrap; font-size: 13px; color: #444; padding: 6px 0; }}
  .empty {{ text-align: center; padding: 32px; color: #888; font-style: italic; }}
  tr.hidden {{ display: none; }}
  @media (max-width: 700px) {{
    table, thead, tbody, tr, td {{ display: block; }}
    thead {{ display: none; }}
    tr.record {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
                  padding: 12px; margin-bottom: 10px; }}
    td {{ border: none; padding: 4px 0; }}
    td.date {{ font-size: 11px; color: #888; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Submissions Archive</h1>
  <div class="meta">{total} records &middot; generated {generated_at}</div>
  <div>{summary}</div>
  <input id="search" type="search" placeholder="Search role, project, casting director, description..." autocomplete="off">
  <div id="count"></div>
</header>
<table>
  <thead>
    <tr>
      <th>Date (UTC)</th>
      <th>Status</th>
      <th>Platform</th>
      <th>Project</th>
      <th>Role &amp; details</th>
    </tr>
  </thead>
  <tbody id="rows">
{rows}
  </tbody>
</table>
<script>
(function() {{
  var input = document.getElementById('search');
  var rows = document.querySelectorAll('tr.record');
  var count = document.getElementById('count');
  function update() {{
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {{
      var r = rows[i];
      if (!q || r.textContent.toLowerCase().indexOf(q) !== -1) {{
        r.classList.remove('hidden');
        shown++;
      }} else {{
        r.classList.add('hidden');
      }}
    }}
    count.textContent = q ? (shown + ' of ' + rows.length + ' matching') : '';
  }}
  input.addEventListener('input', update);
}})();
</script>
</body>
</html>
"""
