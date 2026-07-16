"""Unit tests for the activity-log read service (view-model transforms, no Postgres).

StubCursor feeds canned correction_events rows so we cover the Python side —
actor classification, change-type derivation from the event shape, target/secondary
link building, before/after parsing + pretty-printing, and the limit+1 has_more
probe. The real joins run against brain.* in the integration suite; here we assert
the projection and that pagination params are bound.
"""

from openbrain.brain.services.activity import (
    _change_type,
    _classify_actor,
    get_activity,
)

from ._support import StubCursor, patch_brain_cursor

# Column order MUST match the SELECT aliases in activity._ACTIVITY_SQL.
ACTIVITY_COLUMNS = [
    "id",
    "target_kind",
    "target_id",
    "before",
    "after",
    "reason",
    "created_at",
    "created_by",
    "exp_content",
    "entity_name",
    "winner_id",
    "winner_name",
    "claim_predicate",
    "claim_subject",
]


def _row(
    id="ce-1",
    target_kind="experience",
    target_id="exp-1",
    before=None,
    after=None,
    reason="edit in place",
    created_at=None,
    created_by="ui-session:7",
    exp_content="a captured thought",
    entity_name=None,
    winner_id=None,
    winner_name=None,
    claim_predicate=None,
    claim_subject=None,
):
    return (
        id,
        target_kind,
        target_id,
        {} if before is None else before,
        {} if after is None else after,
        reason,
        created_at,
        created_by,
        exp_content,
        entity_name,
        winner_id,
        winner_name,
        claim_predicate,
        claim_subject,
    )


def _patch(monkeypatch, cursor):
    patch_brain_cursor(monkeypatch, cursor, module="openbrain.brain.services.activity")


def _one(monkeypatch, row):
    cursor = StubCursor([(ACTIVITY_COLUMNS, [row])])
    _patch(monkeypatch, cursor)
    return get_activity(20, 0)["events"][0]


# --- _classify_actor: human (UI/MCP) vs. the autonomous worker vs. unknown ---


def test_classify_actor_ui_session_is_human_web():
    actor = _classify_actor("ui-session:7")
    assert actor["kind"] == "human"
    assert actor["is_auto"] is False
    assert "web" in actor["label"].lower()


def test_classify_actor_mcp_is_human_via_ai():
    actor = _classify_actor("mcp:merge_entities")
    assert actor["kind"] == "human"
    assert actor["is_auto"] is False


def test_classify_actor_consolidation_is_auto_worker():
    actor = _classify_actor("consolidation")
    assert actor["kind"] == "consolidation"
    assert actor["is_auto"] is True


def test_classify_actor_null_is_system_not_auto():
    actor = _classify_actor(None)
    assert actor["kind"] == "system"
    assert actor["is_auto"] is False
    assert actor["label"] == "unknown"


def test_classify_actor_unknown_string_surfaced_verbatim():
    actor = _classify_actor("some-batch-job")
    assert actor["kind"] == "system"
    assert actor["label"] == "some-batch-job"
    assert actor["is_auto"] is False


# --- _change_type: derived from target_kind + before/after keys ---


def test_change_type_experience_supersede():
    assert (
        _change_type("experience", {"superseded_by": None}, {"superseded_by": "x"})
        == "supersede"
    )


def test_change_type_experience_delete():
    assert (
        _change_type("experience", {"deleted_at": None}, {"deleted_at": "t"})
        == "delete"
    )


def test_change_type_experience_visibility():
    assert (
        _change_type("experience", {"visibility": "private"}, {"visibility": "shared"})
        == "visibility"
    )


def test_change_type_experience_edit_in_place():
    before = {"content": "a", "metadata": {}}
    after = {"content": "b", "metadata": {}}
    assert _change_type("experience", before, after) == "edit"


def test_change_type_claim_is_retract():
    assert (
        _change_type("claim", {"polarity": "asserted"}, {"polarity": "retracted"})
        == "retract"
    )


def test_change_type_entity_merge_when_merged_into_set():
    assert (
        _change_type("entity", {"merged_into": None}, {"merged_into": "winner"})
        == "merge"
    )


def test_change_type_entity_unmerge_when_merged_into_null():
    assert (
        _change_type("entity", {"merged_into": "x"}, {"merged_into": None}) == "unmerge"
    )


def test_change_type_entity_rename():
    assert (
        _change_type("entity", {"canonical_name": "Old"}, {"canonical_name": "New"})
        == "rename"
    )


def test_change_type_entity_split():
    before = {"entity_id": "s", "experience_ids": ["e1"]}
    after = {"entity_id": "new"}
    assert _change_type("entity", before, after) == "split"


# --- _format_row: chip, target/secondary links, diff, parsing ---


def test_format_supersede_row_labels_and_links_experience(monkeypatch):
    ev = _one(
        monkeypatch,
        _row(
            before={"content": "old", "superseded_by": None},
            after={"content": "new", "superseded_by": "exp-2"},
            reason="supersede (cosine 0.50)",
        ),
    )
    assert ev["change_type"] == "supersede"
    assert ev["change_label"] == "Superseded"
    assert "is-warning" in ev["chip_class"]
    assert ev["target"]["href"] == "/experience/exp-1"
    assert ev["target"]["label"] == "a captured thought"
    assert ev["secondary"] is None
    assert ev["actor"]["kind"] == "human"


def test_format_merge_row_links_both_source_and_survivor(monkeypatch):
    ev = _one(
        monkeypatch,
        _row(
            target_kind="entity",
            target_id="ent-src",
            before={"merged_into": None},
            after={"merged_into": "ent-win"},
            created_by="mcp:merge_entities",
            exp_content=None,
            entity_name="Acme",
            winner_id="ent-win",
            winner_name="Acme Inc",
        ),
    )
    assert ev["change_label"] == "Merged"
    assert ev["target"]["href"] == "/entity/ent-src"
    assert ev["target"]["label"] == "Acme"
    assert ev["secondary"]["href"] == "/entity/ent-win"
    assert ev["secondary"]["label"] == "Acme Inc"


def test_format_claim_row_has_no_link(monkeypatch):
    ev = _one(
        monkeypatch,
        _row(
            target_kind="claim",
            target_id="claim-1",
            before={"polarity": "asserted"},
            after={"polarity": "retracted"},
            created_by="consolidation",
            exp_content=None,
            claim_predicate="works_at",
            claim_subject="Joe",
        ),
    )
    assert ev["change_label"] == "Retracted"
    assert ev["target"]["href"] is None
    assert "works_at" in ev["target"]["label"]
    assert ev["actor"]["is_auto"] is True


def test_format_parses_json_strings_and_builds_pretty(monkeypatch):
    # jsonb arrives as a string in this stack; the service must parse it.
    ev = _one(
        monkeypatch,
        _row(
            before='{"content": "old"}',
            after='{"content": "new"}',
        ),
    )
    assert ev["before"] == {"content": "old"}
    assert ev["after"] == {"content": "new"}
    assert "old" in ev["before_pretty"]
    assert ev["has_diff"] is True


def test_format_empty_before_after_has_no_diff(monkeypatch):
    ev = _one(monkeypatch, _row(before={}, after={}))
    assert ev["has_diff"] is False
    assert ev["before_pretty"] == ""


# --- pagination: limit+1 probe, params, ordering passthrough ---


def test_get_activity_has_more_when_limit_plus_one_returned(monkeypatch):
    rows = [_row(id=f"ce-{i}", target_id=f"exp-{i}") for i in range(3)]
    cursor = StubCursor([(ACTIVITY_COLUMNS, rows)])
    _patch(monkeypatch, cursor)

    page = get_activity(2, 0)

    assert len(page["events"]) == 2  # probe row trimmed
    assert page["has_more"] is True
    assert page["next_offset"] == 2


def test_get_activity_last_page_reports_no_more(monkeypatch):
    rows = [_row(id=f"ce-{i}", target_id=f"exp-{i}") for i in range(2)]
    cursor = StubCursor([(ACTIVITY_COLUMNS, rows)])
    _patch(monkeypatch, cursor)

    page = get_activity(5, 10)

    assert len(page["events"]) == 2
    assert page["has_more"] is False
    assert page["next_offset"] == 12


def test_get_activity_binds_limit_probe_and_offset(monkeypatch):
    cursor = StubCursor([(ACTIVITY_COLUMNS, [])])
    _patch(monkeypatch, cursor)

    page = get_activity(20, 40)

    sql, params = cursor.calls[0]
    assert "from brain.correction_events" in sql
    assert "order by ce.created_at desc" in sql
    assert params["limit"] == 21  # limit + 1 probe
    assert params["offset"] == 40
    assert page["events"] == []
    assert page["has_more"] is False
