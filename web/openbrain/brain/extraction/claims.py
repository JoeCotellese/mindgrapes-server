# ABOUTME: Claim extractor (predicate vocabulary, system prompt, response schema).
# ABOUTME: parse_claims validates/normalizes the model JSON; extract_claims wraps the LLM call.

from datetime import datetime

from openbrain.brain.extraction.openrouter_json import call_openrouter_json, iso_z

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_TOKENS = 2048

# Mirrors brain.entity_kind in init/03-brain.sql.
ENTITY_KINDS = ("person", "org", "event", "place", "concept")
# Mirrors brain.support_kind, minus 'imported' which only legacy rows use.
SUPPORT_KINDS = ("verbatim", "paraphrased", "inferred")
# Canonical predicates from docs/predicates.md. 'other' is the escape hatch.
CANONICAL_PREDICATES = (
    "knows",
    "met_at",
    "mentored_by",
    "reports_to",
    "introduced_by",
    "works_at",
    "used_to_work_at",
    "founded",
    "invested_in",
    "partnered_with",
    "said",
    "wrote",
    "recommended",
    "discussed",
    "believes",
    "prefers",
    "decided_to",
    "working_on",
    "interested_in",
    "blocked_by",
    "attended",
    "lives_in",
    "happened_at",
    "other",
)


class ClaimValidationError(ValueError):
    """The model's claim JSON failed validation."""


# The shipped extraction contract, with the kind
# and predicate vocabularies interpolated — don't reword without re-validating output.
CLAIM_SYSTEM_PROMPT = "\n".join(
    [
        "You extract atomic factual claims from short notes a user captured into their second brain.",
        'Output a JSON object {"claims": [...]}. Each claim is a (subject, predicate, object) triple with provenance metadata.',
        "",
        "Required fields per claim:",
        "- subject: string — the entity the claim is about",
        "- subject_kind: one of " + " | ".join(ENTITY_KINDS),
        "- predicate: one of " + " | ".join(CANONICAL_PREDICATES),
        '- predicate_detail: string — REQUIRED only when predicate="other"; the original phrasing of the relation',
        "- object: string — the other side of the relation",
        "- object_kind: one of " + " | ".join(ENTITY_KINDS),
        "- support_kind: verbatim | paraphrased | inferred",
        "- confidence: 0..1",
        "",
        "Calibration rules — read these carefully:",
        '- "verbatim": the relation is DIRECTLY stated in the source text. No interpretation.',
        '- "paraphrased": clearly implied by the wording, just expressed differently.',
        '- "inferred": ANY chained or compound claim. If the source says "she knows C from the accelerator, which accepted Fernworks", the claim "(B, knows, C)" is inferred — the source asserts a chain, not the atomic relation. Compound claims of the form "X from Y, who did Z" must be marked inferred for every derived triple.',
        "- If unsure, prefer inferred + low confidence (<0.6) over verbatim.",
        "- Never invent a fact the source does not support. Empty claims array is a valid output.",
        "",
        "Atomization rules:",
        '- A single multi-fact sentence ("B works at X and used to work at Y") becomes multiple claims, one per fact.',
        '- Do NOT collapse compound chains into a single claim. "A knows B from C" is two claims at most: (A, knows, B) and possibly (B, met_at, C) — and the second is inferred.',
        "",
        'Predicate vocabulary is soft. If no canonical predicate fits, use predicate="other" and put the original relation phrasing in predicate_detail (e.g. predicate_detail="is_godparent_to").',
    ]
)

CLAIM_JSON_SCHEMA = {
    "name": "claim_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "subject",
                        "subject_kind",
                        "predicate",
                        "predicate_detail",
                        "object",
                        "object_kind",
                        "support_kind",
                        "confidence",
                    ],
                    "properties": {
                        "subject": {"type": "string"},
                        "subject_kind": {"type": "string", "enum": list(ENTITY_KINDS)},
                        "predicate": {
                            "type": "string",
                            "enum": list(CANONICAL_PREDICATES),
                        },
                        "predicate_detail": {"type": ["string", "null"]},
                        "object": {"type": "string"},
                        "object_kind": {"type": "string", "enum": list(ENTITY_KINDS)},
                        "support_kind": {"type": "string", "enum": list(SUPPORT_KINDS)},
                        # Bedrock rejects bounds on number; parse_claims enforces 0..1.
                        "confidence": {"type": "number"},
                    },
                },
            },
        },
    },
}


def parse_claims(raw: dict) -> list[dict]:
    """Validate + normalize the model's ``{"claims": [...]}`` payload.

    Coerce ``predicate_detail`` ``""``
    to None, enforce enums and a 0..1 confidence, and require a non-empty
    ``predicate_detail`` when ``predicate == 'other'``. Returns a list of
    snake_case claim dicts the consolidation worker (Slice D) consumes.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        raise ClaimValidationError("expected an object with a 'claims' array")
    return [_parse_claim(claim, i) for i, claim in enumerate(raw["claims"])]


def _parse_claim(claim: object, index: int) -> dict:
    if not isinstance(claim, dict):
        raise ClaimValidationError(f"claim {index} is not an object")

    detail = claim.get("predicate_detail")
    detail = None if detail is None or detail == "" else detail
    predicate = _enum(claim, "predicate", CANONICAL_PREDICATES, index)

    if predicate == "other" and not detail:
        raise ClaimValidationError(
            "predicate='other' requires a non-empty predicate_detail"
        )

    confidence = claim.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ClaimValidationError(
            f"claim {index}: confidence must be a number in 0..1"
        )

    return {
        "subject": _nonempty_str(claim, "subject", index),
        "subject_kind": _enum(claim, "subject_kind", ENTITY_KINDS, index),
        "predicate": predicate,
        "predicate_detail": detail,
        "object": _nonempty_str(claim, "object", index),
        "object_kind": _enum(claim, "object_kind", ENTITY_KINDS, index),
        "support_kind": _enum(claim, "support_kind", SUPPORT_KINDS, index),
        "confidence": float(confidence),
    }


def _nonempty_str(claim: dict, field: str, index: int) -> str:
    value = claim.get(field)
    if not isinstance(value, str) or not value:
        raise ClaimValidationError(f"claim {index}: {field} must be a non-empty string")
    return value


def _enum(claim: dict, field: str, allowed: tuple[str, ...], index: int) -> str:
    value = claim.get(field)
    if value not in allowed:
        raise ClaimValidationError(
            f"claim {index}: {field} must be one of {allowed}, got {value!r}"
        )
    return value


def extract_claims(
    content: str,
    captured_at: datetime,
    *,
    model: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    api_key: str | None = None,
    client=None,
) -> dict:
    """Extract atomic claims from one note via OpenRouter; returns ``{"claims": [...]}``."""
    raw = call_openrouter_json(
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": CLAIM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"captured_at: {iso_z(captured_at)}\n\ncontent:\n{content}",
            },
        ],
        json_schema=CLAIM_JSON_SCHEMA,
        max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
        timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        error_prefix="claim extraction",
        api_key=api_key,
        client=client,
    )
    return {"claims": parse_claims(raw)}
