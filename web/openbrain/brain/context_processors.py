# ABOUTME: Template context processors for the Brain UI navbar.
# ABOUTME: Supplies the operator-only pending-review count for the /review badge.
"""Navbar context for every Brain page.

review_badge exposes the pending-review total so the navbar badge can render on
any page. It is operator-only (mirrors the /review route gate, #137 / #52) and
must stay cheap and safe on the sqlite local/test default where brain.* is
absent — both cases degrade to a 0 total rather than querying or erroring.
"""

from openbrain.brain.db import brain_schema_present
from openbrain.brain.services.mcp_reads import pending_reviews


def review_badge(request) -> dict:
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_superuser):
        return {"review_pending_total": 0}
    if not brain_schema_present():
        return {"review_pending_total": 0}
    return {"review_pending_total": pending_reviews()["total"]}
