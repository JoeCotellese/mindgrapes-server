"""Soft-delete + usage-count read for the Brain UI (US-6).

One transaction that sets
deleted_at and retracts every claim whose only remaining live source was this
experience ("live" = no deleted_at and no superseded_by), writing one
correction_event per retraction plus one for the experience. Re-deleting is a
no-op — the coalesce keeps the original timestamp and no further rows are written.

get_usage backs the delete-confirm modal: claim count, mentioned-entity count,
and entity names, gated by the same read-privacy rule (missing or not-readable →
None, which the view renders as a 404).
"""

from django.db import transaction

from openbrain.brain.auth import can_edit_visibility, can_viewer_read
from openbrain.brain.db import brain_cursor, dictfetchall, record_correction
from openbrain.brain.exceptions import ExperienceNotFound, NotOwner

_SELECT_SQL = """
    select owner, visibility::text as visibility, deleted_at
      from brain.experiences
     where id = %s::uuid
     for update
"""

_SOFT_DELETE_SQL = """
    update brain.experiences
       set deleted_at = coalesce(deleted_at, now())
     where id = %s::uuid
    returning deleted_at
"""

# Claims sourced by this experience whose only remaining live source was it.
_ORPHANED_CLAIMS_SQL = """
    with affected as (
        select cs.claim_id::text as claim_id
          from brain.claim_sources cs
         where cs.experience_id = %(id)s::uuid
    )
    select a.claim_id
      from affected a
      join brain.claims c on c.id::text = a.claim_id
     where c.polarity = 'asserted'
       and not exists (
           select 1
             from brain.claim_sources cs2
             join brain.experiences e2 on e2.id = cs2.experience_id
            where cs2.claim_id::text = a.claim_id
              and cs2.experience_id <> %(id)s::uuid
              and e2.deleted_at is null
              and e2.superseded_by is null
       )
"""

_RETRACT_CLAIM_SQL = """
    update brain.claims
       set polarity = 'retracted'
     where id = %s::uuid
       and polarity = 'asserted'
"""

_USAGE_EXISTS_SQL = """
    select owner, visibility::text as visibility
      from brain.experiences
     where id = %s::uuid
"""

_USAGE_SQL = """
    select
      (select count(*)::int
         from brain.claim_sources
        where experience_id = %(id)s::uuid)                 as claim_count,
      (select count(*)::int
         from brain.mentions
        where experience_id = %(id)s::uuid)                 as mentioned_entity_count,
      coalesce((select array_agg(e.canonical_name order by e.canonical_name)
         from brain.mentions m
         join brain.entities e on e.id = m.entity_id
        where m.experience_id = %(id)s::uuid), '{}'::text[]) as mentioned_entity_names
"""


def get_usage(viewer: str, experience_id: str) -> dict | None:
    """Usage counts for the delete-confirm modal, or None when missing/private."""
    with brain_cursor() as cursor:
        cursor.execute(_USAGE_EXISTS_SQL, [experience_id])
        rows = dictfetchall(cursor)
        if not rows:
            return None
        if not can_viewer_read(viewer, rows[0]["owner"], rows[0]["visibility"]):
            return None
        cursor.execute(_USAGE_SQL, {"id": experience_id})
        return dictfetchall(cursor)[0]


def soft_delete_experience(viewer: str, experience_id: str) -> dict:
    """Soft-delete an experience (owner only) and auto-retract sole-source claims.

    Raises ExperienceNotFound when missing or not viewer-readable, NotOwner when
    readable but not owned. Idempotent: a second call sets already_deleted and
    retracts nothing.
    """
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

        already_deleted = before["deleted_at"] is not None
        cursor.execute(_SOFT_DELETE_SQL, [experience_id])
        deleted_at = cursor.fetchone()[0]

        retracted_claim_ids: list[str] = []
        if not already_deleted:
            cursor.execute(_ORPHANED_CLAIMS_SQL, {"id": experience_id})
            retracted_claim_ids = [r["claim_id"] for r in dictfetchall(cursor)]
            created_by = f"ui-session:{viewer}"
            for claim_id in retracted_claim_ids:
                cursor.execute(_RETRACT_CLAIM_SQL, [claim_id])
                record_correction(
                    cursor,
                    target_kind="claim",
                    target_id=claim_id,
                    before={"polarity": "asserted"},
                    after={"polarity": "retracted"},
                    reason="auto-retract: sole source deleted",
                    created_by=created_by,
                )
            record_correction(
                cursor,
                target_kind="experience",
                target_id=experience_id,
                before={"deleted_at": None},
                after={"deleted_at": deleted_at.isoformat()},
                reason="soft-delete via UI",
                created_by=created_by,
            )

    return {
        "deleted_at": deleted_at,
        "retracted_claim_ids": retracted_claim_ids,
        "already_deleted": already_deleted,
    }
