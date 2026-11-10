"""Rule R1 (feed versions in time order) and rule R3 (operational stations).

R1. Vélib' serves the feed from several servers. About once an hour one of
them publishes a state that is 1 to 10 seconds older than the version already
received: its feed time goes back, or repeats. Such a version differs from the
next one about twice as much as the previous version does (measured on 8-9
November 2026, docs/data/quality.md), so keeping it would make hundreds of
stations lose and regain bikes within a minute: fake trips in the target.

The rule keeps a version only if its feed time is strictly later than the last
kept one. On a tie, the later fetch wins: on the three ties observed, it was
always the one consistent with what followed.

R3. A station is operational when it is installed and both renting and
returning are allowed. Availability targets are meaningless otherwise.
"""

from dataclasses import dataclass
from datetime import datetime

import polars as pl


@dataclass(frozen=True, slots=True)
class VersionSelection:
    """Outcome of rule R1 on a set of versions."""

    frame: pl.DataFrame
    kept: int
    stale: int
    replaced_ties: int


def select_versions(frame: pl.DataFrame, *, after: datetime | None = None) -> VersionSelection:
    """Keep the versions whose feed time moves forward (rule R1).

    Args:
        frame: silver ``station_status`` rows, one or several days.
        after: feed time of the last version kept before ``frame`` (for
            example the end of the previous day); versions not later than it
            are stale.

    Returns:
        The rows of the kept versions, ordered by fetch time then station,
        and the counts of kept, stale and replaced versions.
    """
    versions = frame.select("fetched_at", "feed_updated_at").unique().sort("fetched_at").iter_rows()
    kept: list[tuple[datetime, datetime]] = []
    stale = replaced = 0
    for fetched_at, feed_updated_at in versions:
        last = kept[-1][1] if kept else after
        if last is None or feed_updated_at > last:
            kept.append((fetched_at, feed_updated_at))
        elif feed_updated_at == last and kept:
            # Same feed time, different content: trust the later fetch.
            kept[-1] = (fetched_at, feed_updated_at)
            replaced += 1
        else:
            stale += 1

    kept_fetches = pl.Series(
        "fetched_at", [fetched for fetched, _ in kept], dtype=frame.schema["fetched_at"]
    )
    selected = frame.filter(pl.col("fetched_at").is_in(kept_fetches.implode())).sort(
        "fetched_at", "station_id"
    )
    return VersionSelection(frame=selected, kept=len(kept), stale=stale, replaced_ties=replaced)


def with_operational(frame: pl.DataFrame) -> pl.DataFrame:
    """Add the boolean column ``operational`` (rule R3)."""
    return frame.with_columns(
        operational=pl.col("is_installed") & pl.col("is_renting") & pl.col("is_returning")
    )
