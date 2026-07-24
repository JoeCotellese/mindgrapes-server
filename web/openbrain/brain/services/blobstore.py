# ABOUTME: Object-store seam for attachment blobs (#42) — put/get/presign/delete/head.
# ABOUTME: S3 backend (boto3, minio/Hetzner) + an in-memory fake selected by config.
"""Blobstore: the narrow object-store interface the attachment path writes through.

Two backends, one contract, selected by settings.BLOBSTORE_BACKEND:
  * 'memory' — a process-local dict, for the unit suite (no live S3 needed).
  * 's3'     — boto3 against a configurable S3-compatible endpoint. Path-style
               addressing so minio works without per-bucket DNS.

Keys are content-addressed by the caller (see extraction/images.content_key): the
prefix is organizational, never a tenant isolation boundary. `put` passes the
sha256 of the transferred (derivative) bytes so S3 verifies the upload
server-side and rejects a truncated/corrupt object; a mismatch raises so no
attachments row is written for a bad upload.

Endpoints are SPLIT. Server-side operations (put/get/head/delete/list) go to
S3_ENDPOINT — the compose-network address (http://minio:9000). Presigned URLs are
minted against S3_PUBLIC_ENDPOINT, the address the *client* will fetch, because
SigV4 signs the Host header: a URL signed for the internal host 403s
(SignatureDoesNotMatch) when fetched at the tailnet host. S3_PUBLIC_ENDPOINT
falls back to S3_ENDPOINT when unset, which is correct for a single-address
deployment.
"""

import base64
import binascii
from dataclasses import dataclass

from django.conf import settings

DEFAULT_PRESIGN_TTL_SECONDS = 60


class BlobIntegrityError(Exception):
    """A stored object's bytes don't match the sha256 the caller asserted."""


def content_key(account_id: str, original_sha256: str, ext: str = "webp") -> str:
    """The content-addressed object key. Prefix is organizational, not a boundary."""
    return f"{account_id}/{original_sha256}.{ext}"


def _sha256_b64(hex_digest: str) -> str:
    """S3's x-amz-checksum-sha256 wants base64 of the raw 32-byte digest, not hex."""
    return base64.b64encode(binascii.unhexlify(hex_digest)).decode("ascii")


# In-memory backend --------------------------------------------------------

# Module-level so put-then-get/presign round-trips within a test process. Keyed
# by (bucket, key); the fake never speaks HTTP, so the real presigned-GET
# round-trip is an integration-only assertion against minio.
_MEMORY_STORE: dict[tuple[str, str], dict] = {}


@dataclass
class MemoryBlobstore:
    bucket: str

    def put(self, key: str, data: bytes, mime: str, *, sha256: str | None = None) -> None:
        import hashlib

        actual = hashlib.sha256(data).hexdigest()
        if sha256 is not None and actual != sha256:
            raise BlobIntegrityError(
                f"blob sha256 mismatch for {key}: asserted {sha256}, got {actual}"
            )
        _MEMORY_STORE[(self.bucket, key)] = {
            "data": data,
            "mime": mime,
            "sha256": actual,
        }

    def get(self, key: str) -> bytes:
        return _MEMORY_STORE[(self.bucket, key)]["data"]

    def head(self, key: str) -> dict | None:
        obj = _MEMORY_STORE.get((self.bucket, key))
        if obj is None:
            return None
        return {"byte_len": len(obj["data"]), "mime": obj["mime"]}

    def presign(self, key: str, ttl: int = DEFAULT_PRESIGN_TTL_SECONDS) -> str:
        # Not a real URL — the fake can't be HTTP-fetched. Encodes the key so a
        # test can assert the right object was addressed without a live S3.
        return f"memory://{self.bucket}/{key}?ttl={ttl}"

    def delete(self, key: str) -> None:
        _MEMORY_STORE.pop((self.bucket, key), None)

    def list_keys(self) -> list[str]:
        """All keys in this bucket — backs the orphan-detection reconciliation."""
        return [k for (b, k) in _MEMORY_STORE if b == self.bucket]


# S3 backend ---------------------------------------------------------------


class S3Blobstore:
    def __init__(self, *, endpoint, bucket, access_key, secret_key, region,
                 public_endpoint=""):
        # Lazy import so the module loads (and the memory backend works) even if
        # boto3 isn't installed in a given environment.
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        # Path-style so a minio endpoint works without bucket-name DNS. SigV4 is
        # pinned: against a custom endpoint botocore would otherwise fall back to
        # SigV2, which minio is deprecating and which put_object's ChecksumSHA256
        # needs anyway. SigV4 signs the Host header — the reason presigning has to
        # happen against the public endpoint.
        config = Config(
            signature_version="s3v4", s3={"addressing_style": "path"}
        )
        creds = {
            "aws_access_key_id": access_key or None,
            "aws_secret_access_key": secret_key or None,
            "region_name": region or None,
            "config": config,
        }
        self._client = boto3.client("s3", endpoint_url=endpoint or None, **creds)
        # A second client bound to the client-reachable address; used ONLY to
        # mint presigned URLs so the signed Host matches what the client fetches.
        # Same instance when the two addresses coincide.
        if public_endpoint and public_endpoint != endpoint:
            self._presign_client = boto3.client(
                "s3", endpoint_url=public_endpoint, **creds
            )
        else:
            self._presign_client = self._client

    def put(self, key: str, data: bytes, mime: str, *, sha256: str | None = None) -> None:
        kwargs = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": mime,
        }
        if sha256 is not None:
            # S3 recomputes sha256 and rejects the upload on mismatch.
            kwargs["ChecksumSHA256"] = _sha256_b64(sha256)
        self._client.put_object(**kwargs)

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def head(self, key: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {"byte_len": resp["ContentLength"], "mime": resp.get("ContentType")}

    def presign(self, key: str, ttl: int = DEFAULT_PRESIGN_TTL_SECONDS) -> str:
        # Short-TTL bearer URL. NEVER log or persist the return value — log the
        # object_key instead (see reads.get_experience_detail). Un-share cannot
        # revoke an already-minted URL (no clawback, per #48); the TTL bounds it.
        # Signed against the PUBLIC endpoint — see the module docstring.
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys


def get_blobstore():
    """Construct the configured backend. 'memory' for tests, 's3' otherwise."""
    backend = getattr(settings, "BLOBSTORE_BACKEND", "memory")
    bucket = getattr(settings, "S3_BUCKET", "brain-attachments") or "brain-attachments"
    if backend == "memory":
        return MemoryBlobstore(bucket=bucket)
    if backend == "s3":
        return S3Blobstore(
            endpoint=getattr(settings, "S3_ENDPOINT", ""),
            public_endpoint=getattr(settings, "S3_PUBLIC_ENDPOINT", ""),
            bucket=bucket,
            access_key=getattr(settings, "S3_ACCESS_KEY", ""),
            secret_key=getattr(settings, "S3_SECRET_KEY", ""),
            region=getattr(settings, "S3_REGION", ""),
        )
    raise ValueError(f"unknown BLOBSTORE_BACKEND {backend!r}")
