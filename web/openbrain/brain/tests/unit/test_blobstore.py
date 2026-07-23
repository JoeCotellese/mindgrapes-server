# ABOUTME: Unit tests for the blobstore seam (#42) — the in-memory backend contract.
# ABOUTME: The identical S3 round-trip against minio is asserted in the integration suite.

import hashlib

import pytest

from openbrain.brain.services import blobstore


@pytest.fixture(autouse=True)
def _clear_store():
    blobstore._MEMORY_STORE.clear()
    yield
    blobstore._MEMORY_STORE.clear()


def _store():
    return blobstore.MemoryBlobstore(bucket="test-bucket")


def test_content_key_is_account_prefixed():
    key = blobstore.content_key("household", "abc123", ext="webp")
    assert key == "household/abc123.webp"


def test_put_then_get_round_trips_exact_bytes():
    store = _store()
    data = b"webp-bytes-here"
    store.put("household/k.webp", data, "image/webp")
    assert store.get("household/k.webp") == data


def test_put_verifies_sha256_and_rejects_mismatch():
    store = _store()
    data = b"payload"
    good = hashlib.sha256(data).hexdigest()
    store.put("k1", data, "image/webp", sha256=good)  # no raise
    with pytest.raises(blobstore.BlobIntegrityError):
        store.put("k2", data, "image/webp", sha256="0" * 64)


def test_head_reports_len_and_mime_or_none():
    store = _store()
    assert store.head("missing") is None
    store.put("k", b"1234", "image/webp")
    head = store.head("k")
    assert head["byte_len"] == 4
    assert head["mime"] == "image/webp"


def test_presign_encodes_key_and_ttl():
    store = _store()
    store.put("household/x.webp", b"z", "image/webp")
    url = store.presign("household/x.webp", ttl=60)
    assert "household/x.webp" in url
    assert "ttl=60" in url


def test_delete_removes_object():
    store = _store()
    store.put("k", b"z", "image/webp")
    store.delete("k")
    assert store.head("k") is None


def test_list_keys_scoped_to_bucket():
    a = blobstore.MemoryBlobstore(bucket="bucket-a")
    b = blobstore.MemoryBlobstore(bucket="bucket-b")
    a.put("k1", b"1", "image/webp")
    a.put("k2", b"2", "image/webp")
    b.put("k3", b"3", "image/webp")
    assert sorted(a.list_keys()) == ["k1", "k2"]
    assert b.list_keys() == ["k3"]
