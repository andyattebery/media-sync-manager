![ci](https://github.com/andyattebery/media-sync-manager/actions/workflows/ci.yaml/badge.svg)

# media-sync-manager

A Plex-Sync equivalent for Jellyfin. Curate Jellyfin **playlists** (e.g. "2D Animation" and
"Standard"), and this glue keeps a per-device folder in sync with space-efficient **transcoded**
copies — produced by your existing **Tdarr** — for offline/travel viewing.

```
Jellyfin playlist  ->  media-sync-manager  ->  Tdarr (transcode)  ->  sync/ folder  ->  your player
```

The glue is small and state-free: it keeps each Tdarr library's **input folder mirrored to its
playlist** (link in what's listed, remove what isn't) and lets Tdarr transcode and track what's done.
When an item leaves a playlist it deletes that item's input **and** its transcoded output. It
**never modifies or deletes originals** and keeps **no database** — the filesystem is the source of
truth.

## How it works

- **The playlist decides the flow.** Which playlist an item is in picks its **segment** (e.g.
  `animation` = aggressive/smaller, `standard` = higher-quality) — no genre guessing.
- **Dirs are derived from one `transcode_root`.** For a target `T` and segment `S`:
  input = `<transcode_root>/<T>/<S>`, output = `<transcode_root>/<T>/sync`.
- **Tdarr owns transcode tracking.** The glue just feeds inputs; Tdarr won't redo a done file. The
  only output-side job is deleting `sync/` files no longer wanted (matched segment-aware, so moving
  an item between playlists retires the old encode automatically).
- **Pickup** is short-interval polling — Jellyfin emits no playlist events.
- **Hardlink or symlink is detected, not configured**, because the right answer depends on union
  filesystem behaviour no operator should have to reason about. Run it where the media filesystem is
  **local**: over CIFS/SMB neither primitive can be created.

## Quick start

```sh
export JELLYFIN_API_KEY=... TDARR_USER=... TDARR_PASS=...
docker compose run --rm media-sync-manager doctor        # validate before touching anything
docker compose run --rm media-sync-manager sync --dry-run
docker compose up -d                                     # poller + playlist editor
```

Full setup, configuration reference and troubleshooting: **[the user guide](docs/user-guide.md)**.

## Playlist editor

Jellyfin's own web UI has no fast way to remove many items from a playlist. `docker compose up -d`
also serves a small page at `http://<host>:8087` that lists a playlist grouped show → season →
episode, with bulk selection — tick a season or a show, or use **Select first N** to take everything
above a row.

**It has no authentication.** Keep it on your LAN and never port-forward it.

## Documentation

| Doc | For |
|---|---|
| [User guide](docs/user-guide.md) | Installing, configuring, running, reading `doctor`, troubleshooting |
| [Sync spec](docs/media-sync-manager-spec.md) | The design: reconciliation algorithm, edge cases, path remapping |
| [Playlist editor design](docs/playlist-editor.md) | Jellyfin API constraints the editor works around, and why its choices are what they are |
| [Development guide](docs/development.md) | Running it against fixtures with no credentials, the test layers, mutation testing |

```sh
pip install -e ".[web,test]"
pytest                       # 156 tests, no browser

# browser tests for the editor UI (opt-in: ~150MB of browser binaries)
pip install -e ".[e2e]"
playwright install chromium
pytest -m e2e                # 76 browser tests
```
