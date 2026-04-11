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


# Role-type gating for unpaid mode. We only want meaningful screen time on
# unpaid submissions — Lead / Supporting / Principal / Series Regular /
# Recurring. Anything else (Day Player, Featured, Co-Star, etc.) is rejected.
ACCEPTED_ROLE_TYPES = {
    "LEAD",
    "SUPPORTING",
    "PRINCIPAL",
    "SERIES REGULAR",
    "RECURRING",
}

# Uppercase-only to avoid matching "the lead is a doctor" in prose. On AA,
# role type is not a structured field — it's embedded in the breakdown
# description as an ALLCAPS marker (e.g. "LEAD", "DAY PLAYER"). CN/Backstage
# supply role_type as structured data, so the regex is only used for AA.
_ROLE_TYPE_MARKER = re.compile(
    r"\b(LEAD|SUPPORTING|PRINCIPAL|SERIES\s+REGULAR|RECURRING|DAY\s+PLAYER|FEATURED|CO-?STAR)\b"
)


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


def is_lead_or_supporting(role: dict, platform: str) -> tuple[bool, str]:
    """Check whether a role is Lead / Supporting / Principal / Series Regular / Recurring.

    Strict: if the role type can't be determined, the role is rejected.

    Args:
        role: Dict with role_type (CN/Backstage) or description (AA).
        platform: "aa", "cn", or "backstage".

    Returns:
        (True, "") if accepted, or (False, reason) if rejected.
    """
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
        mode: "paid" (default) or "unpaid". In unpaid mode we trust the
            platform's saved search (or AA's paying_only=false config) to
            handle paid-vs-unpaid categorization and skip the text-based
            _is_unpaid check — the pay text field is unreliable because
            saved-search-categorized "unpaid" roles often still carry a
            rate string in that field. The lead/supporting gate + the AI
            narrow things further.

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

    return True, ""
