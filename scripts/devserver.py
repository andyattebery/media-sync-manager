"""Serve the playlist editor against the e2e fixture playlists — no Jellyfin, no API key, no network.

    python scripts/devserver.py                 # http://127.0.0.1:8099
    python scripts/devserver.py --port 9000

The dataset is `tests/e2e/fixtures.py`, deliberately reused rather than reinvented: what you click
through and what the browser tests assert on cannot drift apart. It is also richer than the real
playlist — duplicates, an unaddressable entry, a series-less episode with a real season, an S??E05
row, movies, a markup-laden title, a 120-item playlist that crosses the removal chunk boundary, and
a 21-group one that is the only place the >15-group collapse is visible.

Removals mutate the in-memory fixture, so the tree really does shrink; restart to reset.

Binds 127.0.0.1, unlike the real `web` command, which defaults to 0.0.0.0 because it is meant to run
in a container. This has no reason to leave the machine.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# `tests/` has no __init__.py, so `import tests.e2e.fixtures` fails; `tests/e2e/` does have one.
# Importing the package does NOT pull in Playwright — the importorskip lives in e2e/conftest.py,
# which a plain import never loads — so this runs with only the [web] extra installed.
sys.path.insert(0, str(ROOT / "tests"))

from e2e import fixtures  # noqa: E402
from fakes import FakeJellyfinClient  # noqa: E402

from media_sync_manager.web import create_app  # noqa: E402


def build_app():
    fake = FakeJellyfinClient(
        entries={k: list(v) for k, v in fixtures.PLAYLISTS.items()},
        names=fixtures.NAMES,
    )
    return create_app(fake), fake


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    app, fake = build_app()
    print(f"playlist editor (fixture data) on http://{args.host}:{args.port}")
    for pid, entries in fixtures.PLAYLISTS.items():
        print(f"  {fixtures.NAMES.get(pid, pid):18} {len(entries):>3} entries")
    print("no Jellyfin, no credentials; removals are in-memory, restart to reset")
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
