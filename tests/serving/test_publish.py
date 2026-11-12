"""Tests for the upload of the forecast document to Cloud Storage (no network)."""

import gzip
import json
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import pytest

from veloflow.serving import publish as module
from veloflow.serving.publish import (
    CACHE_CONTROL,
    METADATA_TOKEN_URL,
    metadata_token,
    parse_gs_url,
    publish,
)


def parts(request: httpx.Request) -> list[bytes]:
    """Payloads of a multipart/related request, in order."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    payloads = [part.get_payload(decode=True) for part in message.iter_parts()]
    assert all(isinstance(p, bytes) for p in payloads)
    return [p for p in payloads if isinstance(p, bytes)]


def test_parse_gs_url() -> None:
    assert parse_gs_url("gs://veloflow-forecasts/serving/forecasts.json") == (
        "veloflow-forecasts",
        "serving/forecasts.json",
    )
    for wrong in ("https://x/y", "gs://bucket", "gs:///name"):
        with pytest.raises(ValueError, match="gs://"):
            parse_gs_url(wrong)


def test_publish_uploads_gzipped_json_with_no_cache() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"name": "forecasts.json"})

    document = json.dumps({"stations": [{"name": "Châtelet"}]}).encode()
    client = httpx.Client(transport=httpx.MockTransport(handler))

    sent = publish(document, "gs://bucket/forecasts.json", client=client, token=lambda _: "T")

    (request,) = seen
    assert request.url.path == "/upload/storage/v1/b/bucket/o"
    assert request.url.params["uploadType"] == "multipart"
    assert request.headers["authorization"] == "Bearer T"
    assert request.headers["content-type"].startswith("multipart/related; boundary=")
    metadata, media = parts(request)
    assert json.loads(metadata) == {
        "name": "forecasts.json",
        "contentType": "application/json",
        "contentEncoding": "gzip",
        "cacheControl": CACHE_CONTROL,
    }
    assert gzip.decompress(media) == document
    assert sent == len(request.content)


def test_a_refused_upload_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))

    with pytest.raises(httpx.HTTPStatusError):
        publish(b"{}", "gs://bucket/f.json", client=client, token=lambda _: "T")


def test_metadata_token_asks_the_metadata_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == METADATA_TOKEN_URL
        assert request.headers["metadata-flavor"] == "Google"
        return httpx.Response(200, json={"access_token": "abc", "expires_in": 3599})

    assert metadata_token(httpx.Client(transport=httpx.MockTransport(handler))) == "abc"


def test_default_client_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[float] = []
    real = httpx.Client

    def fake_client(timeout: float) -> httpx.Client:
        created.append(timeout)
        return real(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    monkeypatch.setattr(httpx, "Client", fake_client)

    publish(b"{}", "gs://bucket/f.json", token=lambda _: "T")
    assert created == [module.TIMEOUT_S]
