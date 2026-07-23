# ABOUTME: capture_image write service (#42) — image -> bounded WebP blob + experience.
# ABOUTME: All network I/O (vision, embed, S3 put) runs BEFORE the single insert txn.
"""capture_image: layer image ingest on top of captures.capture().

Order (mirrors captures.py: no network I/O inside the transaction):
  decode -> validate -> re-encode -> vision fallback (opt-in) -> embed
  -> blobstore.put -> transaction.atomic() { insert experience, upsert blob,
     insert attachments row, resolve participants/place/event }

Identity is sha256 of the ORIGINAL bytes (extraction.images), so the same photo
captured twice reuses one blob (ON CONFLICT on the content-addressed key) and
gets a second attachments row. A vision/embed failure aborts before the S3 put,
so no orphan blob is created on that path; a failure AFTER the put but before
commit leaves an orphan blob the reconciliation query (orphan_blob_keys) detects
and the GC follow-up reaps.

Vision is gated on visibility: a private image is never sent to the third-party
vision model — it fails closed to a deterministic placeholder + a pending flag so
a future consolidation pass can re-describe it. Only 'shared' captures with no
caller description egress bytes.
"""

import json
import uuid
from dataclasses import dataclass

from openbrain.brain.db import brain_cursor, dictfetchall, to_vector_literal
from openbrain.brain.embeddings import embed_query
from openbrain.brain.extraction.geo import promote_latlng
from openbrain.brain.extraction.images import process_image
from openbrain.brain.services import blobstore as blobstore_mod
from openbrain.brain.services import captures
from openbrain.brain.services.entity_resolver import (
    link_mention,
    resolve_or_create_entity,
)
from openbrain.brain.vision import describe as vision_describe

# The base64 `image` arg is for small pasted screenshots only. A real photo goes
# through a presigned S3 PUT + the object_key path — a 1MB photo base64 is ~350k
# tokens, uncallable by an LLM client and an OOM vector. The edge (Caddy/Starlette
# body-limit) rejects oversize bodies BEFORE JSON parse; this is the belt-and-
# suspenders check after decode-side arrival.
MAX_BASE64_CHARS = 256 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_METADATA_DEPTH = 6


class ImagePayloadError(Exception):
    """The caller's image payload is malformed, oversized, or self-contradictory."""


_UPSERT_BLOB_SQL = """
    insert into brain.blobs (bucket, object_key, mime, byte_len, sha256, original_sha256)
    values (%s, %s, %s, %s, %s, %s)
    on conflict (bucket, object_key)
      do update set original_sha256 = coalesce(brain.blobs.original_sha256, excluded.original_sha256)
    returning id::text as id
"""

_INSERT_ATTACHMENT_SQL = """
    insert into brain.attachments (id, experience_id, blob_id, width, height)
    values (%s::uuid, %s::uuid, %s::uuid, %s, %s)
    returning id::text as id
"""

# Left-anti-join the object store's keys against the blob rows: any key with no
# referencing blob row is an orphan (a post-put/pre-commit failure). The GC
# follow-up reaps these; this query + its test ships now.
_BLOB_KEYS_SQL = "select object_key from brain.blobs where bucket = %s"


@dataclass
class _Blob:
    """The stored-object facts both intake paths resolve to before the insert."""

    key: str
    derivative: bytes | None  # None on the object_key path (app already uploaded)
    derivative_sha: str
    orig_sha: str
    mime: str
    width: int | None
    height: int | None
    byte_len: int
    exif_lat: float | None = None
    exif_lng: float | None = None
    exif_when: str | None = None


def _prepare_blob(*, store, account_id, image_base64, object_key,
                  original_sha256, mime, width, height) -> _Blob:
    """Resolve the intake (inline base64 vs already-uploaded object_key) to a _Blob.

    Exactly one intake must be supplied; the inline path decodes/re-encodes here,
    the object_key path HEADs the already-uploaded derivative for its byte_len.
    """
    if bool(image_base64) == bool(object_key):
        raise ImagePayloadError(
            "supply exactly one of image_base64 or object_key (+original_sha256/mime/dims)"
        )

    if image_base64 is not None:
        import base64

        if len(image_base64) > MAX_BASE64_CHARS:
            raise ImagePayloadError(
                f"inline image too large ({len(image_base64)} base64 chars > "
                f"{MAX_BASE64_CHARS}); use the presigned-PUT object_key path"
            )
        try:
            raw = base64.b64decode(image_base64, validate=True)
        except Exception as exc:
            raise ImagePayloadError(f"image is not valid base64: {exc}") from exc

        p = process_image(raw)  # raises ImageDecodeError on non-image
        return _Blob(
            key=blobstore_mod.content_key(account_id, p.original_sha256, ext="webp"),
            derivative=p.derivative,
            derivative_sha=p.derivative_sha256,
            orig_sha=p.original_sha256,
            mime=p.mime,
            width=p.width,
            height=p.height,
            byte_len=len(p.derivative),
            exif_lat=p.exif_lat,
            exif_lng=p.exif_lng,
            exif_when=p.exif_occurred_at,
        )

    if not (original_sha256 and mime):
        raise ImagePayloadError("object_key path requires original_sha256 and mime")
    head = store.head(object_key)
    if head is None:
        raise ImagePayloadError(
            f"object {object_key!r} not found in the bucket; upload it first"
        )
    return _Blob(
        key=object_key,
        derivative=None,
        derivative_sha=original_sha256,
        orig_sha=original_sha256,
        mime=mime,
        width=width,
        height=height,
        byte_len=head["byte_len"],
    )


def _metadata_depth(value, depth=0) -> int:
    if depth > MAX_METADATA_DEPTH:
        return depth
    if isinstance(value, dict):
        return max((_metadata_depth(v, depth + 1) for v in value.values()), default=depth)
    if isinstance(value, list):
        return max((_metadata_depth(v, depth + 1) for v in value), default=depth)
    return depth


def _validate_metadata(metadata: dict | None) -> None:
    if not metadata:
        return
    serialized = json.dumps(metadata)
    if len(serialized) > MAX_METADATA_BYTES:
        raise ImagePayloadError(
            f"metadata too large ({len(serialized)} bytes > {MAX_METADATA_BYTES})"
        )
    if _metadata_depth(metadata) > MAX_METADATA_DEPTH:
        raise ImagePayloadError(f"metadata nested deeper than {MAX_METADATA_DEPTH}")


def _compose_content(description, ocr, placeholder) -> tuple[str, bool]:
    """Return (content, is_placeholder). OCR/labels fold into the embedded content
    so search — which embeds/lexes content only — sees the richest signal."""
    base = (description or "").strip()
    if not base:
        return placeholder, True
    if ocr and str(ocr).strip():
        return f"{base}\n\nDetected text: {str(ocr).strip()}", False
    return base, False


def _placeholder(occurred_at, place_label) -> str:
    when = occurred_at or "unknown time"
    where = f" near {place_label}" if place_label else ""
    return f"[image captured {when}{where}, description pending]"


def _resolve_description(*, description, ocr, visibility, derivative, mime,
                         occurred_at, place_label, row_metadata) -> tuple[str, bool]:
    """Caller description is primary; vision is the visibility-gated fallback."""
    if description and description.strip():
        return _compose_content(description, ocr, "")

    # No caller description. Only 'shared' captures may egress bytes to vision.
    if visibility == "shared":
        try:
            text = vision_describe(derivative, mime)
            row_metadata["vision_status"] = "generated"
            return _compose_content(text, ocr, _placeholder(occurred_at, place_label))
        except Exception:
            row_metadata["vision_status"] = "failed"
    else:
        row_metadata["vision_status"] = "skipped_private"

    row_metadata["description_pending"] = True
    return _placeholder(occurred_at, place_label), True


def capture_image(
    *,
    owner: str,
    account_id: str,
    visibility: str = "private",
    image_base64: str | None = None,
    object_key: str | None = None,
    original_sha256: str | None = None,
    mime: str | None = None,
    width: int | None = None,
    height: int | None = None,
    description: str | None = None,
    occurred_at: str | None = None,
    location: dict | None = None,
    participants: list[dict] | None = None,
    metadata: dict | None = None,
    ocr: str | None = None,
    event: str | None = None,
    client: str = "mcp",
) -> dict:
    """Capture an image as a searchable experience backed by a stored blob.

    Two intake shapes: inline `image_base64` (small screenshots, <=256KB base64)
    which the server decodes/re-encodes/uploads, or the durable app path where the
    app already PUT the derivative via a presigned URL and passes
    object_key+original_sha256+mime+width+height. Exactly one must be supplied.
    """
    _validate_metadata(metadata)
    store = blobstore_mod.get_blobstore()
    location = location or {}
    place_label = location.get("label")
    row_metadata: dict = dict(metadata or {})
    if ocr:
        row_metadata["ocr"] = ocr
    if place_label or location.get("accuracy_m") or location.get("source"):
        row_metadata["location"] = {
            k: location.get(k) for k in ("label", "accuracy_m", "source")
            if location.get(k) is not None
        }

    blob = _prepare_blob(
        store=store,
        account_id=account_id,
        image_base64=image_base64,
        object_key=object_key,
        original_sha256=original_sha256,
        mime=mime,
        width=width,
        height=height,
    )

    lat, lng = promote_latlng(
        location.get("lat"), location.get("lng"), blob.exif_lat, blob.exif_lng
    )
    when = occurred_at or blob.exif_when

    content, _is_placeholder = _resolve_description(
        description=description,
        ocr=ocr,
        visibility=visibility,
        derivative=blob.derivative,
        mime=blob.mime,
        occurred_at=when,
        place_label=place_label,
        row_metadata=row_metadata,
    )

    # Embed BEFORE the S3 put so an embed failure aborts with no orphan blob.
    embedding = embed_query(content)

    # Upload the derivative (inline path only) before the transaction.
    if blob.derivative is not None:
        store.put(blob.key, blob.derivative, blob.mime, sha256=blob.derivative_sha)

    attachment_id = str(uuid.uuid4())

    def _after_insert(cursor, experience_id):
        cursor.execute(
            _UPSERT_BLOB_SQL,
            [store.bucket, blob.key, blob.mime, blob.byte_len,
             blob.derivative_sha, blob.orig_sha],
        )
        blob_id = dictfetchall(cursor)[0]["id"]
        cursor.execute(
            _INSERT_ATTACHMENT_SQL,
            [attachment_id, experience_id, blob_id, blob.width, blob.height],
        )
        _link_place_event(cursor, experience_id, to_vector_literal(embedding),
                          place_label, event)

    result = captures.capture(
        content=content,
        owner=owner,
        account_id=account_id,
        visibility=visibility,
        occurred_at=when,
        participants=participants,
        source_kind="imported",
        source_ref=f"attachment:{attachment_id}",
        lat=lat,
        lng=lng,
        client=client,
        metadata_extra=row_metadata,
        embedding=embedding,
        after_insert=_after_insert,
    )
    result["attachment_id"] = attachment_id
    result["object_key"] = blob.key
    result["byte_len"] = blob.byte_len
    return result


def _link_place_event(cursor, experience_id, embedding_lit, place_label, event) -> None:
    """Best-effort semantic links for the location/event (field='topics').

    place / event are resolved and linked so "ask the brain about <event>" and
    "in <place>" surface the image experience. Unlike people, they are linked
    directly (no disambiguation ceremony) — a wrong best-guess is low-cost here.
    """
    for surface, kind in ((place_label, "place"), (event, "event")):
        if not surface or not str(surface).strip():
            continue
        outcome = resolve_or_create_entity(
            cursor, experience_id, embedding_lit,
            surface=str(surface).strip(), field="topics", kind=kind,
        )
        link_mention(cursor, experience_id, outcome["entity_id"], str(surface).strip(),
                     "topics")


def orphan_blob_keys(bucket: str | None = None) -> list[str]:
    """Object keys present in the store with no referencing brain.blobs row.

    The reconciliation the GC follow-up consumes: a post-put/pre-commit crash
    leaves the object but no row. Compares the store's key list against blob rows
    for the same bucket (left-anti-join in Python — key counts are small in v1).
    """
    store = blobstore_mod.get_blobstore()
    store_keys = set(store.list_keys())
    with brain_cursor() as cursor:
        cursor.execute(_BLOB_KEYS_SQL, [bucket or store.bucket])
        row_keys = {r["object_key"] for r in dictfetchall(cursor)}
    return sorted(store_keys - row_keys)
