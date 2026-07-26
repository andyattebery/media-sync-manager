"""HTTP surface of the playlist editor, against an in-memory Jellyfin."""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

from fakes import FakeJellyfinClient  # noqa: E402

from media_sync_manager.errors import TransientError  # noqa: E402
from media_sync_manager.models import PlaylistEntry  # noqa: E402
from media_sync_manager.web import create_app  # noqa: E402


def ep(pid, name, *, sid="s1", series="Meadowlark", snum=1, enum=1):
    return PlaylistEntry(
        playlist_item_id=pid, item_id=pid, name=name, type="Episode",
        series_id=sid, series_name=series, season_number=snum, episode_number=enum,
    )


ENTRIES = {
    "p1": [ep("a", "One", enum=1), ep("b", "Two", enum=2),
           PlaylistEntry(playlist_item_id="", item_id="", name="Broken", type="Movie")],
    "p2": [],
}
NAMES = {"p1": "Animation", "p2": "Empty"}


@pytest.fixture
def fake():
    return FakeJellyfinClient(entries={k: list(v) for k, v in ENTRIES.items()}, names=NAMES)


@pytest.fixture
def client(fake):
    app = create_app(fake)
    app.config.update(TESTING=True)
    return app.test_client()


def test_list_playlists(client):
    body = client.get("/api/playlists").get_json()
    assert body["playlists"] == [{"id": "p1", "name": "Animation"}, {"id": "p2", "name": "Empty"}]
    # The page needs this to say which Jellyfin it is talking to and link back to it.
    assert body["server_url"] == "http://jf.test"


def test_items_shape(client):
    body = client.get("/api/playlists/p1/items").get_json()
    assert body["total"] == 3
    groups = body["groups"]
    assert [g["title"] for g in groups] == ["Meadowlark", "Movies"]
    item = groups[0]["seasons"][0]["items"][0]
    assert item["entry_id"] == "a" and item["removable"] is True
    assert item["duplicate"] is False
    # `entry_id`, never `id`: the JS must not be able to confuse it with the media item id.
    assert "id" not in item


def test_unaddressable_entry_marked_not_removable(client):
    body = client.get("/api/playlists/p1/items").get_json()
    movies = [g for g in body["groups"] if g["key"] == "type:Movie"][0]
    assert movies["seasons"][0]["items"][0]["removable"] is False


def test_duplicate_flag():
    fake = FakeJellyfinClient(entries={"p1": [ep("a", "One"), ep("a", "One")]}, names={"p1": "x"})
    body = create_app(fake).test_client().get("/api/playlists/p1/items").get_json()
    items = body["groups"][0]["seasons"][0]["items"]
    assert all(i["duplicate"] for i in items)


def test_api_responses_are_not_cacheable(client):
    """Without no-store a browser or proxy may serve the post-removal refetch from cache, making
    the count delta report a stale number — and that delta is the only proof a removal happened."""
    assert client.get("/api/playlists").headers["Cache-Control"] == "no-store"


def test_remove_forwards_exactly_the_posted_ids(client, fake):
    resp = client.post("/api/playlists/p1/remove", json={"entry_ids": ["a", "b"]})
    assert resp.status_code == 200
    assert fake.removals == [("p1", ["a", "b"])]
    assert resp.get_json() == {"requested": 2, "removed": 2, "failed": 0, "errors": []}


def test_remove_actually_removes_so_a_refetch_shrinks(client):
    """Guards the fake itself: a record-only fake would make every removal test report
    'the list didn't shrink' — a green suite over a broken app."""
    before = client.get("/api/playlists/p1/items").get_json()["total"]
    client.post("/api/playlists/p1/remove", json={"entry_ids": ["a"]})
    after = client.get("/api/playlists/p1/items").get_json()["total"]
    assert (before, after) == (3, 2)


def test_partial_failure_is_207():
    fake = FakeJellyfinClient(entries={"p1": [ep("a", "1"), ep("b", "2")]}, fail_after=1)
    resp = create_app(fake).test_client().post(
        "/api/playlists/p1/remove", json={"entry_ids": ["a", "b"]}
    )
    assert resp.status_code == 207
    body = resp.get_json()
    assert (body["removed"], body["failed"]) == (1, 1)
    assert body["errors"]


def test_total_failure_is_502():
    fake = FakeJellyfinClient(
        entries={"p1": [ep("a", "1")]}, remove_error=TransientError("jellyfin down")
    )
    resp = create_app(fake).test_client().post(
        "/api/playlists/p1/remove", json={"entry_ids": ["a"]}
    )
    assert resp.status_code == 502


@pytest.mark.parametrize(
    "payload",
    [[], {"entry_ids": []}, {"entry_ids": "a"}, {"entry_ids": [1]}, {"entry_ids": [""]}, {}],
)
def test_malformed_bodies_are_400(client, payload):
    assert client.post("/api/playlists/p1/remove", json=payload).status_code == 400


def test_non_json_content_type_is_415(client):
    resp = client.post("/api/playlists/p1/remove", data="entry_ids=a")
    assert resp.status_code == 415


def test_jellyfin_failure_becomes_502_json():
    class Broken:
        base_url = "http://jf.test"

        def list_playlists(self):
            raise TransientError("jellyfin GET /x failed: boom")

    resp = create_app(Broken()).test_client().get("/api/playlists")
    assert resp.status_code == 502
    assert "boom" in resp.get_json()["error"]


@pytest.mark.parametrize(
    "path", ["/", "/bootstrap.min.css", "/bootstrap.bundle.min.js", "/app.css", "/app.js"]
)
def test_static_assets_are_served(client, path):
    """Guards the package-data bug: without it the installed image serves /api/* while every
    stylesheet 404s, rendering an unstyled page rather than an obvious error."""
    assert client.get(path).status_code == 200


def test_items_route_surfaces_a_jellyfin_failure_as_502():
    """Only the playlists route had this covered; the items route is the one you hit constantly."""
    class Broken:
        base_url = "http://jf.test"

        def playlist_entries(self, pid):
            raise TransientError("jellyfin GET /Playlists/x/Items failed: boom")

    resp = create_app(Broken()).test_client().get("/api/playlists/p1/items")
    assert resp.status_code == 502
    assert "boom" in resp.get_json()["error"]


def test_remove_forwards_ids_verbatim(client, fake):
    """The route passes entry_ids straight through — it does not dedupe, and must not.

    This asserted post-dedupe ids until the fake was changed to record what it actually received.
    That made it a test of the double: the real client dedupes at jellyfin.py, is never called on
    this path, and could have been broken without turning this red. Deduping is the client's job and
    is covered by test_remove_dedupes_and_drops_empty_ids.
    """
    resp = client.post("/api/playlists/p1/remove", json={"entry_ids": ["a", "a", "b"]})
    assert resp.status_code == 200
    assert fake.removals == [("p1", ["a", "a", "b"])]


def test_unknown_playlist_returns_an_empty_list_not_an_error():
    """Jellyfin answers 200 with no Items for an id that is gone, so the page shows 'empty'
    rather than an error it cannot act on."""
    fake = FakeJellyfinClient(entries={"p1": []}, names={"p1": "x"})
    body = create_app(fake).test_client().get("/api/playlists/nope/items").get_json()
    assert body["total"] == 0 and body["groups"] == []


# --- structural locks for claims that are true by construction ----------------


def test_web_layer_cannot_touch_the_filesystem_or_trigger_a_sync():
    """docs/playlist-editor.md claims the editor deletes nothing on disk and never triggers a sync.
    Both are true only because web.py has no edge to those modules — assert the edge, not the
    behaviour, because a behavioural test would have to prove a negative."""
    import ast
    from pathlib import Path as _Path

    src = _Path(__file__).resolve().parent.parent / "media_sync_manager" / "web.py"
    tree = ast.parse(src.read_text())
    names = set()
    for node in ast.walk(tree):          # walk, so a lazy import inside a function counts too
        if isinstance(node, ast.Import):
            names |= {a.name.rsplit(".", 1)[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.name for a in node.names}
            if node.module:
                names.add(node.module.rsplit(".", 1)[-1])

    forbidden = {"fsops", "paths", "reconcile", "sync", "poller", "tdarr"}
    assert not (names & forbidden), f"web.py reached into {names & forbidden}"


def test_the_only_mutating_route_is_remove(client):
    """Locks every non-goal at once: no add, reorder, rename, create or delete playlist.

    Asserts on mutations rather than route count — static_url_path="" adds Flask's catch-all
    GET /<path:filename>, so 'exactly four endpoints' would be false on day one.
    """
    app = client.application
    mutating = sorted(
        (rule.rule, sorted(rule.methods - {"GET", "HEAD", "OPTIONS"}))
        for rule in app.url_map.iter_rules()
        if rule.methods - {"GET", "HEAD", "OPTIONS"}
    )
    assert mutating == [("/api/playlists/<playlist_id>/remove", ["POST"])]
