# Mind Grapes — canonical predicate vocabulary

Per spec §6 Q2, claims use a **soft predicate vocabulary**: the canonical list below is what the migration LLM is asked to emit, but novel relations are not rejected. When the LLM proposes a relation that doesn't fit any canonical predicate, it emits `predicate='other'` and supplies the original phrasing in `predicate_detail`. A future normalization pass (deferred) can consolidate frequent `other`s into new canonical entries.

The list aims for ~20 predicates that cover the dominant relations in a personal second brain (people, work, communication, opinion, status, time/place). It deliberately avoids over-specialization — the goal is enough vocabulary to be queryable, not a complete ontology.

## Conventions

- Predicates are lowercase, snake_case, and read naturally as `<subject> <predicate> <object>`.
- Subject is always an entity (person, org, event, place, or concept).
- Object is preferably an entity; when it isn't a meaningful entity (e.g., a literal date, a free-form quote), the migration falls back to `object_literal`.
- Tense / temporality lives in `valid_during` (a `tstzrange`) rather than separate `*_at` predicates. The exception is `used_to_*` predicates where the lapsed-relationship semantic is the whole point.

---

## People relations

| Predicate | Meaning | Example triple |
|---|---|---|
| `knows` | Subject is acquainted with object (mutual or asserted by subject). | `(B, knows, C)` |
| `met_at` | Subject met the object person at a place/event (object is the meeting context). | `(B, met_at, "the Founders Summit")` |
| `mentored_by` | Object mentors / has mentored the subject. | `(Ada, mentored_by, Grace)` |
| `reports_to` | Org-chart relationship. Subject reports to object. | `(Ada, reports_to, Acme-CEO)` |
| `introduced_by` | Object introduced the subject to a third party (third party expressed in `predicate_detail` if not the immediate object). | `(B, introduced_by, Ada)` |

## Employment & affiliation

| Predicate | Meaning | Example triple |
|---|---|---|
| `works_at` | Current employer / primary affiliation. | `(B, works_at, "Acme")` |
| `used_to_work_at` | Past employment. Not synonymous with `works_at` + retracted polarity — preserves the historical fact as still-true-of-the-past. | `(B, used_to_work_at, "Initech")` |
| `founded` | Subject founded the object org/project. | `(Ada, founded, Fernworks)` |
| `invested_in` | Investment/backing relationship. | `("Seed Capital", invested_in, Fernworks)` |
| `partnered_with` | Working alongside object on a project; weaker than `works_at`. | `(Fernworks, partnered_with, Acme)` |

## Communication & content

| Predicate | Meaning | Example triple |
|---|---|---|
| `said` | Direct verbatim quote from object. Object is normally a literal string. | `(Grace, said, "latency budgets matter more than throughput")` |
| `wrote` | Subject authored the object (article, doc, post). | `(Ada, wrote, "the v2 spec")` |
| `recommended` | Subject recommended object (a tool, person, technique). | `(Grace, recommended, "pgvector HNSW")` |
| `discussed` | Topic came up in conversation involving the subject. | `(Ada, discussed, "claim provenance")` |

## Opinion, decision, belief

| Predicate | Meaning | Example triple |
|---|---|---|
| `believes` | Subject holds object as a belief / position. | `(Ada, believes, "tests must precede implementation")` |
| `prefers` | Subject prefers the object (over implicit alternatives). | `(Ada, prefers, "fish shell")` |
| `decided_to` | Subject made an explicit decision. Object is the decision content (literal). | `(Ada, decided_to, "ship dual-write before claim migration")` |

## Status, intent, work-in-progress

| Predicate | Meaning | Example triple |
|---|---|---|
| `working_on` | Active, not-yet-complete work. | `(Ada, working_on, "the v2 spec")` |
| `interested_in` | Lower-commitment than `working_on`; signal for what to read/follow. | `(Ada, interested_in, "knowledge graphs")` |
| `blocked_by` | Subject (project/task) blocked by the object. | `("issue #10", blocked_by, "issue #9")` |

## Time & place

| Predicate | Meaning | Example triple |
|---|---|---|
| `attended` | Subject was present at the object event. | `(Ada, attended, "the demo day")` |
| `lives_in` | Subject's residence. | `(Ada, lives_in, "Lisbon")` |
| `happened_at` | Object event happened at subject location/time. | `("the demo", happened_at, "the accelerator office")` |

---

## Escape hatch: `predicate='other'`

When the migration LLM cannot honestly place a relation into one of the canonical predicates above, it emits:

```json
{
  "subject": "B",
  "subject_kind": "person",
  "predicate": "other",
  "predicate_detail": "is_godparent_to",
  "object": "C",
  "object_kind": "person",
  "support_kind": "verbatim",
  "confidence": 0.9
}
```

The migration writes `predicate='other'` and `predicate_detail='is_godparent_to'`. Frequent `predicate_detail` values surface in a future normalization pass — those candidates that recur often enough get promoted to the canonical list in a follow-up doc revision.

## Predicates explicitly excluded (rationale)

- **`is_a` / `instance_of` / type predicates.** Entity kind already lives on `brain.entities.kind`. A claim "(B, is_a, person)" duplicates schema-level information.
- **`mentioned_in` / `references`.** These are provenance, not facts. Provenance is captured in `claim_sources` and `brain.mentions`.
- **`*_count` aggregates.** Aggregations are queries over the graph, not claims.
- **`will_*` predictive predicates.** The brain stores what the user knows or has experienced, not predictions. If a captured thought predicts something, the predicate is `believes` or `decided_to` with the prediction as a literal object.

## Prompt integration

The migration's system prompt enumerates this list with one-line definitions and the `other`+`predicate_detail` escape hatch. It instructs the model that `support_kind='verbatim'` requires the relation to be directly stated in the source text — compound or chained inferences ("she knows C from the accelerator, which accepted Fernworks" → "C is at the accelerator") must always be marked `inferred`. This is the calibration the spec §6 chained-inference example targets.
