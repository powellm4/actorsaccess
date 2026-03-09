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


def select_best_role(roles: list[dict], project_name: str) -> tuple[dict, str]:
    """Pick the best role from a list of matching roles.

    If only one role, returns it directly (no API call).
    If multiple roles, uses Claude Haiku to select the best fit.
    Falls back to the first role if the API call fails.

    Args:
        roles: List of role dicts that already passed filters.
        project_name: Name of the project (for context).

    Returns:
        Tuple of (best role dict, AI reasoning string).
    """
    if len(roles) == 1:
        return roles[0], "only matching role"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set — defaulting to first role")
        return roles[0], "no API key, defaulted to first"

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

        prompt = f"""You are a casting assistant helping an actor decide which single role to submit for on a project.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}

AVAILABLE ROLES:
{chr(10).join(role_options)}

Pick the ONE role that is the best fit for this actor. Consider:
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead > supporting > day player)
4. Overall castability — would this actor realistically be considered?

If NONE of the roles are a good fit, respond with 0 on the first line and explain why.

Respond with the role number on the first line (or 0 to skip), then a brief reason on the second line.
Example:
2
Best physical and type match for leading man."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        lines = text.split("\n", 1)
        # Extract the number from the first line
        num_match = re.search(r'\d+', lines[0])
        if not num_match:
            logger.warning(f"AI returned no number: {text[:100]}")
            return roles[0], "AI returned invalid response, defaulted to first"
        choice = int(num_match.group())
        reason = lines[1].strip() if len(lines) > 1 else ""

        if choice == 0:
            logger.info(f"AI skipped project: {reason}")
            return None, reason

        choice -= 1  # Convert to 0-indexed
        if 0 <= choice < len(roles):
            selected = roles[choice]
            logger.info(
                f"AI selected role: {selected['role_name']} — {reason}"
            )
            return selected, reason

        logger.warning(f"AI returned invalid choice {choice + 1}, defaulting to first")
        return roles[0], "AI returned invalid choice, defaulted to first"

    except Exception as e:
        logger.warning(f"AI role selection failed ({e}), defaulting to first role")
        return roles[0], f"AI failed ({e}), defaulted to first"


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
