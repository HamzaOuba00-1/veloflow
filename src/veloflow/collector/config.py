"""Runtime configuration of the collector.

Values come from environment variables prefixed ``VELOFLOW_COLLECTOR_`` (or a
local ``.env`` file), falling back to the defaults below. Example on the VM:
``VELOFLOW_COLLECTOR_DATA_DIR=/var/lib/veloflow``.
"""

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VELIB_GBFS_URL = (
    "https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/gbfs.json"
)


class CollectorSettings(BaseSettings):
    """Settings validated at start-up: a typo fails fast instead of at 3 a.m."""

    model_config = SettingsConfigDict(
        env_prefix="VELOFLOW_COLLECTOR_", env_file=".env", extra="ignore"
    )

    gbfs_url: str = VELIB_GBFS_URL
    user_agent: str = "VeloFlow-collector/0.1 (+https://github.com/HamzaOuba00-1/veloflow)"
    # 30 s catches every feed version (regenerated every 60-61 s, decision D9).
    # The 10 s floor keeps us polite towards the operator whatever the config says.
    poll_interval_s: float = Field(default=30.0, ge=10.0)
    information_interval_s: float = Field(default=3600.0, ge=60.0)
    timeout_s: float = Field(default=15.0, gt=0.0)
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    # Ping URL of the external monitor (dead man's switch). A secret: set it in
    # the environment of the VM, never in the repository. None disables it.
    healthcheck_url: str | None = None

    @model_validator(mode="after")
    def _timeout_fits_in_interval(self) -> Self:
        """A request must never be able to swallow a whole polling slot."""
        if self.timeout_s >= self.poll_interval_s:
            raise ValueError("timeout_s must be shorter than poll_interval_s")
        return self
