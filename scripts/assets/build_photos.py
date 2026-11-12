"""Build the app's Paris pictures from their sources (run once, by hand).

Sources, kept in ``data/assets_src/`` (ignored by Git): ``hero.webp``
(2000 x 435) and ``sidebar.webp`` (600 x 600), two AI-generated pictures of
the Eiffel Tower at dusk provided by Hamza Ouba (see
``src/veloflow/serving/assets/CREDITS.md``).

    uv run python scripts/assets/build_photos.py

Writes ``src/veloflow/serving/assets/hero.jpg`` (the strip behind page
titles) and ``sidebar.jpg`` (the scene at the bottom of the sidebar). They
win over the drawings of the same name (``serving.ui.asset_uri``). Both are
inlined in every page as data URIs, so they are kept small (< 150 KB).
"""

import argparse
from pathlib import Path

from PIL import Image

SOURCES = Path("data/assets_src")
ASSETS = Path("src/veloflow/serving/assets")

# Crops as fractions of each source (left, top, right, bottom).
# The strip is about 4.6:1 on a 1,920 px screen (3.7:1 at 1,440 px) and is
# anchored top right: wider screens lose some bottom, narrower ones some left.
# The hero source ends in a smeared band from x = 0.918: it is cut off.
HERO_CROP = (0.0, 0.0, 0.915, 1.0)
HERO_WIDTH = 1830  # the cropped source's own width: never upscaled
# The hero source was generated in two panels: a hard vertical seam at
# x = 0.4095 of its width. The left panel is replaced (``mirror_left_panel``).
HERO_SEAM = 0.4095
SIDEBAR_CROP = (0.0, 0.0, 1.0, 1.0)  # square, at the size shown (300 px, twice)
SIDEBAR_WIDTH = 600


def mirror_left_panel(image: Image.Image, at: float) -> Image.Image:
    """``image`` with everything left of ``at`` (a fraction of the width) replaced.

    The source was generated in two panels whose skies do not match: the left
    one is replaced by a mirror image of the strip just right of the seam, so
    the sky is continuous across it by construction. The mirrored tower lands
    in the leftmost tenth, which the app hides under the page title.
    """
    w, h = image.size
    seam = round(at * w) + 4  # past the seam's own blended columns
    out = image.copy()
    out.paste(image.crop((seam, 0, 2 * seam, h)).transpose(Image.Transpose.FLIP_LEFT_RIGHT), (0, 0))
    return out


def crop(image: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """``image`` cut to a box given in fractions of its size."""
    w, h = image.size
    return image.crop((round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h)))


def resized(image: Image.Image, width: int) -> Image.Image:
    """``image`` scaled to ``width``, keeping its proportions."""
    return image.resize(
        (width, round(image.height * width / image.width)), Image.Resampling.LANCZOS
    )


def main(argv: list[str] | None = None) -> None:
    """Write the two pictures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=SOURCES)
    parser.add_argument("--out", type=Path, default=ASSETS)
    args = parser.parse_args(argv)

    hero = Image.open(args.sources / "hero.webp").convert("RGB")
    hero = mirror_left_panel(hero, HERO_SEAM)
    hero = resized(crop(hero, HERO_CROP), HERO_WIDTH)
    hero.save(args.out / "hero.jpg", quality=80, optimize=True, progressive=True)

    sidebar = Image.open(args.sources / "sidebar.webp").convert("RGB")
    sidebar = resized(crop(sidebar, SIDEBAR_CROP), SIDEBAR_WIDTH)
    sidebar.save(args.out / "sidebar.jpg", quality=80, optimize=True, progressive=True)

    for name in ("hero.jpg", "sidebar.jpg"):
        path = args.out / name
        print(f"{path}: {Image.open(path).size}, {path.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
