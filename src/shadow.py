# src/shadow.py
"""Shadow-evaluation wrapper for Claude API calls.

Wraps every Anthropic Claude call site in src/role_selector.py so we can
observe DeepSeek-chat and DeepSeek-reasoner responses side-by-side without
changing production behavior. Claude remains the decision-maker.

DeepSeek calls run in a module-level ThreadPoolExecutor and write rows to
the shadow_comparisons table. Failures in the background never bubble up
to the caller.

Kill switch:
    - DEEPSEEK_API_KEY unset           → pass-through to Claude, no DB writes.
    - SHADOW_ENABLED=0                 → pass-through to Claude, no DB writes.

Design spec: docs/superpowers/specs/2026-05-11-deepseek-shadow-eval-design.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Shared threadpool for background DeepSeek calls. Created once at import so
# flush_pending_shadows() can drain across multiple wrapper invocations.
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="shadow")

# Path resolution mirrors the rest of the codebase: hardcoded relative path,
# overridable via env var (used by tests via set_db_path()).
_db_path = "data/applied.db"


def set_db_path(path: str) -> None:
    """Override the DB path. Used by tests."""
    global _db_path
    _db_path = path


def get_db_path() -> str:
    return _db_path


# ---------------------------------------------------------------------------
# Platform / mode / run_id context
# ---------------------------------------------------------------------------
#
# Entry-point scripts (src/main.py, src/cn/main.py, src/backstage/main.py) set
# the current platform/mode/run_id once per run via set_run_context() or the
# current_platform() context manager, so the role_selector functions don't
# need to thread these arguments through every signature.

_current_platform: str | None = None
_current_mode: str | None = None
_current_run_id: int | None = None


def set_run_context(
    *,
    platform: str | None,
    mode: str | None,
    run_id: int | None = None,
) -> None:
    """Set the platform/mode/run_id stamped onto every shadow row this run."""
    global _current_platform, _current_mode, _current_run_id
    _current_platform = platform
    _current_mode = mode
    _current_run_id = run_id


def clear_run_context() -> None:
    """Clear the current run context."""
    set_run_context(platform=None, mode=None, run_id=None)


def get_run_context() -> tuple[str | None, str | None, int | None]:
    """Return (platform, mode, run_id) — used by shadowed_completion as defaults."""
    return _current_platform, _current_mode, _current_run_id


@contextmanager
def current_platform(
    platform: str,
    mode: str,
    run_id: int | None = None,
):
    """Context manager: set platform/mode/run_id for the duration of the block."""
    prev = get_run_context()
    set_run_context(platform=platform, mode=mode, run_id=run_id)
    try:
        yield
    finally:
        set_run_context(platform=prev[0], mode=prev[1], run_id=prev[2])


# ---------------------------------------------------------------------------
# Verdict extractors
# ---------------------------------------------------------------------------

_SELECTED_RE = re.compile(r"^\s*SELECTED:\s*(\d+)\b", re.IGNORECASE | re.MULTILINE)
_REJECTED_RE = re.compile(r"^\s*REJECTED:\s*(\d+)\b", re.IGNORECASE | re.MULTILINE)
_SKIP_HEAD_RE = re.compile(r"^\s*SKIP\b", re.IGNORECASE)
_ACTION_RE = re.compile(r"^\s*ACTION:\s*(\w+)", re.IGNORECASE | re.MULTILINE)


def _extract_select_best_roles(text: str) -> str | None:
    """Return 'SELECTED:{1,3}|REJECTED:{2,4}' with sorted ints, or 'SKIP'."""
    if text is None:
        return None
    if _SKIP_HEAD_RE.match(text):
        return "SKIP"
    selected = sorted({int(m.group(1)) for m in _SELECTED_RE.finditer(text)})
    rejected = sorted({int(m.group(1)) for m in _REJECTED_RE.finditer(text)})
    if not selected and not rejected:
        return None
    sel_part = "{" + ",".join(str(i) for i in selected) + "}"
    rej_part = "{" + ",".join(str(i) for i in rejected) + "}"
    return f"SELECTED:{sel_part}|REJECTED:{rej_part}"


def _extract_first_word(text: str, allowed: tuple[str, ...]) -> str | None:
    """Find the first line whose first alphanumeric token is in `allowed`.

    Tolerates leading punctuation/whitespace/markdown so 'FIT - reason',
    '**FIT**', '- FIT', etc. all parse to FIT.
    """
    if text is None:
        return None
    for raw in text.splitlines():
        stripped = re.sub(r"^[\W_]+", "", raw).upper()
        for token in allowed:
            if stripped.startswith(token):
                return token
    return None


def _extract_single_fit(text: str) -> str | None:
    return _extract_first_word(text, ("FIT", "SKIP"))


def _extract_partial_availability(text: str) -> str | None:
    return _extract_first_word(text, ("PROCEED", "SKIP"))


def _extract_prescreen(text: str) -> str | None:
    """Pass-through: the call site computes 'answered' / 'needs_input' and
    feeds it in via the prompt's response text. We accept the verdict as-is.

    For shadow comparison we simply normalize: a non-empty 'answered'/'needs_input'
    keyword found in the response wins, otherwise None.
    """
    if text is None:
        return None
    lowered = text.strip().lower()
    # If the caller pre-classified, they may pass exactly 'answered' or 'needs_input'.
    if lowered in ("answered", "needs_input"):
        return lowered
    # Heuristic: presence of 'needs_input' anywhere → needs_input, else if it
    # looks like a JSON answer payload → answered.
    if "needs_input" in lowered:
        return "needs_input"
    if '"answers"' in lowered or "'answers'" in lowered:
        return "answered"
    return None


def _extract_submission_requirements(text: str) -> str | None:
    """Return the first ACTION: value if it's one of the accepted actions."""
    if text is None:
        return None
    m = _ACTION_RE.search(text)
    if not m:
        return None
    action = m.group(1).upper()
    if action in ("SUBMIT", "SUBMIT_WITH_NOTE", "NEEDS_INPUT"):
        return action
    return None


def _extract_cover_letter(text: str) -> str | None:
    """Free-form output — no verdict comparison."""
    return None


VERDICT_EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "select_best_roles": _extract_select_best_roles,
    "single_fit": _extract_single_fit,
    "partial_availability": _extract_partial_availability,
    "prescreen": _extract_prescreen,
    "submission_requirements": _extract_submission_requirements,
    "cover_letter": _extract_cover_letter,
}


# ---------------------------------------------------------------------------
# DeepSeek HTTP client
# ---------------------------------------------------------------------------

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TIMEOUT = 60


def call_deepseek(
    model: str, prompt: str, max_tokens: int, api_key: str
) -> tuple[str, int | None, int | None]:
    """Call DeepSeek's OpenAI-compatible chat completions endpoint.

    Returns (text, prompt_tokens, completion_tokens). Raises on HTTP /
    network / parse failure — caller handles. No retries.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=DEEPSEEK_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    return text, usage.get("prompt_tokens"), usage.get("completion_tokens")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _insert_partial_row(
    *,
    call_site: str,
    platform: str,
    mode: str,
    project_name: str | None,
    role_name: str | None,
    run_id: int | None,
    prompt: str,
    claude_response: str,
    claude_verdict: str | None,
    claude_latency_ms: int,
    claude_input_tokens: int | None,
    claude_output_tokens: int | None,
) -> int:
    """Insert the claude_* half of the row and return its id."""
    conn = sqlite3.connect(_db_path, check_same_thread=False)
    try:
        cursor = conn.execute(
            """INSERT INTO shadow_comparisons (
                   run_id, platform, mode, call_site, project_name, role_name,
                   prompt_hash, prompt_text,
                   claude_response, claude_verdict, claude_latency_ms,
                   claude_input_tokens, claude_output_tokens
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, platform, mode, call_site, project_name, role_name,
                _prompt_hash(prompt), prompt,
                claude_response, claude_verdict, claude_latency_ms,
                claude_input_tokens, claude_output_tokens,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _update_deepseek_half(
    *,
    row_id: int,
    prefix: str,  # 'ds_chat' or 'ds_reasoner'
    match_column: str,  # 'chat_matches_claude' or 'reasoner_matches_claude'
    response: str | None,
    verdict: str | None,
    latency_ms: int | None,
    input_tokens: int | None,
    output_tokens: int | None,
    error: str | None,
    claude_verdict: str | None,
) -> None:
    """UPDATE the DeepSeek half of an existing row under BEGIN IMMEDIATE.

    Each background thread gets its own sqlite3 connection so the executor's
    8 workers don't share state. BEGIN IMMEDIATE acquires the write lock
    early to serialize concurrent updaters on the same row.
    """
    # matches_claude is NULL when either verdict is None (free-form / unparseable).
    if verdict is None or claude_verdict is None:
        matches = None
    else:
        matches = 1 if verdict == claude_verdict else 0

    conn = sqlite3.connect(_db_path, check_same_thread=False, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"""UPDATE shadow_comparisons
                SET {prefix}_response = ?,
                    {prefix}_verdict = ?,
                    {prefix}_latency_ms = ?,
                    {prefix}_input_tokens = ?,
                    {prefix}_output_tokens = ?,
                    {prefix}_error = ?,
                    {match_column} = ?
                WHERE id = ?""",
            (response, verdict, latency_ms, input_tokens, output_tokens,
             error, matches, row_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Background DeepSeek task
# ---------------------------------------------------------------------------


def _run_deepseek_task(
    *,
    row_id: int,
    model: str,
    prefix: str,
    match_column: str,
    prompt: str,
    max_tokens: int,
    api_key: str,
    extract_verdict: Callable[[str], str | None] | None,
    claude_verdict: str | None,
) -> None:
    """Run a single DeepSeek call and write its half of the row.

    All exceptions are caught and recorded as ds_*_error. Never raises.
    """
    started = time.monotonic()
    try:
        text, prompt_tokens, completion_tokens = call_deepseek(
            model, prompt, max_tokens, api_key
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        verdict = None
        if extract_verdict is not None:
            try:
                verdict = extract_verdict(text)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[SHADOW] %s verdict extractor crashed: %s", prefix, e
                )
                verdict = None
        _update_deepseek_half(
            row_id=row_id,
            prefix=prefix,
            match_column=match_column,
            response=text,
            verdict=verdict,
            latency_ms=latency_ms,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            error=None,
            claude_verdict=claude_verdict,
        )
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        err_msg = f"{type(e).__name__}: {e}"
        logger.warning("[SHADOW] %s call failed: %s", model, err_msg)
        try:
            _update_deepseek_half(
                row_id=row_id,
                prefix=prefix,
                match_column=match_column,
                response=None,
                verdict=None,
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
                error=err_msg,
                claude_verdict=claude_verdict,
            )
        except Exception as db_err:  # noqa: BLE001
            logger.error(
                "[SHADOW] failed to record %s error to DB: %s",
                prefix, db_err,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _shadow_enabled() -> bool:
    if os.environ.get("SHADOW_ENABLED") == "0":
        return False
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return False
    return True


def shadowed_completion(
    prompt: str,
    *,
    call_site: str,
    max_tokens: int,
    claude_client,
    claude_model: str = "claude-sonnet-4-6",
    extract_verdict: Callable[[str], str | None] | None,
    platform: str | None = None,
    mode: str | None = None,
    project_name: str | None = None,
    role_name: str | None = None,
    run_id: int | None = None,
) -> str:
    """Call Claude synchronously, fire DeepSeek shadows in background, return Claude's text.

    See module docstring for the kill switch. DeepSeek failures never bubble
    up to the caller — they land in ds_*_error.

    platform/mode/run_id default to the values set by set_run_context() (or
    current_platform() context manager) when not passed explicitly.
    """
    # Fill in any missing context from the module-level run context.
    if platform is None or mode is None or run_id is None:
        ctx_platform, ctx_mode, ctx_run_id = get_run_context()
        if platform is None:
            platform = ctx_platform
        if mode is None:
            mode = ctx_mode
        if run_id is None:
            run_id = ctx_run_id
    # Call Claude synchronously. If this raises, it propagates — exactly the
    # behavior the caller had before the wrapper existed.
    claude_started = time.monotonic()
    response = claude_client.messages.create(
        model=claude_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    claude_latency_ms = int((time.monotonic() - claude_started) * 1000)
    claude_text = response.content[0].text

    if not _shadow_enabled():
        return claude_text

    # Compute Claude's verdict synchronously so each background task knows
    # whether its DeepSeek verdict matches.
    claude_verdict: str | None = None
    if extract_verdict is not None:
        try:
            claude_verdict = extract_verdict(claude_text)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[SHADOW] claude verdict extractor crashed (%s): %s",
                call_site, e,
            )

    usage = getattr(response, "usage", None)
    claude_input_tokens = getattr(usage, "input_tokens", None) if usage else None
    claude_output_tokens = getattr(usage, "output_tokens", None) if usage else None

    # Insert partial row. If the DB insert fails (e.g. table missing), log and
    # bail — we still return Claude's text. We never want shadow to crash prod.
    try:
        row_id = _insert_partial_row(
            call_site=call_site,
            platform=platform,
            mode=mode,
            project_name=project_name,
            role_name=role_name,
            run_id=run_id,
            prompt=prompt,
            claude_response=claude_text,
            claude_verdict=claude_verdict,
            claude_latency_ms=claude_latency_ms,
            claude_input_tokens=claude_input_tokens,
            claude_output_tokens=claude_output_tokens,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[SHADOW] failed to insert partial row: %s", e)
        return claude_text

    api_key = os.environ["DEEPSEEK_API_KEY"]

    # Fire two background tasks (deepseek-chat and deepseek-reasoner).
    for model, prefix, match_col in (
        ("deepseek-chat", "ds_chat", "chat_matches_claude"),
        ("deepseek-reasoner", "ds_reasoner", "reasoner_matches_claude"),
    ):
        _executor.submit(
            _run_deepseek_task,
            row_id=row_id,
            model=model,
            prefix=prefix,
            match_column=match_col,
            prompt=prompt,
            max_tokens=max_tokens,
            api_key=api_key,
            extract_verdict=extract_verdict,
            claude_verdict=claude_verdict,
        )

    return claude_text


def flush_pending_shadows(timeout: float = 60.0) -> None:
    """Wait for all in-flight background DeepSeek tasks to complete.

    Called at the end of each --once invocation so background work doesn't
    get killed mid-flight on process exit. After this returns, the executor
    is re-initialized so subsequent calls (e.g. in long-running daemons or
    tests) still work.
    """
    global _executor
    old = _executor
    # Block new submissions, drain, then replace.
    old.shutdown(wait=True, cancel_futures=False)
    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="shadow")
