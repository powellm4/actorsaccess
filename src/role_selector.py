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
- Appears 25 years old
- Male, White, 6'0", 185 lbs, athletic build
- Type: Leading man, comedic/charming
- Strengths: Charisma-driven roles, protagonists, romantic leads, action heroes, comedy, wit
- Best fit: Characters who are confident, driven, likable, or funny — the guy the audience roots for
- Less ideal: Character roles requiring much older/younger appearance, heavy dramatic trauma-only pieces, or physically mismatched descriptions (very short, very thin, very heavy, etc.)
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
            role_options.append(
                f"{i + 1}. {role['role_name']}: {role.get('description', 'No description')[:500]}"
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

Respond with the role number on the first line, then a brief reason on the second line.
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
        choice = int(num_match.group()) - 1
        reason = lines[1].strip() if len(lines) > 1 else ""

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
