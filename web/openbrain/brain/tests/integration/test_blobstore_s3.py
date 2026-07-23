# ABOUTME: Integration tests for the S3 blobstore backend against a real minio.
# ABOUTME: Proves the presigned-GET round trip, TTL expiry, and the signed-host rule.
"""The blobstore contract against a LIVE object store (#42 follow-up).

test_attachments.py validates the database substrate with the in-memory fake.
This file closes the other half: an actual HTTP round trip through minio, which
is the only way to prove the things the fake cannot model —

  * a presigned GET really returns the exact stored bytes;
  * an expired URL really 403s (the TTL is the only bound on a minted URL);
  * a URL signed for one host really 403s when fetched at another, which is the
    entire reason S3_PUBLIC_ENDPOINT exists (SigV4 signs the Host header);
  * path-style addressing works without per-bucket DNS.

The contract tests run against BOTH backends: the memory fake always, and minio
when the dev stack is up with BLOBSTORE_BACKEND=s3. When minio is unreachable the
s3 parameter SKIPS — loudly, with the endpoint in the message. It never silently
passes.

Requires the dev stack up (make dev-up) with the minio service; run via
make dev-test-integration.
"""

import hashlib
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

import pytest
from django.conf import settings

from openbrain.brain.services import blobstore

pytestmark = pytest.mark.integration

TTL = 30


def _s3_config() -> dict | None:
    """The configured S3 endpoint, or None when the dev stack has no minio."""
    endpoint = getattr(settings, "S3_ENDPOINT", "") or ""
    if not endpoint:
        return None
    return {
        "endpoint": endpoint,
        "bucket": getattr(settings, "S3_BUCKET", "") or "brain-attachments",
        "access_key": getattr(settings, "S3_ACCESS_KEY", "") or "",
        "secret_key": getattr(settings, "S3_SECRET_KEY", "") or "",
        "region": getattr(settings, "S3_REGION", "") or "us-east-1",
    }


def _s3_store(**overrides):
    """An S3Blobstore whose presign endpoint is the one this process can reach.

    The deployed value of S3_PUBLIC_ENDPOINT is a tailnet host that a test runner
    inside the compose network cannot fetch, so these tests sign against the
    internal endpoint by default and vary it explicitly where that is the point.
    """
    config = _s3_config()
    if config is None:
        pytest.skip("no S3_ENDPOINT configured — dev stack has no minio service")
    params = dict(config)
    params.setdefault("public_endpoint", params["endpoint"])
    params.update(overrides)
    try:
        store = blobstore.S3Blobstore(**params)
        store.list_keys()  # forces a real call: bucket exists and creds work
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"minio not reachable at {params['endpoint']} "
            f"(bucket {params['bucket']!r}): {exc}"
        )
    return store


@pytest.fixture
def store(request):
    """The blobstore under test; parametrized over both backends by `backends`."""
    if request.param == "memory":
        blobstore._MEMORY_STORE.clear()
        yield blobstore.MemoryBlobstore(bucket="contract-test-bucket")
        blobstore._MEMORY_STORE.clear()
        return
    live = _s3_store()
    written: list[str] = []
    original_put = live.put

    def tracking_put(key, data, mime, *, sha256=None):
        written.append(key)
        return original_put(key, data, mime, sha256=sha256)

    live.put = tracking_put
    yield live
    for key in written:  # the dev bucket is shared; leave no litter
        try:
            live.delete(key)
        except Exception:
            pass


backends = pytest.mark.parametrize(
    "store", ["memory", "s3"], indirect=True, ids=["memory", "s3"]
)


def _key() -> str:
    return f"contract/{uuid.uuid4().hex}.webp"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read()


# Contract — both backends -------------------------------------------------


@backends
def test_put_then_get_round_trips_exact_bytes(store):
    key, data = _key(), b"webp-derivative-bytes-\x00\xff"
    store.put(key, data, "image/webp")
    assert store.get(key) == data


@backends
def test_put_verifies_the_asserted_sha256(store):
    key, data = _key(), b"payload-to-checksum"
    store.put(key, data, "image/webp", sha256=hashlib.sha256(data).hexdigest())
    assert store.get(key) == data


@backends
def test_put_rejects_a_mismatched_sha256(store):
    # Memory raises BlobIntegrityError locally; S3 recomputes and rejects
    # server-side. Both refuse to store bytes that don't match the assertion.
    from botocore.exceptions import ClientError

    key = _key()
    with pytest.raises((blobstore.BlobIntegrityError, ClientError)):
        store.put(key, b"real-bytes", "image/webp", sha256="0" * 64)
    assert store.head(key) is None


@backends
def test_head_reports_length_or_none(store):
    key = _key()
    assert store.head("contract/definitely-missing.webp") is None
    store.put(key, b"1234", "image/webp")
    assert store.head(key)["byte_len"] == 4


@backends
def test_delete_removes_the_object(store):
    key = _key()
    store.put(key, b"z", "image/webp")
    store.delete(key)
    assert store.head(key) is None


@backends
def test_list_keys_includes_what_was_put(store):
    key = _key()
    store.put(key, b"z", "image/webp")
    assert key in store.list_keys()


@backends
def test_presign_addresses_the_right_object(store):
    key = _key()
    store.put(key, b"z", "image/webp")
    assert key in store.presign(key, ttl=TTL)


# Live minio only — what the fake cannot model -----------------------------


def test_presigned_get_returns_the_exact_bytes():
    live = _s3_store()
    key, data = _key(), bytes(range(256)) * 8
    live.put(key, data, "image/webp")
    try:
        assert _fetch(live.presign(key, ttl=TTL)) == data
    finally:
        live.delete(key)


def test_presigned_url_uses_path_style_addressing():
    live = _s3_store()
    key = _key()
    live.put(key, b"z", "image/webp")
    try:
        url = live.presign(key, ttl=TTL)
        parsed = urlparse(url)
        # Bucket in the PATH, not a DNS subdomain — minio has no per-bucket DNS.
        assert parsed.path.endswith(f"/{live.bucket}/{key}")
        assert not parsed.netloc.startswith(f"{live.bucket}.")
        assert _fetch(url) == b"z"
    finally:
        live.delete(key)


def test_expired_presigned_url_is_rejected():
    live = _s3_store()
    key = _key()
    live.put(key, b"z", "image/webp")
    try:
        url = live.presign(key, ttl=1)
        time.sleep(2)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _fetch(url)
        assert excinfo.value.code == 403
    finally:
        live.delete(key)


def test_url_signed_for_another_host_is_rejected():
    """Why S3_PUBLIC_ENDPOINT exists: SigV4 signs Host, so it must match.

    Sign for a host the client will not use, then fetch at the real one — minio
    rejects the signature. This is exactly the 403 a deployment gets when it
    presigns against the internal compose address and the app fetches over the
    tailnet.
    """
    live = _s3_store()
    key = _key()
    live.put(key, b"z", "image/webp")
    try:
        mis_signed = _s3_store(public_endpoint="http://not-the-real-host:9000")
        url = urlparse(mis_signed.presign(key, ttl=TTL))
        real = urlparse(live.presign(key, ttl=TTL))
        tampered = url._replace(scheme=real.scheme, netloc=real.netloc).geturl()
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _fetch(tampered)
        assert excinfo.value.code == 403
    finally:
        live.delete(key)


def test_presigned_get_of_a_missing_object_is_not_found():
    live = _s3_store()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _fetch(live.presign(_key(), ttl=TTL))
    assert excinfo.value.code == 404
