"""Publish the forecast document to Cloud Storage, from the collector VM.

The VM writes the document every 10 minutes; the API and the app (Cloud Run)
read it from its public URL. The upload uses the identity of the VM: a token
from the metadata server for the service account attached to it, which may
write to the forecast bucket and nothing else. No key file exists anywhere.

Plain HTTP calls, no Google client library: one upload is two requests, and
the VM keeps its memory for the collector.
"""

import gzip
import json
import secrets
from collections.abc import Callable
from urllib.parse import quote

import httpx

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
# Readers must always get the latest document: a public object is otherwise
# cached for an hour by Google's edge caches.
CACHE_CONTROL = "no-cache, max-age=0"
TIMEOUT_S = 30.0


def parse_gs_url(url: str) -> tuple[str, str]:
    """``("bucket", "path/name.json")`` from ``gs://bucket/path/name.json``."""
    if not url.startswith("gs://"):
        raise ValueError(f"not a gs:// URL: {url}")
    bucket, _, name = url.removeprefix("gs://").partition("/")
    if not bucket or not name:
        raise ValueError(f"a gs:// URL needs a bucket and an object name: {url}")
    return bucket, name


def metadata_token(client: httpx.Client) -> str:
    """An access token of the VM's service account (valid about an hour)."""
    response = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


def publish(
    payload: bytes,
    url: str,
    *,
    client: httpx.Client | None = None,
    token: Callable[[httpx.Client], str] = metadata_token,
) -> int:
    """Upload a JSON document gzipped to ``gs://bucket/name``; returns the bytes sent.

    Stored with ``Content-Encoding: gzip``: Cloud Storage hands it compressed
    to clients that accept gzip (httpx does, and decompresses it) and
    decompressed to the others. An upload replaces the object in one step, so
    a reader never sees half a document.
    """
    bucket, name = parse_gs_url(url)
    metadata = {
        "name": name,
        "contentType": "application/json",
        "contentEncoding": "gzip",
        "cacheControl": CACHE_CONTROL,
    }
    content_type, body = multipart_related(json.dumps(metadata).encode(), gzip.compress(payload))
    with client or httpx.Client(timeout=TIMEOUT_S) as http:
        response = http.post(
            UPLOAD_URL.format(bucket=quote(bucket, safe="")),
            params={"uploadType": "multipart"},
            headers={"Authorization": f"Bearer {token(http)}", "Content-Type": content_type},
            content=body,
        )
        response.raise_for_status()
    return len(body)


def multipart_related(metadata: bytes, media: bytes) -> tuple[str, bytes]:
    """The ``multipart/related`` body of a Cloud Storage upload (metadata, then data)."""
    boundary = f"veloflow-{secrets.token_hex(16)}"
    head = f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
    middle = f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/related; boundary={boundary}", head + metadata + middle + media + tail
