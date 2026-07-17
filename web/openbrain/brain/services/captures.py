# ABOUTME: capture_thought write service.
# ABOUTME: Picks the bare vs structured path, embeds before the txn, returns the MCP structuredContent dict.

import json

from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string

from openbrain.brain.db import brain_cursor, dictfetchall, to_vector_literal
from openbrain.brain.embeddings import embed_query
from openbrain.brain.services.entity_resolver import (
    link_mention,
    resolve_or_create_entity,
)
from openbrain.brain.services.reviews import open_provisional_binding_on_cursor

# Same 11 columns + 'pending' status as edits.py's superseding insert. source_kind
# and account_id/visibility coalesce to the brain defaults when null.
_INSERT_EXPERIENCE_SQL = """
    insert into brain.experiences (
        captured_at, occurred_at, source_kind, source_ref,
        content, embedding, metadata, consolidation_status,
        owner, account_id, visibility
    ) values (
        now(),
        %s::timestamptz,
        coalesce(%s::brain.source_kind, 'manual'::brain.source_kind),
        %s,
        %s,
        %s::vector,
        %s::jsonb,
        'pending'::brain.consolidation_status,
        %s,
        coalesce(%s, 'household'),
        coalesce(%s::brain.visibility, 'private'::brain.visibility)
    )
    returning id::text as id
"""

_FETCH_ENTITY_SQL = """
    select id::text as entity_id, kind::text as kind
      from brain.entities
     where id = %s::uuid and merged_into is null
"""


def is_structured_capture(
    occurred_at, participants, predicate_hints, source_kind, source_ref
) -> bool:
    """Structured iff any structured field is present.

    visibility is deliberately absent: it applies to both paths and never
    triggers the structured branch.
    """
    return (
        occurred_at is not None
        or (participants is not None and len(participants) > 0)
        or (predicate_hints is not None and len(predicate_hints) > 0)
        or source_kind is not None
        or source_ref is not None
    )


def capture(
    *,
    content: str,
    owner: str | None,
    account_id: str | None,
    visibility: str | None = "private",
    occurred_at: str | None = None,
    participants: list[dict] | None = None,
    predicate_hints: list[dict] | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
) -> dict:
    """Write one experience and return the capture_thought structuredContent dict.

    Bare form (just content) embeds + runs LLM metadata extraction. Structured
    form (any of occurred_at / participants / predicate_hints / source_kind /
    source_ref) skips metadata extraction and resolves participants to entities.
    The embedding (and bare-path metadata) is computed BEFORE the transaction, so
    an OpenRouter failure aborts with no partial write.
    """
    if is_structured_capture(
        occurred_at, participants, predicate_hints, source_kind, source_ref
    ):
        return _structured_capture(
            content=content,
            owner=owner,
            account_id=account_id,
            visibility=visibility,
            occurred_at=occurred_at,
            participants=participants,
            predicate_hints=predicate_hints,
            source_kind=source_kind,
            source_ref=source_ref,
        )
    return _bare_capture(
        content=content, owner=owner, account_id=account_id, visibility=visibility
    )


def _bare_capture(*, content, owner, account_id, visibility) -> dict:
    embedding_lit = to_vector_literal(embed_query(content))
    metadata = import_string(settings.BRAIN_METADATA_FN)(content)
    full_metadata = {**metadata, "source": "mcp"}

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            _INSERT_EXPERIENCE_SQL,
            [
                None,  # occurred_at
                None,  # source_kind → 'manual'
                None,  # source_ref
                content,
                embedding_lit,
                json.dumps(full_metadata),
                owner,
                account_id,
                visibility,
            ],
        )
        experience_id = dictfetchall(cursor)[0]["id"]

    return {
        "experience_id": experience_id,
        "is_structured": False,
        "metadata": full_metadata,
    }


def _structured_capture(
    *,
    content,
    owner,
    account_id,
    visibility,
    occurred_at,
    participants,
    predicate_hints,
    source_kind,
    source_ref,
) -> dict:
    parts = participants or []
    hints = predicate_hints or []
    people_names = [p["name"] for p in parts]

    embedding_lit = to_vector_literal(embed_query(content))

    # Row metadata: predicate_hints stashed only when non-empty so
    # the consolidation worker can use them as anchors.
    row_metadata: dict = {"source": "mcp", "people": people_names}
    if hints:
        row_metadata["predicate_hints"] = hints

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            _INSERT_EXPERIENCE_SQL,
            [
                occurred_at,
                source_kind,
                source_ref,
                content,
                embedding_lit,
                json.dumps(row_metadata),
                owner,
                account_id,
                visibility,
            ],
        )
        experience_id = dictfetchall(cursor)[0]["id"]
        extracted, borderline, needs_disambiguation = _resolve_participants(
            cursor, experience_id, embedding_lit, parts
        )

    return {
        "experience_id": experience_id,
        "is_structured": True,
        "metadata": _echo_metadata(predicate_hints, source_kind, source_ref),
        "extracted_entities": extracted,
        "borderline_matches": borderline,
        "needs_disambiguation": needs_disambiguation,
        "claims_pending": True,
    }


def _resolve_participants(cursor, experience_id, embedding_lit, parts):
    """Resolve/link each participant inside the open txn.

    Returns (extracted, borderline, needs_disambiguation). A provided entity_id is
    validated (not merged) and linked directly; otherwise the name is resolved
    against existing entities. A borderline best-guess bind (#8) is flagged
    provisional and opens a disambiguation token — recorded on this same cursor so
    it commits with the capture — that the caller reconciles. An invalid entity_id
    raises, rolling the whole insert back. borderline_matches is retained (empty)
    for backward compatibility with the shipped result shape.
    """
    extracted: list[dict] = []
    borderline: list[dict] = []
    needs_disambiguation: list[dict] = []

    for participant in parts:
        surface = (participant.get("name") or "").strip()
        if not surface:
            continue

        entity_id = participant.get("entity_id")
        if entity_id:
            cursor.execute(_FETCH_ENTITY_SQL, [entity_id])
            if not dictfetchall(cursor):
                raise ValueError(
                    f"participant entity_id {entity_id} not found or merged"
                )
            link_mention(cursor, experience_id, entity_id, surface, "people")
            extracted.append(
                {
                    "surface": surface,
                    "entity_id": entity_id,
                    "action": "provided",
                    "provisional": False,
                }
            )
            continue

        outcome = resolve_or_create_entity(
            cursor,
            experience_id,
            embedding_lit,
            surface=surface,
            field="people",
            kind="person",
        )
        provisional = outcome["action"] == "provisional"
        link_mention(cursor, experience_id, outcome["entity_id"], surface, "people")
        extracted.append(
            {
                "surface": surface,
                "entity_id": outcome["entity_id"],
                "action": outcome["action"],
                "provisional": provisional,
            }
        )
        if provisional:
            needs_disambiguation.append(
                open_provisional_binding_on_cursor(
                    cursor,
                    experience_id=experience_id,
                    surface=surface,
                    field="people",
                    entity_kind=outcome["kind"],
                    candidate_entity_id=outcome["candidate_entity_id"],
                    candidate_name=outcome["candidate_name"],
                    trgm_score=outcome["trgm_score"],
                    verification_score=outcome["verification_score"],
                )
            )

    return extracted, borderline, needs_disambiguation


def _echo_metadata(predicate_hints, source_kind, source_ref) -> dict:
    """The structuredContent.metadata echo: only the args passed.

    JS-truthiness semantics are kept: an empty predicate_hints=[] is still echoed
    (it was provided), while absent (None) is omitted; the string fields are
    echoed only when truthy.
    """
    echo: dict = {}
    if predicate_hints is not None:
        echo["predicate_hints"] = predicate_hints
    if source_kind:
        echo["source_kind"] = source_kind
    if source_ref:
        echo["source_ref"] = source_ref
    return echo
