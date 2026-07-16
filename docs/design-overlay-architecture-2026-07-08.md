# Mind Grapes — Overlay Architecture & Transcript Recall (design notes)
2026-07-08 · design discussion · motivating use case: "I have a pile of meeting transcripts and I want to query them"

This note records an architecture decision reached while comparing Mind Grapes to
Nate B. Jones's OB1 (`github.com/NateBJones-Projects/OB1`), which takes the
opposite structural approach. The keystone conclusion — the **overlay
principle** — is general; the transcript-recall design is the use case that
surfaced it.

## The overlay principle (keystone)

> **The data model is fixed and dumb; the concepts live in the overlay.**

Mind Grapes has one common data model — `experiences` (episodic) + `entities`
(identity, all kinds) + `claims` (semantic, subject-predicate-object). A
"concept" (CRM, meeting, pipeline, meal plan) is **not** a new schema. It is a
*lens* over the common model:

> concept = **{predicate vocabulary} + {query/traversal} + {synthesis prompt}**, applied at read time.

The substrate never changes shape to accommodate a new concept. A "contact" is a
`person` entity plus the experiences that mention it plus claims about it,
reconstituted by a query. "CRM" is a *reading* of the common model, not a table.

**What this buys:** adding a concept is adding a lens (a skill/tool/query), not a
migration. No new tables, no new identity store, no new MCP server. The new
concept automatically inherits unified retrieval, the one entity graph,
provenance, supersede, and viewer-privacy — because it is the same rows.

**The one substrate seam:** the predicate vocabulary (`docs/predicates.md`). That
is the shared language the semantic layer speaks and the single lever for making
the model more expressive without making it less common — which is why it is
convention, not a SQL CHECK. Predicates are where overlays plug in.

### Escalation ladder (where the overlay stops)

Concepts overlay the model; they do not fork it. Escalate only when a concept
earns it, and even then keep identity shared:

1. **Lens first.** Express the concept as predicates + query + synthesis over
   `experiences`/`entities`/`claims`.
2. **Typed view only when proven.** If a concept needs relational integrity,
   constraints, or heavy typed aggregates (e.g. an opportunity pipeline: stages,
   money, close dates), add a typed **view/projection derived from experiences** —
   not a parallel base table, never a parallel identity store.
3. **External app when it's mutable future-state.** A live pipeline whose stage
   changes, or a calendar of future events, is an *app*, not memory. Integrate it
   (Todoist / Calendar / a real CRM), consistent with the 2026-06-30 boundary:
   memory stores past facts; other systems own future actions.

## Why not OB1's layered domain schemas

OB1 inverts this: each concept **brings its own data model** as an installable
schema pack (`extensions/professional-crm`, `family-calendar`, `meal-planning`,
`job-hunt`, …). Reviewed `professional-crm` as the concrete specimen. It ships
three standalone tables (`professional_contacts`, `contact_interactions`,
`opportunities`), its own RLS, its own generated `tsvector` FTS, its own MCP
server, and 10 `crm_`-prefixed tools. Findings:

- **Parallel identity store.** `professional_contacts.name` is a different row
  from any `entities` person of the same name. The single-entity-graph strength
  is thrown away — the CRM has its own people, the brain has its own people,
  nothing reconciles them.
- **The "bridge" is a string copy, not a link.** `crm_link_thought` "retrieves
  the thought content and appends it to the contact's notes." The fact now lives
  in two places and drifts.
- **Siloed retrieval.** `crm_search_contacts_fts` is lexical-only over the CRM
  tables, not in `match_brain_hybrid`, no embeddings. "What do I know about
  Sarah" hits the brain *or* the CRM, never both.
- **Tool sprawl, self-admitted.** OB1's own README: "32 tools and counting. With
  5 extensions connected, your AI is holding ~32 tool definitions in context…"
  and ships a tool-audit guide as the band-aid.

Mapping the CRM's *capabilities* onto Mind Grapes's substrate, ~70% is redundant
re-implementation: contacts = entities, interactions = experiences,
`crm_get_contact_history` = search-by-person + `who_was_at` + timeline,
`crm_prep_context` = synthesis over experiences mentioning the entity +
`relationships_to`, `crm_stale_contacts` = `max(occurred_at)` per entity,
`crm_link_thought` = unnecessary (the experience already mentions the entity).
The only genuinely additive 30% is the opportunity **pipeline** — forward-looking
workflow state, which is exactly the escalation-ladder case (typed view, or an
external app), not a reason to fork identity.

**Decision: steal the capabilities, not the schema.** Meeting-prep briefings,
stale-relationship detection, and follow-up surfacing are good product ideas;
implement them as synthesis/queries over `experiences` + `entities`, where they
are a few tools over one graph instead of a fourth silo. Don't cargo-cult a
schema philosophy across the product-goal boundary — OB1 is a recipe/schema
marketplace; Mind Grapes is a personal unified memory.

## Transcript recall design (the motivating use case)

"Query a pile of transcripts" is three query shapes with different machinery:

1. **Point lookup** ("what did we decide on pricing?") — served by the concept /
   claim tier. Top-k retrieval. Already solved.
2. **Attribution / verbatim** ("did Alice actually commit, or did I infer it?") —
   needs the raw transcript; atomization flattens speaker turns. Served by
   drill-down from concept to raw.
3. **Corpus aggregate** ("summarize the whole Acme engagement") — top-k is the
   *wrong tool*; it samples, it doesn't summarize. Needs scope-then-scan-then-
   synthesize.

### Two-tier (parent-document) retrieval

- **Retrieval tier:** atomized concepts, embedded and ranked. This is the good
  chunking; keep it. Over-extract (completeness over curation) so coverage holds.
- **Archive tier:** the raw transcript stored as an `imported` experience, kept
  *out of* default ranking so it can't dilute semantic search.
- **The link is load-bearing.** Each concept carries a hard pointer to its raw
  transcript (a derivation edge / `source_ref`), so "more detail" is a *lookup*
  (`get_experience` by id, ranking-independent), not a re-search of the raw tier.

This is the *parent-document / small-to-big* retrieval pattern — validated, not
bespoke.

### Store-raw criterion

Store the raw transcript **in the brain** iff drill-down must work from clients
that cannot reach the source system. OB1's write-back doc says "never store raw
transcripts, link to the source artifact instead" — correct for OB1, because it
assumes the source is reachable. Mind Grapes's source of record for a transcript is
Obsidian, which is local-only and unreachable from a phone / ChatGPT / any
"plugs in from anywhere" client. Because the brain is the only thing reachable
everywhere, the raw has to live in the brain. This overrides OB1's rule on
purpose, for a stated reason.

### Anchor invariant (no orphan raw)

A raw transcript with no concept pointing at it, fully filtered from search, is
invisible to semantic search (reachable only by time or id). Avoid it by
construction: **every `imported` raw transcript requires ≥1 anchor concept whose
pointer references it.** Even a one-line auto-summary gives search a hook and
drill-down a pointer. Belt-and-suspenders: keep raw in the *lexical* index (no
vector, `tsvector` yes) so a distinctive keyword still finds it.

## Concepts borrowed from OB1 recipes

- **panning-for-gold** — production-hardened transcript workflow. "Summaries
  first, transcript second" *is* the two-tier design above. Also surfaces a
  gotcha: voice transcripts generate 3–5× more speaker labels than speakers and
  swap them across environments (one 2-person lunch → 10 labels, 40+ threads
  misattributed). **Speaker consolidation is a prerequisite for attribution
  queries.**
- **provenance-chains** — a first-class `derived_from` edge plus
  `trace_provenance(id)` (walk up to sources = the drill-down) and
  `find_derivatives(id)` (walk down). Mind Grapes has `claim_sources` and supersede
  but no experience→experience derivation edge; this is the formalized
  concept→raw link. Note their redaction of restricted ancestors at the SQL layer
  ties into the viewer-filter work (#167) — drill-down and privacy must be
  designed together.
- **wiki-synthesis** — corpus summaries as **emergent, regenerable views**
  written *back* as derived thoughts with `derived_from` edges to their sources.
  This is the answer to query shape 3, and it settles the client-vs-server
  synthesis question: synthesis is a batch that writes back, not a live per-query
  call.

## Follow-ups

Tracked in issue #183 (derived_from edge, corpus synthesis, speaker
consolidation), which also links the #166/#167 dependencies.
