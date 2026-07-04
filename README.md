![ci](https://github.com/andyattebery/media-sync-manager/actions/workflows/ci.yaml/badge.svg)

# media-sync-manager

A Plex-Sync equivalent for Jellyfin. Curate Jellyfin **playlists** (e.g. "2D Animation" and
"Standard"), and this glue keeps a per-device SMB folder in sync with space-efficient **transcoded**
copies — produced by your existing **Tdarr** — for offline/travel viewing in Infuse.

```
Jellyfin playlist  ->  media-sync-manager  ->  Tdarr (transcode)  ->  per-device SMB folder  ->  Infuse
```

The glue is small and state-free: it reads playlists, routes each item to its transcode flow by
which playlist it's in, hardlinks originals into the right Tdarr input folder, and removes outputs
whose playlist entry was removed. It **never modifies or deletes originals** and keeps **no
database** — the filesystem is the source of truth. See
[the spec](docs/media-sync-manager-spec.md) for the full design.

## How it works

- **The playlist decides the flow.** Each config *target* maps one playlist to a **segment** (e.g.
  `animation` = aggressive/smaller, `standard` = higher-quality) — no genre guessing. The segment
  rides in the input path (`<input_dir>/<segment>/...`), the only per-file channel Tdarr exposes.
- **In-flight** = the input hardlink exists. **Done** = the output exists in the device folder
  (matched segment-aware, so moving an item between playlists re-encodes it). No completion polling,
  no markers.
- **Pickup** is short-interval polling (Jellyfin emits no playlist events), plus `sync --once` for
  last-minute trips.
- Targets that share an `output_dir` are one device; the glue reconciles them together so neither
  deletes the other's files.

## Tdarr setup (you own this)

Recommended topology: **one Tdarr library per device, all sharing one flow**. Each library:

- watches that device's `input_dir`,
- sets a library variable `output_dir` = the device's SMB folder,
- runs the shared flow, which branches encode settings on the `/<segment>/` path component
  (`Check File Name Includes`, "include file directory") and writes output with **Keep Relative
  Path** to `{{{args.userVariables.library.output_dir}}}`.

The glue only needs, per target, an `input_dir` to hardlink into and a `library_id` to scan — so
N×2-libraries or a single-library topology work too without code changes. Because matching is
segment-aware, the flow must keep the relative path (output lands at
`output_dir/<segment>/<source_rel>`).

**Hardlink requirement:** the media and every `input_dir` must be on **one shared filesystem**. In
Docker, mount their common parent as a single volume (not two). `doctor` verifies this.

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
- `media-sync-manager status` — desired vs present per device (read-only).
- `media-sync-manager doctor` — validate config, connectivity, library IDs, and the same-filesystem hardlink
  precondition.

## Development

```sh
pip install -e ".[test]"
pytest tests/ -v
```
