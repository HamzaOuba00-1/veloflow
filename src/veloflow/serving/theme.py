"""Design tokens of the web app: one source for the CSS, the maps and the charts.

Every colour, size and duration of the interface is named here once. The
style sheet (``static/css/veloflow.css``) only reads them as CSS custom
properties (``var(--vf-...)``); pydeck and Altair read the same values from
Python.

Text colours meet WCAG 2.2 AA (4.5:1) on white, on the page background and
on their own tinted background. Contrast on white / on ``BACKGROUND``:
primary 6.2 / 6.0 (white on primary: 6.2) · text 17.1 / 16.5 ·
text-secondary 6.5 / 6.3 · text-tertiary 5.1 / 4.9 · success 5.3 / 5.1 ·
warning 5.6 / 5.4 · error 5.1 / 4.9 · e-bike 5.8 / 5.6 · docks 5.2 / 5.0.
"""

from functools import lru_cache
from importlib.resources import files

STATIC = files("veloflow.serving") / "static"
STATIC_URL = "app/static"  # Streamlit serves ``static/`` there (server.enableStaticServing)

# --- Colours -------------------------------------------------------------

PRIMARY = "#6247D4"  # violet: actions, selection, focus
PRIMARY_HOVER = "#5238BF"
PRIMARY_PRESSED = "#4A31AD"
PRIMARY_LIGHT = "#F3F0FD"  # tinted backgrounds (selected items, icon tiles)
PRIMARY_SOFT = "#E6E0FB"  # borders of tinted surfaces
SECONDARY = "#2B2640"  # aubergine ink: dark panels, tooltips, secondary emphasis
BACKGROUND = "#FBFAFD"
SURFACE = "#FFFFFF"
SURFACE_MUTED = "#F5F4F8"  # wells: segmented controls, table heads, empty bars
TEXT = "#1C1A27"
TEXT_SECONDARY = "#5F5B73"
TEXT_TERTIARY = "#6F6B83"
BORDER = "#ECEAF2"
BORDER_STRONG = "#DCD9E5"
SUCCESS = "#1B7A4B"
WARNING = "#9A5807"
ERROR = "#C9373C"

# The three counts of a station, each with its own hue.
COUNT_COLOURS = {"mechanical": PRIMARY, "ebike": "#2867AE", "docks": "#0E7A69"}

# Status of a chance on maps (graphics: 3:1 is enough, brighter than text).
MAP_GOOD = [46, 168, 110, 235]
MAP_FAIR = [232, 160, 54, 235]
MAP_POOR = [222, 76, 82, 235]
MAP_MUTED = [178, 174, 196, 120]
MAP_PRIMARY = [98, 71, 212, 255]
MAP_HERE = [43, 38, 64, 255]  # "you are here": secondary ink

# --- Type, space, shape, motion --------------------------------------------

FONT = '"Inter", -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif'

TOKENS: dict[str, str] = {
    # colours
    "color-primary": PRIMARY,
    "color-primary-hover": PRIMARY_HOVER,
    "color-primary-pressed": PRIMARY_PRESSED,
    "color-primary-light": PRIMARY_LIGHT,
    "color-primary-soft": PRIMARY_SOFT,
    "color-secondary": SECONDARY,
    "color-bg": BACKGROUND,
    "color-surface": SURFACE,
    "color-surface-muted": SURFACE_MUTED,
    "color-text": TEXT,
    "color-text-secondary": TEXT_SECONDARY,
    "color-text-tertiary": TEXT_TERTIARY,
    "color-border": BORDER,
    "color-border-strong": BORDER_STRONG,
    "color-success": SUCCESS,
    "color-success-light": "#EAF6EF",
    "color-warning": WARNING,
    "color-warning-light": "#FDF3E4",
    "color-error": ERROR,
    "color-error-light": "#FCEDED",
    "color-mechanical": COUNT_COLOURS["mechanical"],
    "color-ebike": COUNT_COLOURS["ebike"],
    "color-docks": COUNT_COLOURS["docks"],
    # typography
    "font": FONT,
    "text-display": "clamp(2.5rem, 1.6rem + 3.6vw, 4.25rem)",  # landing H1: 40 to 68 px
    "text-h1": "clamp(1.875rem, 1.5rem + 1.2vw, 2.375rem)",  # page titles: 30 to 38 px
    "text-h2": "clamp(1.625rem, 1.3rem + 1.1vw, 2.25rem)",  # section titles: 26 to 36 px
    "text-h3": "1.1875rem",  # 19 px
    "text-lead": "clamp(1.0625rem, 1rem + .3vw, 1.25rem)",  # 17 to 20 px
    "text-body": "1rem",  # 16 px
    "text-small": ".9375rem",  # 15 px
    "text-caption": ".8125rem",  # 13 px
    "text-label": ".8125rem",  # 13 px
    "text-figure": "1.75rem",  # key figures: 28 px
    # spacing scale (4 px base)
    "space-1": "4px",
    "space-2": "8px",
    "space-3": "12px",
    "space-4": "16px",
    "space-5": "24px",
    "space-6": "32px",
    "space-7": "48px",
    "space-8": "64px",
    "space-9": "96px",
    # layout
    "content": "1240px",
    "gutter": "clamp(20px, 4vw, 40px)",
    "header-h": "64px",
    "control-h": "44px",  # buttons, inputs, segmented controls: one height
    "control-h-sm": "36px",
    # shape
    "radius-sm": "8px",
    "radius-md": "12px",
    "radius-lg": "18px",
    "radius-xl": "28px",
    "radius-pill": "999px",
    "shadow-sm": "0 1px 2px rgba(28, 26, 39, .05)",
    "shadow-md": "0 1px 2px rgba(28, 26, 39, .04), 0 8px 24px rgba(28, 26, 39, .06)",
    "shadow-lg": "0 2px 6px rgba(28, 26, 39, .05), 0 24px 48px rgba(28, 26, 39, .10)",
    "ring": "0 0 0 3px rgba(98, 71, 212, .35)",
    # motion
    "ease": "cubic-bezier(.25, .1, .25, 1)",
    "ease-out": "cubic-bezier(.16, 1, .3, 1)",
    "fast": "150ms",
    "base": "240ms",
    "slow": "480ms",
}


def custom_properties() -> str:
    """The tokens as a ``:root`` rule of CSS custom properties (``--vf-<name>``)."""
    body = "".join(f"--vf-{name}:{value};" for name, value in TOKENS.items())
    return f":root{{{body}}}"


@lru_cache
def style_sheet() -> str:
    """The whole style of the app, in one ``<style>`` element for ``st.html``.

    The tokens come first, then the style sheet that uses them. The sheet is
    read once per server.
    """
    sheet = (STATIC / "css" / "veloflow.css").read_text(encoding="utf-8")
    return f"<style>{custom_properties()}\n{sheet}</style>"


def static_url(*parts: str) -> str:
    """URL of a file under ``static/`` (relative: works behind any base path)."""
    return "/".join((STATIC_URL, *parts))


def image_url(stem: str) -> str | None:
    """URL of the picture ``static/images/<stem>.*``, or ``None`` if not added yet.

    Lets a page ship before its picture exists: the caller draws a fallback.
    The first format found wins, in the order WebP, JPEG, PNG, SVG.
    """
    for suffix in (".webp", ".jpg", ".jpeg", ".png", ".svg"):
        if (STATIC / "images" / f"{stem}{suffix}").is_file():
            return static_url("images", f"{stem}{suffix}")
    return None
