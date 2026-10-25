"""The HTML report's colour tokens meet WCAG 2.1 AA text contrast in both themes (issue #110)."""

import re
from importlib.resources import files

import pytest

AA_TEXT = 4.5
TEXT_TOKENS = ("text", "muted", "accent", "bad", "good", "warn")
BACKGROUND_TOKENS = ("bg", "panel")


def _themes() -> dict[str, dict[str, str]]:
    template = (files("hecklr_report") / "templates" / "report.html.j2").read_text("utf-8")
    blocks = re.findall(r":root\s*\{([^}]*)\}", template)
    assert len(blocks) == 2, "expected a light and a dark :root block"
    return {
        theme: dict(re.findall(r"--([a-z]+):\s*(#[0-9a-fA-F]{6})", block))
        for theme, block in zip(("light", "dark"), blocks, strict=True)
    }


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(foreground: str, background: str) -> float:
    """The WCAG 2.1 contrast ratio of two sRGB colours."""
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_matches_the_wcag_reference_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("foreground", TEXT_TOKENS)
@pytest.mark.parametrize("background", BACKGROUND_TOKENS)
def test_issue110_report_text_tokens_meet_wcag_aa(
    theme: str, foreground: str, background: str
) -> None:
    tokens = _themes()[theme]

    ratio = contrast(tokens[foreground], tokens[background])

    assert ratio >= AA_TEXT, f"{theme} --{foreground} on --{background}: {ratio:.2f}:1"
