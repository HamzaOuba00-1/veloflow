"""Client and parser for the Vélib' Métropole GBFS feed.

This is the only module that knows the operator's wire format. It is split in
two layers:

* ``GbfsClient`` performs HTTP calls and returns raw bytes (I/O);
* ``parse_station_status`` / ``parse_station_information`` turn those bytes
  into VéloFlow models (pure functions, trivial to test).

The collector keeps the raw bytes as an immutable backup and works on the
parsed models.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from veloflow.collector.models import (
    FeedSnapshot,
    InformationSnapshot,
    StationInfo,
    StationStatus,
    StatusSnapshot,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class GbfsError(Exception):
    """Base class for every feed problem."""


class FeedUnavailableError(GbfsError):
    """The feed could not be downloaded (network, timeout, HTTP error)."""


class FeedFormatError(GbfsError):
    """The feed was downloaded but does not have the expected structure."""


# --------------------------------------------------------------------------
# Wire format (pydantic). Unknown fields are ignored so that the operator can
# add fields without breaking us; missing or ill-typed ones are errors.
# --------------------------------------------------------------------------


class _Envelope(BaseModel):
    """Fields shared by every GBFS file."""

    model_config = ConfigDict(extra="ignore")

    # The standard name is ``last_updated``; Vélib' publishes ``lastUpdatedOther``.
    last_updated: int = Field(validation_alias=AliasChoices("last_updated", "lastUpdatedOther"))
    data: dict[str, Any]


class _RawStationStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    station_id: int
    station_code: str = Field(alias="stationCode")
    num_bikes_available: int = Field(ge=0)
    num_bikes_available_types: list[dict[str, int]]
    num_docks_available: int = Field(ge=0)
    # Published as 0/1 integers; pydantic accepts 0 and 1 for bool, rejects 2.
    is_installed: bool
    is_renting: bool
    is_returning: bool
    last_reported: int

    @model_validator(mode="after")
    def _types_add_up(self) -> Self:
        """Reject records whose per-type counts do not match the total.

        An unknown bike type (e.g. a new cargo bike) also ends up here: we would
        rather reject loudly than silently drop bikes from the counts.
        """
        by_type = _bike_counts(self.num_bikes_available_types)
        unknown = set(by_type) - {"mechanical", "ebike"}
        if unknown:
            raise ValueError(f"unknown bike types: {sorted(unknown)}")
        if sum(by_type.values()) != self.num_bikes_available:
            raise ValueError("per-type bike counts do not add up to num_bikes_available")
        return self

    def to_model(self) -> StationStatus:
        by_type = _bike_counts(self.num_bikes_available_types)
        return StationStatus(
            station_id=self.station_id,
            station_code=self.station_code,
            mechanical=by_type.get("mechanical", 0),
            ebike=by_type.get("ebike", 0),
            docks=self.num_docks_available,
            is_installed=self.is_installed,
            is_renting=self.is_renting,
            is_returning=self.is_returning,
            last_reported=_utc(self.last_reported),
        )


class _RawStationInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    station_id: int
    station_code: str = Field(alias="stationCode")
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    capacity: int = Field(ge=0)

    def to_model(self) -> StationInfo:
        return StationInfo(
            station_id=self.station_id,
            station_code=self.station_code,
            name=self.name,
            lat=self.lat,
            lon=self.lon,
            capacity=self.capacity,
        )


def _bike_counts(types: list[dict[str, int]]) -> dict[str, int]:
    """Merge ``[{"mechanical": 4}, {"ebike": 2}]`` into ``{"mechanical": 4, "ebike": 2}``."""
    counts: dict[str, int] = {}
    for item in types:
        for bike_type, count in item.items():
            counts[bike_type] = counts.get(bike_type, 0) + count
    return counts


def _utc(unix_seconds: int) -> datetime:
    return datetime.fromtimestamp(unix_seconds, tz=UTC)


# --------------------------------------------------------------------------
# Parsing (pure functions)
# --------------------------------------------------------------------------


def _parse_envelope(content: bytes) -> tuple[_Envelope, list[Any]]:
    """Validate the envelope and return it with the raw station list."""
    try:
        envelope = _Envelope.model_validate_json(content)
    except ValidationError as error:
        raise FeedFormatError(f"invalid feed envelope: {error}") from error
    stations = envelope.data.get("stations")
    if not isinstance(stations, list):
        raise FeedFormatError("feed has no data.stations list")
    return envelope, stations


def _parse_stations[R: (_RawStationStatus, _RawStationInfo)](
    records: list[Any], schema: type[R], feed: str
) -> tuple[list[R], int]:
    """Validate station records one by one.

    One malformed station must not cost us the 1,500 others: invalid records
    are counted and logged, valid ones are kept.
    """
    valid: list[R] = []
    rejected = 0
    for record in records:
        try:
            valid.append(schema.model_validate(record))
        except ValidationError as error:
            rejected += 1
            code = record.get("stationCode") if isinstance(record, dict) else None
            logger.warning(
                "%s: rejected station %s: %s",
                feed,
                code,
                error.errors(include_url=False)[0]["msg"],
            )
    return valid, rejected


def parse_station_status(content: bytes, fetched_at: datetime) -> StatusSnapshot:
    """Parse a ``station_status`` response.

    Raises:
        FeedFormatError: the envelope is unusable (not JSON, no timestamp, no station list).
    """
    envelope, records = _parse_envelope(content)
    valid, rejected = _parse_stations(records, _RawStationStatus, "station_status")
    return FeedSnapshot(
        feed_updated_at=_utc(envelope.last_updated),
        fetched_at=fetched_at,
        stations=tuple(raw.to_model() for raw in valid),
        rejected=rejected,
    )


def parse_station_information(content: bytes, fetched_at: datetime) -> InformationSnapshot:
    """Parse a ``station_information`` response.

    Raises:
        FeedFormatError: the envelope is unusable (not JSON, no timestamp, no station list).
    """
    envelope, records = _parse_envelope(content)
    valid, rejected = _parse_stations(records, _RawStationInfo, "station_information")
    return FeedSnapshot(
        feed_updated_at=_utc(envelope.last_updated),
        fetched_at=fetched_at,
        stations=tuple(raw.to_model() for raw in valid),
        rejected=rejected,
    )


def parse_feed_urls(content: bytes) -> dict[str, str]:
    """Map feed name to URL from the ``gbfs.json`` auto-discovery file.

    Raises:
        FeedFormatError: no feed list could be found.
    """
    try:
        envelope = _Envelope.model_validate_json(content)
        data = envelope.data
        # GBFS 1.x/2.x group feeds by language; GBFS 3.x lists them directly.
        feeds = data["feeds"] if "feeds" in data else next(iter(data.values()))["feeds"]
        return {str(feed["name"]): str(feed["url"]) for feed in feeds}
    except (ValidationError, KeyError, TypeError, StopIteration) as error:
        raise FeedFormatError(f"invalid gbfs.json: {error!r}") from error


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawResponse:
    """Bytes exactly as received, with the time we received them."""

    content: bytes
    fetched_at: datetime


class GbfsClient:
    """Synchronous HTTP client for a GBFS feed.

    Keeps one ``httpx.Client`` (connection reuse across polls) and caches the
    feed URLs found by auto-discovery. Use it as a context manager so the
    connection pool is always closed.
    """

    def __init__(
        self,
        entry_point_url: str,
        *,
        user_agent: str,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create the client.

        Args:
            entry_point_url: URL of ``gbfs.json``.
            user_agent: identifies VéloFlow to the operator.
            timeout_s: per-request timeout. Responses take ~3 s at times,
                so this must stay well above that.
            transport: injected in tests to simulate the server.
        """
        self._entry_point_url = entry_point_url
        self._feed_urls: dict[str, str] | None = None
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool."""
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def feed_urls(self) -> dict[str, str]:
        """Return feed URLs, downloading ``gbfs.json`` on first call only."""
        if self._feed_urls is None:
            self._feed_urls = parse_feed_urls(self._get(self._entry_point_url).content)
        return self._feed_urls

    def fetch(self, feed_name: str) -> RawResponse:
        """Download one feed by name (e.g. ``"station_status"``).

        Raises:
            FeedFormatError: the feed is not listed in ``gbfs.json``.
            FeedUnavailableError: the download failed.
        """
        urls = self.feed_urls()
        if feed_name not in urls:
            raise FeedFormatError(f"feed {feed_name!r} not listed in gbfs.json: {sorted(urls)}")
        return self._get(urls[feed_name])

    def forget_feed_urls(self) -> None:
        """Drop cached URLs so the next call re-reads ``gbfs.json``."""
        self._feed_urls = None

    def _get(self, url: str) -> RawResponse:
        try:
            response = self._http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            # HTTPError covers timeouts, connection failures and 4xx/5xx statuses.
            raise FeedUnavailableError(f"GET {url} failed: {error!r}") from error
        return RawResponse(content=response.content, fetched_at=datetime.now(UTC))
