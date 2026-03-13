# src/role_selector.py
"""AI-powered role selection for projects with multiple matching roles.

When a project has multiple roles that pass filters, uses Claude Haiku
to pick the single best fit based on the actor's profile.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

ACTOR_PROFILE = """
- Appears 25 years old — very young-looking, people don't believe he's over 30. Can convincingly play 20-30.
- Male, White, 6'0", 185 lbs, athletic build
- Type: Leading man, comedic/charming
- Strengths: Charisma-driven roles, protagonists, romantic leads, action heroes, comedy, wit
- Best fit: Characters who are confident, driven, likable, or funny — the guy the audience roots for
- Age range sweet spot: STRONGLY prioritize roles casting 22-30. Roles casting 18-35 are acceptable. Roles requiring 40+ appearance are a poor fit.
- Less ideal: Roles requiring 40+, heavy dramatic trauma-only pieces, or physically mismatched descriptions (very short, very thin, very heavy, etc.)
"""


def select_best_roles(roles: list[dict], project_name: str) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Pick the best role(s) from a list of matching roles.

    If only one role, returns it directly (no API call).
    If multiple roles, uses Claude Haiku to select 1-2 best fits.
    Falls back to the first role if the API call fails.

    Args:
        roles: List of role dicts that already passed filters.
        project_name: Name of the project (for context).

    Returns:
        Tuple of:
        - List of (role dict, reason string) for selected roles
        - Dict mapping rejected role names to rejection reasons
    """
    if len(roles) == 1:
        return [(roles[0], "only matching role")], {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set — defaulting to first role")
        return [(roles[0], "no API key, defaulted to first")], {}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        # Build role summaries for the prompt
        role_options = []
        for i, role in enumerate(roles):
            meta = []
            if role.get("role_type"):
                meta.append(role["role_type"])
            if role.get("age_range"):
                meta.append(f"Age: {role['age_range']}")
            if role.get("gender"):
                meta.append(role["gender"])
            if role.get("pay"):
                meta.append(role["pay"])
            meta_str = f" ({', '.join(meta)})" if meta else ""
            role_options.append(
                f"{i + 1}. {role['role_name']}{meta_str}: {role.get('description', 'No description')[:500]}"
            )

        prompt = f"""You are a casting assistant helping an actor decide which role(s) to submit for on a project.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}

AVAILABLE ROLES:
{chr(10).join(role_options)}

Pick the ONE role that is the best fit for this actor. If a second role is also a genuinely strong fit, return it too — but only if both are truly well-matched. Don't force a second pick.

Consider:
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead > supporting > day player)
4. Overall castability — would this actor realistically be considered?

If NONE of the roles are a good fit, respond with SKIP on the first line and explain why.

Respond with this exact format (one line per role):
SELECTED: <number> - <reason for picking this role>
REJECTED: <number> - <reason for rejecting this role>

Example for selecting one role out of three:
SELECTED: 2 - Best physical and type match for leading man
REJECTED: 1 - Age range 40-50 is too old
REJECTED: 3 - Background role, not a fit"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        return _parse_structured_response(text, roles, project_name)

    except Exception as e:
        logger.warning(f"AI role selection failed ({e}), defaulting to first role")
        rejections = {r["role_name"]: f"AI failed ({e}), defaulted to first" for r in roles[1:]}
        return [(roles[0], f"AI failed ({e}), defaulted to first")], rejections


def _parse_structured_response(
    text: str, roles: list[dict], project_name: str,
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Parse the structured SELECTED/REJECTED response from the AI."""
    lines = text.strip().split("\n")

    # Check for SKIP
    if lines[0].strip().upper().startswith("SKIP"):
        skip_reason = lines[0].split("-", 1)[1].strip() if "-" in lines[0] else lines[0].strip()
        rejections = {r["role_name"]: skip_reason for r in roles}
        logger.info(f"AI skipped project {project_name}: {skip_reason}")
        return [], rejections

    selected = []
    rejections = {}
    selected_re = re.compile(r"SELECTED:\s*(\d+)\s*-\s*(.*)", re.IGNORECASE)
    rejected_re = re.compile(r"REJECTED:\s*(\d+)\s*-\s*(.*)", re.IGNORECASE)

    for line in lines:
        m = selected_re.match(line.strip())
        if m:
            idx = int(m.group(1)) - 1  # Convert to 0-indexed
            reason = m.group(2).strip()
            if 0 <= idx < len(roles):
                selected.append((roles[idx], reason))
            continue
        m = rejected_re.match(line.strip())
        if m:
            idx = int(m.group(1)) - 1
            reason = m.group(2).strip()
            if 0 <= idx < len(roles):
                rejections[roles[idx]["role_name"]] = reason

    # Fallback: no SELECTED lines found
    if not selected:
        logger.warning(f"AI returned unparseable response for {project_name}: {text[:100]}")
        fallback_reason = "AI returned unparseable response, defaulted to first"
        rejections = {r["role_name"]: fallback_reason for r in roles[1:]}
        return [(roles[0], fallback_reason)], rejections

    # Fill in any roles not mentioned in rejections
    selected_names = {s[0]["role_name"] for s in selected}
    for role in roles:
        if role["role_name"] not in selected_names and role["role_name"] not in rejections:
            rejections[role["role_name"]] = "not mentioned by AI"

    # Cap at 2 selections per spec
    if len(selected) > 2:
        for extra_role, extra_reason in selected[2:]:
            rejections[extra_role["role_name"]] = f"Capped at 2 selections (was: {extra_reason})"
        selected = selected[:2]

    logger.info(f"AI selected {len(selected)} role(s) for {project_name}: {[s[0]['role_name'] for s in selected]}")
    return selected, rejections


_NOTE_REQUEST_PATTERN = re.compile(
    r"\*?\s*note\s+(?:on|in)\s+submission|please\s+(?:include|mention|note)\s+in\s+(?:your\s+)?(?:submission|notes)",
    re.IGNORECASE,
)


def generate_submission_note(role: dict, project_name: str) -> str | None:
    """Generate a custom submission note if the role description requests one.

    Returns the note string, or None if no note is needed or generation fails.
    """
    desc = role.get("description", "")
    if not _NOTE_REQUEST_PATTERN.search(desc):
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set — skipping note generation")
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""You are writing a brief submission note for an actor applying to a casting call. The casting post asks for specific info in the submission notes.

ACTOR PROFILE:
{ACTOR_PROFILE}
ADDITIONAL INFO:
- 2 years of improv training at UCB and The Groundlings
- 5+ years of salsa dancing
- Experience with comedy, drama, and commercial work
- Comfortable with physical comedy and action sequences

PROJECT: {project_name}
ROLE: {role.get('role_name', '')}
DESCRIPTION: {desc[:800]}

Write a short, natural submission note (1-2 sentences max) addressing what the casting post asks for in the notes. Be specific but concise. Do NOT use a greeting, sign-off, or placeholders like "[Actor Name]". Write in first person. Just the relevant info they asked for."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )

        note = response.content[0].text.strip()
        logger.info(f"Generated submission note for {role.get('role_name', '')}: {note}")
        return note

    except Exception as e:
        logger.warning(f"Note generation failed ({e})")
        return None
