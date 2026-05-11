# src/role_selector.py
"""AI-powered role selection for projects with multiple matching roles.

When a project has multiple roles that pass filters, uses Claude Haiku
to pick the single best fit based on the actor's profile.
"""

import logging
import os
import re

from src.shadow import VERDICT_EXTRACTORS, shadowed_completion

logger = logging.getLogger(__name__)

# Prefix on a rejection reason that means "AI hit a transient error" — callers
# must NOT persist these to rejected_roles so the next run retries the role.
TRANSIENT_REJECTION_PREFIX = "[transient] "


def _is_transient_error(exc: Exception) -> bool:
    """True for API errors worth re-trying on the next run (overload, rate-limit, network)."""
    status = getattr(exc, "status_code", None)
    if status in (408, 409, 425, 429, 500, 502, 503, 504, 529):
        return True
    name = type(exc).__name__
    return name in (
        "APIConnectionError", "APITimeoutError", "InternalServerError",
        "RateLimitError", "ConnectionError", "Timeout", "ReadTimeout",
    )

ACTOR_PROFILE = """
- Appears 25 but people often think he's younger; plays 17-29 convincingly (yes, including 17-22 college-aged roles)
- Male, White / Latino, 6'0", 185 lbs, athletic build, brown hair (thick, slightly curly, short-medium length), clean-shaven (no beard)
- Instagram: @marshallpowell
- Type: Leading man, comedic/charming; also strong as villain/antagonist/mean characters; also excels at grounded/calm/straight man/voice-of-reason roles
- 2 years improv training at UCB and The Groundlings
- 5+ years salsa dancing
- Strong volleyball player
- Plays guitar well
- Can sing (comfortable with roles requiring singing ability)
- Fluent in Spanish (bilingual English/Spanish — can perform Spanish-speaking roles, including roles requiring native or fluent Spanish)
- American accent in English (can play accents but not authentic/native non-American English accents). Spanish is the exception: he speaks it fluently and can play roles requiring Spanish dialogue or a native Spanish speaker.
- Comfortable with physical comedy, action sequences, and 12+ hour shoot days
- Shoe size: 9-10
- Non-union
- Based in Los Angeles with reliable transportation; willing to travel to other cities for shoots
- Has a valid driver's license
- Has a current/valid US passport
- Generally open availability
- Comfortable and willing to play LGBTQ+ characters of any orientation or gender identity
- Comfortable with on-screen kissing, intimacy, and nudity (any gender of scene partner)
- No demo reel currently — still apply for roles requesting one
- Available for modeling work (print, stills, photo shoots, TFP/Time-For-Print, swimwear, beach, fitness, lifestyle, fashion, editorial, brand campaigns, portfolio shoots, catalog, lookbook). Modeling gigs do NOT have acting role types — evaluate them on physical/type fit only, not on Lead/Principal/etc. labels. Unpaid TFP shoots are acceptable.
- No voice over / voice acting roles
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
    "new york", "nyc", "brooklyn", "manhattan", "queens", "bronx",
    "staten island", "atlanta", "chicago", "vancouver", "toronto",
    "miami", "dallas", "houston", "boston", "seattle", "portland, or",
    "philadelphia", "detroit", "denver", "nashville", "new orleans",
    "wilmington, nc", "wilmington nc", "pittsburgh", "minneapolis",
    "washington, dc", "d.c.", "st. louis", "charlotte", "orlando",
    "savannah", "albuquerque", "salt lake", "honolulu", "hawaii",
    "italy", "china", "london", "paris", "australia", "canada",
    "mexico", "spain", "germany", "japan", "korea", "india",
    "bari, italy", "beijing",
]

# US states that are fly-to from LA (excludes CA, NV, AZ which are handled above).
# Full names matched as substrings; codes matched only in clear state-reference context
# (e.g., "Greenville, SC") to avoid false positives like "SC" appearing inside random text.
_FLY_TO_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arkansas": "AR", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


_PAID_TRAVEL_PAY_BLOCK = """TRAVEL PAY MINIMUMS — the actor is based in Los Angeles but is willing to work as "local hire" ANYWHERE as long as the pay meets the threshold. "Must be local hire to [city]", "local to [city]", or "must be based in [city]" are NOT disqualifiers — the actor will travel at his own expense if the total pay is high enough. Apply these rules:
- LA area (Los Angeles, Burbank, Pasadena, Long Beach, Orange County, etc.): any pay is fine — NEVER reject an LA role for low pay, even $50
- Short drive (San Diego, Palm Springs, Santa Barbara, Bakersfield): SKIP if total pay is under $250
- Medium drive (Las Vegas, San Francisco, Phoenix, Tucson, Arizona generally, NV/AZ): SKIP if total pay is under $500
- Fly-to locations (New York, Atlanta, Chicago, Vancouver, or anywhere requiring air travel): SKIP if total pay is under $1000
- If the shoot location is not mentioned or unclear, do NOT reject based on pay
- "Total pay" means the full amount for the job, not per day. If the listing says "$200/day" for a 3-day shoot, total pay is $600 — that passes the $500 medium-drive threshold. For hourly rates, assume an 8-hour day (e.g., "$150/hour" = $1,200/day). If the total pay clearly exceeds the threshold, do NOT reject

WHEN PAY CLEARS THE THRESHOLD, FIT/SELECT — these are NOT valid reasons to reject, no matter how the casting post is worded:
- "Local hire only" / "must be local to [city]" / "talent local to [city] only" / "Casting talent local to [CITY] ONLY" — the actor flies in and pays his own way; this is exactly the case the threshold rule was written for.
- The casting post does not mention travel, lodging, or relocation reimbursement — assume there is none. The actor still wants the role and will cover his own travel/accommodations.
- "The actor cannot authentically/genuinely present as a [city] local" — irrelevant. Casting is not going to vet residency; he books the job and shows up. Do not invent this concern.
- The actor lives in Los Angeles and the shoot is in another state/city — that is the entire point of the threshold rule. Do not cite it as a reason to reject.
- Acknowledging the math ("pay exceeds the threshold, however...") then rejecting anyway — there is no "however." If pay clears the threshold and the only objection is location/local-hire framing, the verdict is FIT/SELECT."""

_UNPAID_TRAVEL_BLOCK = """TRAVEL/LOCATION — the actor is based in Los Angeles. Do NOT reject based on location; the platform filter has already narrowed the pool. If the role is shooting locally in LA or LA metro (including Santa Clarita, Burbank, Long Beach, etc.), accept it."""


def _travel_pay_block(mode: str) -> str:
    """Return the travel/location guidance block for the given mode."""
    return _UNPAID_TRAVEL_BLOCK if mode == "unpaid" else _PAID_TRAVEL_PAY_BLOCK


def _extract_total_pay(text: str) -> float | None:
    """Try to extract a numeric pay amount from text. Returns estimated total or None."""
    text_lower = text.lower()

    # Treat vague/empty pay as $0 (not unknown) so travel guard can reject
    vague_pay_patterns = ["see below", "see above", "see details", "see role", "tbd", "tbr", "n/a", "negotiable", "deferred", "no pay", "unpaid", "copy/credit"]
    if any(p in text_lower for p in vague_pay_patterns):
        if not re.search(r'\$\d', text):  # no actual dollar amount alongside
            return 0

    # Helper: find number of days mentioned in text
    def _find_days() -> int | None:
        # Explicit "X days" or "Shoot Days: X" format
        days_m = re.search(r'(?:approx\.?\s*)?(?:shoot\s*)?days?(?:\s*(?:of work))?[:\s]+(\d+)', text_lower)
        if not days_m:
            days_m = re.search(r'(?:approx\.?\s*)?(\d+)\s*(?:days?|shoot\s*days?)\s*(?:of work)?', text_lower)
        if days_m:
            return int(days_m.group(1))
        # Date range: "Month D - Month D" or "Month D - D"
        from datetime import datetime
        range_m = re.search(
            r'(\w+ \d{1,2})\s*[-–—]\s*(\w+ \d{1,2}),?\s*(\d{4})',
            text,
        )
        if range_m:
            try:
                year = range_m.group(3)
                start = datetime.strptime(f"{range_m.group(1)} {year}", "%B %d %Y")
                end = datetime.strptime(f"{range_m.group(2)} {year}", "%B %d %Y")
                return max(1, (end - start).days + 1)
            except ValueError:
                pass
        return None

    # Look for per-day rates: "$X/day", "$X per day", "$X/12hr day", "$X + 10 per shoot day"
    m = re.search(r'\$[\s]*([\d,]+(?:\.\d+)?)\s*(?:\+\s*\d+\s*)?(?:/|per)\s*(?:\d+\s*hr?\s*)?(?:shoot\s*)?day', text_lower)
    if m:
        per_day = float(m.group(1).replace(",", ""))
        days = _find_days()
        if days:
            return per_day * days
        return per_day * 2  # per-day rates imply multi-day work; assume at least 2 days

    # Look for hourly rates: "$X/hour", "$X/hr", "$X per hour"
    m = re.search(r'\$[\s]*([\d,]+(?:\.\d+)?)\s*(?:/|per)\s*(?:hour|hr)', text_lower)
    if m:
        per_hour = float(m.group(1).replace(",", ""))
        return per_hour * 8  # assume 8-hour day

    # Look for "$XK" shorthand (e.g., "$1K total", "$2.5K")
    m = re.search(r'\$([\d,]+(?:\.\d+)?)\s*[kK]\b', text)
    if m:
        return float(m.group(1).replace(",", "")) * 1000

    # Look for explicit "total" amount: "$X total"
    m = re.search(r'\$([\d,]+(?:\.\d+)?)\s*total', text_lower)
    if m:
        return float(m.group(1).replace(",", ""))

    # Sum all dollar amounts (e.g., "$300 session + $700 usage" = $1000)
    amounts = re.findall(r'\$([\d,]+(?:\.\d+)?)', text)
    if amounts:
        values = [float(a.replace(",", "")) for a in amounts]
        return sum(values)

    return None


def check_travel_pay(
    project_name: str,
    role_description: str = "",
    project_notes: str = "",
    mode: str = "paid",
) -> tuple[bool, str | None]:
    """Programmatic travel pay check. Returns (should_apply, rejection_reason).

    In paid mode: returns (True, None) if the role passes or location/pay
    can't be determined, (False, reason) if pay clearly violates minimums.

    In unpaid mode: always returns (True, None). Location filtering is
    delegated to the platform's saved search (Backstage "unpaid", CN
    "unpaid") — the programmatic keyword list was too brittle and was
    incorrectly rejecting LA-metro locations like Santa Clarita, and
    matching state codes like "IN" inside ordinary phrases.
    """
    if mode == "unpaid":
        return True, None

    combined = f"{project_name} {role_description} {project_notes}".lower()

    def _match_location(keywords: list[str]) -> str | None:
        """Match location keywords using word boundaries to avoid substring false positives."""
        for kw in keywords:
            # Use word boundaries so "paris" doesn't match inside "comparisons"
            if re.search(r'\b' + re.escape(kw) + r'\b', combined):
                return kw
        return None

    # Determine location tier (check LA first — LA always passes)
    tier = None
    matched_location = _match_location(_LA_AREA_KEYWORDS)
    if matched_location:
        tier = "la"

    if tier is None:
        matched_location = _match_location(_FLY_TO_KEYWORDS)
        if matched_location:
            tier = "fly"

    if tier is None:
        matched_location = _match_location(_MEDIUM_DRIVE_KEYWORDS)
        if matched_location:
            tier = "medium"

    if tier is None:
        matched_location = _match_location(_SHORT_DRIVE_KEYWORDS)
        if matched_location:
            tier = "short"

    if tier is None:
        # Fallback: any US state outside CA/NV/AZ counts as fly-to
        for full_name, code in _FLY_TO_US_STATES.items():
            if full_name in combined:
                tier = "fly"
                matched_location = full_name
                break
            # Match codes ONLY in unambiguous "City, ST" form (comma-prefixed) to avoid
            # false positives like "shoot in LA" being read as Louisiana.
            if re.search(rf',\s*{code}\b', combined, re.IGNORECASE):
                tier = "fly"
                matched_location = code
                break

    # If we can't determine location, don't reject
    if tier is None or tier == "la":
        return True, None

    # If travel/lodging is provided, pay threshold doesn't apply
    travel_provided_patterns = [
        r'travel\s+(?:and\s+)?(?:lodging|housing|accommodations?)\s+(?:will be\s+)?provided',
        r'(?:lodging|housing|accommodations?)\s+(?:and\s+)?travel\s+(?:will be\s+)?provided',
        r'travel\s+(?:is\s+)?(?:covered|included|paid)',
        r'(?:we|production)\s+(?:will\s+)?(?:cover|provide|pay\s+for)\s+travel',
    ]
    for pattern in travel_provided_patterns:
        if re.search(pattern, combined):
            logger.info(f"[TRAVEL PAY] Travel provided for {tier} location ({matched_location}), skipping pay check")
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


# Patterns matching AI rejection reasoning that boils down to "you're not local
# to the shoot city." The actor is happy to travel as a local hire when pay
# clears the threshold, so these reasons should not stand on their own.
_LOCAL_HIRE_REJECTION_PATTERNS = [
    r"\blocal hire\b",
    r"\blocal to\b",
    r"\blocal talent\b",
    r"\blocal (?:only|hire only)\b",
    r"\bmust be local\b",
    r"\bmust be based\b",
    r"\bbased in [A-Z]",  # "based in Los Angeles" framed as a problem
    r"\bcannot.*\blocal\b",
    r"\bauthentically.*\blocal\b",
    r"\bgenuinely.*\blocal\b",
    r"\bpresent as.*\blocal\b",
    r"\bno (?:travel|relocation|reimbursement|housing|lodging|accommodations?)\b",
]


def _is_local_hire_rationalization(reason: str) -> bool:
    """True if AI rejection reason boils down to a local-hire / no-travel-reimbursement objection."""
    if not reason:
        return False
    return any(re.search(p, reason, re.IGNORECASE) for p in _LOCAL_HIRE_REJECTION_PATTERNS)


def _maybe_override_local_hire_skip(
    role: dict, project_name: str, ai_reason: str, mode: str,
    project_notes: str = "",
) -> tuple[bool, str]:
    """Override an AI SKIP that's a local-hire rationalization when pay clears the threshold.

    Returns (overridden, new_reason). Only fires in paid mode. The override
    runs ``check_travel_pay`` as the source of truth; if that programmatic
    check would have passed (pay clears the threshold for the location, or
    location is unknown), the AI's local-hire objection is treated as
    rationalization and we accept the role.
    """
    if mode != "paid":
        return False, ai_reason
    if not _is_local_hire_rationalization(ai_reason):
        return False, ai_reason
    tp_ok, tp_reason = check_travel_pay(
        project_name, role.get("description", ""), project_notes, mode="paid",
    )
    if tp_ok:
        new_reason = (
            f"travel pay clears threshold; overriding AI local-hire objection "
            f"(AI said: {ai_reason[:120]})"
        )
        logger.info(
            f"[TRAVEL PAY OVERRIDE] {project_name} — {role.get('role_name', '?')}: "
            f"AI rejected for local-hire/no-travel-reimbursement but pay clears threshold; accepting"
        )
        return True, new_reason
    logger.debug(
        f"[TRAVEL PAY OVERRIDE] Not overriding {project_name} — {role.get('role_name', '?')}: "
        f"programmatic check also rejects ({tp_reason})"
    )
    return False, ai_reason


def select_best_roles(
    roles: list[dict], project_name: str, mode: str = "paid",
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Pick the best role(s) from a list of matching roles.

    If only one role, returns it directly (no API call).
    If multiple roles, uses Claude Sonnet to select 1-2 best fits.
    Falls back to the first role if the API call fails.

    Args:
        roles: List of role dicts that already passed filters.
        project_name: Name of the project (for context).
        mode: "paid" (default) or "unpaid". Unpaid mode swaps in a
            stricter prompt that requires Lead/Supporting role types and
            LA-only location.

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
        return _check_single_role_fit(roles[0], project_name, api_key, mode=mode)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, max_retries=8)

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
                f"{i + 1}. {role['role_name']}{meta_str}: {role.get('description', 'No description')[:2000]}"
            )

        if mode == "unpaid":
            scope_line = (
                "Select ALL roles that are a reasonable fit for this actor. Only reject roles "
                "that have a genuine disqualifier. ROLE-TYPE RULE: only Lead, Principal, or "
                "Series Regular roles are acceptable — reject Supporting, Recurring, Day Player, "
                "Featured, Co-Star, or single-scene roles. EXCEPTION: modeling / print / photo / "
                "stills / TFP gigs are EXEMPT from the role-type rule — they are not acting roles "
                "and do not have Lead/Principal/Series Regular labels; accept them on physical/type "
                "fit alone. Pay is NOT a reason to reject — any role, paid or unpaid, is fine as "
                "long as the role-type rule (or modeling exemption) is satisfied."
            )
        else:
            scope_line = (
                "Select ALL roles that are a reasonable fit for this actor. Only reject roles "
                "that have a genuine disqualifier. Being a day player or lower pay is NOT a "
                "reason to reject — submit for every role the actor could realistically book."
            )

        prompt = f"""You are a casting assistant helping an actor decide which role(s) to submit for on a project.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}

AVAILABLE ROLES:
{chr(10).join(role_options)}

{scope_line}

IMPORTANT: If ANY role description says "submit for only one role", "one submission per actor", or similar, you MUST select only ONE role (the best fit) no matter what.

HARD DISQUALIFIERS — reject any role that requires:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes both White AND Latino/Hispanic (e.g., "Black only", "Asian only" — reject. But "Hispanic", "Latino", "White" — accept)
- Specific hair color that is NOT brown ONLY when stated as a casting requirement (e.g., "must have blonde hair", "redhead required"). The actor has BROWN hair. Character descriptions like "dyed hair" or "punk vibes" are styling choices that can be achieved — these are NOT disqualifiers.
- Age range with NO overlap with 17-29 (e.g., "30-40" is a rejection, but "25-35" is NOT because it overlaps with the actor's range)
- Skills the actor doesn't have (singing, musical instrument, specific martial art)
- Requires an authentic/native non-American English accent (e.g., "must have authentic British accent", "native French speaker"). EXCEPTION: Spanish is fine — the actor is fluent in Spanish, so roles requiring Spanish dialogue, a Spanish-speaking character, a native/fluent Spanish speaker, or bilingual English/Spanish are ACCEPTABLE, not disqualifiers.
- Requires a beard or facial hair (actor is clean-shaven)
- NOT a real acting or modeling role — consumer studies, product testing, paid research studies, focus groups, medical studies, or any role where participants are selected based on personal conditions (skin conditions, health issues, etc.) rather than acting/modeling ability. IMPORTANT: Modeling gigs ARE legitimate and the actor actively wants them — do NOT reject. This includes (non-exhaustive): TFP / Time-For-Print, print modeling, swimwear/beach/poolside shoots, fitness modeling, lifestyle content shoots, fashion/editorial, portfolio shoots, brand campaigns, catalog, lookbook, photo shoots, stills work, and photo/video commercial work. Modeling gigs do not need to be "acting roles" with named characters — judge them on physical/type fit only. Only reject actual research studies, focus groups, and medical studies.
- Background/extra work — roles where the actor is atmosphere/background with no individual identity. Key signals: PLURAL role names (e.g., "BODYGUARDS", "THUGS", "WEDDING GUESTS 1 AND 2", "STAFF", "BANQUET GUESTS", "FEMALE GUESTS 1 AND 2", "PARTYGOERS", "CROWD", "MASKED AUDIENCE MEMBERS") are always background. Singular generic titles like "WAITER", "CLERK", "NURSE" are borderline — reject these only if the description confirms they are pure atmosphere with no character arc or meaningful action. Named characters (e.g., "SAMUEL", "DR. LARSON", "DETECTIVE MORRIS") should generally be accepted even if the role is small. EXCEPTION: In commercials, roles labeled "featured", "featured talent", "principal", or "hero" where the talent is prominently on-camera ARE legitimate commercial bookings even without dialogue — do NOT reject these. Only reject commercial roles that are clearly crowd/atmosphere with no camera focus.
- "Must look" or "play younger" age requirements UNDER 17 — the actor appears 25 and can play 17-29. He CAN play down to 17 but CANNOT convincingly look younger than that (under 17, i.e., 13-16). "Plays age 17" or "plays age 18" is fine. Only SKIP if the role explicitly needs someone who looks like a child or young teenager (under 17).
- Requires the actor to OWN something specific that is not a standard wardrobe/prop item — e.g., "must have a dog", "must own a motorcycle", "bring your own surfboard", "real couples only". If the casting post requires the actor to personally possess something (pet, vehicle, relationship, property) as a condition of the role, SKIP it. Standard wardrobe items (suit, business casual, etc.) are NOT disqualifiers.

NOTE: For doubles, stand-ins, or photo doubles, physical specs (height, hair color, build) are EXACT requirements — any mismatch is a hard disqualifier.

{_travel_pay_block(mode)}

Also consider (but these are NOT reasons to reject — only to rank):
1. Physical match (age, build, height, ethnicity)
2. Type match (leading man, comedic/charming)
3. Role prominence (lead, supporting, day player — all acceptable)
4. Overall castability — would this actor realistically be considered?

If NONE of the roles are a good fit, respond with SKIP on the first line and explain why.

Respond with ONLY this exact format (one line per role, no preamble, no reasoning, no explanation before the lines):
SELECTED: <number> - <explain WHY this role is a good fit>
REJECTED: <number> - <explain WHY this role is not a fit>

CRITICAL: Your response must start IMMEDIATELY with SELECTED, REJECTED, or SKIP. Do NOT write any reasoning, analysis, or thinking before the structured lines. No "Let me check...", no "I need to evaluate...", no "The travel pay rule...". Just the structured lines.

IMPORTANT: The reason must explain WHY, not just restate the role name. Bad: "HARRISON". Good: "Age and type match for athletic leading man".

Example:
SELECTED: 1 - Age and type match for athletic leading man
SELECTED: 3 - Age range fits, playboy type works for actor's look
REJECTED: 2 - Requires heavyset build, actor is athletic
REJECTED: 4 - Background/extra role, actor does not do background work"""

        text = shadowed_completion(
            prompt,
            call_site="select_best_roles",
            max_tokens=1000,
            claude_client=client,
            extract_verdict=VERDICT_EXTRACTORS["select_best_roles"],
            project_name=project_name,
        ).strip()
        logger.info(f"[AI] Raw response for {project_name}: {text}")
        selected, rejections = _parse_structured_response(text, roles, project_name)
        # Override AI rejections that boil down to "you're not a [city] local"
        # when the programmatic travel-pay check would have passed.
        roles_by_name = {r["role_name"]: r for r in roles}
        for role_name in list(rejections.keys()):
            ai_reason = rejections[role_name]
            role_obj = roles_by_name.get(role_name)
            if role_obj is None:
                continue
            overridden, new_reason = _maybe_override_local_hire_skip(
                role_obj, project_name, ai_reason, mode,
            )
            if overridden:
                selected.append((role_obj, new_reason))
                del rejections[role_name]
        return selected, rejections

    except Exception as e:
        if _is_transient_error(e):
            logger.warning(f"AI role selection transient failure ({e}); will retry next run")
            rejections = {
                r["role_name"]: f"{TRANSIENT_REJECTION_PREFIX}AI transient failure ({e})"
                for r in roles
            }
        else:
            logger.warning(f"AI role selection failed ({e}), skipping all to be safe")
            rejections = {r["role_name"]: f"AI failed ({e}), skipped" for r in roles}
        return [], rejections


def _check_single_role_fit(
    role: dict, project_name: str, api_key: str, mode: str = "paid",
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Quick AI check: is this single role a physical/type fit?"""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, max_retries=8)

        desc = role.get("description", "No description")[:2000]

        if mode == "unpaid":
            unpaid_line = (
                "ROLE-TYPE RULE: only Lead, Supporting, Principal, Series Regular, or "
                "Recurring roles are acceptable. SKIP if this is a Day Player, Featured, "
                "Co-Star, or single-scene role. EXCEPTION: modeling / print / photo / "
                "stills / TFP gigs are EXEMPT from the role-type rule — they are not "
                "acting roles and do not have Lead/Principal/Series Regular labels; "
                "accept them on physical/type fit alone. Pay is NOT a reason to skip — "
                "any role, paid or unpaid, is fine as long as the role-type rule (or "
                "modeling exemption) is satisfied.\n\n"
            )
        else:
            unpaid_line = ""

        prompt = f"""You are a casting assistant. Quickly decide if this actor should submit for this role.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role['role_name']}
DESCRIPTION: {desc}

{unpaid_line}HARD DISQUALIFIERS — SKIP if the role requires ANY of these:
- Height outside 5'10"–6'1" (e.g., "must be 6'3"+", "under 5'6"")
- Large/heavyset/stocky/overweight build (actor is athletic, 185 lbs)
- Female only
- Specific ethnicity that excludes both White AND Latino/Hispanic (e.g., "Black only", "Asian only" — reject. But "Hispanic", "Latino", "White" — accept)
- Specific hair color that is NOT brown ONLY when stated as a casting requirement (e.g., "must have blonde hair", "redhead required"). The actor has BROWN hair. Character descriptions like "dyed hair" or "punk vibes" are styling choices that can be achieved — these are NOT disqualifiers.
- Age range with NO overlap with 17-29 (e.g., "30-40" is a rejection, but "25-35" is NOT because it overlaps with the actor's range)
- Skills the actor doesn't have (singing, musical instrument, specific martial art)
- Requires an authentic/native non-American English accent (e.g., "must have authentic British accent", "native French speaker"). EXCEPTION: Spanish is fine — the actor is fluent in Spanish, so roles requiring Spanish dialogue, a Spanish-speaking character, a native/fluent Spanish speaker, or bilingual English/Spanish are ACCEPTABLE, not disqualifiers.
- Requires a beard or facial hair (actor is clean-shaven)
- NOT a real acting or modeling role — consumer studies, product testing, paid research studies, focus groups, medical studies, or any role where participants are selected based on personal conditions (skin conditions, health issues, etc.) rather than acting/modeling ability. IMPORTANT: Modeling gigs ARE legitimate and the actor actively wants them — do NOT reject. This includes (non-exhaustive): TFP / Time-For-Print, print modeling, swimwear/beach/poolside shoots, fitness modeling, lifestyle content shoots, fashion/editorial, portfolio shoots, brand campaigns, catalog, lookbook, photo shoots, stills work, and photo/video commercial work. Modeling gigs do not need to be "acting roles" with named characters — judge them on physical/type fit only. Only reject actual research studies, focus groups, and medical studies.
- Background/extra work — roles where the actor is atmosphere/background with no individual identity. Key signals: PLURAL role names (e.g., "BODYGUARDS", "THUGS", "WEDDING GUESTS 1 AND 2", "STAFF", "BANQUET GUESTS", "FEMALE GUESTS 1 AND 2", "PARTYGOERS", "CROWD", "MASKED AUDIENCE MEMBERS") are always background. Singular generic titles like "WAITER", "CLERK", "NURSE" are borderline — reject these only if the description confirms they are pure atmosphere with no character arc or meaningful action. Named characters (e.g., "SAMUEL", "DR. LARSON", "DETECTIVE MORRIS") should generally be accepted even if the role is small. EXCEPTION: In commercials, roles labeled "featured", "featured talent", "principal", or "hero" where the talent is prominently on-camera ARE legitimate commercial bookings even without dialogue — do NOT reject these. Only reject commercial roles that are clearly crowd/atmosphere with no camera focus.
- "Must look" or "play younger" age requirements UNDER 17 — the actor appears 25 and can play 17-29. He CAN play down to 17 but CANNOT convincingly look younger than that (under 17, i.e., 13-16). "Plays age 17" or "plays age 18" is fine. Only SKIP if the role explicitly needs someone who looks like a child or young teenager (under 17).
- Requires the actor to OWN something specific that is not a standard wardrobe/prop item — e.g., "must have a dog", "must own a motorcycle", "bring your own surfboard", "real couples only". If the casting post requires the actor to personally possess something (pet, vehicle, relationship, property) as a condition of the role, SKIP it. Standard wardrobe items (suit, business casual, etc.) are NOT disqualifiers.

NOTE: For doubles, stand-ins, or photo doubles, physical specs (height, hair color, build) are EXACT requirements — any mismatch is a hard disqualifier.

{_travel_pay_block(mode)}

Respond with ONLY one line in this exact format (no preamble, no reasoning, no analysis before the verdict):
FIT - <brief reason>
or
SKIP - <brief reason>

CRITICAL: Your response must start IMMEDIATELY with FIT or SKIP. Do NOT write any reasoning, analysis, or thinking before the verdict line."""

        text = shadowed_completion(
            prompt,
            call_site="single_fit",
            max_tokens=500,
            claude_client=client,
            extract_verdict=VERDICT_EXTRACTORS["single_fit"],
            project_name=project_name,
            role_name=role.get("role_name"),
        ).strip()
        logger.debug(f"[AI] Raw fitness check for {role['role_name']} on {project_name}: {text}")

        # Find the first line that starts (after markdown/punctuation) with FIT or SKIP.
        # Sonnet sometimes writes preamble ("Looking at this role...") before the verdict
        # — we scan all lines instead of only the first so the decision isn't lost.
        verdict_line = ""
        verdict = ""
        for raw_line in text.splitlines():
            stripped_line = re.sub(r'^[\W_]+', '', raw_line).upper()
            if stripped_line.startswith("SKIP"):
                verdict_line = raw_line
                verdict = "SKIP"
                break
            if stripped_line.startswith("FIT"):
                verdict_line = raw_line
                verdict = "FIT"
                break

        if verdict == "SKIP":
            reason = verdict_line.split("-", 1)[1].strip() if "-" in verdict_line else verdict_line
            logger.info(f"AI skipped single role {role['role_name']} on {project_name}: {reason}")
            overridden, new_reason = _maybe_override_local_hire_skip(
                role, project_name, reason, mode,
            )
            if overridden:
                return [(role, new_reason)], {}
            return [], {role["role_name"]: reason}

        if verdict == "FIT":
            reason = verdict_line.split("-", 1)[1].strip() if "-" in verdict_line else "only matching role"
            return [(role, reason)], {}

        logger.warning(
            f"AI fitness check returned unrecognized format for {role['role_name']} on {project_name}; "
            f"defaulting to SKIP. Raw: {text!r}"
        )
        return [], {role["role_name"]: f"AI response unrecognized: {text[:200]}"}

    except Exception as e:
        if _is_transient_error(e):
            logger.warning(f"AI fitness check transient failure ({e}); will retry next run")
            return [], {role["role_name"]: f"{TRANSIENT_REJECTION_PREFIX}AI transient failure ({e})"}
        logger.warning(f"AI fitness check failed ({e}), skipping role")
        return [], {role["role_name"]: f"AI check failed: {e}"}


def _parse_structured_response(
    text: str, roles: list[dict], project_name: str,
) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Parse the structured SELECTED/REJECTED response from the AI."""
    lines = text.strip().split("\n")

    # Check for SKIP — must be "SKIP" alone or "SKIP - reason" (may appear after AI preamble)
    skip_re = re.compile(r"^SKIP\s*(?:[-–—]\s*(.*))?$", re.IGNORECASE)
    for line in lines:
        m = skip_re.match(line.strip())
        if m:
            skip_reason = (m.group(1) or "").strip() or line.strip()
            rejections = {r["role_name"]: skip_reason for r in roles}
            logger.info(f"AI skipped project {project_name}: {skip_reason}")
            return [], rejections

    selected = []
    rejections = {}
    selected_re = re.compile(r"SELECTED:\s*(\d+)\s*[-–—]\s*(.*)", re.IGNORECASE)
    rejected_re = re.compile(r"REJECTED:\s*(\d+)\s*[-–—]\s*(.*)", re.IGNORECASE)

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

    # If we found REJECTED lines but no SELECTED lines, the AI legitimately rejected all roles
    if not selected and rejections:
        logger.info(f"AI rejected all {len(rejections)} role(s) for {project_name}")
        return [], rejections

    # Fallback: no SELECTED or REJECTED lines found — truly unparseable
    if not selected:
        logger.warning(f"AI returned unparseable response for {project_name}: {text[:200]}")
        fallback_reason = "AI returned unparseable response, skipped to be safe"
        rejections = {r["role_name"]: fallback_reason for r in roles}
        return [], rejections

    # If a role appears in both SELECTED and REJECTED, selected wins
    selected_names = {s[0]["role_name"] for s in selected}
    for name in list(rejections):
        if name in selected_names:
            del rejections[name]

    # Fill in any roles not mentioned in rejections
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

        client = anthropic.Anthropic(api_key=api_key, max_retries=8)

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

        text = shadowed_completion(
            prompt,
            call_site="partial_availability",
            max_tokens=100,
            claude_client=client,
            extract_verdict=VERDICT_EXTRACTORS["partial_availability"],
            project_name=project_name,
            role_name=role.get("role_name"),
        ).strip()
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


def answer_prescreen_questions(
    questions: list[dict],
    role: dict,
    project_name: str,
    confirmed_dates: str | None = None,
    unavailable_dates: str | None = None,
) -> dict:
    """Use Claude to answer Backstage applicant prescreen questions from the actor profile + calendar.

    Each question is a dict like:
        {"id": int, "text": str, "type": "YES_NO" | "SHORT_ANSWER",
         "answer_options": [{"id": int, "text": "Yes"|"No"}, ...]}

    Returns:
        {"answers": [{"question_id": int, "selected_answer_id": int|None, "answer_text": str|None}, ...]}
            — only when ALL questions could be answered confidently from profile/calendar.
        {"needs_input": "<reason mentioning the unanswerable question>"}
            — if any question cannot be answered (subjective, open-ended pitch, missing info).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot answer prescreen questions")

    if not questions:
        return {"answers": []}

    import anthropic
    import json as _json

    client = anthropic.Anthropic(api_key=api_key, max_retries=8)

    availability_context = ""
    if confirmed_dates:
        availability_context = (
            f"\nCONFIRMED AVAILABILITY: The actor's calendar has been checked and is FREE for "
            f"{confirmed_dates}. For any availability question that falls within these dates, answer Yes."
        )
    if unavailable_dates:
        availability_context += (
            f"\nUNAVAILABLE DATES: The actor's calendar shows conflicts on "
            f"{unavailable_dates}. For any availability question on these dates, answer No."
        )

    # Build a compact question list for the prompt
    q_lines = []
    for q in questions:
        opts = q.get("answer_options") or []
        opts_str = ", ".join(f'{o.get("id")}={o.get("text")}' for o in opts) if opts else "(free text)"
        q_lines.append(
            f'- id={q.get("id")} type={q.get("type")} options=[{opts_str}] text="{q.get("text", "")}"'
        )
    q_block = "\n".join(q_lines)

    prompt = f"""You are answering applicant questions on a casting submission on behalf of an actor.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role.get('role_name', '')}
ROLE DESCRIPTION: {role.get('description', '')[:800]}
{availability_context}

QUESTIONS:
{q_block}

For EACH question, decide if it can be answered confidently and truthfully from the actor profile or confirmed availability above.

ANSWERABLE (respond with the answer):
- Availability questions on dates within CONFIRMED AVAILABILITY → "Yes"
- Availability questions on dates within UNAVAILABLE DATES → "No"
- Identity/comfort/willingness questions where the profile EXPLICITLY states the position (LGBTQ+, intimacy/nudity, travel, long shoot days, driver's license, passport, accents, physical comedy, action sequences) → answer per profile
- Biographical facts in the profile (height, weight, build, hair, location, age range, contact, shoe size, union status) → answer with the fact
- Questions about specific listed skills (improv, salsa, guitar) → answer Yes ONLY if that exact skill is listed

NOT ANSWERABLE → respond NEEDS_INPUT for that question:
- Open-ended pitch questions ("Why are you right for this role?", "Tell us about yourself", "What draws you to this project?")
- Skills/experience NOT listed in the profile (do NOT fabricate, do NOT volunteer "no")
- Questions requiring judgment about the script, the character, or personal opinions
- Anything ambiguous, subjective, or where the profile is silent
- Availability questions for dates NOT covered by CONFIRMED AVAILABILITY or UNAVAILABLE DATES

Output STRICT JSON only, no prose, no markdown fences. Schema:
{{
  "answers": [
    {{"question_id": <int>, "type": "YES_NO"|"SHORT_ANSWER", "selected_answer_id": <int or null>, "answer_text": <string or null>, "answerable": <true or false>, "reasoning": "<one short sentence>"}}
  ]
}}

Rules:
- You MUST include an entry for EVERY question, even unanswerable ones.
- For answerable YES_NO: set answerable=true, selected_answer_id to the option id matching your answer (Yes or No), answer_text to null.
- For answerable SHORT_ANSWER: set answerable=true, answer_text to a concise factual answer (max 25 words, first person), selected_answer_id to null.
- For unanswerable questions: set answerable=false, selected_answer_id to null, answer_text to null, reasoning to why it cannot be answered.
- Never fabricate. If unsure, mark answerable=false.
"""

    try:
        text = shadowed_completion(
            prompt,
            call_site="prescreen",
            max_tokens=600,
            claude_client=client,
            extract_verdict=VERDICT_EXTRACTORS["prescreen"],
            project_name=project_name,
            role_name=role.get("role_name"),
        ).strip()
        logger.debug(f"[PRESCREEN-AI] Raw response for {role.get('role_name', '')}: {text}")

        # Strip accidental code fences
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        parsed = _json.loads(text)
    except Exception as e:
        logger.warning(f"[PRESCREEN-AI] Failed to parse AI response for {role.get('role_name', '')}: {e}")
        return {"needs_input": f"AI prescreen check failed: {e}"}

    raw_answers = parsed.get("answers") or []
    if len(raw_answers) != len(questions):
        return {"needs_input": f"AI did not answer all {len(questions)} prescreen questions"}

    by_id = {q.get("id"): q for q in questions}
    answered: list[dict] = []
    unanswered_reasons: list[str] = []

    for a in raw_answers:
        qid = a.get("question_id")
        q = by_id.get(qid)
        if not q:
            return {"needs_input": f"AI returned answer for unknown question id {qid}"}

        if not a.get("answerable", True):
            unanswered_reasons.append(a.get("reasoning") or f"question {qid} unanswerable")
            continue

        qtype = q.get("type")
        if qtype == "YES_NO":
            sel = a.get("selected_answer_id")
            valid_ids = {o.get("id") for o in (q.get("answer_options") or [])}
            if sel not in valid_ids:
                unanswered_reasons.append(f"invalid answer option for question {qid}")
                continue
            answered.append({"question_id": qid, "selected_answer_id": sel, "answer_text": None})
        elif qtype == "SHORT_ANSWER":
            txt = (a.get("answer_text") or "").strip()
            if not txt:
                unanswered_reasons.append(f"empty short_answer for question {qid}")
                continue
            answered.append({"question_id": qid, "selected_answer_id": None, "answer_text": txt})
        else:
            unanswered_reasons.append(f"unsupported question type {qtype!r} for question {qid}")

    if not answered:
        reason = "; ".join(unanswered_reasons[:3])
        logger.info(f"[PRESCREEN-AI] No questions answerable for {role.get('role_name', '')}: {reason}")
        return {"needs_input": f"No prescreen questions answerable: {reason}"}

    if unanswered_reasons:
        reason = "; ".join(unanswered_reasons[:3])
        logger.info(
            f"[PRESCREEN-AI] Partially answered {len(answered)}/{len(questions)} question(s) "
            f"for {role.get('role_name', '')}: {reason}"
        )
        return {"answers": answered, "partial": True, "unanswered_reason": reason}

    logger.info(
        f"[PRESCREEN-AI] Answered {len(answered)} prescreen question(s) for {role.get('role_name', '')}"
    )
    return {"answers": answered}


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

    client = anthropic.Anthropic(api_key=api_key, max_retries=8)

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
- CONDITIONAL-IF REQUESTS: phrases like "Please note IF you are/have X", "note if X applies", "let us know IF you are a veteran / parent / nurse / pilot / Mandarin speaker / Y-licensed / have Z experience" are CONDITIONAL. The casting is addressing ONLY people who already have that attribute — everyone else should stay silent. If the attribute is NOT explicitly in the actor profile, respond with ACTION: SUBMIT (no note). Do NOT affirm ("I am a veteran"), do NOT deny ("I am not a veteran"), do NOT hedge. Silence is the correct response. This covers: military/veteran status, parenthood, profession/career history (nurse, teacher, pilot, lawyer, etc.), language fluency beyond what's profiled, licenses/certifications beyond driver's license and passport, medical conditions, LGBTQ+ identity if not in profile, ethnicity specifics beyond White/Latino, ownership of things (pets, vehicles, property), and lived-experience claims (grew up in X, immigrated from Y, survived Z).
- If the post asks about SPECIFIC TYPES OF EXPERIENCE (e.g., vertical experience, stunt experience, hosting experience, combat experience, mocap experience, etc.) that are NOT listed in the actor profile above, respond with ACTION: SUBMIT. Do NOT write "I have [X] experience" (fabrication) or "I do not have [X] experience" (volunteering a negative). BOTH responses are wrong. Simply submit with no note.
- BAD note examples (NEVER do these): "I have improv training from UCB", "I'm a devoted TheraBreath user", "I've played lead roles in micro-series", "I have 5+ years of dance experience", "I am a military veteran", "I am a parent", "I am a nurse", "I speak fluent Mandarin". These are all WRONG — either fabricated or volunteering unrequested info. NOTE: Spanish fluency IS in the actor profile, so "I am fluent in Spanish" is an acceptable response when the casting explicitly asks about Spanish ability.
- If a casting asks for Instagram, include @marshallpowell
- NEVER volunteer information that was not explicitly asked for — no location, no transportation, no contact info, no availability unless the post explicitly asks you to NOTE it in the submission
- If the post asks for availability/dates and CONFIRMED AVAILABILITY is provided above, include the specific dates in the note
- If they ask for contact info, provide the email and phone number from the profile
- If they ask for a demo reel, reel link, demo clips, online clips, video clips, footage, sample work, or any variant thereof, respond with ACTION: SUBMIT (apply anyway, do not mention lack of reel). The standard submission process attaches whatever clips are in the actor's profile automatically — this is never a blocker.
- If multiple requirements exist and you can answer SOME but not all, use NEEDS_INPUT — UNLESS the unanswerable item is a demo reel / demo clips / reel link / video samples (apply anyway)
- When in doubt between SUBMIT and SUBMIT_WITH_NOTE, ALWAYS prefer SUBMIT
- Treat project-level REQUIREMENTS (e.g., "NOTE YOUR DETAILED AVAILABILITY") with the same weight as role-level requests — these apply to every role submission
- "PLEASE INCLUDE SIZE CARDS" or "include size card" means wardrobe measurements — these are handled in the actor's Actors Access profile, NOT in submission notes. Respond with ACTION: SUBMIT
- Do NOT combine multiple unrelated facts into one note (e.g., don't add shoe size + transportation + availability when only one thing was asked for). Answer ONLY what was explicitly requested.

Respond with ONLY the action line (and NOTE/REASON line if applicable). No other text."""

    text = shadowed_completion(
        prompt,
        call_site="submission_requirements",
        max_tokens=200,
        claude_client=client,
        extract_verdict=VERDICT_EXTRACTORS["submission_requirements"],
        project_name=project_name,
        role_name=role.get("role_name"),
    ).strip()
    logger.debug(f"[ANALYSIS] Raw response for {role.get('role_name', '')} on {project_name}: {text}")
    result = _parse_analysis_response(text, role, project_name)

    # Validate note quality before allowing submission with note
    if result["action"] == "SUBMIT_WITH_NOTE" and result["note"]:
        if not _validate_note(result["note"], role, project_name):
            result["action"] = "SUBMIT"
            result["note"] = None

    return result


def generate_cover_letter(role: dict, project_name: str) -> str:
    """Generate a short cover letter for a Backstage role that requires one.

    Returns plain text (no greeting, no signature) suitable for the user to
    paste into Backstage's cover letter field. 2-4 sentences, first person,
    grounded tone. Must respect the never-fabricate rule: no invented
    credits, no invented experience, no training-resume talk, no
    affirming conditional-if attributes not in the profile.

    Returns empty string on error or if the output fails the note validator.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — cannot generate cover letter")
        return ""

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=8)

    desc = role.get("description", "")
    role_name = role.get("role_name", "")

    prompt = f"""You are writing a short cover letter for the actor described below, for a specific casting role. The user will paste this text into Backstage's cover letter field, then review/edit before submitting.

ACTOR PROFILE:
{ACTOR_PROFILE}

PROJECT: {project_name}
ROLE: {role_name}
DESCRIPTION: {desc[:1500]}

Write a 2-4 sentence cover letter in first person, warm but grounded tone. Focus on why the actor is a natural fit based ONLY on attributes explicitly listed in the profile above (type, look, age range, language skills, physical attributes, modeling comfort if relevant). Reference one or two concrete details from the role/project so it doesn't read as boilerplate.

HARD RULES — violations make the letter worse than no letter at all:
- NO greeting ("Hi", "Dear", "Hello casting"). NO sign-off ("Thanks", "Best", "— Matt"). Plain text only, ready to paste into a form field.
- NEVER fabricate acting credits, past roles, past projects, specific brand/product usage, personal stories, or lived experience not in the profile.
- NEVER mention improv/UCB/Groundlings/salsa/guitar/dance training or any skill-resume content — the actor's profile already covers this.
- NEVER claim or deny conditional-if attributes that aren't in the profile (veteran, parent, nurse, pilot, speaker of another language, licensed for X, grew up in Y). Stay silent on anything not explicitly listed.
- Spanish fluency IS in the profile — if the role asks for Spanish, mention fluency. Do not mention Spanish if the role doesn't ask for it.
- Modeling comfort IS in the profile — if this is a modeling/print/stills/TFP/lifestyle/fitness gig, you may mention being comfortable with that work.
- Do NOT claim availability for specific dates unless the dates are confirmed; you may say "available for the shoot window" in general terms.
- Do NOT use placeholder brackets [like this] or {{like this}} or "insert X here" language.
- Output ONLY the cover letter text. No preamble, no explanation, no quote marks around the text.

Write the cover letter now."""

    try:
        text = shadowed_completion(
            prompt,
            call_site="cover_letter",
            max_tokens=300,
            claude_client=client,
            extract_verdict=VERDICT_EXTRACTORS["cover_letter"],
            project_name=project_name,
            role_name=role_name,
        ).strip()
    except Exception as e:
        logger.warning(f"Cover letter generation failed for {role_name} on {project_name}: {e}")
        return ""

    # Strip any stray quote wrappers the model sometimes adds
    if len(text) >= 2 and text[0] in {'"', "'"} and text[-1] == text[0]:
        text = text[1:-1].strip()

    if not text:
        logger.warning(f"Cover letter generation returned empty text for {role_name} on {project_name}")
        return ""

    # Reuse _validate_note to block placeholders, skill-mentions, "experience"
    # claims, etc. If the letter fails validation, return empty so the caller
    # falls back to "no suggested letter" rather than surfacing fabrication.
    if not _validate_note(text, role, project_name):
        logger.warning(f"Cover letter failed validation for {role_name} on {project_name}")
        return ""

    logger.info(f"Generated cover letter for {role_name} on {project_name} ({len(text)} chars)")
    return text


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

    # Reject notes that claim or deny experience (fabrication/negative-volunteering risk)
    # No legitimate submission note should mention "experience" — actor skills are already
    # forbidden above, and any other experience claim risks fabrication or volunteering negatives.
    if re.search(r'\bexperience\b', note, re.IGNORECASE):
        logger.warning(f"Rejected note mentioning experience for {role.get('role_name', '')} on {project_name}: {note}")
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
