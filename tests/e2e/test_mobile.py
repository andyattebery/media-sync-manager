"""Mobile, on a real device profile driven by real touch events.

Not `set_viewport_size` — that is a narrow desktop window with no touch, no mobile user-agent and no
device pixel ratio, which is how a tap-target regression went unnoticed: the Pico build had explicit
44px rows and grown checkbox targets, the Bootstrap rewrite deleted them, and the single existing
mobile test only checked horizontal scroll.

Pixel 5 rather than iPhone 13: the latter's `default_browser_type` is webkit, which this suite does
not install. iPhone SE covers the narrow extreme (320px) where truncation bites first.
"""

from __future__ import annotations

import pytest

from . import fixtures as fx

pytestmark = pytest.mark.e2e

DEVICES = ["Pixel 5", "iPhone SE"]

# The label is the tap target (it carries `for`), not the row. Bootstrap's spacing scale has no step
# at 44: the label's 24px content plus py-2 lands on 40, plus py-3 on 56. 40 is taken deliberately —
# 56px rows would add ~780px of scrolling to a 52-episode season to buy 16px of target.
MIN_TARGET_PX = 40


@pytest.fixture(params=DEVICES)
def mobile(request, browser, editor):
    """A page on a real device profile, with a playlist already open."""
    from playwright.sync_api import sync_playwright  # noqa: F401  (devices come off the fixture)

    device = dict(request.getfixturevalue("playwright").devices[request.param])
    device.pop("default_browser_type", None)         # not a valid context argument
    ctx = browser.new_context(**device)
    page = ctx.new_page()
    page.goto(editor.url, wait_until="networkidle")
    page.select_option("#playlist", label=fx.NAMES[fx.CASE_SET_ID])
    page.wait_for_selector(".show-group")
    page.device_name = request.param
    yield page
    ctx.close()


def box(page, selector):
    return page.locator(selector).first.bounding_box()


def test_tap_targets_are_big_enough(mobile):
    """Measures the label and the button — NOT the row. A row-height assertion passes at 56px while
    the thing you actually press sits at 39, which is exactly the mistake this fix corrects."""
    label = box(mobile, "li.list-group-item label.text-truncate")
    button = box(mobile, 'button[data-role="upto"]')
    assert label["height"] >= MIN_TARGET_PX, f"{mobile.device_name}: label {label['height']}px"
    assert button["height"] >= MIN_TARGET_PX, f"{mobile.device_name}: button {button['height']}px"


def test_no_horizontal_scroll(mobile):
    assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_no_horizontal_scroll_in_landscape(mobile):
    size = mobile.viewport_size
    mobile.set_viewport_size({"width": size["height"], "height": size["width"]})
    mobile.wait_for_timeout(150)
    assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_tapping_the_episode_title_selects_the_row(mobile):
    """The `for`/label association is the whole reason the target is 40px rather than the 16px
    checkbox; if it broke, the big target would select nothing."""
    mobile.locator("li.list-group-item label.text-truncate").first.tap()
    assert mobile.locator("#counts").inner_text().startswith("1 of ")


def test_checkbox_and_range_button_do_not_crowd_each_other(mobile):
    """Two controls with opposite effects on a delete tool; a mis-tap should not be easy."""
    cb = box(mobile, 'li.list-group-item input[data-role="item"]')
    btn = box(mobile, 'button[data-role="upto"]')
    gap = btn["x"] - (cb["x"] + cb["width"])
    assert gap >= 8, f"{mobile.device_name}: only {gap:.0f}px between checkbox and range button"
    assert btn["x"] > cb["x"] + cb["width"], "controls overlap"


def test_toolbar_stays_reachable_when_scrolled(mobile):
    """sticky-top: on a 154-episode playlist the Remove button must not be a scroll away."""
    mobile.evaluate("window.scrollTo({top: 1500, behavior: 'instant'})")
    mobile.wait_for_timeout(150)
    assert mobile.locator("#remove").is_visible()
    assert box(mobile, "#toolbar")["y"] <= 1, "toolbar did not stick to the top"


def test_header_and_provenance_do_not_clip(mobile):
    """320px is where the long real playlist names and the explanation line get tight."""
    for sel in ("#server-link", "#playlist", "p.border-start"):
        el = mobile.locator(sel)
        assert el.is_visible(), f"{sel} not visible on {mobile.device_name}"
        assert el.bounding_box()["x"] >= 0
    assert mobile.evaluate(
        "() => { const e = document.querySelector('p.border-start');"
        "        return e.scrollHeight <= e.clientHeight + 1; }"
    ), "the provenance line is clipped rather than wrapped"


def test_full_removal_flow_by_touch(mobile, editor):
    """The actual thing you would do on a phone, never once exercised with touch events.

    Uses the 120-item playlist because that is the shape of the real one: over SMALL_PLAYLIST, so
    seasons start collapsed and the flow genuinely begins with an expand. On the small case-set
    fixture the seasons are already open and a tap would *collapse* them instead.
    """
    mobile.on("dialog", lambda d: d.accept())
    mobile.select_option("#playlist", label=fx.NAMES[fx.BULK_ID])
    mobile.wait_for_selector(".season-group")

    season = mobile.locator(".season-group").first
    assert season.locator("label.text-truncate").first.is_hidden(), "expected a collapsed season"

    season.locator(".accordion-button").tap()
    mobile.wait_for_selector(".season-group label.text-truncate", state="visible")

    mobile.locator(".season-group label.text-truncate").first.tap()
    assert mobile.locator("#counts").inner_text().startswith("1 of ")

    mobile.locator("#remove").tap()
    mobile.wait_for_function(
        "() => /^Removed /.test(document.querySelector('#status').textContent)"
    )
    assert len(editor.fake.removals) == 1
    assert mobile.locator("#counts").inner_text().startswith("0 of ")


def test_desktop_density_is_unchanged(page, editor):
    """The mobile fix must not bloat the desktop list — the -md- utilities exist for exactly this."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(editor.url, wait_until="networkidle")
    page.select_option("#playlist", label=fx.NAMES[fx.CASE_SET_ID])
    page.wait_for_selector(".show-group")
    row = page.locator("li.list-group-item").first.bounding_box()
    assert row["height"] <= 36, f"desktop rows grew to {row['height']}px"
