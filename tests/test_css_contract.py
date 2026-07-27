"""The CSS contract, enforced instead of exhorted.

With a framework the failure mode is not duplicated declarations — Bootstrap supplies the baseline —
it is specificity war with vendor rules, then !important when that stops working. These assertions
are binary and run in CI, so none of it depends on anyone remembering to check.

Comments are stripped first: a rule that merely *mentions* !important is documentation, not a
violation, and greps that cannot tell the difference measure prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "media_sync_manager" / "static"
APP_CSS = STATIC / "app.css"
INDEX = STATIC / "index.html"

# sha256 of the vendored bootstrap@5.3.8 dist files, recorded so an edit to a vendored file (rather
# than an override in app.css) shows up as a failing test instead of silently working.
VENDORED = {
    "bootstrap.min.css": "d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede",
    "bootstrap.bundle.min.js": "e4fd49181388c48ec5040bd3fe66f57c29c8e67fcd8502b3354b96ec7ab47cc7",
}


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.fixture(scope="module")
def css() -> str:
    return _strip_comments(APP_CSS.read_text())


def test_app_css_stays_thin(css: str):
    """Bootstrap ships the controls. Past ~15 declaration lines we are rebuilding them by hand,
    which is exactly what produced the previous UI."""
    lines = [ln for ln in css.splitlines() if ln.strip()]
    assert len(lines) <= 15, f"app.css has {len(lines)} rule lines; use a Bootstrap utility instead"


def test_no_important(css: str):
    """The highest-signal check with a framework: !important is what losing an override fight
    looks like. If a rule needs it, the selector above it is wrong."""
    assert "!important" not in css


def test_colours_come_from_framework_tokens(css: str):
    """Literal colours mean we stopped theming through Bootstrap and started fighting it."""
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert not literals, f"use var(--bs-*) instead of {literals}"


def test_app_css_never_sets_a_root_font_size(css: str):
    """The previous framework scaled the root font with viewport width (125% at 1280px), which made
    its 1.25em checkbox render at 25px on a desktop — the reported bug. A guard forbidding overrides
    of that variable is what locked the scaling in. Bootstrap leaves the root at the browser's 16px,
    so the correct rule is simply: app.css sets no root font-size, in either direction."""
    for block in re.findall(r"(?:^|\})\s*(?::root|html)\s*\{([^}]*)\}", css):
        assert "font-size" not in block, "do not set a root font-size; 1em controls depend on it"


def test_no_hand_rolled_breakpoints(css: str):
    """Bootstrap's responsive utilities replace the hand-written media query that used to live
    here. A new one is a signal we started laying things out by hand again."""
    assert not re.findall(r"@media[^{]*(?:max|min)-width", css)


def test_hover_is_guarded(css: str):
    """Unguarded :hover sticks after a tap on iOS and reads as selection state."""
    for match in re.finditer(r":hover", css):
        before = css[: match.start()]
        assert "hover: hover" in before[before.rfind("@media") :], (
            ":hover must sit inside @media (hover: hover)"
        )


def test_stylesheet_order_is_framework_then_app():
    """Order is the entire override mechanism."""
    html = INDEX.read_text()
    assert html.index("bootstrap.min.css") < html.index("app.css")


def test_viewport_meta_present():
    """Everything responsive is void without it."""
    assert "width=device-width" in INDEX.read_text()


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_vendored_files_are_unmodified(name):
    import hashlib

    digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
    assert digest == VENDORED[name], f"{name} was edited; override in app.css instead"
