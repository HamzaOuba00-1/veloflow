"""Tests for the heartbeat sent to the external monitor."""

import httpx
import pytest

from veloflow.collector.heartbeat import MAX_MESSAGE_BYTES, Heartbeat

URL = "https://hc-ping.com/secret-uuid"


class Clock:
    """Manual monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def recording(requests: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(handler)


def test_ping_posts_to_the_url() -> None:
    requests: list[httpx.Request] = []
    with Heartbeat(URL + "/", transport=recording(requests)) as heartbeat:
        heartbeat.ping()

    assert [(r.method, str(r.url)) for r in requests] == [("POST", URL)]


def test_pings_are_throttled() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    heartbeat = Heartbeat(URL, min_interval_s=60, transport=recording(requests), monotonic=clock)

    heartbeat.ping()
    clock.now += 30
    heartbeat.ping()  # too soon
    clock.now += 31
    heartbeat.ping()

    assert len(requests) == 2


def test_fail_reports_message_and_resets_throttle() -> None:
    requests: list[httpx.Request] = []
    clock = Clock()
    heartbeat = Heartbeat(URL, transport=recording(requests), monotonic=clock)

    heartbeat.ping()
    heartbeat.fail("x" * (MAX_MESSAGE_BYTES + 50))
    heartbeat.ping()  # recovery is reported at once, despite the throttle

    assert [str(r.url) for r in requests] == [URL, URL + "/fail", URL]
    assert len(requests[1].content) == MAX_MESSAGE_BYTES


@pytest.mark.parametrize(
    "transport",
    [
        recording([], status=500),
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("t"))),
    ],
)
def test_delivery_problems_never_raise_and_never_log_the_url(
    transport: httpx.MockTransport, caplog: pytest.LogCaptureFixture
) -> None:
    heartbeat = Heartbeat(URL, transport=transport)

    heartbeat.ping()
    heartbeat.ping()  # a failed ping does not start the throttle window

    assert caplog.text.count("heartbeat not delivered") == 2
    assert "secret-uuid" not in caplog.text
