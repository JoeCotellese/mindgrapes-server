# ABOUTME: Static brain://workflows document.
# ABOUTME: Named tool-composition recipes clients read on session start (data-driven
# ABOUTME: resources live in services/mcp_reads.py).


def workflows_document() -> dict:
    """Canonical tool-composition recipes (spec §3.3). Static — no SQL round-trip."""
    return {
        "schema_version": 1,
        "workflows": [
            {
                "name": "capture_with_dedup",
                "description": (
                    "Capture a new experience without creating duplicate entities "
                    "for named participants. Resolve each participant first; reuse "
                    "the existing id when the match is strong, surface options when "
                    "borderline."
                ),
                "steps": [
                    {
                        "tool": "resolve_entity",
                        "when": (
                            "BEFORE capture, for every named participant. Inspect "
                            "the top trgm_score."
                        ),
                        "args_hint": {"name": "<participant name>", "kind": "person"},
                    },
                    {
                        "tool": "request_disambiguation",
                        "when": (
                            "top trgm_score is in the 0.55–0.85 borderline band. "
                            "Surface options to the user verbatim."
                        ),
                    },
                    {
                        "tool": "capture_thought",
                        "when": (
                            "high-confidence match (reuse entity_id) OR no match "
                            "(create fresh). Pass participants with entity_id when "
                            "matched, name-only when new."
                        ),
                        "args_hint": {
                            "content": "<thought>",
                            "participants": [
                                {"name": "<name>", "entity_id": "<uuid-if-matched>"}
                            ],
                        },
                    },
                ],
            },
            {
                "name": "research_topic",
                "description": (
                    "Look up everything we know about a topic. Start with semantic "
                    "search; on empty result, fall back to a metadata-filtered "
                    "listing; surface low-confidence claims via review_queue so the "
                    "caller can disclose uncertainty rather than propagate it."
                ),
                "steps": [
                    {
                        "tool": "search_thoughts",
                        "when": (
                            "first pass: evergreen lookup by topic or person. Set "
                            "with_provenance=true so confidence is visible."
                        ),
                        "args_hint": {"query": "<topic>", "with_provenance": True},
                    },
                    {
                        "tool": "list_thoughts",
                        "when": (
                            "search_thoughts returned zero hits. Fall back to a "
                            "chronological topic-filtered scan."
                        ),
                        "args_hint": {"topic": "<topic>", "days": 30},
                    },
                    {
                        "tool": "review_queue",
                        "when": (
                            "results carry low confidence (<0.6) or include "
                            "support_kind=inferred. Check the queue for related "
                            "contradictions before reporting."
                        ),
                        "args_hint": {"kind": "low_confidence_claims"},
                    },
                ],
            },
            {
                "name": "correct_identity",
                "description": (
                    "Two real-world entities ended up as separate rows. Soft-merge "
                    "them so claims and mentions follow the merge pointer; the audit "
                    "trail in correction_events makes the change reversible."
                ),
                "steps": [
                    {
                        "tool": "resolve_entity",
                        "when": (
                            "confirm both rows refer to the same real-world entity. "
                            "Pick the canonical one as winner."
                        ),
                        "args_hint": {"name": "<canonical name>"},
                    },
                    {
                        "tool": "merge_entities",
                        "when": (
                            "after confirming. Provide a reason — it lands in "
                            "correction_events for the audit trail."
                        ),
                        "args_hint": {
                            "loser_id": "<uuid>",
                            "winner_id": "<uuid>",
                            "reason": "Confirmed same person via <evidence>",
                        },
                    },
                ],
            },
        ],
    }
