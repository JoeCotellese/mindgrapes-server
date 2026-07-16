"""Unit tests for the brain views (routing, auth gate, schema guard, rendering).

These run on sqlite, where brain.* is absent, so the schema guard short-circuits
to a friendly page. To exercise the populated render paths we monkeypatch the
schema probe to True and the read service to canned data — the template wiring
and a11y markers are what we assert, not SQL.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user(email="m@example.com", password="x")


def test_dashboard_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/accounts/login/" in resp.url
    assert "next=/" in resp.url


def test_dashboard_schema_unavailable_renders_friendly_page(client, member):
    client.force_login(member)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"not available" in resp.content.lower()


def test_dashboard_renders_stat_cards(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_summary",
        lambda: {
            "experience_count": 12,
            "entity_count": 7,
            "claim_count": 30,
            "time_range_earliest": None,
            "time_range_latest": None,
            "top_entities": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "canonical_name": "Acme",
                    "kind": "org",
                    "mention_count": 9,
                }
            ],
            "top_topics": [{"topic": "product", "count": 4}],
            "refreshed_at": None,
        },
    )
    monkeypatch.setattr(
        "openbrain.brain.views.recently_active_entities", lambda viewer: []
    )
    resp = client.get("/")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "12" in body and "Acme" in body and "product" in body
    # Top entities link through to entity detail.
    assert "/entity/11111111-1111-1111-1111-111111111111" in body
    # Top topics link through to a search for that topic.
    assert "/search?q=product" in body
    # The navbar already carries Search; the dashboard header must not duplicate it.
    assert 'class="button is-link is-light level-item" href="/search"' not in body


def test_dashboard_empty_state_when_brain_empty(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.get_summary", lambda: None)
    monkeypatch.setattr(
        "openbrain.brain.views.recently_active_entities", lambda viewer: []
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Capture your first thought" in resp.content


def _populated_summary():
    return {
        "experience_count": 12,
        "entity_count": 7,
        "claim_count": 30,
        "time_range_earliest": None,
        "time_range_latest": None,
        "top_entities": [],
        "top_topics": [],
        "refreshed_at": None,
    }


def test_dashboard_renders_recently_active_section(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.get_summary", _populated_summary)
    monkeypatch.setattr(
        "openbrain.brain.views.recently_active_entities",
        lambda viewer: [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "canonical_name": "Acme",
                "kind": "org",
                "last_mentioned_at": timezone.now() - timedelta(days=3),
                "recency": "3d",
            }
        ],
    )
    resp = client.get("/")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Recently active" in body
    # Chip links to entity detail, shows the recency suffix, and is labelled.
    assert "/entity/22222222-2222-2222-2222-222222222222" in body
    assert "3d" in body
    assert "Acme, last mentioned" in body


def test_dashboard_recently_active_empty_state(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.get_summary", _populated_summary)
    monkeypatch.setattr(
        "openbrain.brain.views.recently_active_entities", lambda viewer: []
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No recent activity." in resp.content


EXP_URL = "/experience/11111111-1111-1111-1111-111111111111"


def _superseded_detail():
    return {
        "experience": {
            "id": "11111111-1111-1111-1111-111111111111",
            "content": "a thought worth keeping",
            "captured_at": None,
            "occurred_at": None,
            "occurred_window": None,
            "source_kind": "chat",
            "source_ref": None,
            "metadata": {},
            "consolidation_status": "raw",
            "superseded_by": "22222222-2222-2222-2222-222222222222",
            "deleted_at": None,
            "owner": "1",
            "visibility": "private",
            "is_live": False,
            "can_change_visibility": True,
        },
        "mentions": [
            {
                "entity_id": "33333333-3333-3333-3333-333333333333",
                "canonical_name": "Acme",
                "kind": "org",
                "surface_form": "Acme",
                "merged_into": None,
            }
        ],
        "claims_sourced_here": [],
    }


def test_experience_detail_requires_login(client):
    resp = client.get(EXP_URL)
    assert resp.status_code == 302


def test_experience_detail_404_when_service_returns_none(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.get_experience_detail", lambda *a: None)
    resp = client.get(EXP_URL)
    assert resp.status_code == 404


def test_experience_detail_renders_content_and_lifecycle_banner(
    client, member, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda *a: _superseded_detail(),
    )
    resp = client.get(EXP_URL)
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "a thought worth keeping" in body
    # Lifecycle banner is color-independent: the word, not just a hue.
    assert "superseded" in body.lower()
    # Mentions link through to entity detail.
    assert "Acme" in body
    assert "/entity/33333333-3333-3333-3333-333333333333" in body


ENT_URL = "/entity/33333333-3333-3333-3333-333333333333"


def test_entity_detail_requires_login(client):
    resp = client.get(ENT_URL)
    assert resp.status_code == 302


def test_entity_detail_404_when_service_returns_none(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.get_entity_detail", lambda *a, **k: None)
    resp = client.get(ENT_URL)
    assert resp.status_code == 404


def test_entity_detail_canonical_renders_mentions_and_links(
    client, member, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    detail = {
        "is_merged": False,
        "entity": {
            "id": "33333333-3333-3333-3333-333333333333",
            "canonical_name": "Acme",
            "kind": "org",
            "aliases": ["Acme Inc"],
            "confidence": 0.9,
            "metadata": {},
            "merged_into": None,
            "created_at": None,
        },
        "mention_count": 1,
        "mentions": [
            {
                "experience_id": "11111111-1111-1111-1111-111111111111",
                "captured_at": None,
                "occurred_at": None,
                "surface_form": "Acme",
                "content_excerpt": "met with <mark>Acme</mark> today",
            }
        ],
        "has_more": False,
        "next_offset": 1,
        "claims_as_subject": [],
        "claims_as_object": [],
    }
    monkeypatch.setattr(
        "openbrain.brain.views.get_entity_detail", lambda *a, **k: detail
    )
    body = client.get(ENT_URL).content.decode()
    assert "Acme" in body
    # Excerpt HTML is rendered, not escaped.
    assert "<mark>Acme</mark>" in body
    assert "/experience/11111111-1111-1111-1111-111111111111" in body


def test_entity_detail_merged_shows_redirect(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    detail = {
        "is_merged": True,
        "merged_into": "44444444-4444-4444-4444-444444444444",
        "merge_audit": [
            {
                "correction_event_id": "ce1",
                "before": {},
                "after": {},
                "reason": "duplicate",
                "created_at": None,
                "created_by": "consolidation",
            }
        ],
        "winner": {
            "id": "44444444-4444-4444-4444-444444444444",
            "canonical_name": "Acme",
            "kind": "org",
        },
    }
    monkeypatch.setattr(
        "openbrain.brain.views.get_entity_detail", lambda *a, **k: detail
    )
    body = client.get(ENT_URL).content.decode()
    assert "merged" in body.lower()
    assert "/entity/44444444-4444-4444-4444-444444444444" in body
    assert "Acme" in body


def test_entity_mentions_partial_renders_next_page(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    page = {
        "mentions": [
            {
                "experience_id": "55555555-5555-5555-5555-555555555555",
                "captured_at": None,
                "occurred_at": None,
                "surface_form": "Acme",
                "content_excerpt": "x <mark>Acme</mark> y",
            }
        ],
        "mention_count": 3,
        "next_offset": 2,
        "has_more": True,
    }
    monkeypatch.setattr(
        "openbrain.brain.views.get_entity_mentions", lambda *a, **k: page
    )
    resp = client.get(ENT_URL + "/mentions?offset=1&limit=1")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "/experience/55555555-5555-5555-5555-555555555555" in body
    # The Load more control advances to the next offset.
    assert "offset=2" in body


SEARCH_URL = "/search"


def _result_row():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "content": "Met with Acme about the deal",
        "metadata": {},
        "captured_at": None,
        "occurred_at": None,
        "vec_score": 0.8,
        "lex_score": 0.4,
        "fused_score": 0.9,
        "mentioned_entities": [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "canonical_name": "Acme",
                "kind": "org",
            }
        ],
        "claim_count": 2,
    }


def test_search_requires_login(client):
    assert client.get(SEARCH_URL).status_code == 302


def test_search_empty_query_shows_form(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    resp = client.get(SEARCH_URL)
    body = resp.content.decode()
    assert resp.status_code == 200
    assert 'name="q"' in body


def test_search_full_page_renders_results_with_chips(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.search_experiences", lambda *a: [_result_row()]
    )
    resp = client.get(SEARCH_URL + "?q=acme")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Acme" in body
    assert "/experience/11111111-1111-1111-1111-111111111111" in body
    assert "/entity/33333333-3333-3333-3333-333333333333" in body
    # Score chips carry a text label, not hue alone.
    assert "fused" in body and "vec" in body and "lex" in body
    assert 'name="q"' in body  # full page keeps the form


def test_search_htmx_returns_results_partial_without_form(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.search_experiences", lambda *a: [])
    resp = client.get(SEARCH_URL + "?q=nothingmatches", HTTP_HX_REQUEST="true")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "<form" not in body
    assert "nothingmatches" in body  # zero-result state echoes the query


def test_search_embedding_failure_degrades_gracefully(client, member, monkeypatch):
    from openbrain.brain.embeddings import EmbeddingError

    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)

    def boom(*a):
        raise EmbeddingError("service down")

    monkeypatch.setattr("openbrain.brain.views.search_experiences", boom)
    resp = client.get(SEARCH_URL + "?q=x")
    assert resp.status_code == 200
    assert "unavailable" in resp.content.decode().lower()


RECENT_URL = "/recent"


def _feed_row(visibility="private", source="mcp"):
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "snippet": "Met with Acme about the deal",
        "captured_at": None,
        "source": source,
        "visibility": visibility,
        "entities": [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "canonical_name": "Acme",
                "kind": "org",
            }
        ],
    }


def _feed_page(rows=None, has_more=False, next_offset=1):
    return {
        "experiences": rows if rows is not None else [_feed_row()],
        "has_more": has_more,
        "next_offset": next_offset,
    }


def test_recent_requires_login(client):
    assert client.get(RECENT_URL).status_code == 302


def test_recent_schema_unavailable_renders_friendly_page(client, member):
    client.force_login(member)
    resp = client.get(RECENT_URL)
    assert resp.status_code == 200
    assert b"not available" in resp.content.lower()


def test_recent_renders_feed_rows_with_badges_and_links(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed",
        lambda *a, **k: _feed_page(rows=[_feed_row(visibility="shared")]),
    )
    body = client.get(RECENT_URL).content.decode()
    assert "Met with Acme about the deal" in body
    # Snippet links to the experience; entity tag links to entity detail.
    assert "/experience/11111111-1111-1111-1111-111111111111" in body
    assert "/entity/33333333-3333-3333-3333-333333333333" in body
    # Badges are color-independent: the word, not just a hue.
    assert "Shared" in body
    assert "mcp" in body  # source badge text


def test_recent_private_badge_shows_word(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed",
        lambda *a, **k: _feed_page(rows=[_feed_row(visibility="private")]),
    )
    body = client.get(RECENT_URL).content.decode()
    assert "Private" in body


def test_recent_empty_state(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed", lambda *a, **k: _feed_page(rows=[])
    )
    body = client.get(RECENT_URL).content.decode()
    assert "Capture a thought" in body


def test_recent_htmx_returns_feed_page_partial(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed",
        lambda *a, **k: _feed_page(has_more=True, next_offset=20),
    )
    resp = client.get(RECENT_URL + "?offset=20", HTTP_HX_REQUEST="true")
    body = resp.content.decode()
    assert resp.status_code == 200
    # A partial: no page chrome (navbar) and no tabs header.
    assert "<nav" not in body
    # The Load more control advances to the next offset.
    assert "offset=20" in body
    assert "/experience/11111111-1111-1111-1111-111111111111" in body


def test_recent_no_load_more_on_last_page(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed",
        lambda *a, **k: _feed_page(has_more=False),
    )
    resp = client.get(RECENT_URL, HTTP_HX_REQUEST="true")
    assert "Load more" not in resp.content.decode()


def _tl_row(id, captured_at, snippet="a timeline thought"):
    return {
        "id": id,
        "snippet": snippet,
        "captured_at": captured_at,
        "source": "mcp",
        "visibility": "private",
        "entities": [],
    }


def _tl_page(rows, has_more=False, next_offset=20):
    return {"experiences": rows, "has_more": has_more, "next_offset": next_offset}


def test_recent_timeline_renders_section_headers_and_omits_empty(
    client, member, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    now = timezone.now()
    rows = [
        _tl_row("11111111-1111-1111-1111-111111111111", now),  # Today
        _tl_row("22222222-2222-2222-2222-222222222222", now - timedelta(days=40)),
    ]
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed", lambda *a, **k: _tl_page(rows)
    )
    resp = client.get(RECENT_URL + "?view=timeline")
    body = resp.content.decode()
    assert resp.status_code == 200
    # Real headings grouped in labelled sections so the rotor lists buckets.
    assert '<section aria-labelledby="bucket-today"' in body
    assert 'id="bucket-today"' in body and ">Today<" in body
    assert ">Earlier<" in body
    # Empty buckets are omitted entirely.
    assert "Yesterday" not in body
    assert "This week" not in body
    assert "/experience/11111111-1111-1111-1111-111111111111" in body


def test_recent_timeline_loadmore_continuation_suppresses_duplicate_header(
    client, member, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    old = timezone.now() - timedelta(days=40)  # all fall in Earlier
    rows = [_tl_row("33333333-3333-3333-3333-333333333333", old)]
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed",
        lambda *a, **k: _tl_page(rows, has_more=True, next_offset=40),
    )
    resp = client.get(
        RECENT_URL + "?view=timeline&offset=20&cont=earlier", HTTP_HX_REQUEST="true"
    )
    body = resp.content.decode()
    assert resp.status_code == 200
    # Continuing the prior page's bucket: no new header/section, rows append OOB.
    assert "<h2" not in body
    assert "bucket-earlier" not in body
    assert 'hx-swap-oob="beforeend:#rows-earlier"' in body
    # Load-more keeps paging, still continuing the same bucket.
    assert "offset=40" in body
    assert "cont=earlier" in body


def test_recent_timeline_loadmore_new_bucket_emits_section(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    old = timezone.now() - timedelta(days=40)  # Earlier
    rows = [_tl_row("44444444-4444-4444-4444-444444444444", old)]
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed", lambda *a, **k: _tl_page(rows)
    )
    # Prior page ended in This week; this page opens a fresh Earlier section.
    resp = client.get(
        RECENT_URL + "?view=timeline&offset=20&cont=this-week", HTTP_HX_REQUEST="true"
    )
    body = resp.content.decode()
    assert resp.status_code == 200
    assert '<section aria-labelledby="bucket-earlier"' in body
    assert ">Earlier<" in body
    assert "hx-swap-oob" not in body


def test_recent_timeline_empty_state(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed", lambda *a, **k: _tl_page([])
    )
    body = client.get(RECENT_URL + "?view=timeline").content.decode()
    assert "Capture a thought" in body


def test_recent_navbar_marks_active_item(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_recent_feed", lambda *a, **k: _feed_page(rows=[])
    )
    body = client.get(RECENT_URL).content.decode()
    # The shared navbar exposes the suite's destinations; Recent is current.
    assert 'aria-current="page"' in body
    assert "Activity" in body and "Review" in body and "Search" in body


# --- /activity (#136): superuser-gated change-event audit log ----------------

ACTIVITY_URL = "/activity"


@pytest.fixture
def admin(django_user_model):
    return django_user_model.objects.create_superuser(email="a@example.com")


def _event(
    change_type="supersede",
    change_label="Superseded",
    chip_class="is-warning is-light",
    target=None,
    secondary=None,
    actor=None,
    has_diff=True,
):
    return {
        "id": "ce-1",
        "change_type": change_type,
        "change_label": change_label,
        "chip_class": chip_class,
        "target": target
        or {
            "kind": "experience",
            "id": "11111111-1111-1111-1111-111111111111",
            "href": "/experience/11111111-1111-1111-1111-111111111111",
            "label": "Met with Acme about the deal",
        },
        "secondary": secondary,
        "actor": actor or {"kind": "human", "label": "You (web)", "is_auto": False},
        "reason": "supersede (cosine 0.50)",
        "created_at": None,
        "before": {"content": "old"},
        "after": {"content": "new"},
        "before_pretty": '{\n  "content": "old"\n}',
        "after_pretty": '{\n  "content": "new"\n}',
        "has_diff": has_diff,
    }


def _activity_page(events=None, has_more=False, next_offset=1):
    return {
        "events": events if events is not None else [_event()],
        "has_more": has_more,
        "next_offset": next_offset,
        "feed_limit": 20,
    }


def test_activity_requires_login(client):
    assert client.get(ACTIVITY_URL).status_code == 302


def test_activity_schema_unavailable_renders_friendly_page(client, admin):
    client.force_login(admin)
    resp = client.get(ACTIVITY_URL)
    assert resp.status_code == 200
    assert b"not available" in resp.content.lower()


def test_activity_forbidden_for_non_superuser(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity", lambda *a, **k: _activity_page()
    )
    assert client.get(ACTIVITY_URL).status_code == 403


def test_activity_renders_event_rows_for_superuser(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity", lambda *a, **k: _activity_page()
    )
    body = client.get(ACTIVITY_URL).content.decode()
    # Chip carries the WORD (not color alone) and the target links through.
    assert "Superseded" in body
    assert "/experience/11111111-1111-1111-1111-111111111111" in body
    # The before/after expander is a native <details>.
    assert "<details" in body and "Show change" in body


def test_activity_merge_links_both_source_and_survivor(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    event = _event(
        change_type="merge",
        change_label="Merged",
        chip_class="is-link is-light",
        target={
            "kind": "entity",
            "id": "aaaa",
            "href": "/entity/aaaa",
            "label": "Acme",
        },
        secondary={"href": "/entity/bbbb", "label": "Acme Inc"},
    )
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity",
        lambda *a, **k: _activity_page(events=[event]),
    )
    body = client.get(ACTIVITY_URL).content.decode()
    assert "/entity/aaaa" in body and "/entity/bbbb" in body
    assert "Acme Inc" in body


def test_activity_worker_event_shows_auto_marker(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    event = _event(
        change_type="retract",
        change_label="Retracted",
        actor={
            "kind": "consolidation",
            "label": "Consolidation worker",
            "is_auto": True,
        },
    )
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity",
        lambda *a, **k: _activity_page(events=[event]),
    )
    body = client.get(ACTIVITY_URL).content.decode()
    assert "Consolidation worker" in body
    assert "auto" in body


def test_activity_empty_state(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity", lambda *a, **k: _activity_page(events=[])
    )
    body = client.get(ACTIVITY_URL).content.decode()
    assert "No changes recorded yet" in body


def test_activity_htmx_returns_page_partial(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity",
        lambda *a, **k: _activity_page(has_more=True, next_offset=20),
    )
    resp = client.get(ACTIVITY_URL + "?offset=20", HTTP_HX_REQUEST="true")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "<nav" not in body  # a partial — no page chrome
    assert "offset=20" in body  # Load-more advances


def test_activity_no_load_more_on_last_page(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.get_activity",
        lambda *a, **k: _activity_page(has_more=False),
    )
    resp = client.get(ACTIVITY_URL, HTTP_HX_REQUEST="true")
    assert "activity-more" not in resp.content.decode()


# --- /review (#137): superuser-gated review workbench -------------------------

REVIEW_URL = "/review"


def _counts(**over):
    base = {
        "merge_candidates": 0,
        "low_confidence_claims": 0,
        "contradictions": 0,
        "disambiguations": 0,
        "proposed_corrections": 0,
    }
    base.update(over)
    base["total"] = sum(base.values())
    return base


def _merge_row(rid="mc-1"):
    return {
        "id": rid,
        "entity_a": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "entity_b": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "entity_a_name": "Acme",
        "entity_a_kind": "org",
        "entity_a_count": 12,
        "entity_b_name": "Acme Inc",
        "entity_b_kind": "org",
        "entity_b_count": 2,
        "similarity": 0.82,
        "created_at": None,
    }


def _correction_row(rid="pc-1"):
    return {
        "id": rid,
        "target_kind": "entity",
        "target_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "suggested_change": {"action": "rename", "new_canonical_name": "Right"},
        "reason": "typo",
        "created_at": None,
    }


def _contradiction_row(cid="cl-1"):
    return {
        "claim_id": cid,
        "superseded_by": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "subject_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "subject_name": "Acme",
        "predicate": "works_at",
    }


def _patch_review(monkeypatch, *, counts, queue=None, deferred=0):
    """Make the /review view + badge context processor render against canned data."""
    queue = queue or {}

    def _review_queue(kind="all"):
        full = {
            "merge_candidates": [],
            "merge_candidates_deferred": deferred,
            "low_confidence_claims": [],
            "contradictions": [],
            "disambiguations": [],
            "proposed_corrections": [],
        }
        if kind == "all":
            full.update(queue)
        elif kind in queue:
            full[kind] = queue[kind]
        return full

    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.pending_reviews", lambda: counts)
    monkeypatch.setattr("openbrain.brain.views.review_queue", _review_queue)
    monkeypatch.setattr(
        "openbrain.brain.views.attach_entity_names", lambda rows, surface: rows
    )
    # Badge is rendered by the context processor on full-page loads.
    monkeypatch.setattr(
        "openbrain.brain.context_processors.brain_schema_present", lambda: True
    )
    monkeypatch.setattr(
        "openbrain.brain.context_processors.pending_reviews", lambda: counts
    )


def test_review_requires_login(client):
    assert client.get(REVIEW_URL).status_code == 302


def test_review_schema_unavailable_renders_friendly_page(client, admin):
    client.force_login(admin)
    resp = client.get(REVIEW_URL)
    assert resp.status_code == 200
    assert b"not available" in resp.content.lower()


def test_review_forbidden_for_non_superuser(client, member, monkeypatch):
    client.force_login(member)
    _patch_review(monkeypatch, counts=_counts(merge_candidates=1))
    assert client.get(REVIEW_URL).status_code == 403


def test_review_renders_tabs_and_badge_for_superuser(client, admin, monkeypatch):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(merge_candidates=2, proposed_corrections=1),
        queue={"merge_candidates": [_merge_row()]},
    )
    body = client.get(REVIEW_URL).content.decode()
    # Default active surface is the first non-empty one (merge candidates here).
    assert "Acme" in body and "Acme Inc" in body
    # Navbar badge carries the total (3) as a danger tag.
    assert 'id="review-badge"' in body
    assert "tag is-danger is-rounded" in body
    assert ">3<" in body


def test_review_merge_surface_shows_deferred_note(client, admin, monkeypatch):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(merge_candidates=1),
        queue={"merge_candidates": [_merge_row()]},
        deferred=41,
    )
    body = client.get(REVIEW_URL).content.decode()
    assert "41 low-impact pairs deferred" in body


def test_review_merge_surface_omits_deferred_note_when_zero(
    client, admin, monkeypatch
):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(merge_candidates=1),
        queue={"merge_candidates": [_merge_row()]},
    )
    body = client.get(REVIEW_URL).content.decode()
    assert "low-impact" not in body


def test_review_merge_row_has_confirm_reject_with_a11y_labels(
    client, admin, monkeypatch
):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(merge_candidates=1),
        queue={"merge_candidates": [_merge_row()]},
    )
    body = client.get(REVIEW_URL).content.decode()
    # Decision buttons use text labels, never icon-only, and name the target.
    assert "aria-label" in body
    assert "Acme" in body  # the labels spell out the entities
    # #155: the row buttons are recolored to equal-weight neutral — Merge… is a
    # neutral is-link, Keep separate sheds the is-danger red that framed the safe
    # no-op as dangerous.
    assert "is-link" in body
    assert "is-danger is-light" not in body


def test_review_merge_row_shows_counts_reversibility_and_evidence_toggle(
    client, admin, monkeypatch
):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(merge_candidates=1),
        queue={"merge_candidates": [_merge_row()]},
    )
    body = client.get(REVIEW_URL).content.decode()
    # Mention counts render as plain text next to each name (a11y: not color-only).
    assert "(12)" in body and "(2)" in body
    # One quiet line tells the user the merge is reversible.
    assert "reversible" in body.lower()
    # Evidence toggle is a real button with aria-expanded that lazy-loads once.
    assert 'aria-expanded="false"' in body
    assert 'hx-get="/review/merge_candidates/mc-1/evidence"' in body
    assert 'hx-trigger="click once"' in body
    assert 'id="evidence-mc-1"' in body


# --- /review/merge_candidates/<id>/evidence (#155) ----------------------------

EVIDENCE_URL = "/review/merge_candidates/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/evidence"


def _evidence():
    return {
        "a": {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "name": "Acme",
            "experiences": [
                {
                    "experience_id": "11111111-1111-1111-1111-111111111111",
                    "captured_at": None,
                    "occurred_at": None,
                    "surface_form": "Acme",
                    # Pre-escaped HTML from format_excerpt: the <mark> must survive
                    # to the page, not be re-escaped into visible &lt;mark&gt;.
                    "content_excerpt": "Met <mark>Acme</mark> about the renewal.",
                }
            ],
        },
        "b": {
            "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "name": "Acme Inc",
            "experiences": [],
        },
    }


def test_evidence_requires_login(client):
    assert client.get(EVIDENCE_URL).status_code == 302


def test_evidence_forbidden_for_non_superuser(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    assert client.get(EVIDENCE_URL).status_code == 403


def test_evidence_renders_experiences_and_empty_side(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr(
        "openbrain.brain.views.merge_candidate_evidence",
        lambda viewer, cid: _evidence(),
    )
    body = client.get(EVIDENCE_URL).content.decode()
    # Excerpt HTML passes through unescaped (|safe) so the <mark> highlight renders.
    assert "Met <mark>Acme</mark> about the renewal." in body
    assert "&lt;mark&gt;" not in body
    assert "Acme" in body and "Acme Inc" in body
    # The side with no readable experiences says so rather than rendering blank.
    assert "No readable examples" in body


def test_review_display_only_surface_has_deeplink_no_action_buttons(
    client, admin, monkeypatch
):
    client.force_login(admin)
    _patch_review(
        monkeypatch,
        counts=_counts(contradictions=1),
        queue={"contradictions": [_contradiction_row()]},
    )
    body = client.get(REVIEW_URL + "?surface=contradictions").content.decode()
    # Deep-links to the subject entity; no inline confirm/resolve button.
    assert "/entity/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee" in body
    assert "Acme" in body
    assert "hx-post" not in body


def test_review_inbox_zero_when_all_clear(client, admin, monkeypatch):
    client.force_login(admin)
    _patch_review(monkeypatch, counts=_counts())
    body = client.get(REVIEW_URL).content.decode()
    assert "Inbox zero" in body
    # Badge suppressed entirely at zero.
    assert "tag is-danger is-rounded" not in body


# --- /review action POST -----------------------------------------------------


def _action_url(surface, rid, action):
    return f"/review/{surface}/{rid}/{action}"


def test_review_action_requires_superuser(client, member, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    resp = client.post(
        _action_url(
            "merge_candidates", "11111111-1111-1111-1111-111111111111", "reject"
        )
    )
    assert resp.status_code == 403


def test_review_action_get_not_allowed(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    resp = client.get(
        _action_url(
            "merge_candidates", "11111111-1111-1111-1111-111111111111", "reject"
        )
    )
    assert resp.status_code == 405


def test_review_action_unknown_surface_or_action_is_400(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    resp = client.post(
        _action_url("bogus", "11111111-1111-1111-1111-111111111111", "reject")
    )
    assert resp.status_code == 400
    resp = client.post(
        _action_url(
            "merge_candidates", "11111111-1111-1111-1111-111111111111", "frobnicate"
        )
    )
    assert resp.status_code == 400


def test_review_action_confirm_merge_swaps_row_and_oob_badge(
    client, admin, monkeypatch
):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    captured = {}

    def _resolve(candidate_id, decision, *, winner_id=None, **kw):
        captured.update(id=candidate_id, decision=decision, winner_id=winner_id)
        return {"id": candidate_id, "decision": decision, "status": "merged"}

    monkeypatch.setattr("openbrain.brain.views.resolve_merge_candidate", _resolve)
    monkeypatch.setattr(
        "openbrain.brain.views.pending_reviews", lambda: _counts(merge_candidates=1)
    )
    rid = "11111111-1111-1111-1111-111111111111"
    win = "22222222-2222-2222-2222-222222222222"
    resp = client.post(
        _action_url("merge_candidates", rid, "confirm"), {"winner_id": win}
    )
    assert resp.status_code == 200
    assert captured == {"id": rid, "decision": "confirm", "winner_id": win}
    body = resp.content.decode()
    # The row is swapped to a resolved state and the badge updates out-of-band.
    assert 'hx-swap-oob="true"' in body
    assert 'id="review-badge"' in body


def test_review_action_idempotent_already_handled_not_500(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)

    def _raise(*a, **k):
        raise ValueError("resolve_merge_candidate: candidate X is already merged")

    monkeypatch.setattr("openbrain.brain.views.resolve_merge_candidate", _raise)
    monkeypatch.setattr("openbrain.brain.views.pending_reviews", lambda: _counts())
    resp = client.post(
        _action_url(
            "merge_candidates", "11111111-1111-1111-1111-111111111111", "reject"
        )
    )
    assert resp.status_code == 200
    assert "already handled" in resp.content.decode().lower()


def test_review_action_confirm_merge_requires_winner(client, admin, monkeypatch):
    client.force_login(admin)
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)
    monkeypatch.setattr("openbrain.brain.views.pending_reviews", lambda: _counts())
    resp = client.post(
        _action_url(
            "merge_candidates", "11111111-1111-1111-1111-111111111111", "confirm"
        )
    )
    assert resp.status_code == 400
