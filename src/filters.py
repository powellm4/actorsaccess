# src/filters.py
"""Role filtering logic for auto-apply.

Uses the site's own "fit for me" highlighting and keyword checks
to determine if a role should be submitted for.
"""

import re

_BG_PATTERN = re.compile(
    r"\bbackground\b|\bBG\b|\b(?:EXTRA|Extra)s?\b",
    re.IGNORECASE,
)


def _is_background(role: dict) -> bool:
    """Check if a role is a background/extra role."""
    name = role.get("role_name", "")
    desc = role.get("description", "")
    return bool(_BG_PATTERN.search(name) or _BG_PATTERN.search(desc))


_SKIP_PROJECT_TYPES = {"theater", "theatre", "musical"}


def project_matches(project: dict) -> tuple[bool, str]:
    """Check if a project should be considered.

    Args:
        project: Dict with key project_type.

    Returns:
        (True, "") if the project passes, or
        (False, reason) explaining why it was skipped.
    """
    ptype = project.get("project_type", "").lower()
    if ptype in _SKIP_PROJECT_TYPES:
        return False, f"project type: {project['project_type']}"
    return True, ""


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

    return True, ""
