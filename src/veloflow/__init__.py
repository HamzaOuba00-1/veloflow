"""VéloFlow: station-level availability forecasts for Vélib' Métropole."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth for the version: pyproject.toml.
    __version__ = version("veloflow")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

__all__ = ["__version__"]
