"""Entry point: ``uv run veloflow-collector`` or ``python -m veloflow.collector``."""

import logging
import signal
import sys
import time
from types import FrameType

from veloflow.collector.config import CollectorSettings
from veloflow.collector.gbfs import GbfsClient
from veloflow.collector.service import Collector
from veloflow.collector.storage import RawArchive

logger = logging.getLogger("veloflow.collector")


def configure_logging(level: str) -> None:
    """Log lines with UTC timestamps, so VM and laptop logs line up."""
    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    logging.Formatter.converter = time.gmtime


def main() -> int:
    """Run the collector until SIGINT (Ctrl+C) or SIGTERM (systemd stop)."""
    settings = CollectorSettings()
    configure_logging(settings.log_level)
    archive = RawArchive(settings.data_dir)

    with GbfsClient(
        settings.gbfs_url, user_agent=settings.user_agent, timeout_s=settings.timeout_s
    ) as client:
        collector = Collector(
            client,
            archive,
            poll_interval_s=settings.poll_interval_s,
            information_interval_s=settings.information_interval_s,
        )

        def request_stop(signum: int, _frame: FrameType | None) -> None:
            logger.info("signal %d received, stopping after the current poll", signum)
            collector.stop()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

        logger.info(
            "collector started: every %.0f s, data in %s",
            settings.poll_interval_s,
            settings.data_dir.resolve(),
        )
        collector.run()

    logger.info("collector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
