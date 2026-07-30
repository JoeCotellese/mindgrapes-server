"""Unit tests for the pure helpers in the reviews service (no Postgres).

plan_correction_dispatch maps a proposed_corrections.suggested_change onto the
repair tool that applies it; match_choice coerces a disambiguation selection
(index / label / object) back to the stored option. The DB-effecting paths
(review_queue / propose / resolve / disambiguation writes) are covered in the
integration suite.
"""

import pytest

from openbrain.brain.services import reviews
from openbrain.brain.services.reviews import (
    SOURCE_CAPTURE,
    SOURCE_CLAIM,
    _reconcile_provisional_binding,
    match_choice,
    plan_correction_dispatch,
)

TARGET = "22222222-2222-2222-2222-222222222222"


# --- provisional-binding reject dispatch --------------------------------------


def _binding_context(source):
    return {
        "experience_id": "exp-1",
        "surface": "Jon Smith",
        "field": "people" if source == SOURCE_CAPTURE else "claims",
        "entity_kind": "person",
        "source": source,
        "provisional_entity_id": "existing-1",
    }


def _spy_entities(monkeypatch):
    calls = {}

    def _spy(name, result):
        def _recorded(*_args, **kwargs):
            calls[name] = kwargs
            return result

        return _recorded

    monkeypatch.setattr(
        reviews.entities,
        "split_mention",
        _spy("split_mention", {"mentions_repointed": 1}),
    )
    monkeypatch.setattr(
        reviews.entities, "split_claims", _spy("split_claims", {"claims_repointed": 2})
    )
    return calls


def test_reject_of_a_capture_guess_repoints_the_mention(monkeypatch):
    calls = _spy_entities(monkeypatch)

    outcome = _reconcile_provisional_binding(
        _binding_context(SOURCE_CAPTURE), {"value": {"action": "reject"}}
    )

    assert outcome["action"] == "repointed"
    assert "split_mention" in calls and "split_claims" not in calls


def test_reject_of_a_claim_guess_repoints_the_claims(monkeypatch):
    # #73: the claim pass writes no mention, so split_mention would move nothing
    # and the human's "no, different" would be a silent no-op.
    calls = _spy_entities(monkeypatch)

    outcome = _reconcile_provisional_binding(
        _binding_context(SOURCE_CLAIM), {"value": {"action": "reject"}}
    )

    assert outcome["action"] == "repointed"
    assert outcome["claims_repointed"] == 2
    assert "split_claims" in calls and "split_mention" not in calls
    assert calls["split_claims"]["experience_id"] == "exp-1"


def test_reject_without_a_source_marker_still_repoints_the_mention(monkeypatch):
    # Rows written before #73 carry no source; every one of them is capture-path.
    calls = _spy_entities(monkeypatch)
    context = _binding_context(SOURCE_CAPTURE)
    del context["source"]

    _reconcile_provisional_binding(context, {"value": {"action": "reject"}})

    assert "split_mention" in calls and "split_claims" not in calls


# --- plan_correction_dispatch -------------------------------------------------


def test_dispatch_requires_action():
    with pytest.raises(ValueError):
        plan_correction_dispatch({}, TARGET)


def test_dispatch_unknown_action_raises():
    with pytest.raises(ValueError):
        plan_correction_dispatch({"action": "frobnicate"}, TARGET)


def test_dispatch_rename_uses_target_when_entity_id_absent():
    plan = plan_correction_dispatch(
        {"action": "rename", "new_canonical_name": "New Name"}, TARGET
    )
    assert plan["tool"] == "rename_entity"
    assert plan["params"]["entity_id"] == TARGET
    assert plan["params"]["new_canonical_name"] == "New Name"


def test_dispatch_rename_requires_new_name():
    with pytest.raises(ValueError):
        plan_correction_dispatch(
            {"action": "rename", "new_canonical_name": "  "}, TARGET
        )


def test_dispatch_retract_uses_target_when_claim_id_absent():
    plan = plan_correction_dispatch({"action": "retract"}, TARGET)
    assert plan["tool"] == "retract_claim"
    assert plan["params"]["claim_id"] == TARGET


def test_dispatch_repoint_accepts_agent_field_vocabulary():
    # Agents emit current_entity_id / new_entity; the explicit names are
    # source_entity_id / into. Both must be accepted, and a missing experience
    # scope falls back to the proposal's own target.
    plan = plan_correction_dispatch(
        {
            "action": "repoint_participant",
            "current_entity_id": "src-id",
            "new_entity": {"canonical_name": "Other Karen"},
        },
        TARGET,
    )
    assert plan["tool"] == "split_entity"
    assert plan["params"]["source_entity_id"] == "src-id"
    assert plan["params"]["experience_ids"] == [TARGET]
    assert plan["params"]["into"] == {"canonical_name": "Other Karen"}


def test_dispatch_repoint_folds_notes_into_metadata():
    plan = plan_correction_dispatch(
        {
            "action": "repoint_participant",
            "source_entity_id": "src-id",
            "experience_ids": ["e1", "e2"],
            "into": {"canonical_name": "X", "notes": "the other one"},
        },
        TARGET,
    )
    assert plan["params"]["experience_ids"] == ["e1", "e2"]
    assert plan["params"]["into"] == {
        "canonical_name": "X",
        "metadata": {"notes": "the other one"},
    }


def test_dispatch_repoint_requires_source_and_target():
    with pytest.raises(ValueError):
        plan_correction_dispatch(
            {"action": "repoint_participant", "into": {"canonical_name": "X"}}, TARGET
        )


def test_dispatch_coerces_non_string_reason_to_none():
    # suggested_change is free-form jsonb; a non-string reason must not slip past
    # the dispatched tool's string-typed reason validation.
    plan = plan_correction_dispatch(
        {"action": "rename", "new_canonical_name": "N", "reason": 123}, TARGET
    )
    assert plan["params"]["reason"] is None


def test_dispatch_passes_string_reason_through():
    plan = plan_correction_dispatch(
        {"action": "rename", "new_canonical_name": "N", "reason": "because"}, TARGET
    )
    assert plan["params"]["reason"] == "because"


# --- match_choice -------------------------------------------------------------

OPTIONS = [{"label": "A", "value": 1}, {"label": "B", "value": 2}]


def test_match_choice_by_index():
    assert match_choice(OPTIONS, 1) == {"label": "B", "value": 2}


def test_match_choice_index_out_of_range_is_none():
    assert match_choice(OPTIONS, 9) is None


def test_match_choice_by_label_string():
    assert match_choice(OPTIONS, "A") == {"label": "A", "value": 1}


def test_match_choice_by_object_label():
    assert match_choice(OPTIONS, {"label": "B"}) == {"label": "B", "value": 2}


def test_match_choice_no_match_is_none():
    assert match_choice(OPTIONS, "Z") is None
    assert match_choice(OPTIONS, {"value": 1}) is None
