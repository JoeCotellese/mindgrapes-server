"""Edit write service for the Brain UI (US-5).

A patch may carry content,
metadata, and/or paraphrased_claim_retractions:

  - metadata-only        no content        in-place metadata update
  - in-place content     cosine > 0.95     in-place content + embedding update
  - supersede            cosine <= 0.95    new pending row + back-link, auto-retract
                                           verbatim/inferred claims, surface
                                           paraphrased ones for review

The new embedding is computed BEFORE the transaction so a slow/failed OpenRouter
call never holds a row lock and never leaves a partial write — an EmbeddingError
propagates before any DB mutation. Every mutation lands at least one
correction_event. Owner-gated like the rest of Slice C: missing/unreadable → 404,
readable-but-not-owned → 403.
"""

import json

from django.db import transaction

from openbrain.brain.auth import can_edit_visibility, can_viewer_read
from openbrain.brain.db import (
    brain_cursor,
    dictfetchall,
    parse_json,
    record_correction,
    to_vector_literal,
)
from openbrain.brain.embeddings import embed_query
from openbrain.brain.exceptions import ExperienceNotFound, NotOwner

COSINE_INPLACE_THRESHOLD = 0.95

_SELECT_SQL = """
    select id::text,
           content,
           metadata,
           occurred_at,
           source_kind::text as source_kind,
           source_ref,
           owner,
           account_id,
           visibility::text as visibility,
           lat,
           lng
      from brain.experiences
     where id = %s::uuid
     for update
"""

_COSINE_SQL = """
    select 1 - (embedding <=> %s::vector) as similarity
      from brain.experiences
     where id = %s::uuid
"""

_UPDATE_METADATA_SQL = """
    update brain.experiences
       set metadata = %s::jsonb
     where id = %s::uuid
"""

_UPDATE_CONTENT_INPLACE_SQL = """
    update brain.experiences
       set content = %s,
           embedding = %s::vector,
           metadata = %s::jsonb
     where id = %s::uuid
"""

# The superseding row inherits owner/account_id/visibility verbatim (#85) so a
# content edit never silently re-privatizes a shared item or orphans ownership.
# lat/lng (#43) carry forward the same way: editing a caption must not orphan the
# geotag from the map, since only the live row is mappable.
_INSERT_SUPERSEDING_SQL = """
    insert into brain.experiences (
        captured_at, occurred_at, source_kind, source_ref,
        content, embedding, metadata, consolidation_status,
        owner, account_id, visibility, lat, lng
    ) values (
        now(),
        %s::timestamptz,
        %s::brain.source_kind,
        %s,
        %s,
        %s::vector,
        %s::jsonb,
        'pending'::brain.consolidation_status,
        %s,
        %s,
        %s::brain.visibility,
        %s,
        %s
    )
    returning id::text as id
"""

_SET_SUPERSEDED_BY_SQL = """
    update brain.experiences
       set superseded_by = %s::uuid
     where id = %s::uuid
"""

_CLAIM_SOURCES_BY_KIND_SQL = """
    select cs.claim_id::text as claim_id,
           cs.support_kind::text as support_kind
      from brain.claim_sources cs
      join brain.claims c on c.id = cs.claim_id
     where cs.experience_id = %s::uuid
       and c.polarity = 'asserted'
"""

_RETRACT_CLAIM_SQL = """
    update brain.claims
       set polarity = 'retracted'
     where id = %s::uuid
       and polarity = 'asserted'
    returning id::text
"""

_ELIGIBLE_PARAPHRASED_SQL = """
    select cs.claim_id::text as claim_id
      from brain.claim_sources cs
      join brain.claims c on c.id = cs.claim_id
     where cs.experience_id = %s::uuid
       and cs.support_kind = 'paraphrased'
       and c.polarity = 'asserted'
       and cs.claim_id = any(%s::uuid[])
"""


def _new_result() -> dict:
    return {
        "mode": "metadata_only",
        "similarity": None,
        "new_id": None,
        "auto_retracted_claim_ids": [],
        "paraphrased_claim_ids_pending": [],
        "paraphrased_retracted_claim_ids": [],
    }


def edit_experience(
    viewer: str,
    experience_id: str,
    *,
    content: str | None = None,
    metadata: dict | None = None,
    paraphrased_claim_retractions: list[str] | None = None,
) -> dict:
    """Apply a curated edit to one experience; see module docstring for modes.

    Raises ValueError for an empty patch, ExperienceNotFound when missing or not
    viewer-readable, and NotOwner when readable but not owned. May raise
    EmbeddingError (before any DB write) when a content edit can't be embedded.
    """
    has_content = content is not None
    if not has_content and metadata is None and not paraphrased_claim_retractions:
        raise ValueError(
            "patch must include content, metadata, or paraphrased_claim_retractions"
        )

    # Embed outside the transaction — never hold a row lock across a network call.
    new_embedding_lit = to_vector_literal(embed_query(content)) if has_content else None

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(_SELECT_SQL, [experience_id])
        rows = dictfetchall(cursor)
        if not rows:
            raise ExperienceNotFound(experience_id)
        before = rows[0]
        if not can_viewer_read(viewer, before["owner"], before["visibility"]):
            raise ExperienceNotFound(experience_id)
        if not can_edit_visibility(viewer, before["owner"]):
            raise NotOwner(experience_id)
        before["metadata"] = parse_json(before["metadata"])

        created_by = f"ui-session:{viewer}"
        result = _new_result()

        if not has_content:
            # A paraphrased-only retraction call carries no metadata change, so
            # only write the experience event when metadata actually changed.
            if metadata is not None:
                cursor.execute(_UPDATE_METADATA_SQL, [_json(metadata), experience_id])
                record_correction(
                    cursor,
                    target_kind="experience",
                    target_id=experience_id,
                    before={"metadata": before["metadata"]},
                    after={"metadata": metadata},
                    reason="edit metadata via UI",
                    created_by=created_by,
                )
        else:
            cursor.execute(_COSINE_SQL, [new_embedding_lit, experience_id])
            similarity = float(dictfetchall(cursor)[0]["similarity"])
            result["similarity"] = similarity
            merged_metadata = metadata if metadata is not None else before["metadata"]

            if similarity > COSINE_INPLACE_THRESHOLD:
                result["mode"] = "in_place"
                cursor.execute(
                    _UPDATE_CONTENT_INPLACE_SQL,
                    [content, new_embedding_lit, _json(merged_metadata), experience_id],
                )
                record_correction(
                    cursor,
                    target_kind="experience",
                    target_id=experience_id,
                    before={
                        "content": before["content"],
                        "metadata": before["metadata"],
                    },
                    after={"content": content, "metadata": merged_metadata},
                    reason=f"edit in place (cosine {similarity:.4f})",
                    created_by=created_by,
                )
            else:
                _supersede(
                    cursor,
                    experience_id,
                    before,
                    content,
                    new_embedding_lit,
                    merged_metadata,
                    similarity,
                    created_by,
                    result,
                )

        if paraphrased_claim_retractions:
            _retract_paraphrased(
                cursor, experience_id, paraphrased_claim_retractions, created_by, result
            )

    return result


def _supersede(
    cursor,
    experience_id,
    before,
    content,
    new_embedding_lit,
    merged_metadata,
    similarity,
    created_by,
    result,
):
    result["mode"] = "superseded"
    cursor.execute(
        _INSERT_SUPERSEDING_SQL,
        [
            before["occurred_at"],
            before["source_kind"],
            before["source_ref"],
            content,
            new_embedding_lit,
            _json(merged_metadata),
            before["owner"],
            before["account_id"],
            before["visibility"],
            before["lat"],
            before["lng"],
        ],
    )
    new_id = dictfetchall(cursor)[0]["id"]
    result["new_id"] = new_id
    cursor.execute(_SET_SUPERSEDED_BY_SQL, [new_id, experience_id])

    cursor.execute(_CLAIM_SOURCES_BY_KIND_SQL, [experience_id])
    auto_retract = []
    paraphrased_pending = []
    for source in dictfetchall(cursor):
        if source["support_kind"] in ("verbatim", "inferred"):
            auto_retract.append(source["claim_id"])
        elif source["support_kind"] == "paraphrased":
            paraphrased_pending.append(source["claim_id"])

    for claim_id in auto_retract:
        cursor.execute(_RETRACT_CLAIM_SQL, [claim_id])
        if dictfetchall(cursor):
            result["auto_retracted_claim_ids"].append(claim_id)
            record_correction(
                cursor,
                target_kind="claim",
                target_id=claim_id,
                before={"polarity": "asserted"},
                after={"polarity": "retracted"},
                reason="auto-retract: source experience superseded",
                created_by=created_by,
            )

    result["paraphrased_claim_ids_pending"] = paraphrased_pending

    record_correction(
        cursor,
        target_kind="experience",
        target_id=experience_id,
        before={
            "content": before["content"],
            "metadata": before["metadata"],
            "superseded_by": None,
        },
        after={
            "content": content,
            "metadata": merged_metadata,
            "superseded_by": new_id,
        },
        reason=f"supersede (cosine {similarity:.4f})",
        created_by=created_by,
    )


def _retract_paraphrased(cursor, experience_id, requested, created_by, result):
    # Only paraphrased-sourced asserted claims of THIS experience are eligible —
    # verbatim/inferred are the supersede path's job and must not leak in here.
    cursor.execute(_ELIGIBLE_PARAPHRASED_SQL, [experience_id, list(requested)])
    for row in dictfetchall(cursor):
        claim_id = row["claim_id"]
        cursor.execute(_RETRACT_CLAIM_SQL, [claim_id])
        if dictfetchall(cursor):
            result["paraphrased_retracted_claim_ids"].append(claim_id)
            record_correction(
                cursor,
                target_kind="claim",
                target_id=claim_id,
                before={"polarity": "asserted"},
                after={"polarity": "retracted"},
                reason="paraphrased retraction confirmed via UI",
                created_by=created_by,
            )


def _json(value) -> str:
    return json.dumps(value if value is not None else {})


_UPDATE_EXPERIENCE_ALLOWED = ("occurred_at", "metadata", "source_ref", "visibility")


def _build_experience_update(patch: dict, before: dict):
    """Compute (sets, params, changed, before_json, after_json) for an update patch.

    One branch per editable field; the caller has already validated the patch
    keys and the visibility vocabulary. occurred_at is stringified (ISO form)
    for the audit diff, metadata null normalizes to {}.
    """
    sets: list[str] = []
    params: list = []
    changed: list[str] = []
    before_json: dict = {}
    after_json: dict = {}

    if "occurred_at" in patch:
        params.append(patch["occurred_at"])
        sets.append("occurred_at = %s::timestamptz")
        changed.append("occurred_at")
        before_json["occurred_at"] = (
            before["occurred_at"].isoformat() if before["occurred_at"] else None
        )
        after_json["occurred_at"] = patch["occurred_at"]
    if "metadata" in patch:
        params.append(_json(patch["metadata"]))
        sets.append("metadata = %s::jsonb")
        changed.append("metadata")
        before_json["metadata"] = before["metadata"]
        after_json["metadata"] = patch["metadata"] or {}
    if "source_ref" in patch:
        params.append(patch["source_ref"])
        sets.append("source_ref = %s")
        changed.append("source_ref")
        before_json["source_ref"] = before["source_ref"]
        after_json["source_ref"] = patch["source_ref"]
    if "visibility" in patch:
        params.append(patch["visibility"])
        sets.append("visibility = %s::brain.visibility")
        changed.append("visibility")
        before_json["visibility"] = before["visibility"]
        after_json["visibility"] = patch["visibility"]

    return sets, params, changed, before_json, after_json


def update_experience(
    viewer_sub: str | None,
    experience_id: str,
    patch: dict,
    *,
    reason: str | None = None,
    created_by: str = "mcp:update_experience",
) -> dict:
    """Edit non-content fields on an experience: occurred_at/metadata/source_ref/visibility.

    content is immutable by spec — captures stay verbatim and corrections flow
    through claims. Visibility is owner-only (#82/#84): only the owner — or a
    null/operator viewer — may flip it; seeing a shared row never grants the
    write. Every change lands one correction_events row. Backs the MCP
    update_experience tool; distinct from edit_experience (the curated UI edit path).
    """
    patch = patch or {}
    for key in patch:
        if key not in _UPDATE_EXPERIENCE_ALLOWED:
            raise ValueError(
                f'update_experience: field "{key}" is not editable; allowed = '
                + ", ".join(_UPDATE_EXPERIENCE_ALLOWED)
            )
    if not patch:
        raise ValueError("update_experience: patch must touch at least one field")
    # Reject an out-of-vocabulary visibility before opening a transaction.
    if "visibility" in patch and patch["visibility"] not in ("private", "shared"):
        raise ValueError(
            "update_experience: visibility must be 'private' or 'shared', got "
            f'"{patch["visibility"]}"'
        )

    with transaction.atomic(), brain_cursor() as cursor:
        cursor.execute(
            "select id::text, occurred_at, metadata, source_ref, owner, "
            "visibility::text as visibility from brain.experiences "
            "where id = %s::uuid for update",
            [experience_id],
        )
        rows = dictfetchall(cursor)
        if not rows:
            raise ValueError(f"update_experience: experience {experience_id} not found")
        before = rows[0]

        # Ownership guard (#84): only the owner — or a null/operator viewer — may
        # change visibility. Seeing a shared row never grants the write.
        if "visibility" in patch and not can_edit_visibility(
            viewer_sub, before["owner"]
        ):
            raise NotOwner(experience_id)

        before["metadata"] = parse_json(before["metadata"])
        sets, params, changed, before_json, after_json = _build_experience_update(
            patch, before
        )

        params.append(before["id"])
        cursor.execute(
            "update brain.experiences set " + ", ".join(sets) + " where id = %s::uuid",
            params,
        )
        correction_id = record_correction(
            cursor,
            target_kind="experience",
            target_id=before["id"],
            before=before_json,
            after=after_json,
            reason=reason,
            created_by=created_by,
        )

    return {
        "id": before["id"],
        "changed_fields": changed,
        "correction_event_id": correction_id,
    }
