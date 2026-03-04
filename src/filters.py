# src/filters.py
"""Role filtering logic for auto-apply.

Uses the site's own "fit for me" highlighting (yellow background via
the `role_fit_for_me` CSS class) to determine if a role matches the
user's profile demographics.
"""


def role_matches(role: dict) -> tuple[bool, str]:
    """Check if a role is a fit based on the site's highlighting.

    Args:
        role: Dict with key fit_for_me (bool).

    Returns:
        (True, "") if the role is a fit, or
        (False, reason) explaining why it was skipped.
    """
    if not role.get("fit_for_me"):
        return False, "not fit for me"

    return True, ""
