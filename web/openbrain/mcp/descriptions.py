# ABOUTME: Canonical MCP tool descriptions shipped to clients.
# ABOUTME: The five-section template is the shipped MCP contract; keep it identical.

SERVER_INSTRUCTIONS = """Mind Grapes is a durable, cross-AI memory store: episodic experiences plus a semantic entity/claim graph. Any MCP client can read from it and contribute to it — treat it as a persistent knowledge layer, not session scratch space.

Reading — pick the tool by intent:
- search_thoughts: evergreen lookups by topic, person, or idea ("what do we know about X").
- recall_recent: time-anchored queries ("last night", "yesterday", "this week") — the window is applied before semantic ranking.
- list_thoughts: deterministic metadata scans (type / topic / person / last-N-days).
Before reporting "nothing found", broaden the query or try another read.

Writing — capture proactively. Use capture_thought for decisions, findings, identity context, and outcomes worth remembering, written as standalone self-identifying statements. For named people, call resolve_entity (or pass structured participants) first so you don't create duplicate entities.

Maintenance — periodically drain review_queue so merges, low-confidence claims, and disambiguations don't accumulate.

See the brain://workflows resource for canonical multi-tool recipes."""

SEARCH_THOUGHTS = """Hybrid search over the brain (vector + lexical fused via reciprocal rank fusion). person/topic filters dereference through entity aliases — searching by canonical name surfaces experiences captured under any alias and follows soft-merge pointers. Set with_provenance=true to attach the claim provenance block (support_kind, confidence, sources) to each hit so the caller can surface uncertainty rather than propagate it as fact. Results are scoped to the calling member — your own experiences plus anything marked shared; the legacy operator key sees everything.

Use when: the caller wants evergreen lookups by topic, person, or idea ("what do we know about Fernworks", "captures mentioning Grace").
Don't use when: the query is time-anchored ("last night", "yesterday", "this week") — call recall_recent instead so the time window is applied before semantic ranking.
On empty result: broaden the query, drop person/topic filters, or fall back to recall_recent with a wider days window. Reporting "nothing found" without one of those fallbacks is a bug.

Note: this server intentionally does not expose `search` / `fetch` aliases for ChatGPT compatibility. Callers must use search_thoughts directly.

Cost: low (one embedding call + one SQL hybrid query, <300ms typical).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: writes a brain.recall_events row per call so salience can be recomputed."""

LIST_THOUGHTS = """Recency-ordered listing of captured thoughts with optional metadata filters (type, topic, person, last-N-days). Uses jsonb metadata containment, not embeddings — fast and exact, but only matches what was extracted at capture time. Results are scoped to the calling member — your own experiences plus anything marked shared; the legacy operator key sees everything.

Use when: the caller wants a chronological scan ("what did I capture last week?", "all observations about Acme") or needs deterministic filtering by an extracted tag.
Don't use when: the query is semantic or fuzzy — search_thoughts will rank by meaning instead of relying on metadata that may not have been extracted. For time-anchored semantic queries use recall_recent.
On empty result: relax the filter (drop topic or person), widen days, or call search_thoughts with the same intent phrased as a query.

Cost: low (single indexed SQL query).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

THOUGHT_STATS = """Aggregate counts for the brain: total experiences, type histogram, top topics, top mentioned people, and date range. Computed from brain.experiences metadata, so reflects what extraction has captured rather than the brain.entities canonical view.

Use when: priming a fresh session ("what's in this brain?"), spot-checking corpus growth, or surfacing top-of-mind topics.
Don't use when: the caller wants entity-aware counts (canonical names with merged aliases collapsed) — that belongs to a future brain://summary resource (issue #16), not this tool.
On empty result: an empty brain is a real signal; don't paper over it. Suggest capture_thought to populate.

Cost: low (one count + one full metadata scan; acceptable while the brain is small).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

GET_EXPERIENCE = """Fetch one experience by id in full: its content, the entities mentioned in it (surface forms + merge pointers), and the claims sourced from it (each with predicate, polarity, confidence, support_kind, and subject/object entities). This is the fetch half of the search/fetch pair — search_thoughts / list_thoughts / recall_recent return ids and excerpts; get_experience re-reads a single capture completely.

Use when: you already hold an experience_id — returned by capture_thought, surfaced as a search_thoughts / list_thoughts / recall_recent hit, or referenced by who_was_at — and want the full record (complete content + mentions + claim provenance) rather than the excerpt.
Don't use when: you're searching by topic, person, or time (use search_thoughts / recall_recent / list_thoughts), or you want an entity's profile rather than one capture (that's a different shape).
On empty result: found=false means the id does not exist OR it is private and not yours — these are deliberately indistinguishable, so do not infer existence from a miss. Re-check the id; one taken from a search hit will resolve.

Lifecycle: superseded and soft-deleted experiences still resolve (so an audit deep-link loads), flagged is_live=false; live rows are is_live=true.

Cost: low (three indexed SQL lookups by primary/foreign key, no embedding call).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none — unlike search_thoughts / recall_recent, this does not write a brain.recall_events row."""

CAPTURE_THOUGHT = """Save a new experience to the brain. Bare form (just `content`) generates an embedding and runs server-side LLM metadata extraction synchronously. Structured form (any of occurred_at / participants / predicate_hints / source_kind / source_ref) skips LLM extraction for the provided fields, resolves participants to brain.entities synchronously, and returns the new experience_id plus extracted/borderline entity info. Claim extraction always runs asynchronously via pg_cron. An optional `visibility` ('private' default — only the owner; 'shared' — readable by other household members) sets who can read it; every capture is stamped with the authenticated member as owner.

Use when: the caller has a standalone, self-identifying statement worth remembering — a decision, a finding, identity context, a meeting outcome. Pass structured fields whenever the caller already knows them (process-meeting skill, transcript ingestion) so extraction can't hallucinate them.
Don't use when: the same fact is already captured (search_thoughts first to dedupe), the content is a transient code edit or intermediate debugging step, or the caller is mid-stream and could batch the capture later. For named participants, call resolve_entity first so duplicates are not created.
On empty result: N/A (this is a write).

Cost: medium (one embedding call + bare form runs a synchronous LLM metadata extraction; structured form is faster).
Idempotent: no (each call writes a new experience).
Reversible: partially — content is immutable by spec, but occurred_at/metadata/source_ref can be edited via update_experience and inferred claims can be retracted via retract_claim.
Side effects: writes brain.experiences (legacy public.thoughts is now a view that reflects this row, stamped with owner + visibility), enqueues async claim extraction."""

MERGE_ENTITIES = """Soft-merge a duplicate (loser) entity into the canonical (winner) entity. The loser's canonical_name and aliases are appended to the winner's aliases; loser.merged_into points at winner so existing claims and mentions follow the merge pointer at read time.

Use when: resolve_entity surfaces two rows for the same real-world person/topic, or review_queue(kind='merge_candidates') shows a borderline match the caller has confirmed.
Don't use when: the caller wants to repoint a claim onto a stronger source (that's retract_claim plus a fresh capture), or when the two entities are different real-world things that just share a name (call request_disambiguation first if unsure).
On empty result: N/A (this is a write).

Cost: low.
Idempotent: no (subsequent calls error if the loser is already merged).
Reversible: yes via the recorded correction_events row, which carries the prior loser/winner state.
Side effects: writes brain.entities (loser.merged_into, winner.aliases) and brain.correction_events."""

RENAME_ENTITY = """Change an entity's canonical_name. The prior name is preserved as an alias so historical references still resolve.

Use when: the captured surface form was wrong (e.g. "A (last name unknown)" → "B") and the correction is confirmed, or canonicalization standardizes spelling/casing for a known entity.
Don't use when: the new name refers to a different real-world entity — that's a fresh capture, not a rename.
On empty result: N/A (this is a write).

Cost: low.
Idempotent: yes — renaming to the current canonical name is a no-op.
Reversible: yes via the recorded correction_events row.
Side effects: writes brain.entities and brain.correction_events."""

RETRACT_CLAIM = """Mark a claim as retracted (polarity='retracted') so it stops surfacing in search by default while remaining auditable.

Use when: a fact was wrong (the classic chained-inference hallucination — "C works at the accelerator" extracted from "she knows C from the accelerator") — typically because an inferred claim was extracted from a misread experience.
Don't use when: the claim was true at capture time but is now stale (use update_experience or supersede the claim with a fresh capture instead — retraction is for falsehoods, not for normal change-over-time).
On empty result: N/A (this is a write).

Cost: low.
Idempotent: no (errors if the claim is already retracted).
Reversible: yes via the recorded correction_events row.
Side effects: writes brain.claims.polarity and brain.correction_events."""

SPLIT_ENTITY = """Split one over-collapsed entity into two by repointing a subset of its references onto a different entity. This is the brain's only HARD reference rewrite: unlike merge_entities (soft — it just sets merged_into and lets reads chase the pointer), split_entity physically rewrites mentions.entity_id + claims.subject_id + claims.object_entity_id for the experiences in experience_ids (claims are scoped via claim_sources). into either mints a new entity ({canonical_name, kind?, aliases?, metadata?}; kind defaults to the source's) or names an existing one ({entity_id}).

Use when: resolve_entity bound two real-world things to one entity at capture time — two people sharing a first name (the Karen case), a company vs. its product, two concepts fused by the embedder — and you can enumerate the experiences belonging to the other thing.
Don't use when: the two were wrongly split apart and are really the same thing (that's merge_entities), or you only need to undo a soft merge (that's unmerge_entity). If unsure which experiences belong to which side, scope first with who_was_at / search_thoughts.
On empty result: N/A (this is a write). A scope that repoints zero references means experience_ids was wrong — re-scope rather than assume success.

Cost: low.
Idempotent: no — re-running with into.create mints a second entity; with into.entity_id it's a no-op once the references have moved.
Reversible: yes — split back over the same experience set (swap source/target), guided by the recorded correction_events row.
Side effects: writes brain.mentions, brain.claims, optionally a new brain.entities row, and brain.correction_events."""

UNMERGE_ENTITY = """Undo a soft merge: clear entities.merged_into so the entity stands on its own again. True inverse of merge_entities, and cheap because merge never rewrote references — they simply stop following the pointer.

Use when: auto-consolidation or a manual merge_entities joined two entities that are actually distinct, and you want to separate them without touching their individual mentions/claims (those were never moved).
Don't use when: the entity was over-collapsed at CAPTURE time (references bound directly, merged_into is null) — there is no pointer to clear, so use split_entity instead. Not for renames or retractions.
On empty result: N/A (this is a write). Errors if the entity is not merged.

Cost: low.
Idempotent: no — errors if merged_into is already null.
Reversible: yes — re-run merge_entities, or consult the recorded correction_events row.
Side effects: writes brain.entities.merged_into and brain.correction_events. Aliases that merge appended to the winner are intentionally left in place."""

UPDATE_EXPERIENCE = """Edit non-content fields on an experience: occurred_at, metadata, source_ref, visibility. content is immutable by spec — captures stay verbatim and corrections flow through claims.

Use when: temporal extraction got occurred_at wrong, the caller wants to add a source_ref they knew but didn't pass at capture time, richer metadata needs to be merged in after the fact, or the owner wants to share a private item with the household (visibility='shared') or pull it back (visibility='private').
Don't use when: the captured content itself is wrong — captures are immutable; either retract the wrong claims or supersede the experience with a fresh capture. Also don't try to change another member's item's visibility: only the owner may, and the call is refused otherwise.
On empty result: N/A (this is a write).

Visibility is owner-only and soft. Only the experience's owner can change it (the legacy operator key may change anything). Flipping shared→private stops the other member seeing the item going forward but does not retroactively un-share what they already saw or claims already derived — there is no clawback.

Cost: low.
Idempotent: yes.
Reversible: yes via the recorded correction_events row, which holds the full before/after diff.
Side effects: writes brain.experiences and brain.correction_events."""

RECALL_RECENT = """Time-window-then-semantic recall: filters experiences to the last N days, then runs hybrid search inside that window. Solves the "who did I meet last night" failure that motivated v2. Results are scoped to the calling member — your own experiences plus anything marked shared; the legacy operator key sees everything.

Use when: the query has implicit recency ("yesterday", "last week", "last night", "this morning"). Pass an empty query for a pure recency listing.
Don't use when: the lookup is evergreen and unbounded — search_thoughts is cheaper and will surface older matches that recall_recent's window would hide.
On empty result: broaden the days window, drop the source_kind filter, or fall back to search_thoughts with a date phrase in the query if the recency was approximate rather than literal.

Cost: low (one optional embedding + one SQL window+hybrid query).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: writes a brain.recall_events row per call."""

WHO_WAS_AT = """Resolve the set of entities mentioned at a specific experience or on a specific calendar date. Pass experience_id for a single capture, or date (YYYY-MM-DD) to span every experience whose occurred_at falls on that day (with captured_at fallback when temporal extraction was inconclusive).

Use when: the caller is reconstructing context ("who was at last night's dinner", "who came up in yesterday's notes").
Don't use when: the question is "everyone I've ever met named X" — call resolve_entity, or relationships_to for graph reachability.
On empty result: try a neighboring date (the temporal extractor may have placed the event one day off), or fall back to recall_recent with the same date to surface the underlying experiences.

Cost: low.
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

RELATIONSHIPS_TO = """Recursive walk over non-retracted entity-to-entity claims. Returns reachable entities with their minimum hop count (BFS) and a propagated confidence — the product of each edge's claim confidence along the path — following merged_into so soft-merges don't sever the path. Paths whose running confidence falls below min_confidence (default 0.6, matching the review_queue low-confidence cutoff) are pruned; pass min_confidence=0 to disable the floor and walk every edge.

Use when: the caller wants graph reachability ("everyone I know through X", "orgs reachable from this person", "two-hop neighborhood of this entity"), optionally trusting only well-supported chains.
Don't use when: the caller only needs entities mentioned in one experience or on one date — that's who_was_at and is far cheaper. Don't use for free-text similarity either; that's search_thoughts.
On empty result: lower min_confidence (the default 0.6 floor may have pruned every multi-hop chain), increase max_hops, or call resolve_entity to confirm the starting entity_id is correct (a typo in the seed will make the graph look empty).

Cost: medium (recursive CTE, bounded by max_hops; cost grows roughly with the local degree of the seed).
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

RESOLVE_ENTITY = """Fuzzy + phonetic + semantic candidate lookup for a name. Returns ranked candidates with per-channel scores (trgm_score for name similarity, phon_match for dmetaphone equality, vec_score from embedding cosine, fused_score via reciprocal-rank fusion). Pass context_text to bias the embedding channel toward the right person when several share a name.

Use when: BEFORE asserting an entity exists or capturing a named participant — call this, inspect the top trgm_score, then either (a) reuse the existing id when >0.85, (b) request_disambiguation when 0.55–0.85, or (c) create a new entity (via capture_thought with that participant) when below.
Don't use when: the caller wants every entity mentioned in one experience (whoWasAt) or graph reachability (relationships_to) — those are different shapes.
On empty result: the name is genuinely new; proceed to capture_thought, which will create the entity. Do not silently invent an id.

Cost: low.
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

REVIEW_QUEUE = """Returns items awaiting human review across five surfaces: borderline merge_candidates, low-confidence inferred claims (confidence<0.6), contradictions (claims with superseded_by set but not yet retracted), pending disambiguations, and pending propose_correction rows. Pass kind to scope to one surface; default 'all' returns every queue. Zero-impact merge candidates (both sides claim-free concepts with at most one mention) are deferred rather than listed — merge_candidates_deferred carries their count, and each pair resurfaces automatically once either entity gains a mention or claim.

Use when: starting a Phase-3-style reconciliation session, or periodically draining the queues so the brain doesn't accumulate uncertainty.
Don't use when: the caller is looking for content matches — this surfaces queue state, not search hits. Use search_thoughts or list_thoughts instead.
On empty result: the queues are clean — that's a healthy outcome, not an error. Don't suggest a fallback; report the empty state plainly.

Cost: low.
Idempotent: yes.
Reversible: N/A (read-only).
Side effects: none."""

PROPOSE_CORRECTION = """Queue a non-destructive correction proposal for human approval. Creates a brain.proposed_corrections row in 'pending' status — does not mutate the target.

Use when: the caller suspects something is wrong but lacks authority or confidence to mutate directly. Pair with review_queue(kind='proposed_corrections') to drain.
Don't use when: the caller is confident and authorized to fix the thing themselves — call merge_entities / rename_entity / retract_claim / update_experience directly so the change lands now and is auditable via correction_events.
On empty result: N/A (this is a write).

Cost: low.
Idempotent: no (each call queues a fresh proposal).
Reversible: trivial — the row is the change; reject it or delete it to undo.
Side effects: writes brain.proposed_corrections."""

RESOLVE_CORRECTION = """Drain a queued propose_correction: transition a brain.proposed_corrections row from pending to applied or rejected. On apply, dispatches suggested_change.action to the matching repair tool (repoint_participant → split_entity, rename → rename_entity, retract → retract_claim) and stamps the row applied; on reject, stamps the row without mutating. This is the only thing that makes a proposed correction actionable.

Use when: draining review_queue(kind='proposed_corrections') — an agent (or a past session) suspected something and queued a fix for later human decision; apply the good ones, reject the rest.
Don't use when: you are confident and authorized to fix the thing directly — call split_entity / rename_entity / retract_claim / merge_entities yourself rather than round-tripping through a proposal. Don't call on a proposal that is already resolved (it errors).
On empty result: N/A (this is a write).

Cost: low (plus the cost of the dispatched repair on apply).
Idempotent: no — errors if the proposal is not pending; apply mutates through the dispatched tool.
Reversible: yes — the dispatched mutation records its own correction_events row; reject is a status flip.
Side effects: writes brain.proposed_corrections (status/resolved_at/resolved_by) and, on apply, whatever the dispatched tool writes (entities/claims/mentions + correction_events)."""

REQUEST_DISAMBIGUATION = """Halt the current task and ask the user to choose between options. Returns status='awaiting_user_disambiguation', a token, the question, and the options array — consuming LLMs MUST surface those options to the user verbatim instead of guessing, then call resolve_disambiguation(token, choice) with the user's selection.

Use when: resolve_entity returns multiple borderline candidates, a query is ambiguous, or a destructive operation needs explicit consent before the caller proceeds.
Don't use when: one candidate clearly dominates (top trgm_score >0.85 with a meaningful gap to runner-up) — guessing is fine there. Don't use for purely informational confirmations either; just proceed.
On empty result: N/A (this is a write).

Cost: low.
Idempotent: no (each call creates a fresh disambiguation row).
Reversible: yes — leave the token unresolved or resolve it with the corrective choice.
Side effects: writes brain.disambiguations."""

RESOLVE_DISAMBIGUATION = """Apply the user's choice to a pending disambiguation token. Pass either the option's index (number), its label (string), or the matching {label, value} object.

Use when: closing the loop after request_disambiguation surfaced options to the user — feed the user's literal choice through; don't reinterpret.
Don't use when: the original disambiguation has already been resolved (the call will error) or when the user declined to choose (leave the token unresolved so it shows up in review_queue).
On empty result: N/A (this is a write).

Cost: low.
Idempotent: no (errors if the token is already resolved).
Reversible: yes via the recorded correction_events row keyed on the token.
Side effects: writes brain.disambiguations and brain.correction_events."""
