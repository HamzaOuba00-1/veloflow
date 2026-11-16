"""Take the README screenshots of the live app (run by hand, after a deployment).

Playwright is not a project dependency: ``uv run --with`` brings it for this
script only.

    uv run --with playwright playwright install chromium   # once
    uv run --with playwright python scripts/assets/screenshots.py

Writes ``docs/images/<name>.webp``: each desktop page in a 1440 x 900 window,
saved 1200 px wide, and one phone view in a 390 x 844 window. Every figure on
them comes from the live forecast document, so the pictures show the network
at the time they were taken.
"""

import argparse
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

DEFAULT_URL = "https://veloflow.spaceh.online"
OUT = Path("docs/images")
MAX_BYTES = 450_000  # under the 500 KB pre-commit limit, with a margin
DESKTOP = (1440, 900)
PHONE = (390, 844)
SAVED_WIDTH = 1200  # desktop pictures, as wide as the README shows them
QUALITY = 85
# Streamlit draws over a WebSocket and the maps fetch their tiles afterwards:
# no network event says "done", so each page gets a fixed settling time.
SETTLE_MS = 9000


@dataclass(frozen=True)
class Shot:
    """One picture: a page of the app, in a window of a given size."""

    name: str
    slug: str
    phone: bool = False


SHOTS = (
    Shot("home", "home"),
    Shot("map", "map"),
    Shot("go-now", "go"),
    Shot("at-risk", "at-risk"),
    Shot("station", "station"),
    Shot("admin", "admin"),
    Shot("phone-go-now", "go", phone=True),
)


def capture(page: Page, url: str) -> bytes:
    """The visible window of ``url`` as PNG bytes, once the app has drawn it."""
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    # The freshness pill is drawn from the forecast document: the data is in.
    page.wait_for_selector(".vf-live", timeout=90_000)
    page.wait_for_timeout(SETTLE_MS)
    png: bytes = page.screenshot(type="png")
    return png


def save(png: bytes, path: Path, width: int | None) -> int:
    """Save ``png`` as WebP at ``width`` px (unchanged if None); return its size."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if width is not None and image.width > width:
        height = round(image.height * width / image.width)
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    image.save(path, "WEBP", quality=QUALITY, method=6)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{path} weighs {size:,} bytes, over {MAX_BYTES:,}.")
    return size


def main() -> None:
    """Capture every picture of ``SHOTS`` from the live app."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="root URL of the app")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # Software WebGL: the maps draw without a graphics card.
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        for shot in SHOTS:
            width, height = PHONE if shot.phone else DESKTOP
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=2 if shot.phone else 1,
                is_mobile=shot.phone,
                has_touch=shot.phone,
                locale="en-GB",
                timezone_id="Europe/Paris",
            )
            png = capture(context.new_page(), f"{args.url}/?page={shot.slug}")
            context.close()
            path = OUT / f"{shot.name}.webp"
            size = save(png, path, None if shot.phone else SAVED_WIDTH)
            print(f"{path}  {size / 1000:.0f} KB")
        browser.close()


if __name__ == "__main__":
    main()
