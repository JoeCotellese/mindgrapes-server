# ABOUTME: Batch second-stage entity dedup scanner (#16) — blocks live entities into
# ABOUTME: candidate pairs, verifies, and auto-merges the confident ones / queues the rest.
"""Offline entity dedup pass over brain.entities (dev only).

Capture-time resolution only sees the one entity a surface trgm-matches, so
duplicates that never co-occur at capture stay fragmented. This command scans
every live entity, blocks them into candidate pairs (token overlap + MinHash/LSH),
runs each pair through the same `name_matching` verification seam the resolver
uses, then:

  * auto-merges (soft, audited via correction_events, reversible with
    unmerge_entity) every pair at/above the auto-merge threshold, using the
    shared merge_entities service, and
  * records the rest as pending merge_candidates for a human.

Pairs a reviewer has already ruled on ('kept_separate' or 'skipped') are held out
of the merge leg entirely and reported as held (#95). The queue leg needs no such
guard: its insert is `on conflict do nothing`, so a resolved row is never
resurrected to pending.

    python manage.py dedup_entities                 # dry-run: report only
    python manage.py dedup_entities --apply          # execute merges + queue writes
    python manage.py dedup_entities --kind person    # restrict to one kind

Dry-run is the default. Dev only — it writes to brain.* directly; do not point it
at a production database.
"""

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from openbrain.brain.services import name_matching, probabilistic
from openbrain.brain.services.dedup import plan_dedup
from openbrain.brain.services.entities import merge_entities

_KINDS = ("person", "org", "event", "place", "concept", "animal")

# Scorer seam (#31): 'default' is the current name_matching decision, 'fs' is the
# probabilistic Fellegi-Sunter scorer. Blocking is scorer-independent either way.
_SCORERS = {"default": name_matching, "fs": probabilistic}

_LOAD_SQL = """
    select id::text as id, kind::text as kind, canonical_name, aliases
      from brain.entities
     where merged_into is null
       and (%s::text is null or kind = %s::brain.entity_kind)
"""

# Pairs a human has already ruled on, in live-entity terms (#95).
#
# merge_entities only restamps a candidate row `where status = 'pending'`, so a
# reviewer's verdict neither blocks the batch nor gets overwritten by it — the
# merge just happens and nothing records that a decision was reversed.
#
# 'skipped' counts alongside 'kept_separate'. A skip is a deferral, which a human
# draining the queue can revisit because they can see the row; an unattended batch
# cannot. On this path, not-pending means not the batch's call.
#
# Both endpoints resolve through coalesce(merged_into, id) because the stored key
# goes stale: a verdict on (A, B) stops matching once B merges into C, and the pair
# the planner now proposes is (A, C).
_HELD_PAIRS_SQL = """
    select distinct
           least(coalesce(ea.merged_into, ea.id),
                 coalesce(eb.merged_into, eb.id))::text as lo,
           greatest(coalesce(ea.merged_into, ea.id),
                    coalesce(eb.merged_into, eb.id))::text as hi
      from brain.merge_candidates mc
      join brain.entities ea on ea.id = mc.entity_a
      join brain.entities eb on eb.id = mc.entity_b
     where mc.status in ('kept_separate', 'skipped')
       and coalesce(ea.merged_into, ea.id) <> coalesce(eb.merged_into, eb.id)
"""

_INSERT_CANDIDATE_SQL = """
    insert into brain.merge_candidates (entity_a, entity_b, similarity, evidence)
         values (
           least(%s::uuid, %s::uuid),
           greatest(%s::uuid, %s::uuid),
           %s,
           %s::jsonb
         )
    on conflict (entity_a, entity_b) do nothing
"""


class Command(BaseCommand):
    help = (
        "Scan brain.entities for duplicate pairs; auto-merge or queue them (dev only)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind",
            choices=_KINDS,
            default=None,
            help="Restrict the scan to a single entity kind.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Execute the plan (merges + queue writes). Default is dry-run.",
        )
        parser.add_argument(
            "--scorer",
            choices=tuple(_SCORERS),
            default="default",
            help="Match scorer: 'default' (name_matching) or 'fs' (probabilistic, #31).",
        )

    def handle(self, *args, **options):
        kind = options["kind"]
        with connection.cursor() as cursor:
            cursor.execute(_LOAD_SQL, [kind, kind])
            columns = [c[0] for c in cursor.description]
            entities = [
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            ]

        scorer = _SCORERS[options["scorer"]]
        plan = plan_dedup(entities, scorer=scorer)
        merges, queue = plan["merges"], plan["queue"]
        by_id = {e["id"]: e for e in entities}

        # Filter before reporting, not just before writing: dry-run is the preview
        # of what --apply would do, and #83 leans on it as the prod-safe read.
        merges, held = self._drop_held_pairs(merges)

        self.stdout.write(
            f"Scanned {len(entities)} live entit(y/ies)"
            + (f" of kind '{kind}'" if kind else "")
            + f" [scorer={options['scorer']}]"
            + f": {len(merges)} auto-merge candidate(s), {len(queue)} to queue."
        )
        for loser_id, winner_id, score in held:
            self.stdout.write(
                self.style.WARNING(
                    f"  held   {by_id[loser_id]['canonical_name']!r} vs "
                    f"{by_id[winner_id]['canonical_name']!r}  (score {score:.3f}) — "
                    "a reviewer already ruled on this pair; not merging."
                )
            )
        for loser_id, winner_id, score in merges:
            self.stdout.write(
                f"  merge  {by_id[loser_id]['canonical_name']!r} -> "
                f"{by_id[winner_id]['canonical_name']!r}  (score {score:.3f})"
            )

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry-run: no changes written. Re-run with --apply.")
            )
            return

        merged = self._apply_merges(merges)
        queued = self._apply_queue(queue)
        self.stdout.write(
            self.style.SUCCESS(
                f"Applied: {merged} merge(s) executed, {queued} candidate(s) queued."
            )
        )

    def _drop_held_pairs(self, merges):
        """Split the planner's merges into (allowed, held-by-a-human-verdict).

        The guard lives here rather than inside merge_entities because the review
        UI calls that same service to APPLY a confirmed merge (reviews.py), and a
        human overriding their own earlier keep-separate decision is legitimate.
        This is the batch path, where no human is present to be overridden.
        """
        with connection.cursor() as cursor:
            cursor.execute(_HELD_PAIRS_SQL)
            held_keys = {(row[0], row[1]) for row in cursor.fetchall()}
        if not held_keys:
            return merges, []

        allowed, held = [], []
        for merge in merges:
            loser_id, winner_id, _ = merge
            key = (min(loser_id, winner_id), max(loser_id, winner_id))
            (held if key in held_keys else allowed).append(merge)
        return allowed, held

    def _apply_merges(self, merges) -> int:
        merged = 0
        for loser_id, winner_id, score in merges:
            try:
                merge_entities(
                    loser_id,
                    winner_id,
                    reason=f"batch dedup auto-merge (verification={score:.3f})",
                    created_by="manage.py:dedup_entities",
                )
                merged += 1
            except ValueError as exc:
                # A loser/winner already merged by an earlier pair this run — skip;
                # the next run re-discovers anything still outstanding.
                self.stdout.write(self.style.WARNING(f"  skipped: {exc}"))
        return merged

    def _apply_queue(self, queue) -> int:
        queued = 0
        with transaction.atomic(), connection.cursor() as cursor:
            for a_id, b_id, score in queue:
                evidence = json.dumps(
                    {"source": "dedup_entities", "verification_score": score}
                )
                cursor.execute(
                    _INSERT_CANDIDATE_SQL, [a_id, b_id, a_id, b_id, score, evidence]
                )
                queued += cursor.rowcount or 0
        return queued
