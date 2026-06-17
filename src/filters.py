# src/filters.py
"""Role filtering logic for auto-apply.

Uses the site's own "fit for me" highlighting and keyword checks
to determine if a role should be submitted for.
"""

import re
import logging

logger = logging.getLogger(__name__)

# For project names — plain "background" is a clear signal
_BG_PROJECT_PATTERN = re.compile(
    r"\bbackground\b|\bBG\b|\b(?:EXTRA|Extra)s?\b",
    re.IGNORECASE,
)

# For role descriptions — match "BACKGROUND" as a standalone label
# (typically at end of description or after a period) but not in
# character backstory phrases like "Latino background" or "privileged background"
_BG_ROLE_PATTERN = re.compile(
    r"\bFEATURED\s+BACKGROUND\b"
    r"|\bbackground\s+(?:actor|performer|role|talent|work|artist|player)\b"
    r"|\.\s*BACKGROUND\s*\.?\s*$"
    r"|^\s*BACKGROUND\s*\.?\s*$"
    r"|\bBG\b"
    r"|\b(?:EXTRA|Extra)s?\b",
    re.IGNORECASE | re.MULTILINE,
)

_UGC_PATTERN = re.compile(r"\bUGC\b", re.IGNORECASE)

_VO_PATTERN = re.compile(
    r"\bvoice\s*over\b|\bvoiceover\b|\bVO\b|\bvoice\s*acting\b|\bvoice\s*actor\b|\bvoice\s*role\b",
    re.IGNORECASE,
)

_COURT_TV_PATTERN = re.compile(
    r"\bcourt\b|\bplaintiff\b|\bdefendant\b|\bsmall\s*claims\b",
    re.IGNORECASE,
)

_SCENE_RECREATION_PATTERN = re.compile(
    r"\bscene\s+recreation\b|\bscene\s+study\b|\bscene\s+reenactment\b|\bscene\s+remake\b",
    re.IGNORECASE,
)

# Roles requiring a complete family unit, real couple, or group act —
# structurally impossible for a solo submission. Filtering early avoids
# wasted AI evaluation cycles.
_FAMILY_CASTING_PATTERN = re.compile(
    r"\breal\s+family\b"
    r"|\bfamily\s+of\s+\d"
    r"|\bfamily\s+unit\b"
    r"|\bfamily\s+consisting\s+of\b"
    r"|\breal\s+siblings?\b"
    # Real-couple roles: actor must have an existing partner to co-submit
    r"|\breal\s+couple\b"
    r"|\bmust\s+submit\s+as\s+a\s+couple\b"
    r"|\bmust\s+submit\s+(?:together\s+)?with\s+(?:a\s+)?partner\b"
    r"|\binterracial\s+couple\b",
    re.IGNORECASE,
)

_UNPAID_PATTERN = re.compile(
    r"\bno\s*pay\b|\bunpaid\b|\bnon[- ]?paid\b|\bdeferred\b|\bcopy\s*&?\s*credit\b|\bcopy/credit\b",
    re.IGNORECASE,
)

_SAG_ONLY_PATTERN = re.compile(
    r"(?:only\s+submit\s+if\s+you\s+are\s+(?:paid[- ]?up\s+)?sag)"
    r"|(?:must\s+be\s+(?:a\s+)?(?:paid[- ]?up\s+)?sag[- ]?aftra\s+member)"
    r"|(?:sag[- ]?aftra\s+members?\s+only)"
    r"|(?:union\s+members?\s+only)"
    r"|(?:only\s+(?:paid[- ]?up\s+)?sag[- ]?aftra\s+members?\s+(?:should|may)\s+submit)",
    re.IGNORECASE,
)

# Modeling / print / photo / stills work doesn't carry acting role types
# (Lead/Principal/Series Regular). The actor accepts these gigs and they
# should bypass the unpaid-mode role-type whitelist.
_MODELING_PROJECT_TYPES = {"print", "photo", "modeling", "stills"}

_MODELING_KEYWORD_PATTERN = re.compile(
    r"\bTFP\b"
    r"|\b(?:print|photo|beach|swimwear|fitness|lifestyle|fashion|editorial|portfolio|catalog|lookbook|brand)\s+model\b"
    r"|\bmodel(?:ing)?\s+(?:shoot|gig|session|portfolio|campaign)\b"
    r"|\bphoto(?:\s*shoot|graphy)\b"
    r"|\bstills?\s+(?:shoot|model|talent)\b",
    re.IGNORECASE,
)


def is_modeling_role(role: dict, project_type: str = "") -> bool:
    """True if the role is a modeling / print / stills / photo gig.

    Modeling work doesn't carry acting role types (Lead/Principal/etc.) and
    should be evaluated on physical/type fit only. Detected from (in order):
    project_type field, role_type field, or keyword signals in role_name +
    description.
    """
    if project_type and project_type.strip().lower() in _MODELING_PROJECT_TYPES:
        return True
    role_type = (role.get("role_type") or "").lower()
    if any(kw in role_type for kw in ("model", "print", "photo", "stills")):
        return True
    blob = f"{role.get('role_name', '')} {role.get('description', '')}"
    return bool(_MODELING_KEYWORD_PATTERN.search(blob))


def _is_background(role: dict) -> bool:
    """Check if a role is a background/extra role."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    return bool(_BG_ROLE_PATTERN.search(name) or _BG_ROLE_PATTERN.search(desc))


def _is_voiceover(role: dict) -> bool:
    """Check if a role is a voice over/voice acting role."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    role_type = role.get("role_type", "")
    return bool(
        _VO_PATTERN.search(name)
        or _VO_PATTERN.search(role_type)
        or _VO_PATTERN.search(desc)
    )


def _is_unpaid(role: dict) -> bool:
    """Check if a role is explicitly unpaid based on the pay/rate field."""
    pay = role.get("pay", "").strip()
    if not pay:
        return False
    return bool(_UNPAID_PATTERN.search(pay))


# Role-type gating for unpaid mode. Unpaid submissions are reserved for
# top-billed character slots only — Lead, Principal, Series Regular.
# Supporting, Recurring, Day Player, Featured, Co-Star, and single-scene
# roles are rejected.
ACCEPTED_ROLE_TYPES = {
    "LEAD",
    "PRINCIPAL",
    "SERIES REGULAR",
}

# Uppercase-only to avoid matching "the lead is a doctor" in prose. On AA,
# role type is not a structured field — it's embedded in the breakdown
# description as an ALLCAPS marker (e.g. "LEAD", "DAY PLAYER"). CN/Backstage
# supply role_type as structured data, so the regex is only used for AA.
_ROLE_TYPE_MARKER = re.compile(
    r"\b(LEAD|SUPPORTING|PRINCIPAL|SERIES\s+REGULAR|RECURRING|DAY\s+PLAYER|FEATURED|CO-?STAR)\b"
)

# ALL-CAPS only: AA breakdowns format demographic markers ("FEMALE", "MALE")
# in caps. Matching lowercase would false-positive on prose like "his female
# cousin" in a male role's backstory.
_FEMALE_AA_MARKER = re.compile(r"\b(?:FEMALE|WOMAN|GIRL)\b")


def extract_role_type_marker(description: str) -> str | None:
    """Find the first uppercase role-type marker in an AA description.

    Returns the normalized marker (whitespace collapsed, uppercased) or None
    if no marker is present.
    """
    if not description:
        return None
    m = _ROLE_TYPE_MARKER.search(description)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).upper()


def _gender_indicates_female(value: str) -> bool:
    """True if a structured gender field value indicates a female actor.

    Accepts "Female", "Female and Male", "Trans Female", "Cisgender Female",
    "Female/Non-binary"; rejects "Male", "All Genders", "Non-binary", "".
    Substring on "female" is intentionally strict — "All Genders" is too
    ambiguous about who actually shows up to count.
    """
    return "female" in (value or "").strip().lower()


def project_has_female_cast(roles: list[dict], current_role: dict, platform: str) -> bool:
    """True if any role in the project other than current_role is for a female.

    Identity-based exclusion (`r is not current_role`) — name equality would
    wrongly conflate two unnamed peer roles like "Cop #1" / "Cop #2".

    Platform detection:
      - CN / Backstage: structured `gender` field.
      - AA: ALL-CAPS marker in role_name + description.
    """
    for r in roles:
        if r is current_role:
            continue
        if platform == "aa":
            blob = f"{r.get('role_name', '')}\n{r.get('description', '')}"
            if _FEMALE_AA_MARKER.search(blob):
                return True
        else:
            if _gender_indicates_female(r.get("gender", "")):
                return True
    return False


def is_lead_or_supporting(
    role: dict,
    platform: str,
    project_type: str = "",
    has_female_cast: bool = False,
) -> tuple[bool, str]:
    """Check whether a role is Lead / Principal / Series Regular.

    Named for historical reasons — the accepted set has since tightened to
    top-billed character slots only (no Supporting, no Recurring).

    Strict: if the role type can't be determined, the role is rejected.

    Modeling / print / photo / stills gigs short-circuit to accepted —
    they don't have acting role types and the actor accepts them.

    `has_female_cast=True` also short-circuits to accepted: in unpaid mode the
    actor opens the role-type aperture for projects with a female on the cast,
    independent of role tier. Background/voiceover/court-TV remain filtered by
    `role_matches()` upstream.

    Args:
        role: Dict with role_type (CN/Backstage) or description (AA).
        platform: "aa", "cn", or "backstage".
        project_type: Optional project_type string (e.g. "Print", "Photo").
        has_female_cast: If True, bypass the role-type whitelist.

    Returns:
        (True, "") if accepted, or (False, reason) if rejected.
    """
    if is_modeling_role(role, project_type):
        return True, ""

    if has_female_cast:
        return True, ""

    if platform == "aa":
        marker = extract_role_type_marker(role.get("description", ""))
        if marker is None:
            return False, "no role-type marker in description"
        if marker not in ACCEPTED_ROLE_TYPES:
            return False, f"role type {marker} not in unpaid whitelist"
        return True, ""

    # CN and Backstage: structured role_type field
    role_type = (role.get("role_type") or "").strip().upper()
    if not role_type:
        return False, "empty role_type"
    if role_type not in ACCEPTED_ROLE_TYPES:
        return False, f"role type {role_type} not in unpaid whitelist"
    return True, ""


def _is_family_casting(role: dict) -> bool:
    """Return True if the role requires a real family/group unit (solo submissions don't apply)."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    return bool(_FAMILY_CASTING_PATTERN.search(name) or _FAMILY_CASTING_PATTERN.search(desc))


def _is_court_tv(role: dict) -> bool:
    """Check if a role is for a court TV show."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    return bool(_COURT_TV_PATTERN.search(name) or _COURT_TV_PATTERN.search(desc))


def _is_ugc(project_name: str, role: dict) -> bool:
    """Check if a role/project is UGC (user-generated content)."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    return bool(
        _UGC_PATTERN.search(project_name)
        or _UGC_PATTERN.search(name)
        or _UGC_PATTERN.search(desc)
    )


_SKIP_PROJECT_TYPES = {"theater", "theatre", "musical", "staged reading"}


def project_matches(project: dict) -> tuple[bool, str]:
    """Check if a project should be considered.

    Args:
        project: Dict with keys project_type, project_name.

    Returns:
        (True, "") if the project passes, or
        (False, reason) explaining why it was skipped.
    """
    ptype = project.get("project_type", "").lower()
    if ptype in _SKIP_PROJECT_TYPES:
        return False, f"project type: {project['project_type']}"

    name = project.get("project_name", "")
    if _BG_PROJECT_PATTERN.search(name):
        return False, "background project"

    if _UGC_PATTERN.search(name):
        return False, "UGC project"

    if _COURT_TV_PATTERN.search(name):
        return False, "court TV project"

    if _SCENE_RECREATION_PATTERN.search(name):
        return False, "scene recreation project"

    return True, ""


def is_sag_only(project_notes: str) -> bool:
    """Check if project notes indicate SAG-AFTRA members only."""
    return bool(_SAG_ONLY_PATTERN.search(project_notes))


def role_matches(role: dict, mode: str = "paid") -> tuple[bool, str]:
    """Check if a role should be submitted for.

    Args:
        role: Dict with keys fit_for_me (bool), role_name, description.
        mode: "paid" (default) or "unpaid".

    Notes on fit_for_me and mode:
        - The AA site-level "breakdowns fit for me" dropdown is applied in
          both modes via config. It narrows the BREAKDOWN list to listings
          that have at least one role matching the actor profile.
        - Within a fit breakdown, individual roles can still be off (e.g.
          a project with both a male lead and a female senator — the
          breakdown is "fit" but the senator role is not). The per-role
          fit_for_me check below catches those. We keep it enabled in
          both paid and unpaid mode — otherwise unpaid runs submit to
          obviously-wrong roles like female-only or out-of-age-range.
        - We skip the text-based _is_unpaid check in unpaid mode; the
          platform saved search handles paid-vs-unpaid categorization.

    Returns:
        (True, "") if the role passes all filters, or
        (False, reason) explaining why it was skipped.
    """
    if not role.get("fit_for_me"):
        return False, "not fit for me"

    if _is_background(role):
        return False, "background role"

    if _is_voiceover(role):
        return False, "voiceover role"

    if mode != "unpaid" and _is_unpaid(role):
        return False, "unpaid role"

    if _is_court_tv(role):
        return False, "court TV role"

    if _is_family_casting(role):
        return False, "requires real family/group unit"

    return True, ""
