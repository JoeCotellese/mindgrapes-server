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

    python manage.py dedup_entities                 # dry-run: report only
    python manage.py dedup_entities --apply          # execute merges + queue writes
    python manage.py dedup_entities --kind person    # restrict to one kind

Dry-run is the default. Dev only — it writes to brain.* directly; do not point it
at a production database.
"""

import json

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from openbrain.brain.services.dedup import plan_dedup
from openbrain.brain.services.entities import merge_entities

_KINDS = ("person", "org", "event", "place", "concept")

_LOAD_SQL = """
    select id::text as id, kind::text as kind, canonical_name, aliases
      from brain.entities
     where merged_into is null
       and (%s::text is null or kind = %s::brain.entity_kind)
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
    help = "Scan brain.entities for duplicate pairs; auto-merge or queue them (dev only)."

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

    def handle(self, *args, **options):
        kind = options["kind"]
        with connection.cursor() as cursor:
            cursor.execute(_LOAD_SQL, [kind, kind])
            columns = [c[0] for c in cursor.description]
            entities = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

        plan = plan_dedup(entities)
        merges, queue = plan["merges"], plan["queue"]
        by_id = {e["id"]: e for e in entities}

        self.stdout.write(
            f"Scanned {len(entities)} live entit(y/ies)"
            + (f" of kind '{kind}'" if kind else "")
            + f": {len(merges)} auto-merge candidate(s), {len(queue)} to queue."
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
