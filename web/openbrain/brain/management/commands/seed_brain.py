"""Seed the dev brain with a week-in-the-life dataset for UI testing.

Inserts a small, hand-written set of experiences (a software PM who is also a dad,
husband, and civic leader), the entities they mention, and a few claims sourced
from them — with REAL OpenRouter embeddings so hybrid search returns them. Rows
are tagged metadata->>'seed' so the command is idempotent: every run first removes
the prior seed, then re-inserts. Owner defaults to the first user (viewer id "1").

    python manage.py seed_brain                 # owner=1, reseeds
    python manage.py seed_brain --owner 1
    python manage.py seed_brain --clear         # remove seed data and stop

Dev only — it writes to brain.*, which the app otherwise touches only through
the service layer.
"""

from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from openbrain.brain.db import to_vector_literal
from openbrain.brain.embeddings import get_embedding

SEED_MARKER = "week-in-the-life"

# key -> (canonical_name, kind, aliases)
ENTITIES = {
    "maya": ("Maya", "person", []),
    "sarah": ("Sarah", "person", []),
    "david": ("David Okafor", "person", ["David"]),
    "priya": ("Priya Nair", "person", ["Priya"]),
    "rivera": ("Coach Rivera", "person", []),
    "diane": ("Diane Whitfield", "person", ["Diane"]),
    "northwind": ("Northwind", "org", ["the Northwind app", "the product"]),
    "library_board": ("Riverside Library Board", "org", ["the library board"]),
    "soccer": ("Lakeside U10 Soccer", "org", ["Lakeside U10", "the U10 league"]),
    "cleanup": ("Memorial Park cleanup", "event", ["the park cleanup"]),
    "bottega": ("Bottega", "place", []),
    "memorial_park": ("Memorial Park", "place", []),
    "onboarding": ("onboarding revamp", "concept", ["onboarding"]),
    "firstrun": ("first-run experience", "concept", ["first-run", "first run"]),
    "magiclink": ("magic-link sign-in", "concept", ["magic-link", "magic link auth"]),
    "roadmap": ("Q3 roadmap", "concept", ["the roadmap"]),
}

# Each experience: content, when (naive local datetime), source_kind, visibility,
# mentions [(entity_key, surface_form, field)], claims [(subject_key, predicate,
# object, support_kind)] where object is ("entity", key) or ("literal", text).
EXPERIENCES = [
    {
        "content": (
            "Ran sprint planning for the Northwind onboarding revamp. The team "
            "committed to shipping the magic-link sign-in this sprint; David pushed "
            "back on scope, so we pulled SSO into next sprint. Felt good about the focus."
        ),
        "when": datetime(2026, 6, 15, 9, 30),
        "source_kind": "manual",
        "visibility": "private",
        "mentions": [
            ("northwind", "Northwind", "topics"),
            ("onboarding", "onboarding revamp", "topics"),
            ("magiclink", "magic-link sign-in", "topics"),
            ("david", "David", "people"),
        ],
        "claims": [
            ("david", "working_on", ("entity", "onboarding"), "paraphrased"),
            (
                "northwind",
                "decided_to",
                ("literal", "ship magic-link sign-in before SSO"),
                "inferred",
            ),
        ],
    },
    {
        "content": (
            "Maya scored her first goal at the Lakeside U10 game tonight. Coach Rivera "
            "pulled me aside to say her positioning has really come along. She wouldn't "
            "stop grinning the whole drive home."
        ),
        "when": datetime(2026, 6, 15, 18, 0),
        "source_kind": "manual",
        "visibility": "shared",
        "mentions": [
            ("maya", "Maya", "people"),
            ("rivera", "Coach Rivera", "people"),
            ("soccer", "Lakeside U10", "topics"),
        ],
        "claims": [
            ("maya", "attended", ("entity", "soccer"), "verbatim"),
            (
                "rivera",
                "said",
                ("literal", "her positioning has really come along"),
                "verbatim",
            ),
        ],
    },
    {
        "content": (
            "User interview with a churned customer. The empty first-run dashboard is "
            'clearly the drop-off point — she said "I logged in and had no idea what to '
            'do next." Bumping the first-run experience above the export feature.'
        ),
        "when": datetime(2026, 6, 16, 11, 0),
        "source_kind": "transcript",
        "visibility": "private",
        "mentions": [
            ("firstrun", "first-run experience", "topics"),
            ("northwind", "Northwind", "topics"),
        ],
        "claims": [
            (
                "northwind",
                "decided_to",
                ("literal", "prioritize the first-run experience over export"),
                "inferred",
            ),
        ],
    },
    {
        "content": (
            "Riverside Library Board meeting. We approved the budget for the new "
            "children's reading nook. Diane and I are co-leading the fundraising drive — "
            "aiming to close it before the fall."
        ),
        "when": datetime(2026, 6, 16, 19, 30),
        "source_kind": "manual",
        "visibility": "shared",
        "mentions": [
            ("library_board", "Riverside Library Board", "topics"),
            ("diane", "Diane", "people"),
        ],
        "claims": [
            ("diane", "partnered_with", ("entity", "library_board"), "paraphrased"),
            (
                "library_board",
                "decided_to",
                ("literal", "fund the children's reading nook"),
                "inferred",
            ),
        ],
    },
    {
        "content": (
            "Roadmap review with Priya and leadership. Decided to push the analytics "
            "module to Q4 to protect the onboarding bet. Priya backed the tradeoff once "
            "I framed it around activation, not feature count."
        ),
        "when": datetime(2026, 6, 17, 14, 0),
        "source_kind": "manual",
        "visibility": "private",
        "mentions": [
            ("priya", "Priya", "people"),
            ("roadmap", "roadmap", "topics"),
            ("northwind", "Northwind", "topics"),
        ],
        "claims": [
            (
                "northwind",
                "decided_to",
                ("literal", "defer the analytics module to Q4"),
                "inferred",
            ),
            (
                "priya",
                "discussed",
                ("literal", "activation over feature count"),
                "paraphrased",
            ),
        ],
    },
    {
        "content": (
            "Booked our anniversary dinner at Bottega for Saturday — got the corner table "
            "Sarah loves. Eight years. Keeping the weekend trip a surprise."
        ),
        "when": datetime(2026, 6, 17, 20, 0),
        "source_kind": "manual",
        "visibility": "private",
        "mentions": [
            ("bottega", "Bottega", "topics"),
            ("sarah", "Sarah", "people"),
        ],
        "claims": [
            ("sarah", "prefers", ("entity", "bottega"), "paraphrased"),
        ],
    },
    {
        "content": (
            "Helped Maya with fractions homework. Equivalent fractions finally clicked "
            "once we used the pizza-slice trick. Note to self: she learns better with "
            "something she can picture."
        ),
        "when": datetime(2026, 6, 18, 17, 0),
        "source_kind": "manual",
        "visibility": "private",
        "mentions": [
            ("maya", "Maya", "people"),
        ],
        "claims": [],
    },
    {
        "content": (
            "Lined up volunteers for Saturday's Memorial Park cleanup — 22 neighbors "
            "signed up, best turnout yet. Borrowed extra gloves and bags from the "
            "hardware store."
        ),
        "when": datetime(2026, 6, 18, 19, 0),
        "source_kind": "manual",
        "visibility": "shared",
        "mentions": [
            ("cleanup", "Memorial Park cleanup", "topics"),
            ("memorial_park", "Memorial Park", "topics"),
        ],
        "claims": [
            ("cleanup", "happened_at", ("entity", "memorial_park"), "inferred"),
        ],
    },
    {
        "content": (
            "Closed out the sprint — magic-link shipped behind a flag. Then ran straight "
            "to Maya's soccer practice. I keep trading family evenings for one more work "
            "thing. Going to protect Thursday nights."
        ),
        "when": datetime(2026, 6, 19, 16, 0),
        "source_kind": "manual",
        "visibility": "private",
        "mentions": [
            ("maya", "Maya", "people"),
            ("magiclink", "magic-link", "topics"),
            ("northwind", "Northwind", "topics"),
        ],
        "claims": [
            ("northwind", "working_on", ("entity", "magiclink"), "verbatim"),
        ],
    },
    {
        "content": (
            "Date night with Sarah after the kids were down. Talked through the kitchen "
            "remodel and agreed to wait until after summer. Good to just be us for an hour."
        ),
        "when": datetime(2026, 6, 19, 21, 0),
        "source_kind": "manual",
        "visibility": "shared",
        "mentions": [
            ("sarah", "Sarah", "people"),
        ],
        "claims": [
            ("sarah", "discussed", ("literal", "the kitchen remodel"), "paraphrased"),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed the dev brain with a week-in-the-life dataset (dev only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner",
            default="1",
            help="Viewer id (str(user.pk)) to own the seeded rows. Default '1'.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove existing seed data and stop (no re-insert).",
        )

    def handle(self, *args, **options):
        with transaction.atomic(), connection.cursor() as cursor:
            removed = self._clear(cursor)
            if removed:
                self.stdout.write(f"Removed {removed} prior seed experience(s).")
            if options["clear"]:
                self.stdout.write(self.style.SUCCESS("Seed data cleared."))
                return
            self._seed(cursor, options["owner"])
            cursor.execute("call brain.refresh_summary_cache()")
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(EXPERIENCES)} experiences / {len(ENTITIES)} entities "
                f"owned by viewer '{options['owner']}'. Summary cache refreshed."
            )
        )

    def _clear(self, cursor):
        cursor.execute(
            "select id from brain.experiences where metadata->>'seed' = %s",
            [SEED_MARKER],
        )
        exp_ids = [row[0] for row in cursor.fetchall()]
        if exp_ids:
            # Delete claims sourced by seed experiences (cascades claim_sources),
            # then the experiences (cascades mentions), then the seed entities.
            cursor.execute(
                "delete from brain.claims where id in ("
                "  select claim_id from brain.claim_sources"
                "   where experience_id = any(%s::uuid[]))",
                [exp_ids],
            )
            cursor.execute(
                "delete from brain.experiences where id = any(%s::uuid[])", [exp_ids]
            )
        cursor.execute(
            "delete from brain.entities where metadata->>'seed' = %s", [SEED_MARKER]
        )
        return len(exp_ids)

    def _seed(self, cursor, owner):
        entity_ids = {
            key: self._insert_entity(cursor, *spec) for key, spec in ENTITIES.items()
        }
        for exp in EXPERIENCES:
            exp_id = self._insert_experience(cursor, exp, owner)
            for entity_key, surface_form, field in exp["mentions"]:
                self._insert_mention(
                    cursor, exp_id, entity_ids[entity_key], surface_form, field
                )
            for subject_key, predicate, obj, support_kind in exp["claims"]:
                self._insert_claim(
                    cursor,
                    exp_id,
                    entity_ids,
                    subject_key,
                    predicate,
                    obj,
                    support_kind,
                )

    def _insert_entity(self, cursor, canonical_name, kind, aliases):
        cursor.execute(
            "insert into brain.entities (kind, canonical_name, aliases, metadata) "
            "values (%s::brain.entity_kind, %s, %s, jsonb_build_object('seed', %s::text)) "
            "returning id::text",
            [kind, canonical_name, aliases, SEED_MARKER],
        )
        return cursor.fetchone()[0]

    def _insert_experience(self, cursor, exp, owner):
        embedding = to_vector_literal(get_embedding(exp["content"]))
        occurred_at = timezone.make_aware(exp["when"])
        cursor.execute(
            "insert into brain.experiences ("
            "  captured_at, occurred_at, source_kind, content, embedding,"
            "  metadata, consolidation_status, owner, visibility"
            ") values ("
            "  %s, %s, %s::brain.source_kind, %s, %s::vector,"
            "  jsonb_build_object('seed', %s::text), 'complete'::brain.consolidation_status,"
            "  %s, %s::brain.visibility"
            ") returning id::text",
            [
                occurred_at,
                occurred_at,
                exp["source_kind"],
                exp["content"],
                embedding,
                SEED_MARKER,
                owner,
                exp["visibility"],
            ],
        )
        return cursor.fetchone()[0]

    def _insert_mention(self, cursor, exp_id, entity_id, surface_form, field):
        cursor.execute(
            "insert into brain.mentions (experience_id, entity_id, surface_form, field) "
            "values (%s::uuid, %s::uuid, %s, %s) on conflict do nothing",
            [exp_id, entity_id, surface_form, field],
        )

    def _insert_claim(
        self, cursor, exp_id, entity_ids, subject_key, predicate, obj, support_kind
    ):
        object_entity_id = entity_ids[obj[1]] if obj[0] == "entity" else None
        object_literal = obj[1] if obj[0] == "literal" else None
        cursor.execute(
            "insert into brain.claims ("
            "  subject_id, predicate, object_entity_id, object_literal, polarity, confidence"
            ") values (%s::uuid, %s, %s::uuid, %s, 'asserted'::brain.polarity, 0.8) "
            "returning id::text",
            [entity_ids[subject_key], predicate, object_entity_id, object_literal],
        )
        claim_id = cursor.fetchone()[0]
        cursor.execute(
            "insert into brain.claim_sources (claim_id, experience_id, support_kind, extracted_by) "
            "values (%s::uuid, %s::uuid, %s::brain.support_kind, %s)",
            [claim_id, exp_id, support_kind, f"seed:{SEED_MARKER}"],
        )
