# ABOUTME: Unit tests for the blobstore seam (#42) — the in-memory backend contract.
# ABOUTME: The identical S3 round-trip against minio is asserted in the integration suite.

import hashlib
from urllib.parse import urlparse

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


# Split-endpoint presign ---------------------------------------------------
#
# The server reaches minio over the compose network (http://minio:9000) but the
# client fetches the presigned URL from the tailnet host. SigV4 signs the Host
# header, so a URL minted against the internal endpoint 403s when fetched at the
# public one. These sign offline — boto3 mints presigned URLs without network.

INTERNAL = "http://minio:9000"
PUBLIC = "https://mac-mini.tail1234.ts.net/attachments"


def _s3(**overrides):
    params = dict(
        endpoint=INTERNAL,
        public_endpoint=PUBLIC,
        bucket="brain-attachments",
        access_key="testkey",
        secret_key="testsecret",
        region="us-east-1",
    )
    params.update(overrides)
    return blobstore.S3Blobstore(**params)


def test_presign_signs_against_the_public_endpoint_not_the_internal_one():
    url = _s3().presign("household/abc.webp", ttl=60)
    parsed = urlparse(url)
    public = urlparse(PUBLIC)
    assert parsed.scheme == public.scheme
    assert parsed.netloc == public.netloc
    assert urlparse(INTERNAL).netloc not in url
    # Path-style addressing: bucket in the path, not a DNS subdomain.
    assert parsed.path.endswith("/brain-attachments/household/abc.webp")
    assert "X-Amz-Signature" in url


def test_presign_falls_back_to_the_internal_endpoint_when_public_unset():
    url = _s3(public_endpoint="").presign("household/abc.webp")
    assert urlparse(url).netloc == urlparse(INTERNAL).netloc


def test_internal_operations_keep_using_the_internal_endpoint():
    store = _s3()
    assert store._client.meta.endpoint_url.startswith(INTERNAL)


def test_get_blobstore_wires_the_public_endpoint_from_settings(settings):
    settings.BLOBSTORE_BACKEND = "s3"
    settings.S3_ENDPOINT = INTERNAL
    settings.S3_PUBLIC_ENDPOINT = PUBLIC
    settings.S3_BUCKET = "brain-attachments"
    settings.S3_ACCESS_KEY = "testkey"
    settings.S3_SECRET_KEY = "testsecret"
    settings.S3_REGION = "us-east-1"
    url = blobstore.get_blobstore().presign("household/abc.webp")
    assert urlparse(url).netloc == urlparse(PUBLIC).netloc


def test_get_blobstore_public_endpoint_defaults_to_the_internal_endpoint(settings):
    settings.BLOBSTORE_BACKEND = "s3"
    settings.S3_ENDPOINT = INTERNAL
    settings.S3_PUBLIC_ENDPOINT = ""
    settings.S3_BUCKET = "brain-attachments"
    settings.S3_ACCESS_KEY = "testkey"
    settings.S3_SECRET_KEY = "testsecret"
    settings.S3_REGION = "us-east-1"
    url = blobstore.get_blobstore().presign("household/abc.webp")
    assert urlparse(url).netloc == urlparse(INTERNAL).netloc
