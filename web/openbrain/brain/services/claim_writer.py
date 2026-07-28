# ABOUTME: Per-experience claim writer for the consolidation pipeline.
# ABOUTME: resolve-or-create subject/object entities, then insert claims + claim_sources.

from openbrain.brain.db import dictfetchall
from openbrain.brain.services.name_matching import _is_abbreviation, _normalize

# trgm_score (0..1) is the channel for entity binding. This is a DISTINCT policy
# from entity_resolver.py: an inclusive >= threshold, and NO alias-append / NO
# merge-candidate / NO mention side effects. It mirrors the historical claims
# backfill exactly so backfilled and consolidation-written rows are bit-identical in shape.
MATCH_THRESHOLD = 0.85

_RESOLVE_ENTITY_SQL = """
    select entity_id::text, trgm_score, phon_match, vec_score, fused_score
      from brain.resolve_entity(%s, %s::vector, %s::brain.entity_kind, 1)
"""

_INSERT_ENTITY_SQL = """
    insert into brain.entities (kind, canonical_name, aliases, embedding)
         values (%s::brain.entity_kind, %s, array[%s]::text[], %s::vector)
      returning id::text as id
"""

# The entities this experience's capture already resolved and wrote mentions for.
# merged_into is followed one level (as init/06-tools.sql does) so a mention whose
# entity was merged after capture binds to the survivor, not the tombstone.
_EXPERIENCE_BINDINGS_SQL = """
    select m.surface_form,
           coalesce(t.id, e.id)::text                    as entity_id,
           coalesce(t.kind, e.kind)::text                as kind,
           coalesce(t.canonical_name, e.canonical_name)  as canonical_name,
           coalesce(t.aliases, e.aliases)                as aliases
      from brain.mentions m
      join brain.entities e on e.id = m.entity_id
      left join brain.entities t on t.id = e.merged_into
     where m.experience_id = %s::uuid
"""

_INSERT_CLAIM_SQL = """
    insert into brain.claims (
        subject_id, predicate, predicate_detail,
        object_entity_id, object_literal, confidence
    ) values (
        %s::uuid, %s, %s,
        %s::uuid, %s, %s
    ) returning id::text as id
"""

_INSERT_CLAIM_SOURCE_SQL = """
    insert into brain.claim_sources (
        claim_id, experience_id, support_kind, source_confidence, extracted_by
    ) values (
        %s::uuid, %s::uuid, %s::brain.support_kind, %s, %s
    )
"""


# First-person surface forms that must never become an entity. A note is written
# in the owner's voice, so "I"/"me"/"my" resolve to the owner — but there is no
# owner self-entity yet (that binding is entangled with multi-user tenancy, #48),
# so the safe stopgap is to DROP the claim rather than mint a junk `person` entity
# literally named "I". Whole-string match only: "my team" / "my dog Roger" are real
# things to remember and must not be caught. See #56.
_OWNER_SELF_FORMS = frozenset(
    {
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "the user",
        # Plural: "we decided", "our" — bare pronouns mint the same junk entity.
        # Multi-word ("our team") survives, since this is a whole-string match.
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
    }
)


def is_owner_self_reference(name: str) -> bool:
    """True when ``name`` is a bare first-person reference to the capturing owner."""
    normalized = name.strip().strip(".,!?;:\"'").strip().lower()
    return normalized in _OWNER_SELF_FORMS


def build_binding_index(rows: list[dict]) -> dict:
    """Index capture-time bindings as (kind, normalized name) -> entity_id (#73).

    Every name the capture already tied to an entity is a key: the mention's own
    surface form plus that entity's canonical name and aliases. A key two DIFFERENT
    entities both claim is dropped rather than guessed — the capture bound both, so
    a coin-flip here is the failure this exists to prevent.
    """
    index: dict[tuple[str, str], str | None] = {}
    for row in rows:
        names = [row["surface_form"], row["canonical_name"], *(row["aliases"] or [])]
        for name in names:
            normalized = _normalize(name)
            if not normalized:
                continue
            key = (row["kind"], normalized)
            if key in index and index[key] != row["entity_id"]:
                index[key] = None
            else:
                index.setdefault(key, row["entity_id"])
    return {key: value for key, value in index.items() if value is not None}


def load_experience_bindings(cursor, experience_id: str) -> dict:
    """Load and index what the capture bound for ``experience_id``."""
    cursor.execute(_EXPERIENCE_BINDINGS_SQL, [experience_id])
    return build_binding_index(dictfetchall(cursor))


def lookup_binding(index: dict, name: str, kind: str) -> str | None:
    """The entity this capture already bound for ``name``, or None to resolve it.

    Exact normalized match first. Failing that, a bare given name the extractor
    shortened ("Bonnie" for the bound "Bonnie Ravina") binds when exactly one bound
    entity of that kind expands it. Two bound Bonnies is ambiguous, so it falls
    through to resolution rather than picking one — the namesake caution in
    name_matching applies here too, just narrowed to one experience's participants.
    """
    normalized = _normalize(name)
    if not normalized:
        return None

    exact = index.get((kind, normalized))
    if exact is not None:
        return exact

    tokens = normalized.split()
    if len(tokens) != 1:
        return None
    expansions = {
        entity_id
        for (indexed_kind, indexed_name), entity_id in index.items()
        if indexed_kind == kind and _is_abbreviation(tokens, indexed_name.split())
    }
    return expansions.pop() if len(expansions) == 1 else None


def new_accumulator() -> dict:
    """Per-batch write counters, returned to the worker for its log line."""
    return {
        "claims_inserted": 0,
        "claim_sources_inserted": 0,
        "entities_created_for_objects": 0,
        "literal_objects_fell_back": 0,
        "claims_skipped_self": 0,
    }


def _object_should_be_literal(claim: dict, top: dict | None) -> bool:
    """'concept'-typed objects are usually free-form quotes/rationales — wrong to
    spawn an entity for. Try resolution first; fall back to a literal when there's
    no strong name match. Non-concept objects always bind to an entity.
    """
    if claim["object_kind"] != "concept":
        return False
    if not top or top["entity_id"] is None:
        return True
    return top["trgm_score"] < MATCH_THRESHOLD


def _resolve_top(cursor, name: str, kind: str, embedding: str | None) -> dict | None:
    cursor.execute(_RESOLVE_ENTITY_SQL, [name, embedding, kind])
    rows = dictfetchall(cursor)
    return rows[0] if rows else None


def _insert_entity(cursor, name: str, kind: str, embedding: str | None) -> str:
    cursor.execute(_INSERT_ENTITY_SQL, [kind, name, name, embedding])
    return dictfetchall(cursor)[0]["id"]


def _resolve_or_create_entity(
    cursor,
    name: str,
    kind: str,
    embedding: str | None,
    acc: dict,
    bindings: dict | None = None,
) -> str:
    bound = lookup_binding(bindings, name, kind) if bindings else None
    if bound is not None:
        return bound
    top = _resolve_top(cursor, name, kind, embedding)
    if top and top["entity_id"] is not None and top["trgm_score"] >= MATCH_THRESHOLD:
        return top["entity_id"]
    acc["entities_created_for_objects"] += 1
    return _insert_entity(cursor, name, kind, embedding)


def write_claim_for_experience(
    cursor,
    experience_id: str,
    embedding: str | None,
    claim: dict,
    extracted_by: str,
    acc: dict,
    bindings: dict | None = None,
) -> None:
    """Resolve subject/object entities and insert one claim + its claim_source.

    ``claim`` is the snake_case dict that extraction/claims.py:parse_claims emits.
    ``embedding`` is the experience embedding as a pgvector text literal (used as
    resolver context). ``bindings`` is the capture-time binding index from
    load_experience_bindings: a surface the capture already tied to an entity binds
    to it directly, since re-resolving it is what forked entities in #73. The caller
    owns the surrounding transaction.

    A claim whose subject OR object is a bare first-person reference to the owner
    ("I met Jim", "Jim knows me") is DROPPED rather than written: there is no owner
    self-entity to bind it to yet (#48), and the alternative — a `person` entity
    named "I" — is silent data corruption on the most common capture shape (#56).
    """
    if is_owner_self_reference(claim["subject"]) or is_owner_self_reference(
        claim["object"]
    ):
        acc["claims_skipped_self"] += 1
        return

    subject_id = _resolve_or_create_entity(
        cursor, claim["subject"], claim["subject_kind"], embedding, acc, bindings
    )

    object_entity_id: str | None = None
    object_literal: str | None = None
    bound_object = (
        lookup_binding(bindings, claim["object"], claim["object_kind"])
        if bindings
        else None
    )
    top = (
        None
        if bound_object is not None
        else _resolve_top(cursor, claim["object"], claim["object_kind"], embedding)
    )
    if bound_object is not None:
        # A bound object never degrades to a literal: the capture already decided
        # this surface names an entity.
        object_entity_id = bound_object
    elif _object_should_be_literal(claim, top):
        object_literal = claim["object"]
        acc["literal_objects_fell_back"] += 1
    elif top and top["entity_id"] is not None and top["trgm_score"] >= MATCH_THRESHOLD:
        object_entity_id = top["entity_id"]
    else:
        object_entity_id = _insert_entity(
            cursor, claim["object"], claim["object_kind"], embedding
        )
        acc["entities_created_for_objects"] += 1

    cursor.execute(
        _INSERT_CLAIM_SQL,
        [
            subject_id,
            claim["predicate"],
            claim["predicate_detail"],
            object_entity_id,
            object_literal,
            claim["confidence"],
        ],
    )
    claim_id = dictfetchall(cursor)[0]["id"]
    acc["claims_inserted"] += 1

    cursor.execute(
        _INSERT_CLAIM_SOURCE_SQL,
        [
            claim_id,
            experience_id,
            claim["support_kind"],
            claim["confidence"],
            extracted_by,
        ],
    )
    acc["claim_sources_inserted"] += 1


def write_claims_for_experience(
    cursor,
    experience_id: str,
    embedding: str | None,
    claims: list[dict],
    extracted_by: str,
    acc: dict | None = None,
    bindings: dict | None = None,
) -> dict:
    """Write every claim for one experience; return the accumulator."""
    a = acc if acc is not None else new_accumulator()
    for claim in claims:
        write_claim_for_experience(
            cursor, experience_id, embedding, claim, extracted_by, a, bindings
        )
    return a
