"""Tests for the City of Paris bike counts."""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from veloflow.context.counters import (
    ATTACHMENTS_URL,
    COUNTS_SCHEMA,
    CountersFormatError,
    convert_zip,
    main,
    parse_years,
    raw_zip_path,
    read_counts_csv,
    silver_counts_path,
    yearly_files,
)

HEADER = (
    '"Identifiant du compteur","Nom du compteur","Identifiant du site de comptage",'
    '"Nom du site de comptage","Comptage horaire","Date et heure de comptage",'
    '"Date d\'installation du site de comptage","Lien vers photo du site de comptage",'
    '"Coordonnées géographiques"'
)
# Rows shaped like the 2025 file: winter and summer offsets, a count with a
# non-breaking space as thousands separator, and a row without identifiers.
ROWS = [
    '"100003096-101003096","97 av. Denfert-Rochereau SO-NE","100003096","97 av.",'
    '11,2025-01-01 00:00:00.000 +0100,2012-02-22 00:00:00.000,"[\'x.jpg\']","48.83504, 2.33314"',
    '"100047536-101047536","102 bd de Magenta SE-NO","100047536","102 bd",'
    '1\xa0022,2025-09-18 19:00:00.000 +0200,2018-11-29 00:00:00.000,"[]","48.87, 2.35"',
    ',,,,10\xa0606,2025-06-23 18:00:00.000 +0200,,,"null, null"',
]


def write_csv(path: Path, rows: list[str] = ROWS, header: str = HEADER) -> Path:
    # The City publishes Latin-1, not UTF-8.
    path.write_bytes(("\n".join([header, *rows]) + "\n").encode("latin-1"))
    return path


def test_rows_are_read_with_utc_hour_starts(tmp_path: Path) -> None:
    frame = read_counts_csv(write_csv(tmp_path / "2025.csv"))

    assert dict(frame.schema) == COUNTS_SCHEMA
    assert frame.height == 2  # the row without identifiers is not a counter
    first, second = frame.rows(named=True)
    assert first["hour_start"] == datetime(2024, 12, 31, 23, tzinfo=UTC)
    assert second["hour_start"] == datetime(2025, 9, 18, 17, tzinfo=UTC)
    assert second["count"] == 1022
    assert (first["lat"], first["lon"]) == (48.83504, 2.33314)


def test_missing_column_is_refused(tmp_path: Path) -> None:
    header = HEADER.replace("Comptage horaire", "Comptage")

    with pytest.raises(CountersFormatError, match="missing columns"):
        read_counts_csv(write_csv(tmp_path / "bad.csv", header=header))


def test_unreadable_timestamp_is_refused(tmp_path: Path) -> None:
    rows = [ROWS[0].replace("2025-01-01 00:00:00.000 +0100", "01/01/2025 00h")]

    with pytest.raises(CountersFormatError, match="1 unreadable timestamps"):
        read_counts_csv(write_csv(tmp_path / "bad.csv", rows))


LISTING = {
    "attachments": [
        {
            "href": "https://x/2021",
            "metas": {"title": "2021-comptage-velo-donnees-compteurs.csv.zip"},
        },
        {
            "href": "https://x/2018",
            "metas": {"title": "2018_comptage-velo-donnees-compteurs.csv.zip"},
        },
        {
            "href": "https://x/s",
            "metas": {"title": "2018_comptage-velo-donnees-sites-comptage.csv.zip"},
        },
    ]
}


def test_yearly_files_keep_counter_files_whatever_the_separator() -> None:
    assert yearly_files(json.dumps(LISTING).encode()) == {
        2018: "https://x/2018",
        2021: "https://x/2021",
    }


def test_unexpected_listing() -> None:
    with pytest.raises(CountersFormatError, match="attachments listing"):
        yearly_files(b"{}")


def make_zip(path: Path, csv_path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(csv_path, "2025-comptage-velo-donnees-compteurs.csv")
    return path.read_bytes()


def test_convert_zip_leaves_no_csv_behind(tmp_path: Path) -> None:
    zip_path = tmp_path / "raw" / "bike_counts_2025.zip"
    zip_path.parent.mkdir()
    make_zip(zip_path, write_csv(tmp_path / "source.csv"))

    rows = convert_zip(zip_path, tmp_path / "out" / "bike_counts.parquet")

    assert rows == 2
    assert sorted(path.name for path in zip_path.parent.iterdir()) == ["bike_counts_2025.zip"]


def test_zip_without_a_single_csv_is_refused(tmp_path: Path) -> None:
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("readme.txt", "nothing")

    with pytest.raises(CountersFormatError, match="expected one CSV"):
        convert_zip(zip_path, tmp_path / "out.parquet")


@pytest.mark.parametrize(
    ("text", "years"),
    [("2016-2018", [2016, 2017, 2018]), ("2023,2019", [2019, 2023]), ("2025", [2025])],
)
def test_parse_years(text: str, years: list[int]) -> None:
    assert parse_years(text) == years


def test_command_downloads_once_and_reports_missing_years(tmp_path: Path) -> None:
    zip_bytes = make_zip(tmp_path / "source.zip", write_csv(tmp_path / "source.csv"))
    listing = {
        "attachments": [
            {
                "href": "https://x/2025",
                "metas": {"title": "2025-comptage-velo-donnees-compteurs.zip"},
            }
        ]
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == ATTACHMENTS_URL:
            return httpx.Response(200, json=listing)
        return httpx.Response(200, content=zip_bytes)

    data_dir = tmp_path / "data"
    first = main(
        ["--data-dir", str(data_dir), "--years", "2025"], transport=httpx.MockTransport(handler)
    )
    again = main(
        ["--data-dir", str(data_dir), "--years", "2024-2025"],
        transport=httpx.MockTransport(handler),
    )

    assert first == 0
    assert again == 1  # 2024 is not published
    assert silver_counts_path(data_dir, 2025).exists()
    assert raw_zip_path(data_dir, 2025).exists()
    assert requests.count("https://x/2025") == 1


def test_command_fails_when_the_listing_is_unavailable(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))

    assert main(["--data-dir", str(tmp_path), "--years", "2025"], transport=transport) == 1


def test_command_reports_a_broken_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ATTACHMENTS_URL:
            return httpx.Response(200, json=LISTING)
        return httpx.Response(200, content=b"not a zip")

    code = main(
        ["--data-dir", str(tmp_path), "--years", "2018", "--force"],
        transport=httpx.MockTransport(handler),
    )

    assert code == 1
    assert not silver_counts_path(tmp_path, 2018).exists()
