"""Dark mode has to actually render, not merely set an attribute.

The theme is applied by one line of JS at startup reading `prefers-color-scheme`, so it is only
correct if the browser was told about the preference *before* the page loaded — which is why these
build their own context rather than calling emulate_media() afterwards.
"""

from __future__ import annotations

import pytest

from . import fixtures as fx

pytestmark = pytest.mark.e2e


def luminance(css_colour: str) -> float:
    """Rough relative luminance, 0 (black) to 1 (white), from a computed rgb()/rgba() string."""
    nums = [float(n) for n in css_colour.replace("rgba", "").replace("rgb", "")
            .strip("() ").split(",")[:3]]
    r, g, b = (n / 255 for n in nums)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@pytest.fixture
def themed_page(browser, editor):
    def _open(scheme: str):
        ctx = browser.new_context(color_scheme=scheme)
        page = ctx.new_page()
        page.goto(editor.url, wait_until="networkidle")
        page.select_option("#playlist", label=fx.NAMES[fx.CASE_SET_ID])
        page.wait_for_selector(".show-group")
        return page

    return _open


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_attribute_follows_the_os_preference(themed_page, scheme):
    page = themed_page(scheme)
    assert page.evaluate("() => document.documentElement.dataset.bsTheme") == scheme


@pytest.mark.parametrize(
    "scheme,want_dark_background", [("light", False), ("dark", True)]
)
def test_the_page_actually_renders_in_the_chosen_theme(themed_page, scheme, want_dark_background):
    """Asserts pixels, not the attribute: a stale Bootstrap build or a hard-coded colour in
    app.css would set data-bs-theme correctly and still paint a white page."""
    page = themed_page(scheme)
    bg = luminance(page.evaluate("() => getComputedStyle(document.body).backgroundColor"))
    fg = luminance(page.evaluate("() => getComputedStyle(document.body).color"))

    if want_dark_background:
        assert bg < 0.25, f"background luminance {bg:.2f} is not dark"
        assert fg > 0.5, f"text luminance {fg:.2f} is not light-on-dark"
    else:
        assert bg > 0.75, f"background luminance {bg:.2f} is not light"
        assert fg < 0.5, f"text luminance {fg:.2f} is not dark-on-light"
    assert abs(bg - fg) > 0.4, "text and background are too close to read"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_tree_and_controls_follow_the_theme_too(themed_page, scheme):
    """The body is easy; the parts we build are what could be left behind. A row, a group header
    and the destructive button all have to sit on the themed surface."""
    page = themed_page(scheme)
    body_bg = luminance(page.evaluate("() => getComputedStyle(document.body).backgroundColor"))

    for sel, what in [
        ("li.list-group-item", "episode row"),
        (".accordion-button", "group header"),
        ("#toolbar", "toolbar"),
    ]:
        text = luminance(page.locator(sel).first.evaluate("e => getComputedStyle(e).color"))
        assert abs(text - body_bg) > 0.4, f"{what} text is unreadable on the {scheme} background"

    # The destructive action must stay visibly destructive in both themes.
    remove_bg = page.locator("#remove").evaluate("e => getComputedStyle(e).backgroundColor")
    nums = [float(n) for n in remove_bg.replace("rgba", "").replace("rgb", "").strip("() ").split(",")[:3]]
    assert nums[0] > nums[1] + 40 and nums[0] > nums[2] + 40, f"Remove is not red: {remove_bg}"
