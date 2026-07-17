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
                    "for named participants. The common path is one call: "
                    "capture_thought resolves each name server-side (reuse / "
                    "provisional-bind / create), so no resolve_entity pre-call is "
                    "needed. Only reconcile when the result asks — a "
                    "needs_disambiguation block means the server made a borderline "
                    "best guess and wants a human decision."
                ),
                "steps": [
                    {
                        "tool": "capture_thought",
                        "when": (
                            "always, for the capture itself. Pass participants by "
                            "name; the server resolves each one (strong match → "
                            "reuse, clear miss → create, borderline → bind to the "
                            "best guess flagged provisional). Pass an explicit "
                            "entity_id only when you already resolved it yourself."
                        ),
                        "args_hint": {
                            "content": "<thought>",
                            "participants": [{"name": "<name>"}],
                        },
                    },
                    {
                        "tool": "resolve_disambiguation",
                        "when": (
                            "ONLY when the capture_thought result carries a "
                            "needs_disambiguation block. Surface its options to the "
                            "user verbatim, then feed the choice back with the block's "
                            "token — confirm keeps the provisional bind, reject "
                            "repoints the mention to a new entity."
                        ),
                        "args_hint": {"token": "<token>", "choice": "<user choice>"},
                    },
                    {
                        "tool": "resolve_entity",
                        "when": (
                            "OPTIONAL, before capture, only when you want the "
                            "server's reuse/disambiguate/create recommendation for a "
                            "name up front — e.g. to pass an explicit entity_id."
                        ),
                        "args_hint": {"name": "<participant name>", "kind": "person"},
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
