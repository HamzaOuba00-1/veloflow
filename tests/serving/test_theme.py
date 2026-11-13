"""Tests for the design tokens and the style sheet that reads them."""

import re
from pathlib import Path

import pytest

from veloflow.serving import theme


def test_every_variable_of_the_style_sheet_is_a_token() -> None:
    """The style sheet picks no value of its own: each ``var(--vf-x)`` is defined."""
    sheet = (theme.STATIC / "css" / "veloflow.css").read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--vf-([a-z0-9-]+)\)", sheet))

    assert used
    assert used <= set(theme.TOKENS), sorted(used - set(theme.TOKENS))


def test_style_sheet_starts_with_the_tokens() -> None:
    style = theme.style_sheet()

    assert style.startswith("<style>:root{--vf-color-primary:#6247D4;")
    assert style.endswith("</style>")
    assert "@font-face" in style
    assert "app/static/fonts/InterVariable-latin.woff2" in style
    assert (theme.STATIC / "fonts" / "InterVariable-latin.woff2").is_file()


def test_contrast_of_text_colours() -> None:
    """Text colours meet WCAG AA (4.5:1) on white and on the page background."""

    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def ratio(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    texts = [
        theme.PRIMARY,
        theme.TEXT,
        theme.TEXT_SECONDARY,
        theme.TEXT_TERTIARY,
        theme.SUCCESS,
        theme.WARNING,
        theme.ERROR,
        *theme.COUNT_COLOURS.values(),
    ]
    for colour in texts:
        for background in (theme.SURFACE, theme.BACKGROUND):
            assert ratio(colour, background) >= 4.5, (colour, background)


def test_pictures_are_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr(theme, "STATIC", tmp_path)

    assert theme.image_url("landing-cta") is None  # not added yet: the page draws a fallback
    (images / "landing-cta.jpg").write_bytes(b"\xff\xd8")
    assert theme.image_url("landing-cta") == "app/static/images/landing-cta.jpg"
    (images / "landing-cta.webp").write_bytes(b"RIFF")
    assert theme.image_url("landing-cta") == "app/static/images/landing-cta.webp"  # WebP wins


def test_bundled_logo_and_favicon() -> None:
    assert theme.image_url("logo") == "app/static/images/logo.png"  # the PNG wins over the SVG
    assert (theme.STATIC / "images" / "favicon.png").is_file()
