"""Owner-only visibility flip for the Brain UI (US-7).

The visibility path of update_experience: an
owner-guarded UPDATE of brain.experiences.visibility recorded as a
correction_event. Soft un-share — flipping to private stops the other member
seeing it going forward but claws back nothing already seen or derived.

This layer also applies the read-privacy 404: a private row
owned by someone else raises ExperienceNotFound (indistinguishable from missing)
rather than leaking its existence through a 403. A readable-but-not-owned row
(shared, owned by another) raises NotOwner → 403.
"""

from django.db import transaction

from openbrain.brain.auth import can_edit_visibility, can_viewer_read
from openbrain.brain.db import brain_cursor, dictfetchall, record_correction
from openbrain.brain.exceptions import ExperienceNotFound, NotOwner

_VISIBILITIES = ("private", "shared")

_SELECT_SQL = """
    select owner, visibility::text as visibility
      from brain.experiences
     where id = %s::uuid
     for update
"""

_UPDATE_SQL = """
    update brain.experiences
       set visibility = %s::brain.visibility
     where id = %s::uuid
"""


def set_visibility(viewer: str, experience_id: str, visibility: str) -> dict:
    """Flip an experience's visibility (owner only) and record a correction.

    Raises ValueError for an out-of-vocab value (before any DB work),
    ExperienceNotFound when missing or not viewer-readable, and NotOwner when the
    viewer may read the row but does not own it.
    """
    if visibility not in _VISIBILITIES:
        raise ValueError(
            f"visibility must be one of {_VISIBILITIES}, got {visibility!r}"
        )

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

        cursor.execute(_UPDATE_SQL, [visibility, experience_id])
        record_correction(
            cursor,
            target_kind="experience",
            target_id=experience_id,
            before={"visibility": before["visibility"]},
            after={"visibility": visibility},
            reason="set visibility via UI",
            created_by=f"ui-session:{viewer}",
        )

    return {
        "id": experience_id,
        "visibility": visibility,
        "changed_fields": ["visibility"],
    }
