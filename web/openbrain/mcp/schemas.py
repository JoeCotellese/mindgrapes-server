# ABOUTME: Pydantic output models — the shipped MCP structuredContent shapes.
# ABOUTME: A: read tools + brain:// resources; B: capture_thought I/O; C: entity/recall/review I/O.
from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
#
# Field nullability is deliberate: some fields are
# REQUIRED-but-may-be-null (kept here as `X | None` with no default), while the
# single omittable field — `provenance` — has a default None and is dropped
# from structured output when absent. Getting that distinction right is what
# keeps the shipped output shapes stable for clients.


class ProvenanceClaim(BaseModel):
    claim_id: str
    predicate: str
    predicate_detail: str | None
    object_literal: str | None
    polarity: str
    confidence: float | None
    support_kind: str
    source_confidence: float | None
    extracted_by: str | None
    superseded_by: str | None


class HybridSearchHit(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any]
    captured_at: str
    occurred_at: str | None
    vec_score: float
    lex_score: float
    fused_score: float
    provenance: list[ProvenanceClaim] | None = None


class SearchThoughtsResult(BaseModel):
    count: int
    hits: list[HybridSearchHit]


class ListThoughtItem(BaseModel):
    content: str
    metadata: dict[str, Any]
    created_at: str


class ListThoughtsResult(BaseModel):
    count: int
    thoughts: list[ListThoughtItem]


class DateRange(BaseModel):
    first: str
    last: str


class TopName(BaseModel):
    name: str
    count: int


class ThoughtStatsResult(BaseModel):
    total: int
    date_range: DateRange | None
    types: dict[str, int]
    top_topics: list[TopName]
    top_people: list[TopName]


# get_experience — the fetch half of the search/fetch pair (#149). Mirrors the
# shape returned by reads.get_experience_detail; `found=false` is the identical
# response for a missing id and a private-not-yours id (no existence leak), so
# the detail fields are omittable and dropped from output on that path.


class ExperienceMention(BaseModel):
    entity_id: str
    canonical_name: str
    kind: str
    surface_form: str | None
    merged_into: str | None


class ExperienceClaimEntity(BaseModel):
    id: str
    canonical_name: str
    kind: str


class ExperienceClaimObject(BaseModel):
    # The claim object is a LEFT JOIN: id/canonical_name/kind are all null for an
    # object-literal claim, where `literal` carries the value instead.
    id: str | None
    canonical_name: str | None
    kind: str | None
    literal: str | None


class ExperienceSourcedClaim(BaseModel):
    claim_id: str
    predicate: str
    predicate_detail: str | None
    polarity: str
    confidence: float | None
    support_kind: str
    source_confidence: float | None
    extracted_by: str | None
    subject: ExperienceClaimEntity
    object: ExperienceClaimObject


class ExperienceDetail(BaseModel):
    id: str
    content: str
    captured_at: str
    occurred_at: str | None
    occurred_window: str | None
    source_kind: str
    source_ref: str | None
    metadata: dict[str, Any]
    consolidation_status: str
    # superseded_by / deleted_at are set on audit rows; is_live=false flags them.
    superseded_by: str | None
    deleted_at: str | None
    owner: str
    visibility: str
    is_live: bool
    can_change_visibility: bool


class GetExperienceResult(BaseModel):
    found: bool
    experience: ExperienceDetail | None = None
    mentions: list[ExperienceMention] | None = None
    claims_sourced_here: list[ExperienceSourcedClaim] | None = None


# ---------------------------------------------------------------------------
# capture_thought (Slice B) — input + output
# ---------------------------------------------------------------------------


class ParticipantInput(BaseModel):
    name: str
    # Pre-resolved entity id; if present the participant is linked directly,
    # otherwise the server resolves the name against existing entities.
    entity_id: str | None = None


class PredicateHintInput(BaseModel):
    subject: str
    predicate: str
    object: str
    support_kind: Literal["verbatim", "paraphrased"]


class ExtractedEntity(BaseModel):
    surface: str
    entity_id: str
    action: str
    # True when the bind is a borderline best-guess awaiting human reconciliation
    # (#8). Present on every structured-path participant; false for strong-match,
    # clear-miss, provided-id, and auto-merge binds.
    provisional: bool = False


class BorderlineMatch(BaseModel):
    surface: str
    new_entity_id: str
    candidate_entity_id: str
    trgm_score: float


class DisambiguationOption(BaseModel):
    label: str
    # Free-form payload (e.g. {entity_id: "..."}); omittable so an option can be
    # a bare label — the shipped {label, value?} shape.
    value: Any | None = None


class NeedsDisambiguation(BaseModel):
    # One block per provisionally-bound participant (#8): the surface, the
    # best-guess entity it was bound to, the candidate ids, and the
    # request_disambiguation token + question/options the caller surfaces to the
    # user (then feeds the choice back through resolve_disambiguation).
    surface: str
    provisional_entity_id: str
    candidate_entity_ids: list[str]
    token: str
    question: str
    options: list[DisambiguationOption]


class CaptureThoughtResult(BaseModel):
    experience_id: str
    is_structured: bool
    metadata: dict[str, Any]
    # Structured-path-only fields: omittable, dropped from output on the bare path.
    extracted_entities: list[ExtractedEntity] | None = None
    borderline_matches: list[BorderlineMatch] | None = None
    # Provisional best-guess binds the caller must reconcile (#8); empty when the
    # capture had no borderline participant.
    needs_disambiguation: list[NeedsDisambiguation] | None = None
    claims_pending: bool | None = None


# ---------------------------------------------------------------------------
# Resources (brain://...) — modeled here for clarity (the wire format is plain JSON).
# ---------------------------------------------------------------------------


class SummaryTimeRange(BaseModel):
    earliest: str | None
    latest: str | None


class SummaryEntity(BaseModel):
    id: str
    canonical_name: str
    kind: str
    mention_count: int


class SummaryTopic(BaseModel):
    topic: str
    count: int


class SummaryResource(BaseModel):
    experience_count: int
    entity_count: int
    claim_count: int
    time_range: SummaryTimeRange
    top_entities: list[SummaryEntity]
    top_topics: list[SummaryTopic]
    refreshed_at: str


class RecentEntity(BaseModel):
    id: str
    kind: str
    canonical_name: str
    aliases: list[str]
    merged_into: str | None
    created_at: str


class RecentEntitiesResource(BaseModel):
    window_days: int
    entities: list[RecentEntity]


class PendingReviewsResource(BaseModel):
    merge_candidates: int
    low_confidence_claims: int
    contradictions: int
    disambiguations: int
    proposed_corrections: int
    total: int


# ---------------------------------------------------------------------------
# Slice C — entity repair / recall / review tools (input + output)
# ---------------------------------------------------------------------------


class UpdateExperiencePatch(BaseModel):
    # Only these four fields are editable; content is immutable by spec. All
    # omittable — the service rejects an empty patch. occurred_at/source_ref may
    # be explicitly null to clear them.
    occurred_at: str | None = None
    metadata: dict[str, Any] | None = None
    source_ref: str | None = None
    visibility: Literal["private", "shared"] | None = None


class MergeEntitiesResult(BaseModel):
    loser_id: str
    winner_id: str
    correction_event_id: str
    alias_appended: bool


class RenameEntityResult(BaseModel):
    entity_id: str
    old_canonical_name: str
    new_canonical_name: str
    correction_event_id: str


class RetractClaimResult(BaseModel):
    claim_id: str
    prior_polarity: str
    correction_event_id: str


class SplitEntityResult(BaseModel):
    source_entity_id: str
    target_entity_id: str
    target_created: bool
    mentions_repointed: int
    claims_repointed: int
    correction_event_ids: list[str]


class UnmergeEntityResult(BaseModel):
    entity_id: str
    prior_merged_into: str
    correction_event_id: str


class UpdateExperienceResult(BaseModel):
    id: str
    changed_fields: list[str]
    correction_event_id: str


class RecallRecentResult(BaseModel):
    hits: list[HybridSearchHit]


class WhoWasAtEntity(BaseModel):
    entity_id: str
    canonical_name: str
    kind: str
    surface_form: str
    occurred_at: str | None


class WhoWasAtResult(BaseModel):
    resolved_via: Literal["experience_id", "date"]
    entities: list[WhoWasAtEntity]


class RelatedEntity(BaseModel):
    entity_id: str
    canonical_name: str
    kind: str
    hops: int
    confidence: float


class RelationshipsToResult(BaseModel):
    seed_entity_id: str
    related: list[RelatedEntity]


class ResolveEntityCandidate(BaseModel):
    entity_id: str
    canonical_name: str
    kind: str
    trgm_score: float
    phon_match: bool
    vec_score: float
    fused_score: float


class ResolveEntityResult(BaseModel):
    query_name: str
    query_kind: str
    candidates: list[ResolveEntityCandidate]
    # Server-computed banding of the top candidate's trgm_score (#8): 'reuse'
    # (pass the top entity_id back), 'disambiguate' (borderline — surface options),
    # or 'create' (no confident match). Decouples the cut-points from client prompts.
    recommendation: Literal["reuse", "disambiguate", "create"]


class MergeCandidateItem(BaseModel):
    id: str
    entity_a: str
    entity_b: str
    similarity: float
    created_at: str


class LowConfidenceClaimItem(BaseModel):
    claim_id: str
    subject_id: str
    predicate: str
    confidence: float
    support_kind: str


class ContradictionItem(BaseModel):
    claim_id: str
    superseded_by: str
    subject_id: str
    predicate: str


class DisambiguationItem(BaseModel):
    token: str
    question: str
    options: Any
    created_at: str


class ProposedCorrectionItem(BaseModel):
    id: str
    target_kind: str
    target_id: str
    suggested_change: Any
    reason: str | None
    created_at: str


class SplitCandidateItem(BaseModel):
    entity_id: str
    canonical_name: str
    kind: str
    degree: int


class ReviewQueueResult(BaseModel):
    merge_candidates: list[MergeCandidateItem]
    # Pending pairs hidden by the low-impact gate (mindgrapes-server#18);
    # additive so the shipped five-list shape is unchanged.
    merge_candidates_deferred: int = 0
    low_confidence_claims: list[LowConfidenceClaimItem]
    contradictions: list[ContradictionItem]
    disambiguations: list[DisambiguationItem]
    proposed_corrections: list[ProposedCorrectionItem]
    # Over-connected "god node" entities surfaced by the read-time degree pass
    # (mindgrapes-server#15). Defaults to [] so pre-#15 clients are unaffected.
    split_candidates: list[SplitCandidateItem] = []


class ProposeCorrectionResult(BaseModel):
    id: str
    status: Literal["pending"]


class ResolveCorrectionResult(BaseModel):
    id: str
    decision: Literal["apply", "reject"]
    status: Literal["applied", "rejected"]
    # Present only on apply; dropped from output on reject.
    dispatched_tool: str | None = None
    result: Any | None = None


class RequestDisambiguationResult(BaseModel):
    status: Literal["awaiting_user_disambiguation"]
    token: str
    question: str
    options: list[DisambiguationOption]


class ResolveDisambiguationResult(BaseModel):
    token: str
    resolved_choice: Any
    question: str
    # Present only when the token reconciled a provisional participant binding
    # (#8): {action: 'confirmed', entity_id} on confirm, or {action: 'repointed',
    # ...split result...} on reject. Dropped from output for a plain disambiguation.
    reconciliation: Any | None = None
