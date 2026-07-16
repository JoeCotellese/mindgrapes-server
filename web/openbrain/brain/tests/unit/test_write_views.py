"""Unit tests for the brain write views (gating, wiring, exception mapping).

Run on sqlite with the schema guard forced True and the write services
monkeypatched to canned results/exceptions — we assert the owner gating, the
HTTP status mapping (404/403/400), the success redirect, and the edit-error
microcopy, not SQL. The DB-effect parity lives in the integration suite.
"""

import pytest

from openbrain.brain.embeddings import EmbeddingError
from openbrain.brain.exceptions import NotOwner

pytestmark = pytest.mark.django_db

ID = "11111111-1111-1111-1111-111111111111"
NEW_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user(email="m@example.com", password="x")


@pytest.fixture
def schema_on(monkeypatch):
    monkeypatch.setattr("openbrain.brain.views.brain_schema_present", lambda: True)


def _detail(*, is_owner=True, is_live=True):
    return {
        "experience": {
            "id": ID,
            "content": "the original thought",
            "metadata": {"k": 1},
            "visibility": "private",
            "is_live": is_live,
            "can_change_visibility": is_owner,
            "captured_at": None,
            "consolidation_status": "consolidated",
        },
        "mentions": [],
        "claims_sourced_here": [],
    }


def test_detail_shows_owner_controls(client, member, schema_on, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=True),
    )
    body = client.get(f"/experience/{ID}").content.decode()
    assert f"/experience/{ID}/edit" in body
    assert f"/experience/{ID}/delete" in body
    assert f"/experience/{ID}/visibility" in body


def test_detail_hides_controls_for_non_owner(client, member, schema_on, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=False),
    )
    body = client.get(f"/experience/{ID}").content.decode()
    assert f"/experience/{ID}/edit" not in body
    assert f"/experience/{ID}/delete" not in body


def test_edit_get_renders_form_for_owner(client, member, schema_on, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=True),
    )
    resp = client.get(f"/experience/{ID}/edit")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "the original thought" in body
    # The always-on supersede warning (no live cosine preview in v1).
    assert "new version" in body.lower()


def test_edit_get_forbidden_for_non_owner(client, member, schema_on, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=False),
    )
    assert client.get(f"/experience/{ID}/edit").status_code == 403


def test_edit_post_redirects_to_same_row_in_place(
    client, member, schema_on, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.edit_experience",
        lambda *a, **k: {"mode": "in_place", "new_id": None},
    )
    resp = client.post(f"/experience/{ID}/edit", {"content": "tweaked"})
    assert resp.status_code == 302
    assert resp.url == f"/experience/{ID}"


def test_edit_post_redirects_to_new_row_on_supersede(
    client, member, schema_on, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.edit_experience",
        lambda *a, **k: {"mode": "superseded", "new_id": NEW_ID},
    )
    resp = client.post(f"/experience/{ID}/edit", {"content": "totally different"})
    assert resp.status_code == 302
    assert resp.url == f"/experience/{NEW_ID}"


def test_edit_post_embedding_failure_rerenders_with_microcopy(
    client, member, schema_on, monkeypatch
):
    client.force_login(member)

    def _boom(*a, **k):
        raise EmbeddingError("openrouter down")

    monkeypatch.setattr("openbrain.brain.views.edit_experience", _boom)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=True),
    )
    resp = client.post(f"/experience/{ID}/edit", {"content": "tweaked"})
    assert resp.status_code == 200
    # No partial write happened; the user keeps their text and a retry message.
    assert "nothing was changed" in resp.content.decode().lower()


def test_edit_post_non_owner_forbidden(client, member, schema_on, monkeypatch):
    client.force_login(member)

    def _deny(*a, **k):
        raise NotOwner(ID)

    monkeypatch.setattr("openbrain.brain.views.edit_experience", _deny)
    assert client.post(f"/experience/{ID}/edit", {"content": "x"}).status_code == 403


def test_delete_get_renders_modal_with_usage(client, member, schema_on, monkeypatch):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.get_experience_detail",
        lambda v, i: _detail(is_owner=True),
    )
    monkeypatch.setattr(
        "openbrain.brain.views.get_usage",
        lambda v, i: {
            "claim_count": 3,
            "mentioned_entity_count": 2,
            "mentioned_entity_names": ["Acme", "Fernworks"],
        },
    )
    body = client.get(f"/experience/{ID}/delete").content.decode()
    assert "3" in body and "Acme" in body


def test_delete_post_soft_deletes_and_redirects(client, member, schema_on, monkeypatch):
    client.force_login(member)
    called = {}
    monkeypatch.setattr(
        "openbrain.brain.views.soft_delete_experience",
        lambda v, i: called.setdefault("hit", (v, i)) or {"already_deleted": False},
    )
    resp = client.post(f"/experience/{ID}/delete")
    assert resp.status_code == 302
    assert resp.url == f"/experience/{ID}"
    assert called["hit"][1] == ID


def test_delete_post_non_owner_forbidden(client, member, schema_on, monkeypatch):
    client.force_login(member)

    def _deny(*a, **k):
        raise NotOwner(ID)

    monkeypatch.setattr("openbrain.brain.views.soft_delete_experience", _deny)
    assert client.post(f"/experience/{ID}/delete").status_code == 403


def test_visibility_post_flips_and_returns_control(
    client, member, schema_on, monkeypatch
):
    client.force_login(member)
    monkeypatch.setattr(
        "openbrain.brain.views.set_visibility",
        lambda v, i, vis: {
            "id": i,
            "visibility": vis,
            "changed_fields": ["visibility"],
        },
    )
    resp = client.post(f"/experience/{ID}/visibility", {"visibility": "shared"})
    assert resp.status_code == 200
    # The swapped-in control reflects the new state and offers the reverse toggle.
    assert "shared" in resp.content.decode().lower()


def test_visibility_post_bad_value_is_400(client, member, schema_on, monkeypatch):
    client.force_login(member)

    def _bad(*a, **k):
        raise ValueError("nope")

    monkeypatch.setattr("openbrain.brain.views.set_visibility", _bad)
    assert (
        client.post(
            f"/experience/{ID}/visibility", {"visibility": "public"}
        ).status_code
        == 400
    )


def test_visibility_post_non_owner_forbidden(client, member, schema_on, monkeypatch):
    client.force_login(member)

    def _deny(*a, **k):
        raise NotOwner(ID)

    monkeypatch.setattr("openbrain.brain.views.set_visibility", _deny)
    resp = client.post(f"/experience/{ID}/visibility", {"visibility": "private"})
    assert resp.status_code == 403
