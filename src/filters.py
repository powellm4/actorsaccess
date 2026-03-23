# src/filters.py
"""Role filtering logic for auto-apply.

Uses the site's own "fit for me" highlighting and keyword checks
to determine if a role should be submitted for.
"""

import re
import logging

logger = logging.getLogger(__name__)

_BG_PATTERN = re.compile(
    r"\bbackground\b|\bBG\b|\b(?:EXTRA|Extra)s?\b",
    re.IGNORECASE,
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
    return bool(_BG_PATTERN.search(name) or _BG_PATTERN.search(desc))


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


_SKIP_PROJECT_TYPES = {"theater", "theatre", "musical"}


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
    if _BG_PATTERN.search(name):
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


def role_matches(role: dict) -> tuple[bool, str]:
    """Check if a role should be submitted for.

    Args:
        role: Dict with keys fit_for_me (bool), role_name, description.

    Returns:
        (True, "") if the role passes all filters, or
        (False, reason) explaining why it was skipped.
    """
    if not role.get("fit_for_me"):
        return False, "not fit for me"

    if _is_background(role):
        return False, "background role"

    if _is_court_tv(role):
        return False, "court TV role"

    return True, ""
