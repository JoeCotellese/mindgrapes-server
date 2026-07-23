# ABOUTME: Per-tool MCP annotations (read-only / additive / destructive hint sets).
# ABOUTME: Slice A reads share the read-only set; B adds additive write; C adds the destructive sets.

# readOnlyHint: does not change user-visible brain state (recall_events telemetry
# doesn't count). openWorldHint=false everywhere: the brain is a closed-world store.
READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

# Additive write: appends a new experience without
# destroying or overwriting existing rows; not idempotent (each call writes one).
WRITE_ADDITIVE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

# Destructive write: modifies or removes existing rows;
# not idempotent (a second call errors or mints a duplicate).
WRITE_DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

# Idempotent destructive: mutates existing rows but
# a repeat call with identical args leaves observable state unchanged.
WRITE_IDEMPOTENT_DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

TITLES = {
    "search_thoughts": "Search Thoughts",
    "list_thoughts": "List Recent Thoughts",
    "thought_stats": "Thought Statistics",
    "get_experience": "Get Experience",
    "capture_thought": "Capture Thought",
    "capture_image": "Capture Image",
    "merge_entities": "Merge Entities",
    "rename_entity": "Rename Entity",
    "retract_claim": "Retract Claim",
    "split_entity": "Split Entity",
    "unmerge_entity": "Unmerge Entity",
    "update_experience": "Update Experience",
    "recall_recent": "Recall Recent",
    "who_was_at": "Who Was At",
    "relationships_to": "Relationships To",
    "resolve_entity": "Resolve Entity",
    "review_queue": "Review Queue",
    "propose_correction": "Propose Correction",
    "resolve_correction": "Resolve Correction",
    "request_disambiguation": "Request Disambiguation",
    "resolve_disambiguation": "Resolve Disambiguation",
}
