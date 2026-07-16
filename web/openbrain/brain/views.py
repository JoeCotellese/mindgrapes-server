"""Brain UI views — thin FBVs that orchestrate a read service and render.

Every view is gated by brain_login_required and guards on brain_schema_present()
so the app still renders a friendly page on the sqlite local/test default, where
brain.* does not exist. The viewer for the privacy filter is always the logged-in
member (viewer_id) — never the all-seeing null viewer the MCP service allows.
"""

import json

from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.shortcuts import redirect, render
from django.utils import timezone

from openbrain.brain.auth import brain_login_required, viewer_id
from openbrain.brain.db import brain_schema_present
from openbrain.brain.embeddings import EmbeddingError
from openbrain.brain.exceptions import ExperienceNotFound, NotOwner
from openbrain.brain.services.activity import get_activity
from openbrain.brain.services.deletes import get_usage, soft_delete_experience
from openbrain.brain.services.edits import edit_experience
from openbrain.brain.services.feed import bucket_by_day, get_recent_feed
from openbrain.brain.services.mcp_reads import pending_reviews
from openbrain.brain.services.reads import (
    get_entity_detail,
    get_entity_mentions,
    get_experience_detail,
    get_summary,
    recently_active_entities,
    search_experiences,
)
from openbrain.brain.services.reviews import (
    attach_entity_names,
    merge_candidate_evidence,
    resolve_correction,
    resolve_disambiguation,
    resolve_merge_candidate,
    review_queue,
)
from openbrain.brain.services.visibility import set_visibility

# Shown on the edit form after an embedding-service failure: the write is aborted
# before any DB mutation, so the user's text is intact and a retry is safe.
_EDIT_SERVICE_ERROR = (
    "The embedding service didn’t respond, so nothing was changed. Try again."
)

DEFAULT_MENTIONS_LIMIT = 50
MAX_MENTIONS_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
DEFAULT_FEED_LIMIT = 20
MAX_FEED_LIMIT = 100


def _int_param(request, name, default):
    try:
        return int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default


def _pagination(request):
    limit = _int_param(request, "limit", DEFAULT_MENTIONS_LIMIT)
    limit = max(1, min(limit, MAX_MENTIONS_LIMIT))
    offset = max(0, _int_param(request, "offset", 0))
    return limit, offset


@brain_login_required
def dashboard(request):
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    return render(
        request,
        "brain/dashboard.html",
        {
            "summary": get_summary(),
            "recently_active": recently_active_entities(viewer_id(request)),
        },
    )


@brain_login_required
def recent_feed(request):
    """Reverse-chronological feed of recent captures; htmx Load-more pages it.

    Hard nav renders recent.html (tabs + first page); an htmx request returns the
    matching page partial so the trailing Load-more button can append the next
    page in place. The List|Timeline tabs share this URL via ?view. Timeline
    buckets the same rows by day (#135); the cont slug carried in the Load-more
    URL lets the next page suppress a repeated leading header.
    """
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    limit = max(
        1, min(_int_param(request, "limit", DEFAULT_FEED_LIMIT), MAX_FEED_LIMIT)
    )
    offset = max(0, _int_param(request, "offset", 0))
    view = "timeline" if request.GET.get("view") == "timeline" else "list"
    page = get_recent_feed(viewer_id(request), limit, offset)
    page["feed_limit"] = limit
    page["view"] = view
    if view == "timeline":
        groups = bucket_by_day(page["experiences"], timezone.localdate())
        page["groups"] = groups
        page["cont"] = request.GET.get("cont", "")
        page["next_cont"] = groups[-1]["slug"] if groups else ""
        partial = "brain/_timeline.html"
    else:
        partial = "brain/_feed_page.html"
    template = partial if request.htmx else "brain/recent.html"
    return render(request, template, page)


@brain_login_required
def activity_log(request):
    """The change-event audit log (#136); htmx Load-more pages it.

    Reverse-chronological brain.correction_events with the actor (human vs. the
    consolidation worker) and an expandable before/after. Gated to the operator
    (superuser): correction_events has no per-row owner, so there is no viewer
    filter — see #52. A hard nav renders activity.html; an htmx request returns
    the page partial so the trailing Load-more appends in place.
    """
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "The activity log is available to the operator only."
        )
    limit = max(
        1, min(_int_param(request, "limit", DEFAULT_FEED_LIMIT), MAX_FEED_LIMIT)
    )
    offset = max(0, _int_param(request, "offset", 0))
    page = get_activity(limit, offset)
    page["feed_limit"] = limit
    template = "brain/_activity_page.html" if request.htmx else "brain/activity.html"
    return render(request, template, page)


# Surfaces shown on /review, in tab order, each with a human label. The first
# three are actionable; low_confidence_claims and contradictions are display-only
# (#137 decision) — no single clean mutation exists, so they deep-link out and
# the AI repair tools (retract_claim, edits) handle them.
_REVIEW_SURFACES = (
    ("merge_candidates", "Merge candidates"),
    ("proposed_corrections", "Proposed corrections"),
    ("disambiguations", "Disambiguations"),
    ("low_confidence_claims", "Low-confidence claims"),
    ("contradictions", "Contradictions"),
)
_REVIEW_SURFACE_KEYS = {key for key, _ in _REVIEW_SURFACES}

# The element-id / action-url key each surface's rows are addressed by.
_ROW_ID_FIELD = {
    "merge_candidates": "id",
    "proposed_corrections": "id",
    "disambiguations": "token",
    "low_confidence_claims": "claim_id",
    "contradictions": "claim_id",
}

# (surface, action) the POST router accepts; anything else is a 400. The view is
# only a router over the existing mutations — no new write logic lives here.
_REVIEW_ACTIONS = {
    ("merge_candidates", "confirm"),
    ("merge_candidates", "reject"),
    ("proposed_corrections", "apply"),
    ("proposed_corrections", "reject"),
    ("disambiguations", "resolve"),
}


class _ActionBadRequest(Exception):
    """A malformed review action (missing winner/choice) → 400, not an error."""


def _first_nonempty_surface(counts):
    for key, _ in _REVIEW_SURFACES:
        if counts.get(key, 0):
            return key
    return _REVIEW_SURFACES[0][0]


@brain_login_required
def review_inbox(request):
    """Operator workbench over the five review-queue surfaces (#137).

    Gated to the superuser like /activity (#136): review_queue / pending_reviews
    are not viewer-scoped (single-user janitorial), so exposing them to another
    member would leak the operator's queue. multi-user: revisit under #52.
    Server-rendered tab links select the surface; the first non-empty one is the
    default. Actions are htmx POSTs that swap a single row + the navbar badge.
    """
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "The review queue is available to the operator only."
        )
    counts = pending_reviews()
    surfaces = [
        {"key": key, "label": label, "count": counts.get(key, 0)}
        for key, label in _REVIEW_SURFACES
    ]
    active = request.GET.get("surface")
    if active not in _REVIEW_SURFACE_KEYS:
        active = _first_nonempty_surface(counts)
    rows = attach_entity_names(review_queue(active)[active], active)
    id_field = _ROW_ID_FIELD[active]
    for row in rows:
        row["row_id"] = row.get(id_field)
    return render(
        request,
        "brain/review.html",
        {
            "surfaces": surfaces,
            "active": active,
            "rows": rows,
            "counts": counts,
            "total": counts["total"],
        },
    )


def _dispatch_review_action(request, surface, row_id, action, actor):
    """Route one (surface, action) to its existing mutation; return the result."""
    if surface == "merge_candidates":
        if action == "confirm":
            winner = request.POST.get("winner_id")
            if not winner:
                raise _ActionBadRequest("confirm requires a winner_id")
            return resolve_merge_candidate(
                row_id, "confirm", winner_id=winner, created_by=actor
            )
        return resolve_merge_candidate(row_id, "reject", created_by=actor)
    if surface == "proposed_corrections":
        return resolve_correction(row_id, action, created_by=actor)
    # disambiguations
    choice = request.POST.get("choice")
    if not choice:
        raise _ActionBadRequest("resolve requires a choice")
    return resolve_disambiguation(row_id, choice)


@brain_login_required
def review_action(request, surface, id, action):
    """POST-only: apply one review decision and swap the row + navbar badge (#137).

    Routes to the matching existing mutation. A row already resolved elsewhere
    raises ValueError from the service; we render an idempotent "already handled"
    partial (200) rather than a 500. The response carries the resolved row plus an
    hx-swap-oob badge (and an Inbox-zero flash when the last item clears).
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "The review queue is available to the operator only."
        )
    if (surface, action) not in _REVIEW_ACTIONS:
        return HttpResponseBadRequest("Unknown review surface or action.")

    row_id = str(id)
    actor = f"ui-session:{request.user.pk}"
    try:
        item = _dispatch_review_action(request, surface, row_id, action, actor)
        item["resolved"] = True
    except _ActionBadRequest as exc:
        return HttpResponseBadRequest(str(exc))
    except ValueError:
        item = {"already_handled": True}
    item["row_id"] = row_id
    total = pending_reviews()["total"]
    return render(
        request,
        "brain/_review_action.html",
        {
            "surface": surface,
            "item": item,
            "review_pending_total": total,
            "inbox_zero": total == 0,
        },
    )


@brain_login_required
def merge_evidence(request, id):
    """Lazy-loaded evidence partial for a merge-candidate card (#155).

    Superuser-gated like the rest of /review. Returns ≤2 viewer-scoped example
    experiences per side so the operator can confirm two entities mean the same
    thing without leaving the queue. A vanished candidate renders an empty
    partial rather than 404 — the toggle just shows "no examples".
    """
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "The review queue is available to the operator only."
        )
    evidence = merge_candidate_evidence(viewer_id(request), str(id))
    return render(request, "brain/_review_evidence.html", {"evidence": evidence})


@brain_login_required
def experience_detail(request, id):
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    detail = get_experience_detail(viewer_id(request), str(id))
    if detail is None:
        # Missing and private-and-not-mine are an identical 404 (US-3).
        raise Http404
    # Owner-only write controls; can_change_visibility is the owner predicate.
    detail["is_owner"] = detail["experience"]["can_change_visibility"]
    return render(request, "brain/experience_detail.html", detail)


@brain_login_required
def entity_detail(request, id):
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    limit, offset = _pagination(request)
    detail = get_entity_detail(viewer_id(request), str(id), limit, offset)
    if detail is None:
        raise Http404
    detail["entity_id"] = str(id)
    detail["mentions_limit"] = limit
    return render(request, "brain/entity_detail.html", detail)


@brain_login_required
def search(request):
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    query = (request.GET.get("q") or "").strip()
    limit = max(
        1, min(_int_param(request, "limit", DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT)
    )
    # results is None until a query is run, distinguishing "no query yet" from
    # an empty result set in the template.
    context = {"q": query, "results": None, "error": False}
    if query:
        try:
            context["results"] = search_experiences(viewer_id(request), query, limit)
        except EmbeddingError:
            # The embedding service is a read-time dependency; degrade to a
            # retry-able message instead of a 500.
            context["error"] = True
    template = "brain/_results.html" if request.htmx else "brain/search.html"
    return render(request, template, context)


@brain_login_required
def entity_mentions(request, id):
    """htmx 'Load more' partial: one further page of an entity's mentions."""
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    limit, offset = _pagination(request)
    page = get_entity_mentions(viewer_id(request), str(id), limit, offset)
    page["entity_id"] = str(id)
    page["mentions_limit"] = limit
    return render(request, "brain/_mentions_page.html", page)


def _redirect(request, url):
    """302 on a hard nav; 204 + HX-Redirect so htmx does a full client redirect."""
    if getattr(request, "htmx", False):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _owned_experience_or_deny(viewer, experience_id):
    """Load an experience for an owner-only GET; raise Http404/return 403 view."""
    detail = get_experience_detail(viewer, experience_id)
    if detail is None:
        raise Http404
    if not detail["experience"]["can_change_visibility"]:
        return None, HttpResponseForbidden("Only the owner may edit this experience.")
    return detail["experience"], None


@brain_login_required
def experience_edit(request, id):
    """GET the owner-only edit form; POST applies a content/metadata edit (US-5)."""
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    viewer = viewer_id(request)
    experience_id = str(id)

    if request.method == "POST":
        return _do_edit(request, viewer, experience_id)

    experience, denied = _owned_experience_or_deny(viewer, experience_id)
    if denied is not None:
        return denied
    if not experience["is_live"]:
        # A superseded/deleted row is an audit artifact, not editable.
        raise Http404
    experience["metadata_json"] = _metadata_json(experience.get("metadata"))
    return render(request, "brain/edit.html", {"experience": experience})


def _metadata_json(metadata):
    """Pretty JSON for the edit textarea; empty string when there's no metadata."""
    return json.dumps(metadata, indent=2) if metadata else ""


def _do_edit(request, viewer, experience_id):
    kwargs = {}
    content = request.POST.get("content")
    if content:
        kwargs["content"] = content
    metadata_raw = (request.POST.get("metadata") or "").strip()
    if metadata_raw:
        try:
            kwargs["metadata"] = json.loads(metadata_raw)
        except json.JSONDecodeError:
            return _edit_error(
                request, viewer, experience_id, "Metadata must be valid JSON."
            )
    if not kwargs:
        return _edit_error(request, viewer, experience_id, "Nothing to change.")

    try:
        result = edit_experience(viewer, experience_id, **kwargs)
    except ExperienceNotFound as exc:
        raise Http404 from exc
    except NotOwner:
        return HttpResponseForbidden("Only the owner may edit this experience.")
    except EmbeddingError:
        return _edit_error(request, viewer, experience_id, _EDIT_SERVICE_ERROR)

    # On supersede the old row is now an audit artifact; land on the live version.
    target = result.get("new_id") or experience_id
    return _redirect(request, f"/experience/{target}")


def _edit_error(request, viewer, experience_id, message):
    # Re-render the form with the user's submitted text preserved (no write ran).
    detail = get_experience_detail(viewer, experience_id)
    if detail is None:
        raise Http404
    experience = detail["experience"]
    experience["metadata_json"] = _metadata_json(experience.get("metadata"))
    return render(
        request,
        "brain/edit.html",
        {
            "experience": experience,
            "error": message,
            "submitted_content": request.POST.get("content"),
            "submitted_metadata": request.POST.get("metadata"),
        },
    )


@brain_login_required
def experience_delete(request, id):
    """GET the owner-only confirm modal with usage counts; POST soft-deletes (US-6)."""
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    viewer = viewer_id(request)
    experience_id = str(id)

    if request.method == "POST":
        try:
            soft_delete_experience(viewer, experience_id)
        except ExperienceNotFound as exc:
            raise Http404 from exc
        except NotOwner:
            return HttpResponseForbidden("Only the owner may delete this experience.")
        # The row stays reachable, now with a deleted banner.
        return _redirect(request, f"/experience/{experience_id}")

    experience, denied = _owned_experience_or_deny(viewer, experience_id)
    if denied is not None:
        return denied
    return render(
        request,
        "brain/_delete_modal.html",
        {"experience": experience, "usage": get_usage(viewer, experience_id)},
    )


@brain_login_required
def experience_visibility(request, id):
    """POST-only owner share toggle; returns the refreshed control partial (US-7)."""
    if not brain_schema_present():
        return render(request, "brain/_schema_unavailable.html")
    if request.method != "POST":
        return HttpResponseBadRequest("visibility is changed via POST")
    viewer = viewer_id(request)
    experience_id = str(id)
    try:
        result = set_visibility(
            viewer, experience_id, request.POST.get("visibility") or ""
        )
    except ValueError:
        return HttpResponseBadRequest("visibility must be 'private' or 'shared'")
    except ExperienceNotFound as exc:
        raise Http404 from exc
    except NotOwner:
        return HttpResponseForbidden("Only the owner may change visibility.")
    return render(
        request,
        "brain/_visibility_control.html",
        {
            "experience": {
                "id": experience_id,
                "visibility": result["visibility"],
                "is_owner": True,
            }
        },
    )
