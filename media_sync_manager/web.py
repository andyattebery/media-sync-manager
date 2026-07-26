"""Flask app for the playlist editor. The only module that imports flask.

Kept out of the import path of run/sync/status/doctor so the core CLI keeps its two-dependency
footprint (requests, pyyaml) — cli.py imports this lazily, inside cmd_web.
"""

from __future__ import annotations

from flask import Flask, jsonify, request, send_from_directory

from . import log, playlists
from .errors import TransientError
from .models import PlaylistEntry, SeasonGroup, ShowGroup

_log = log.get("web")


def _entry_json(entry: PlaylistEntry, *, duplicate: bool) -> dict:
    # `entry_id`, not `id`: it is named for its purpose so the JS never sees a field it could
    # confuse with the media item's id (which would delete the wrong thing, or nothing).
    return {
        "entry_id": entry.playlist_item_id,
        "item_id": entry.item_id,
        "name": entry.name,
        "type": entry.type,
        "season_number": entry.season_number,
        "episode_number": entry.episode_number,
        "removable": entry.removable,
        "duplicate": duplicate,
    }


def _season_json(season: SeasonGroup, dupes: set[str]) -> dict:
    return {
        "key": season.key,
        "title": season.title,
        "number": season.number,
        "count": season.count,
        "items": [
            _entry_json(e, duplicate=e.playlist_item_id in dupes) for e in season.entries
        ],
    }


def _group_json(group: ShowGroup, dupes: set[str]) -> dict:
    return {
        "key": group.key,
        "title": group.title,
        "kind": group.kind,
        "count": group.count,
        "seasons": [_season_json(s, dupes) for s in group.seasons],
    }


def create_app(jellyfin) -> Flask:
    """Build the app around an already-constructed Jellyfin client.

    Takes no Config: every route below reads only the client, host/port are argparse (cli.py), and
    requiring one would force each test to build a Config (and its tmp media dirs) for a parameter
    that is never read. Tests inject FakeJellyfinClient here.
    """
    # static_url_path="" serves the assets at the root, matching index.html's relative links.
    # Flask's default ("/static") would 404 every stylesheet while GET / still returned 200 —
    # an unstyled page rather than an obvious error.
    app = Flask(__name__, static_folder="static", static_url_path="")

    @app.errorhandler(TransientError)
    def _jellyfin_down(exc: TransientError):
        return jsonify({"error": str(exc)}), 502

    @app.after_request
    def _no_store(resp):
        # Without this a browser or proxy may serve the post-removal refetch from cache, which
        # would make the count delta report a stale number — and that delta is the only evidence
        # we have that a removal actually happened (Jellyfin returns 204 either way).
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/playlists")
    def api_playlists():
        # server_url lets the page say which Jellyfin it is talking to and link back to it. A
        # per-playlist deep link is deliberately not offered: it would send you into Jellyfin's own
        # playlist page — the interface this tool exists because it is unusable — to answer a
        # question the post-removal count delta already answers.
        return jsonify(
            {
                "server_url": jellyfin.base_url,
                "playlists": [{"id": p.id, "name": p.name} for p in jellyfin.list_playlists()],
            }
        )

    @app.get("/api/playlists/<playlist_id>/items")
    def api_items(playlist_id: str):
        entries = jellyfin.playlist_entries(playlist_id)
        dupes = playlists.duplicate_ids(entries)
        groups = playlists.group_entries(entries)
        return jsonify(
            {
                "playlist_id": playlist_id,
                "total": len(entries),
                "groups": [_group_json(g, dupes) for g in groups],
            }
        )

    @app.post("/api/playlists/<playlist_id>/remove")
    def api_remove(playlist_id: str):
        # Requiring JSON is de-facto CSRF protection with no auth and no CORS headers: a
        # cross-origin <form> cannot set this content type, and a cross-origin fetch that does
        # gets preflighted and blocked. Answer 415 rather than folding it into the 400 below, so
        # "you sent the wrong media type" stays distinguishable from "your JSON was malformed".
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 415
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body must be a JSON object"}), 400
        ids = body.get("entry_ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "entry_ids must be a non-empty list"}), 400
        if not all(isinstance(i, str) and i for i in ids):
            return jsonify({"error": "entry_ids must be non-empty strings"}), 400

        result = jellyfin.remove_playlist_entries(playlist_id, ids)
        payload = {
            "requested": result.requested,
            "removed": result.removed,
            "failed": result.failed,
            "errors": list(result.errors),
        }
        if result.failed == 0:
            status = 200
        elif result.removed > 0:
            status = 207  # partial: some chunks landed, some did not
        else:
            status = 502
        return jsonify(payload), status

    return app
