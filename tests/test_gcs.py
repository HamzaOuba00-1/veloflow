"""Tests for the Cloud Storage calls (no network: a mock transport)."""

import json

import httpx
import pytest

from veloflow.gcs import Storage, join, parse_gs_url


def storage(handler: object) -> Storage:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return Storage(client=client, token=lambda _: "secret-token")


def test_urls() -> None:
    assert parse_gs_url("gs://b/a/c.json") == ("b", "a/c.json")
    with pytest.raises(ValueError, match="gs://"):
        parse_gs_url("https://b/c")
    assert join("gs://b/", "/x/", "y") == "gs://b/x/y"


def test_read_write_and_a_missing_object() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={})
        if request.url.path.endswith("missing"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"data")

    gcs = storage(handler)
    assert gcs.read("gs://bucket/dir/file") == b"data"
    assert gcs.read("gs://bucket/missing") is None
    gcs.write("gs://bucket/dir/new.json", b"{}", "application/json")

    assert seen[0].url.raw_path.startswith(b"/storage/v1/b/bucket/o/dir%2Ffile?")
    assert seen[0].url.params["alt"] == "media"
    upload = seen[-1]
    assert upload.url.params["name"] == "dir/new.json"
    assert upload.headers["Authorization"] == "Bearer secret-token"
    assert upload.headers["Content-Type"] == "application/json"


def test_list_follows_the_pages() -> None:
    pages = {
        None: {"items": [{"name": "p/a", "size": "3"}], "nextPageToken": "t"},
        "t": {"items": [{"name": "p/b", "size": "5"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["prefix"] == "p/"
        return httpx.Response(200, content=json.dumps(pages[request.url.params.get("pageToken")]))

    assert storage(handler).list("gs://bucket/p/") == {"gs://bucket/p/a": 3, "gs://bucket/p/b": 5}


def test_an_error_is_raised() -> None:
    gcs = storage(lambda request: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        gcs.read("gs://bucket/x")
