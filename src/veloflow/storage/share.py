"""Share the recent data with the training job: silver days and station references.

``veloflow-share-data`` runs on the VM every night after the compaction. It
uploads to the private data bucket (``VELOFLOW_DATA_URL``) every complete UTC
day of the last ``--days`` days that the bucket lacks or holds with another
size: the silver ``station_status`` (about 3 MB a day) and the raw station
reference (about 130 KB a day). The raw ``station_status`` archive stays on
the VM. Re-running it uploads nothing new.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from veloflow.gcs import Storage, join

logger = logging.getLogger(__name__)

DATA_ENV = "VELOFLOW_DATA_URL"
DAYS = 7


def shared_files(data_dir: Path, today: date, days: int) -> list[Path]:
    """The files to share: complete days (before ``today``) of the last ``days`` days."""
    wanted = {today - timedelta(days=k) for k in range(1, days + 1)}
    status = [
        data_dir / "silver" / "station_status" / f"date={day}" / "station_status.parquet"
        for day in sorted(wanted)
    ]
    reference = [
        data_dir / "raw" / "station_information" / f"station_information_{day}.jsonl.gz"
        for day in sorted(wanted)
    ]
    return [path for path in (*status, *reference) if path.exists()]


def share(storage: Storage, data_dir: Path, url: str, today: date, days: int = DAYS) -> int:
    """Upload what the bucket lacks; return the number of files sent."""
    present = {**storage.list(join(url, "silver")), **storage.list(join(url, "raw"))}
    sent = 0
    for path in shared_files(data_dir, today, days):
        target = join(url, path.relative_to(data_dir).as_posix())
        size = path.stat().st_size
        if present.get(target) == size:
            continue
        storage.write(target, path.read_bytes())
        sent += 1
        logger.info("shared %s (%d KB)", target, size // 1024)
    return sent


def main(argv: Sequence[str] | None = None, *, storage: Storage | None = None) -> int:
    """Share the last days of ``--data-dir`` with the data bucket."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--url", default=os.environ.get(DATA_ENV), help=f"env {DATA_ENV}")
    parser.add_argument("--days", type=int, default=DAYS)
    args = parser.parse_args(argv)
    if not args.url:
        logger.info("no %s: nothing to share", DATA_ENV)
        return 0
    storage = storage or Storage()
    try:
        sent = share(storage, args.data_dir, args.url, datetime.now(UTC).date(), args.days)
    finally:
        storage.close()
    logger.info("%d files shared with %s", sent, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
