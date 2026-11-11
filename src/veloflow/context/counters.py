"""Hourly bike counts of the City of Paris, 2016 onwards: ``uv run veloflow-counters``.

Our own collection started in November 2026 and will hold no summer and only
a few months by the end of the project. The City's permanent counters, on
cycle lanes and bus lanes, cover every hour since 2016: they measure how
cycling demand reacts to the weekday, holidays, rain and seasons (exploration
step 3.4). They count all bikes passing, not Vélib' rentals.

Source: "Comptage vélo - Historique - Données Compteurs et Sites de comptage",
Ville de Paris, Direction de la Voirie et des Déplacements, under the Open
Database License (ODbL). One zipped CSV per year.

The yearly files do not share one format (measured on 2016-2025,
docs/data/counters.md): the separator is ``;`` or ``,``; the encoding UTF-8
(sometimes with a byte order mark) or Latin-1; counts may be written ``12.0``
or ``1 022`` (non-breaking space); timestamps come in three forms, two with a
UTC offset and one in local Paris time without any; coordinates are written
``lat,lon`` or ``lat, lon``; some zips carry macOS metadata files. In every
year a timestamp **starts** the counted hour. Rows without a counter
identifier (one a hour in 2025, with counts up to 13,057) are not a counter
and are dropped.

Output, one Parquet file per year (the zipped CSV is kept as the raw copy)::

    data/raw/bike_counts/bike_counts_2025.zip
    data/silver/bike_counts/year=2025/bike_counts.parquet
"""

import argparse
import json
import logging
import re
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

import httpx
import polars as pl

logger = logging.getLogger(__name__)

ATTACHMENTS_URL = (
    "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/"
    "comptage-velo-historique-donnees-compteurs/attachments"
)
USER_AGENT = "VeloFlow-counters/0.1 (+https://github.com/HamzaOuba00-1/veloflow)"

# Yearly per-counter hourly files; per-site files ("sites-comptage") are not used.
_COUNTER_FILE = re.compile(r"^(\d{4})[_-]comptage-velo-donnees-compteurs")

COUNTS_SCHEMA: dict[str, pl.DataType] = {
    "counter_id": pl.String(),
    "site_id": pl.String(),
    "lat": pl.Float64(),
    "lon": pl.Float64(),
    "hour_start": pl.Datetime("us", "UTC"),
    "count": pl.Int32(),
}

# Source header -> our column. The coordinates header is matched by its
# prefix: its accents differ with the encoding of each year.
_COLUMNS = {
    "Identifiant du compteur": "counter_id",
    "Identifiant du site de comptage": "site_id",
    "Comptage horaire": "count",
    "Date et heure de comptage": "time",
}
_COORDINATES_PREFIX = "Coordonn"
_BOM = "\ufeff"
PARIS = "Europe/Paris"


class CountersFormatError(Exception):
    """A yearly file does not have the expected structure."""


def _separator(path: Path) -> str:
    """``;`` (2016-2024) or ``,`` (2025), whichever splits the header more."""
    with path.open("rb") as file:
        header = file.readline()
    return ";" if header.count(b";") > header.count(b",") else ","


def _hour_start(time: pl.Expr, counter: pl.Expr) -> pl.Expr:
    """UTC start of the counted hour, from any of the three timestamp forms.

    ``2025-01-01 00:00:00.000 +0100`` and ``2018-11-29T01:00:00+01:00`` carry
    their offset. ``2016-01-01T00:00:00`` is local Paris time: when clocks go
    back, the repeated hour is read as the first one on its first appearance
    for a counter and as the second one after; the hour skipped when clocks go
    forward cannot exist and becomes null.
    """
    with_offset = pl.coalesce(
        time.str.to_datetime("%Y-%m-%d %H:%M:%S%.3f %z", strict=False),
        time.str.to_datetime("%Y-%m-%dT%H:%M:%S%:z", strict=False),
    ).dt.convert_time_zone("UTC")
    naive = time.str.to_datetime("%Y-%m-%dT%H:%M:%S", strict=False)
    repeated = naive.is_first_distinct().over(counter)
    local = naive.dt.replace_time_zone(
        PARIS,
        ambiguous=pl.when(repeated).then(pl.lit("earliest")).otherwise(pl.lit("latest")),
        non_existent="null",
    ).dt.convert_time_zone("UTC")
    return pl.coalesce(with_offset, local).dt.cast_time_unit("us")


def read_counts_csv(path: Path) -> pl.DataFrame:
    """Read one yearly CSV into ``COUNTS_SCHEMA`` rows.

    Raises:
        CountersFormatError: a column is missing, or a timestamp or a count
            cannot be read.
    """
    frame = pl.scan_csv(path, separator=_separator(path), encoding="utf8-lossy", infer_schema=False)
    names = {name.removeprefix(_BOM): name for name in frame.collect_schema().names()}
    missing = [name for name in _COLUMNS if name not in names]
    coordinates = next(
        (names[name] for name in names if name.startswith(_COORDINATES_PREFIX)), None
    )
    if missing or coordinates is None:
        raise CountersFormatError(
            f"{path.name}: missing columns {missing or [_COORDINATES_PREFIX]}"
        )

    raw = (
        frame.select(
            *(pl.col(names[source]).alias(target) for source, target in _COLUMNS.items()),
            pl.col(coordinates).alias("coordinates"),
        )
        .filter(pl.col("counter_id").is_not_null() & (pl.col("counter_id") != ""))
        .collect()
    )
    lat_lon = pl.col("coordinates").str.split(",")
    # "1 022" (any non-breaking or broken space) and "12.0": keep digits and
    # the decimal point, then require a whole number.
    count = pl.col("count").str.replace_all(r"[^0-9.]", "").cast(pl.Float64, strict=False)
    parsed = raw.with_columns(
        lat=lat_lon.list.get(0, null_on_oob=True).str.strip_chars().cast(pl.Float64, strict=False),
        lon=lat_lon.list.get(1, null_on_oob=True).str.strip_chars().cast(pl.Float64, strict=False),
        hour_start=_hour_start(pl.col("time"), pl.col("counter_id")),
        count_value=count,
    )
    # Only a local time without offset may legitimately fail (the skipped hour).
    naive = pl.col("time").str.contains(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    bad_times = parsed.filter(pl.col("hour_start").is_null() & ~naive.fill_null(value=False))
    bad_counts = parsed.filter(
        pl.col("count").is_not_null()
        & (pl.col("count_value").is_null() | (pl.col("count_value") % 1 != 0))
    )
    if bad_times.height or bad_counts.height:
        raise CountersFormatError(
            f"{path.name}: {bad_times.height} unreadable timestamps, "
            f"{bad_counts.height} unreadable counts"
        )
    skipped = parsed["hour_start"].null_count()
    if skipped:
        logger.warning("%s: %d rows on the hour skipped by the clock change", path.name, skipped)
    rows = parsed.filter(pl.col("hour_start").is_not_null())
    # On the day clocks go forward, 2018 and 2019 list the same hour twice for
    # every counter, once with a count of 0: the real count is the larger one.
    duplicates = rows.height - rows.select(pl.struct("counter_id", "hour_start").n_unique()).item()
    if duplicates:
        logger.warning("%s: %d duplicated hours, the larger count is kept", path.name, duplicates)
    return (
        rows.group_by("counter_id", "hour_start")
        .agg(
            pl.col("site_id").first(),
            pl.col("lat").first(),
            pl.col("lon").first(),
            pl.col("count_value").max().cast(pl.Int32).alias("count"),
        )
        .select(list(COUNTS_SCHEMA))
        .sort("counter_id", "hour_start")
    )


def yearly_files(content: bytes) -> dict[int, str]:
    """Year -> download URL of the per-counter file, from the attachments listing."""
    try:
        attachments = json.loads(content)["attachments"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise CountersFormatError(f"unexpected attachments listing: {error}") from error
    files: dict[int, str] = {}
    for attachment in attachments:
        match = _COUNTER_FILE.match(attachment["metas"]["title"])
        if match:
            files[int(match.group(1))] = attachment["href"]
    return files


def raw_zip_path(data_dir: Path, year: int) -> Path:
    """Raw copy of one year, as downloaded."""
    return data_dir / "raw" / "bike_counts" / f"bike_counts_{year}.zip"


def silver_counts_path(data_dir: Path, year: int) -> Path:
    """Parquet file of one year."""
    return data_dir / "silver" / "bike_counts" / f"year={year}" / "bike_counts.parquet"


def convert_zip(zip_path: Path, out_path: Path) -> int:
    """Extract the CSV of ``zip_path`` to a temporary folder, convert it, delete it.

    The CSV weighs about 2 GB a year: it never stays on disk.

    Returns:
        Number of rows written.
    """
    with zipfile.ZipFile(zip_path) as archive:
        # macOS adds "__MACOSX/._<name>" metadata entries to some years.
        members = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".csv") and not name.startswith("__MACOSX/")
        ]
        if len(members) != 1:
            raise CountersFormatError(f"{zip_path.name}: expected one CSV, found {members}")
        with tempfile.TemporaryDirectory(dir=zip_path.parent) as workdir:
            csv_path = Path(archive.extract(members[0], workdir))
            frame = read_counts_csv(csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_suffix(".parquet.tmp")
    frame.write_parquet(temporary, compression="zstd")
    temporary.replace(out_path)
    return frame.height


def download(client: httpx.Client, url: str, path: Path) -> None:
    """Stream ``url`` to ``path`` through a ``.part`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with partial.open("wb") as file:
            for chunk in response.iter_bytes():
                file.write(chunk)
    partial.replace(path)


def parse_years(text: str) -> list[int]:
    """``"2016-2025"`` or ``"2019,2023"`` -> sorted years."""
    years: set[int] = set()
    for part in text.split(","):
        first, _, last = part.strip().partition("-")
        years.update(range(int(first), int(last or first) + 1))
    return sorted(years)


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Download and convert the requested years; skip what is already done."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--years", type=parse_years, default=parse_years("2016-2025"))
    parser.add_argument("--force", action="store_true", help="convert again existing years")
    args = parser.parse_args(argv)

    failures = 0
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=120.0,
        follow_redirects=True,
        transport=transport,
    ) as client:
        try:
            listing = client.get(ATTACHMENTS_URL)
            listing.raise_for_status()
            files = yearly_files(listing.content)
        except (httpx.HTTPError, CountersFormatError) as error:
            logger.error("cannot list the yearly files: %s", error)
            return 1
        for year in args.years:
            out_path = silver_counts_path(args.data_dir, year)
            if out_path.exists() and not args.force:
                logger.info("%d: already converted", year)
                continue
            if year not in files:
                logger.error("%d: no yearly file published", year)
                failures += 1
                continue
            zip_path = raw_zip_path(args.data_dir, year)
            try:
                if not zip_path.exists():
                    download(client, files[year], zip_path)
                rows = convert_zip(zip_path, out_path)
            except (httpx.HTTPError, CountersFormatError, zipfile.BadZipFile) as error:
                logger.error("%d: %s", year, error)
                failures += 1
                continue
            logger.info("%d: %d rows, %.1f MB", year, rows, out_path.stat().st_size / 1e6)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
