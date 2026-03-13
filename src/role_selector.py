# src/role_selector.py
"""AI-powered role selection for projects with multiple matching roles.

When a project has multiple roles that pass filters, uses Claude Haiku
to pick the single best fit based on the actor's profile.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

ACTOR_PROFILE = """
- Appears 25, plays 20-30 convincingly
- Male, White, 6'0", 185 lbs, athletic build, brown hair
- Type: Leading man, comedic/charming; also strong as villain/antagonist/mean characters
- 2 years improv training at UCB and The Groundlings
- 5+ years salsa dancing
- Comfortable with physical comedy, action sequences, and 12+ hour shoot days
- Los Angeles local with reliable transportation within the 30-mile studio zone
- Generally open availability
- No demo reel currently — still apply for roles requesting one
- No voice over / voice acting
- No UGC
- No background/extra work
- No theatre/musical work
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY set — defaulting to first role")
        return [(roles[0], "no API key, defaulted to first")], {}

    if len(roles) == 1:
        return _check_single_role_fit(roles[0], project_name, api_key)

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

Pick the ONE role that is the best fit for this actor. If a second or third role is also a genuinely strong fit, return those too — but only if they are truly well-matched. Don't force extra picks.

HARD DISQUALIFIERS — reject any role that requires:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes White
- Specific hair color that excludes brown hair (e.g., "must be blonde", "redhead only")
- Age clearly outside 18-35 range (not "to play younger")
- Skills the actor doesn't have (singing, musical instrument, specific martial art)

Also consider:
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead and supporting are both strong fits; day players are acceptable too)
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


def _check_single_role_fit(
    role: dict, project_name: str, api_key: str,
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Quick AI check: is this single role a physical/type fit?"""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        desc = role.get("description", "No description")[:500]
        prompt = f"""You are a casting assistant. Quickly decide if this actor should submit for this role.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role['role_name']}
DESCRIPTION: {desc}

HARD DISQUALIFIERS — reject if the role requires ANY of these:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes White
- Specific hair color that excludes brown hair (e.g., "must be blonde", "redhead only")
- Age clearly outside 18-35 range (not "to play younger")
- Skills the actor doesn't have (singing, musical instrument, specific martial art)

If the role is a fit or ambiguous, respond: FIT - <brief reason>
If the role is clearly not a fit, respond: SKIP - <brief reason>"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        if text.upper().startswith("SKIP"):
            reason = text.split("-", 1)[1].strip() if "-" in text else text
            logger.info(f"AI skipped single role {role['role_name']} on {project_name}: {reason}")
            return [], {role["role_name"]: reason}

        reason = text.split("-", 1)[1].strip() if "-" in text else "only matching role"
        return [(role, reason)], {}

    except Exception as e:
        logger.warning(f"AI fitness check failed ({e}), applying anyway")
        return [(role, "only matching role (AI check failed)")], {}


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

    # Cap at 3 selections
    if len(selected) > 3:
        for extra_role, extra_reason in selected[3:]:
            rejections[extra_role["role_name"]] = f"Capped at 3 selections (was: {extra_reason})"
        selected = selected[:3]

    logger.info(f"AI selected {len(selected)} role(s) for {project_name}: {[s[0]['role_name'] for s in selected]}")
    return selected, rejections


def analyze_submission_requirements(role: dict, project_name: str) -> dict:
    """Analyze role description for submission requirements.

    Returns:
        {"action": "SUBMIT" | "SUBMIT_WITH_NOTE" | "NEEDS_INPUT",
         "note": str | None,
         "needs_input_reason": str | None}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot analyze submission requirements")

    desc = role.get("description", "")
    if not desc.strip():
        return {"action": "SUBMIT", "note": None, "needs_input_reason": None}

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are analyzing a casting breakdown to determine if it asks for specific information in the submission or submission notes.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role.get('role_name', '')}
DESCRIPTION: {desc[:1000]}

Analyze the role description and determine the correct action:

1. If the description does NOT ask for any specific information in the submission notes, respond:
   ACTION: SUBMIT

2. If the description asks for information you CAN answer from the actor profile (e.g., location/local hire, availability for long days, improv/dance skills, physical attributes, transportation), respond:
   ACTION: SUBMIT_WITH_NOTE
   NOTE: <1-2 sentence note in first person addressing what they asked for. Be specific and concise. No greeting, sign-off, or placeholders.>

3. If the description asks for information you CANNOT answer (e.g., specific date availability, links to demo reel or website, union status/SAG-AFTRA number, specific wardrobe sizes, COVID test results, references, self-tape samples), respond:
   ACTION: NEEDS_INPUT
   REASON: <brief description of what info is needed>

IMPORTANT RULES:
- If they ask for a demo reel or reel link, respond with ACTION: SUBMIT (apply anyway, do not mention lack of reel)
- If multiple requirements exist and you can answer SOME but not all, use NEEDS_INPUT
- When in doubt between SUBMIT and SUBMIT_WITH_NOTE, prefer SUBMIT
- Only use SUBMIT_WITH_NOTE when the casting post clearly asks for specific info in notes

Respond with ONLY the action line (and NOTE/REASON line if applicable). No other text."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    return _parse_analysis_response(text, role, project_name)


def _parse_analysis_response(text: str, role: dict, project_name: str) -> dict:
    """Parse the AI analysis response into a structured result."""
    lines = text.strip().split("\n")
    action_line = lines[0].strip()

    if "NEEDS_INPUT" in action_line:
        reason = None
        for line in lines[1:]:
            if line.strip().upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
                break
        if not reason:
            reason = "Casting post requires information not in actor profile"
        logger.info(f"Flagging {role.get('role_name', '')} on {project_name}: {reason}")
        return {"action": "NEEDS_INPUT", "note": None, "needs_input_reason": reason}

    if "SUBMIT_WITH_NOTE" in action_line:
        note = None
        for line in lines[1:]:
            if line.strip().upper().startswith("NOTE:"):
                note = line.split(":", 1)[1].strip()
                break
        if note:
            logger.info(f"Generated note for {role.get('role_name', '')} on {project_name}: {note}")
            return {"action": "SUBMIT_WITH_NOTE", "note": note, "needs_input_reason": None}
        # Couldn't parse note, fall through to SUBMIT
        logger.warning(f"SUBMIT_WITH_NOTE but no note parsed for {role.get('role_name', '')} on {project_name}")

    return {"action": "SUBMIT", "note": None, "needs_input_reason": None}
