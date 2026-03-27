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
- Appears 25, plays 20-30 most convincingly, but can stretch to 35
- Male, White, 6'0", 185 lbs, athletic build, brown hair (thick, slightly curly, short-medium length), clean-shaven (no beard)
- Instagram: @marshallpowell
- Type: Leading man, comedic/charming; also strong as villain/antagonist/mean characters; also excels at grounded/calm/straight man/voice-of-reason roles
- 2 years improv training at UCB and The Groundlings
- 5+ years salsa dancing
- Plays guitar well
- American accent (can play accents but not authentic/native non-American accents)
- Comfortable with physical comedy, action sequences, and 12+ hour shoot days
- Shoe size: 9-10
- Non-union
- Email: REDACTED | Phone: REDACTED
- Based in Los Angeles with reliable transportation; willing to travel to other cities for shoots
- Generally open availability
- No demo reel currently — still apply for roles requesting one
- Open to voice over / voice acting roles
- No UGC
- No background/extra work
- No theatre/musical work
"""

# Location keywords → pay tier (case-insensitive matching against project name + description + project notes)
_LA_AREA_KEYWORDS = [
    "los angeles", "l.a.", "burbank", "pasadena", "long beach", "orange county",
    "glendale", "culver city", "hollywood", "santa monica", "west hollywood",
    "studio city", "van nuys", "north hollywood", "encino", "sherman oaks",
    "woodland hills", "chatsworth", "northridge", "anaheim", "irvine",
    "palos verdes", "el segundo", "marina del rey", "downtown la", "dtla",
    "socal", "so cal", "southern california",
]
_SHORT_DRIVE_KEYWORDS = ["san diego", "palm springs", "santa barbara", "bakersfield"]
_MEDIUM_DRIVE_KEYWORDS = [
    "las vegas", "vegas", "san francisco", "phoenix", "tucson", "arizona",
    "scottsdale", "tempe", "mesa, az", "reno",
]
_FLY_TO_KEYWORDS = [
    "new york", "nyc", "atlanta", "chicago", "vancouver", "toronto",
    "miami", "dallas", "houston", "boston", "seattle", "portland, or",
    "philadelphia", "detroit", "denver", "nashville", "new orleans",
    "wilmington, nc", "wilmington nc", "pittsburgh", "minneapolis",
    "washington, dc", "d.c.", "st. louis", "charlotte", "orlando",
    "savannah", "albuquerque", "salt lake", "honolulu", "hawaii",
    "italy", "china", "london", "paris", "australia", "canada",
    "mexico", "spain", "germany", "japan", "korea", "india",
    "bari, italy", "beijing",
]


def _extract_total_pay(text: str) -> float | None:
    """Try to extract a numeric pay amount from text. Returns estimated total or None."""
    text_lower = text.lower()

    # Look for per-day rates: "$X/day", "$X per day"
    m = re.search(r'\$[\s]*([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*day', text_lower)
    if m:
        per_day = float(m.group(1).replace(",", ""))
        # Try to find number of days
        days_m = re.search(r'(\d+)\s*(?:days?|shoot days?)', text_lower)
        if days_m:
            return per_day * int(days_m.group(1))
        return per_day  # conservative: assume 1 day

    # Look for hourly rates: "$X/hour", "$X/hr", "$X per hour"
    m = re.search(r'\$[\s]*([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*(?:hour|hr)', text_lower)
    if m:
        per_hour = float(m.group(1).replace(",", ""))
        return per_hour * 8  # assume 8-hour day

    # Look for flat amounts: "$X", "$X total"
    amounts = re.findall(r'\$([\d,]+(?:\.\d+)?)', text)
    if amounts:
        return max(float(a.replace(",", "")) for a in amounts)

    return None


def check_travel_pay(project_name: str, role_description: str = "", project_notes: str = "") -> tuple[bool, str | None]:
    """Programmatic travel pay check. Returns (should_apply, rejection_reason).

    Returns (True, None) if the role passes or location/pay can't be determined.
    Returns (False, reason) if the role clearly violates travel pay minimums.
    """
    combined = f"{project_name} {role_description} {project_notes}".lower()

    # Determine location tier
    tier = None
    matched_location = None

    for kw in _LA_AREA_KEYWORDS:
        if kw in combined:
            tier = "la"
            matched_location = kw
            break

    if tier is None:
        for kw in _FLY_TO_KEYWORDS:
            if kw in combined:
                tier = "fly"
                matched_location = kw
                break

    if tier is None:
        for kw in _MEDIUM_DRIVE_KEYWORDS:
            if kw in combined:
                tier = "medium"
                matched_location = kw
                break

    if tier is None:
        for kw in _SHORT_DRIVE_KEYWORDS:
            if kw in combined:
                tier = "short"
                matched_location = kw
                break

    # If we can't determine location, don't reject
    if tier is None or tier == "la":
        return True, None

    # Try to extract pay
    pay = _extract_total_pay(f"{role_description} {project_notes}")
    if pay is None:
        return True, None  # can't determine pay, don't reject

    thresholds = {"short": 250, "medium": 500, "fly": 1000}
    threshold = thresholds[tier]

    if pay < threshold:
        tier_label = {"short": "short drive", "medium": "medium drive", "fly": "fly-to"}[tier]
        reason = f"Travel pay too low: ${pay:.0f} for {tier_label} location ({matched_location}), minimum ${threshold}"
        logger.info(f"[TRAVEL PAY] Rejecting: {reason}")
        return False, reason

    return True, None


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
        logger.warning("No ANTHROPIC_API_KEY set — skipping all roles to be safe")
        rejections = {r["role_name"]: "no API key, skipped" for r in roles}
        return [], rejections

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

Select ALL roles that are a reasonable fit for this actor. Only reject roles that have a genuine disqualifier. Being a day player or lower pay is NOT a reason to reject — submit for every role the actor could realistically book.

IMPORTANT: If ANY role description says "submit for only one role", "one submission per actor", or similar, you MUST select only ONE role (the best fit) no matter what.

HARD DISQUALIFIERS — reject any role that requires:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes White
- Specific hair color that is NOT brown ONLY when stated as a casting requirement (e.g., "must have blonde hair", "redhead required"). The actor has BROWN hair. Character descriptions like "dyed hair" or "punk vibes" are styling choices that can be achieved — these are NOT disqualifiers.
- Age range with NO overlap with 18-35 (e.g., "40-55" is a rejection, but "30-40" is NOT because it overlaps with the actor's range)
- Skills the actor doesn't have (singing, musical instrument, specific martial art)
- Requires an authentic/native non-American accent (e.g., "must have authentic British accent", "native French speaker")
- Requires a beard or facial hair (actor is clean-shaven)
- NOT a real acting or modeling role — consumer studies, product testing, paid research studies, focus groups, medical studies, or any role where participants are selected based on personal conditions (skin conditions, health issues, etc.) rather than acting/modeling ability. IMPORTANT: Modeling gigs, lifestyle content shoots, brand campaigns, fitness shoots, and photo/video commercial work ARE legitimate — do NOT reject these. The actor wants modeling work. Only reject actual research studies, focus groups, and medical studies.
- Background/extra work disguised as a named role — roles like "MASKED AUDIENCE MEMBERS", "PARTYGOERS", "CROWD", or any role where the actor is essentially atmosphere/background with no lines or character arc
- "Must look" or "play younger" age requirements below 18 — the actor appears 25 and can play 20-30. He CANNOT convincingly look like a teenager (13-17) regardless of "18+ to play younger" casting language. If the role needs someone who looks like a minor (child, teen, high schooler), SKIP it.
- Requires the actor to OWN something specific that is not a standard wardrobe/prop item — e.g., "must have a dog", "must own a motorcycle", "bring your own surfboard", "real couples only". If the casting post requires the actor to personally possess something (pet, vehicle, relationship, property) as a condition of the role, SKIP it. Standard wardrobe items (suit, business casual, etc.) are NOT disqualifiers.

NOTE: For doubles, stand-ins, or photo doubles, physical specs (height, hair color, build) are EXACT requirements — any mismatch is a hard disqualifier.

TRAVEL PAY MINIMUMS — the actor is based in Los Angeles. Apply these rules based on the shoot location mentioned in the role or project description:
- LA area (Los Angeles, Burbank, Pasadena, Long Beach, Orange County, etc.): any pay is fine — NEVER reject an LA role for low pay, even $50
- Short drive (San Diego, Palm Springs, Santa Barbara, Bakersfield): SKIP if total pay is under $250
- Medium drive (Las Vegas, San Francisco, Phoenix, Tucson, Arizona generally, NV/AZ): SKIP if total pay is under $500
- Fly-to locations (New York, Atlanta, Chicago, Vancouver, or anywhere requiring air travel): SKIP if total pay is under $1000
- If the shoot location is not mentioned or unclear, do NOT reject based on pay
- "Total pay" means the full amount for the job, not per day. If the listing says "$200/day" for a 3-day shoot, total pay is $600 — that passes the $500 medium-drive threshold. For hourly rates, assume an 8-hour day (e.g., "$150/hour" = $1,200/day). If the total pay clearly exceeds the threshold, do NOT reject

Also consider (but these are NOT reasons to reject — only to rank):
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead, supporting, day player — all acceptable)
4. Overall castability — would this actor realistically be considered?

If NONE of the roles are a good fit, respond with SKIP on the first line and explain why.

Respond with this exact format (one line per role):
SELECTED: <number> - <explain WHY this role is a good fit>
REJECTED: <number> - <explain WHY this role is not a fit>

IMPORTANT: The reason must explain WHY, not just restate the role name. Bad: "HARRISON". Good: "Age and type match for athletic leading man".

Example:
SELECTED: 1 - Age and type match for athletic leading man
SELECTED: 3 - Age range fits, playboy type works for actor's look
REJECTED: 2 - Requires heavyset build, actor is athletic
REJECTED: 4 - Background/extra role, actor does not do background work"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        logger.debug(f"[AI] Raw response for {project_name}: {text}")
        return _parse_structured_response(text, roles, project_name)

    except Exception as e:
        logger.warning(f"AI role selection failed ({e}), skipping all to be safe")
        rejections = {r["role_name"]: f"AI failed ({e}), skipped" for r in roles}
        return [], rejections


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

HARD DISQUALIFIERS — SKIP if the role requires ANY of these:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes White
- Specific hair color that is NOT brown ONLY when stated as a casting requirement (e.g., "must have blonde hair", "redhead required"). The actor has BROWN hair. Character descriptions like "dyed hair" or "punk vibes" are styling choices that can be achieved — these are NOT disqualifiers.
- Age range with NO overlap with 18-35 (e.g., "40-55" is a rejection, but "30-40" is NOT because it overlaps with the actor's range)
- Skills the actor doesn't have (singing, musical instrument, specific martial art)
- Requires an authentic/native non-American accent (e.g., "must have authentic British accent", "native French speaker")
- Requires a beard or facial hair (actor is clean-shaven)
- NOT a real acting or modeling role — consumer studies, product testing, paid research studies, focus groups, medical studies, or any role where participants are selected based on personal conditions (skin conditions, health issues, etc.) rather than acting/modeling ability. IMPORTANT: Modeling gigs, lifestyle content shoots, brand campaigns, fitness shoots, and photo/video commercial work ARE legitimate — do NOT reject these. The actor wants modeling work. Only reject actual research studies, focus groups, and medical studies.
- Background/extra work disguised as a named role — roles like "MASKED AUDIENCE MEMBERS", "PARTYGOERS", "CROWD", or any role where the actor is essentially atmosphere/background with no lines or character arc
- "Must look" or "play younger" age requirements below 18 — the actor appears 25 and can play 20-30. He CANNOT convincingly look like a teenager (13-17) regardless of "18+ to play younger" casting language. If the role needs someone who looks like a minor (child, teen, high schooler), SKIP it.
- Requires the actor to OWN something specific that is not a standard wardrobe/prop item — e.g., "must have a dog", "must own a motorcycle", "bring your own surfboard", "real couples only". If the casting post requires the actor to personally possess something (pet, vehicle, relationship, property) as a condition of the role, SKIP it. Standard wardrobe items (suit, business casual, etc.) are NOT disqualifiers.

NOTE: For doubles, stand-ins, or photo doubles, physical specs (height, hair color, build) are EXACT requirements — any mismatch is a hard disqualifier.

TRAVEL PAY MINIMUMS — the actor is based in Los Angeles. Apply these rules based on the shoot location mentioned in the role or project description:
- LA area (Los Angeles, Burbank, Pasadena, Long Beach, Orange County, etc.): any pay is fine — NEVER reject an LA role for low pay, even $50
- Short drive (San Diego, Palm Springs, Santa Barbara, Bakersfield): SKIP if total pay is under $250
- Medium drive (Las Vegas, San Francisco, Phoenix, Tucson, Arizona generally, NV/AZ): SKIP if total pay is under $500
- Fly-to locations (New York, Atlanta, Chicago, Vancouver, or anywhere requiring air travel): SKIP if total pay is under $1000
- If the shoot location is not mentioned or unclear, do NOT reject based on pay
- "Total pay" means the full amount for the job, not per day. If the listing says "$200/day" for a 3-day shoot, total pay is $600 — that passes the $500 medium-drive threshold. For hourly rates, assume an 8-hour day (e.g., "$150/hour" = $1,200/day). If the total pay clearly exceeds the threshold, do NOT reject

If the role is a fit or ambiguous, respond: FIT - <brief reason>
If the role is clearly not a fit, respond: SKIP - <brief reason>"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        logger.debug(f"[AI] Raw fitness check for {role['role_name']} on {project_name}: {text}")
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

    # Fallback: no SELECTED lines found — skip all rather than blindly submit
    if not selected:
        logger.warning(f"AI returned unparseable response for {project_name}: {text[:200]}")
        fallback_reason = "AI returned unparseable response, skipped to be safe"
        rejections = {r["role_name"]: fallback_reason for r in roles}
        return [], rejections

    # Fill in any roles not mentioned in rejections
    selected_names = {s[0]["role_name"] for s in selected}
    for role in roles:
        if role["role_name"] not in selected_names and role["role_name"] not in rejections:
            rejections[role["role_name"]] = "not mentioned by AI"

    logger.info(f"AI selected {len(selected)} role(s) for {project_name}: {[s[0]['role_name'] for s in selected]}")
    return selected, rejections


def check_partial_availability(
    role: dict, project_name: str, project_notes: str, partial_info: dict,
) -> dict:
    """Ask AI if a role can work with partial date availability.

    Args:
        role: Role dict with role_name, description.
        project_name: Name of the project.
        project_notes: Full project notes text.
        partial_info: Dict with keys: shoot_range, busy_dates, free_dates,
                      free_count, total_days.

    Returns:
        {"proceed": True/False, "reason": str}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[CALENDAR] No API key for partial availability check, skipping project")
        return {"proceed": False, "reason": "No API key for partial availability check"}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        free_str = ", ".join(partial_info["free_dates"])
        busy_str = ", ".join(partial_info["busy_dates"])

        prompt = f"""You are a casting assistant. An actor has a partial calendar conflict with a project's shoot dates. Decide if they should still submit.

PROJECT: {project_name}
SHOOT DATE RANGE: {partial_info['shoot_range']}
ACTOR'S BUSY DATES: {busy_str}
ACTOR'S FREE DATES: {free_str} ({partial_info['free_count']} of {partial_info['total_days']} days)

PROJECT NOTES (relevant excerpt):
{project_notes[:2000]}

ROLE: {role.get('role_name', '')}
DESCRIPTION: {role.get('description', '')[:500]}

Should the actor submit for this role given their partial availability?

Consider:
- If the role only requires 1-2 days and there are enough free days, PROCEED
- If phrases like "1 day expected", "you may only work one day", or specific single dates appear, PROCEED
- If the role requires ALL shoot days or most of them, SKIP
- If the project notes say "must be available for all dates", SKIP
- When in doubt and the actor has more free days than busy days, PROCEED

Respond with exactly one line:
PROCEED - <brief reason>
or
SKIP - <brief reason>"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        logger.debug(f"[CALENDAR] Partial availability AI response: {text}")

        if text.upper().startswith("PROCEED"):
            reason = text.split("-", 1)[1].strip() if "-" in text else "partial availability acceptable"
            logger.info(f"[CALENDAR] AI says PROCEED for {role.get('role_name', '')}: {reason}")
            return {"proceed": True, "reason": reason}
        else:
            reason = text.split("-", 1)[1].strip() if "-" in text else "partial availability insufficient"
            logger.info(f"[CALENDAR] AI says SKIP for {role.get('role_name', '')}: {reason}")
            return {"proceed": False, "reason": reason}

    except Exception as e:
        logger.warning(f"[CALENDAR] Partial availability check failed ({e}), skipping to be safe")
        return {"proceed": False, "reason": f"AI check failed: {e}"}


def analyze_submission_requirements(role: dict, project_name: str, project_notes: str = "", confirmed_dates: str | None = None) -> dict:
    """Analyze role description and project-level notes for submission requirements.

    Args:
        confirmed_dates: If set (e.g., "2026-04-12 to 2026-04-25"), calendar has already been
            checked and actor is available. The AI can reference these dates in notes.

    Returns:
        {"action": "SUBMIT" | "SUBMIT_WITH_NOTE" | "NEEDS_INPUT",
         "note": str | None,
         "needs_input_reason": str | None}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot analyze submission requirements")

    desc = role.get("description", "")
    if not desc.strip() and not project_notes.strip():
        return {"action": "SUBMIT", "note": None, "needs_input_reason": None}

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    availability_context = ""
    if confirmed_dates:
        availability_context = f"\nCONFIRMED AVAILABILITY: Actor's calendar has been checked and is FREE for {confirmed_dates}. ONLY include this in the note if the post EXPLICITLY asks for availability (e.g., 'note your availability', 'confirm dates'). Do NOT volunteer availability if not asked."

    prompt = f"""You are analyzing a casting breakdown to determine if it asks for specific information in the submission or submission notes.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role.get('role_name', '')}
DESCRIPTION: {desc[:1000]}
{f"PROJECT-LEVEL INSTRUCTIONS: {project_notes[:2000]}" if project_notes.strip() else ""}{availability_context}

Analyze BOTH the role description AND any project-level instructions to determine the correct action:

1. If the description does NOT ask for any specific information in the submission notes, respond:
   ACTION: SUBMIT

2. If the description EXPLICITLY asks for specific factual information in the submission notes that you CAN answer from the actor profile or confirmed availability (e.g., location/local hire, availability/dates, comfort with long days, shoe size, phone number, email address, contact info), respond:
   ACTION: SUBMIT_WITH_NOTE
   NOTE: <1 sentence, max 30 words, in first person. ONLY state the facts they asked for. No selling, no qualifications, no acting skills, no why-you're-right-for-the-role.>

3. If the description asks for information you CANNOT answer (e.g., links to demo reel or website, union status/SAG-AFTRA number, specific wardrobe sizes, COVID test results, references, self-tape samples), respond:
   ACTION: NEEDS_INPUT
   REASON: <brief description of what info is needed>

IMPORTANT RULES:
- ONLY use SUBMIT_WITH_NOTE when the post has a clear, explicit, DIRECT request for information IN the submission notes — e.g., "note your availability in your submission", "include your contact info in notes", "let us know about X experience"
- Statements like "LOCAL SELF-REPORT TO SET", "Los Angeles Local Hire", or "must have reliable transportation" are REQUIREMENTS, not requests for notes. They describe who should apply, not what to write. Respond with ACTION: SUBMIT
- If the post asks "are you local hire?" or "note if you are local" for a NON-Los Angeles location (e.g., NYC, Atlanta, Chicago), respond that you are willing to travel / available to work on location. Do NOT say "LA local" — say you can travel to that city. Example: "Available to work in NYC for the shoot dates." Never claim to be local to a city you're not in.
- A role description describing the character is NOT a request for info — do not respond to it
- ABSOLUTELY NEVER include acting skills, improv training, UCB, Groundlings, dance experience, guitar, or ANY training/experience in submission notes. These are NEVER relevant to submission notes regardless of what the casting asks. If a casting says "let us know about your experience" — respond with ACTION: SUBMIT (no note). The actor's profile/resume already covers this.
- ABSOLUTELY NEVER fabricate ANY claims not explicitly stated in the actor profile above. This includes: product usage, brand loyalty, past roles, past projects, acting credits, specific experience (e.g., "I've played a lead in a micro-series"), personal stories, or hobbies. If a casting asks "have you done X?" or "let us know if you've done X" and the answer is not in the actor profile, respond with ACTION: SUBMIT (no note). Making false claims is worse than no note at all. The ONLY facts you may state are those listed in the actor profile above.
- BAD note examples (NEVER do these): "I have improv training from UCB", "I'm a devoted TheraBreath user", "I've played lead roles in micro-series", "I have 5+ years of dance experience". These are all WRONG — either fabricated or volunteering unrequested info.
- If a casting asks for Instagram, include @marshallpowell
- NEVER volunteer information that was not explicitly asked for — no location, no transportation, no contact info, no availability unless the post explicitly asks you to NOTE it in the submission
- If the post asks for availability/dates and CONFIRMED AVAILABILITY is provided above, include the specific dates in the note
- If they ask for contact info, provide the email and phone number from the profile
- If they ask for a demo reel or reel link, respond with ACTION: SUBMIT (apply anyway, do not mention lack of reel)
- If multiple requirements exist and you can answer SOME but not all, use NEEDS_INPUT — UNLESS the unanswerable item is a demo reel (apply anyway)
- When in doubt between SUBMIT and SUBMIT_WITH_NOTE, ALWAYS prefer SUBMIT
- Treat project-level REQUIREMENTS (e.g., "NOTE YOUR DETAILED AVAILABILITY") with the same weight as role-level requests — these apply to every role submission
- "PLEASE INCLUDE SIZE CARDS" or "include size card" means wardrobe measurements — these are handled in the actor's Actors Access profile, NOT in submission notes. Respond with ACTION: SUBMIT
- Do NOT combine multiple unrelated facts into one note (e.g., don't add shoe size + transportation + availability when only one thing was asked for). Answer ONLY what was explicitly requested.

Respond with ONLY the action line (and NOTE/REASON line if applicable). No other text."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    logger.debug(f"[ANALYSIS] Raw response for {role.get('role_name', '')} on {project_name}: {text}")
    result = _parse_analysis_response(text, role, project_name)

    # Validate note quality before allowing submission with note
    if result["action"] == "SUBMIT_WITH_NOTE" and result["note"]:
        if not _validate_note(result["note"], role, project_name):
            result["action"] = "SUBMIT"
            result["note"] = None

    return result


def _validate_note(note: str, role: dict, project_name: str) -> bool:
    """Validate a generated note before submission. Returns False if the note is bad."""
    import re

    # Reject bracket placeholders like [partner's name], [your name], etc.
    if re.search(r'\[.*?\]', note):
        logger.warning(f"Rejected note with placeholder brackets for {role.get('role_name', '')} on {project_name}: {note}")
        return False

    # Reject curly brace placeholders like {name}, {{name}}
    if re.search(r'\{.*?\}', note):
        logger.warning(f"Rejected note with curly brace placeholder for {role.get('role_name', '')} on {project_name}: {note}")
        return False

    # Reject template/filler language
    filler_patterns = [
        r'\b(TBD|TBA|N/?A|TODO|FIXME)\b',
        r'\b(insert|fill in|replace with|your .* here)\b',
    ]
    for pattern in filler_patterns:
        if re.search(pattern, note, re.IGNORECASE):
            logger.warning(f"Rejected note with filler language for {role.get('role_name', '')} on {project_name}: {note}")
            return False

    # Reject notes that mention training/skills (prompt already forbids this)
    skill_patterns = [
        r'\b(UCB|Groundlings|improv|salsa|guitar)\b',
    ]
    for pattern in skill_patterns:
        if re.search(pattern, note, re.IGNORECASE):
            logger.warning(f"Rejected note mentioning training/skills for {role.get('role_name', '')} on {project_name}: {note}")
            return False

    return True


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

    logger.info(f"[ANALYSIS] Plain SUBMIT for {role.get('role_name', '')} on {project_name} — no specific info requested")
    return {"action": "SUBMIT", "note": None, "needs_input_reason": None}
