"""VéloFlow's own representation of collected data.

These types are independent of the GBFS wire format: if the operator changes
its feed, only ``gbfs.py`` has to change, not the code that consumes these.
All timestamps are timezone-aware and expressed in UTC.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StationStatus:
    """Availability of one station, as published in one feed version."""

    station_id: int
    station_code: str
    mechanical: int
    ebike: int
    docks: int
    is_installed: bool
    is_renting: bool
    is_returning: bool
    # Kept for staleness checks only: on Vélib' it does not follow stock
    # updates (see docs/data/gbfs_exploration.md).
    last_reported: datetime

    @property
    def bikes(self) -> int:
        """Total available bikes, all types."""
        return self.mechanical + self.ebike


@dataclass(frozen=True, slots=True)
class StationInfo:
    """Slowly changing description of one station."""

    station_id: int
    station_code: str
    name: str
    lat: float
    lon: float
    capacity: int


@dataclass(frozen=True, slots=True)
class FeedSnapshot[T]:
    """One parsed version of a feed.

    Attributes:
        feed_updated_at: generation time announced by the feed.
        fetched_at: when VéloFlow received the response.
        stations: stations that passed validation.
        rejected: number of station records that failed validation.
    """

    feed_updated_at: datetime
    fetched_at: datetime
    stations: tuple[T, ...]
    rejected: int

    @property
    def rejected_share(self) -> float:
        """Share of station records rejected (1.0 when the feed had none)."""
        total = len(self.stations) + self.rejected
        return 1.0 if total == 0 else self.rejected / total


type StatusSnapshot = FeedSnapshot[StationStatus]
type InformationSnapshot = FeedSnapshot[StationInfo]
