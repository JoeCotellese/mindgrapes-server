"""Brain UI URL patterns (Epic #35, #101).

Mounted at the site root by config/urls.py: the dashboard is the post-login
landing page. Detail pages are real, deep-linkable URLs; htmx swaps are
progressive enhancement on top.
"""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="brain-dashboard"),
    path("recent", views.recent_feed, name="brain-recent"),
    path("activity", views.activity_log, name="brain-activity"),
    path("review", views.review_inbox, name="brain-review"),
    # Registered before the generic action route below so the literal "evidence"
    # suffix isn't captured as <action> and routed to the POST-only review_action.
    path(
        "review/merge_candidates/<uuid:id>/evidence",
        views.merge_evidence,
        name="brain-merge-evidence",
    ),
    path(
        "review/<str:surface>/<uuid:id>/<str:action>",
        views.review_action,
        name="brain-review-action",
    ),
    path("search", views.search, name="brain-search"),
    path("experience/<uuid:id>", views.experience_detail, name="brain-experience"),
    path(
        "experience/<uuid:id>/edit", views.experience_edit, name="brain-experience-edit"
    ),
    path(
        "experience/<uuid:id>/delete",
        views.experience_delete,
        name="brain-experience-delete",
    ),
    path(
        "experience/<uuid:id>/visibility",
        views.experience_visibility,
        name="brain-experience-visibility",
    ),
    path("entity/<uuid:id>", views.entity_detail, name="brain-entity"),
    path(
        "entity/<uuid:id>/mentions", views.entity_mentions, name="brain-entity-mentions"
    ),
]
