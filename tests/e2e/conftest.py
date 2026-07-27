"""Serve the real Flask app against an in-memory Jellyfin, for the browser tests.

No Jellyfin, no network, no npm — but a real DOM, which is the only thing that can see the bug
class these tests exist for (checkboxes not updating, indeterminate never set).
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("playwright")

from werkzeug.serving import make_server  # noqa: E402

from fakes import FakeJellyfinClient  # noqa: E402
from media_sync_manager.web import create_app  # noqa: E402

from . import fixtures  # noqa: E402


class Editor:
    """A running server plus the fake behind it, so tests can assert on both sides."""

    def __init__(self, fake, url):
        self.fake = fake
        self.url = url


@pytest.fixture
def make_editor():
    servers = []

    def _make(**fake_kwargs) -> Editor:
        fake = FakeJellyfinClient(
            entries={k: list(v) for k, v in fixtures.PLAYLISTS.items()},
            names=fixtures.NAMES,
            **fake_kwargs,
        )
        server = make_server("127.0.0.1", 0, create_app(fake), threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Editor(fake, f"http://127.0.0.1:{server.server_port}")

    yield _make
    for s in servers:
        s.shutdown()


@pytest.fixture
def editor(make_editor) -> Editor:
    return make_editor()


@pytest.fixture
def open_playlist(page):
    """Load the editor and select a playlist, waiting until the tree is painted."""

    def _open(editor: Editor, name: str = fixtures.NAMES[fixtures.CASE_SET_ID]):
        page.goto(editor.url, wait_until="networkidle")
        page.select_option("#playlist", label=name)
        # Wait for the tree, not for `#toolbar.hidden`. The toolbar carries no `hidden` attribute —
        # app.js toggles .d-none/.d-flex, because Bootstrap's display utilities are !important and
        # beat [hidden] — so `.hidden` is permanently false and that condition was already true at
        # first paint, before a playlist was chosen. Measured: it returned instantly with the
        # toolbar invisible, zero .show-group elements and #status still reading "Choose a playlist
        # to begin." The tests survived on Playwright's auto-waiting; the ones that read text
        # synchronously were racing a local fetch. This is the trap docs/development.md §7 names.
        page.wait_for_selector(".show-group, .empty")
        return page

    return _open
