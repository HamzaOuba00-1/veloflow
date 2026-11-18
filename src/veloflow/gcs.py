"""Read and write Cloud Storage objects as the machine's service account.

On the collector VM and in Cloud Run, the metadata server hands out a short
token for the attached service account: no key file exists anywhere. Plain
HTTP calls (JSON API), no Google client library, as for the forecast upload
(``serving.publish``).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
API_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
TIMEOUT_S = 60.0

Token = Callable[[httpx.Client], str]


def parse_gs_url(url: str) -> tuple[str, str]:
    """``("bucket", "path/name.json")`` from ``gs://bucket/path/name.json``."""
    if not url.startswith("gs://"):
        raise ValueError(f"not a gs:// URL: {url}")
    bucket, _, name = url.removeprefix("gs://").partition("/")
    if not bucket or not name:
        raise ValueError(f"a gs:// URL needs a bucket and an object name: {url}")
    return bucket, name


def metadata_token(client: httpx.Client) -> str:
    """An access token of the machine's service account (valid about an hour)."""
    response = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


def join(base: str, *parts: str) -> str:
    """``gs://bucket/a`` + ``b``, ``c`` -> ``gs://bucket/a/b/c`` (no doubled slash)."""
    return "/".join([base.rstrip("/"), *[p.strip("/") for p in parts]])


@dataclass
class Storage:
    """Objects of Cloud Storage, read and written with one token per session."""

    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=TIMEOUT_S))
    token: Token = metadata_token
    _bearer: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._bearer is None:
            self._bearer = self.token(self.client)
        return {"Authorization": f"Bearer {self._bearer}"}

    def read(self, url: str) -> bytes | None:
        """The object's bytes, or ``None`` when it does not exist."""
        bucket, name = parse_gs_url(url)
        response = self.client.get(
            f"{API_URL.format(bucket=quote(bucket, safe=''))}/{quote(name, safe='')}",
            params={"alt": "media"},
            headers=self._headers(),
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.content

    def write(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Create or replace the object in one step (a reader sees old or new, never half)."""
        bucket, name = parse_gs_url(url)
        response = self.client.post(
            UPLOAD_URL.format(bucket=quote(bucket, safe="")),
            params={"uploadType": "media", "name": name},
            headers={**self._headers(), "Content-Type": content_type},
            content=data,
        )
        response.raise_for_status()

    def list(self, url: str) -> dict[str, int]:
        """Objects under the prefix ``gs://bucket/prefix``: full URL -> size in bytes."""
        bucket, prefix = parse_gs_url(url)
        found: dict[str, int] = {}
        page: str | None = None
        while True:
            params = {"prefix": prefix, "fields": "items(name,size),nextPageToken"}
            if page:
                params["pageToken"] = page
            response = self.client.get(
                API_URL.format(bucket=quote(bucket, safe="")),
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            body = response.json()
            for item in body.get("items", []):
                found[f"gs://{bucket}/{item['name']}"] = int(item["size"])
            page = body.get("nextPageToken")
            if not page:
                return found

    def close(self) -> None:
        """Release the HTTP connections."""
        self.client.close()
