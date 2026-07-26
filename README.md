![ci](https://github.com/andyattebery/media-sync-manager/actions/workflows/ci.yaml/badge.svg)

# media-sync-manager

A Plex-Sync equivalent for Jellyfin. Curate Jellyfin **playlists** (e.g. "2D Animation" and
"Standard"), and this glue keeps a per-device SMB folder in sync with space-efficient **transcoded**
copies — produced by your existing **Tdarr** — for offline/travel viewing in Infuse.

```
Jellyfin playlist  ->  media-sync-manager  ->  Tdarr (transcode)  ->  sync/ folder  ->  Infuse
```

The glue is small and state-free: it keeps each Tdarr library's **input folder mirrored to its
playlist** (link in what's listed, remove what isn't) and lets Tdarr transcode and track what's
done. When an item leaves a playlist it deletes that item's input **and** its transcoded output. It
**never modifies or deletes originals** and keeps **no database** — the filesystem is the source of
truth. See [the spec](docs/media-sync-manager-spec.md) for the full design.

## How it works

- **The playlist decides the flow.** Which playlist an item is in picks its **segment** (e.g.
  `animation` = aggressive/smaller, `standard` = higher-quality) — no genre guessing.
- **Dirs are derived from one `transcode_root`.** For a target `T` and segment `S`:
  input = `<transcode_root>/<T>/<S>`, output = `<transcode_root>/<T>/sync`.
- **Tdarr owns transcode tracking.** The glue just feeds inputs; Tdarr won't redo a done file. The
  only output-side job is deleting `sync/` files no longer wanted (matched segment-aware, so moving
  an item between playlists retires the old encode automatically).
- **Pickup** is short-interval polling (Jellyfin emits no playlist events), plus `sync --once` for
  last-minute trips.

## Tdarr setup (you own this)

Recommended: **one Tdarr library per device**. The library scans the segment input folders
(`<transcode_root>/<T>/animation`, `.../standard`) and its flow branches encode settings on the
`/<segment>/` path component (`Check File Name Includes`, "include file directory"), writing output
with **Keep Relative Path** to `<transcode_root>/<T>/sync`.

Requirements the glue depends on (checked/reminded by `doctor`):

- **The flow keeps its input** after transcode — the input folders are the glue's playlist mirror.
- **Enable Folder Watch on each library** (Library settings → Folder Watch). It is also what notices
  a deleted input and retires the file, so removals depend on it; the glue's scan-files call only
  makes pickup of *new* inputs immediate.
- The library **must not process `<transcode_root>/<T>/sync`** (point it at the segment folders, or
  filter out `/sync/`).
- **Keep `transcode_root` under `media_root`** and bind-mount the shared storage once (e.g.
  `/mnt/storage:/media`, same as Jellyfin/Tdarr). `doctor` verifies it.

If one library can't watch both segment folders on your Tdarr version, use one library per segment
via the optional per-playlist `library_id`.

## Where to run it

Run the container on the host where the media filesystem is **local**, not mounted over CIFS/SMB.
The glue has to create a file in the input folder pointing at each original, and over SMB it can do
neither: the protocol has no symlink-create operation (`ln -s` returns `EIO`), and `mfsymlinks` only
produces a marker file the server won't resolve, so Tdarr would be handed the marker instead of media.

Which primitive it uses is **detected, not configured** — `doctor` reports it:

- **hardlink** where it works: cheapest, and the input is genuinely the same bytes.
- **symlink** where it doesn't. Notably a **mergerfs** pool can only hardlink within one underlying
  disk, so `os.link` fails `EXDEV` even though `media_root` and `transcode_root` are one mount and
  report one `st_dev`. Symlinks have no such constraint — they store a path, not an inode reference.
  When the media reaches Tdarr over a share, the server resolves the link during path lookup and
  Tdarr sees an ordinary file, so nothing downstream can tell the difference.

## Quick start (Docker)

1. Copy `config.example.yaml` to `/etc/media-sync-manager/config.yaml` and edit it.
2. Set secrets in the environment (`JELLYFIN_API_KEY`, `TDARR_USER`, `TDARR_PASS`).
3. Validate, dry-run, then run:

```sh
docker compose run --rm media-sync-manager doctor
docker compose run --rm media-sync-manager sync --once --dry-run
docker compose up -d        # starts the poller (`run`)
```

## Commands

- `media-sync-manager run` — poller daemon (the container default).
- `media-sync-manager sync --once [--dry-run]` — one reconcile pass; `--dry-run` changes nothing.
- `media-sync-manager status` — planned actions per target (read-only).
- `media-sync-manager doctor` — validate config, connectivity, library IDs, and probe how inputs can
  be created. Writes and removes temp files under `transcode_root`.

## Development

```sh
pip install -e ".[test]"
pytest tests/ -v
```
