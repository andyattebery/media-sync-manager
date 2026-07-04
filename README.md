![ci](https://github.com/andyattebery/media-sync-manager/actions/workflows/ci.yaml/badge.svg)

# media-sync-manager

A Plex-Sync equivalent for Jellyfin. Curate one Jellyfin **playlist per iOS device**, and this glue
keeps a per-device SMB folder in sync with space-efficient **transcoded** copies — produced by your
existing **Tdarr** — for offline/travel viewing in Infuse.

```
Jellyfin playlist  ->  media-sync-manager  ->  Tdarr (transcode)  ->  per-device SMB folder  ->  Infuse
```

The glue is small and stateful-free: it reads playlists, decides each item's quality profile from
genre metadata (failing safe toward quality), hardlinks originals into the right Tdarr input folder,
and removes outputs whose playlist entry was removed. It **never modifies or deletes originals** and
keeps **no database** — the filesystem is the source of truth. See
[the spec](docs/media-sync-manager-spec.md) for the full design.

## How it works

- **In-flight** = the input hardlink exists. **Done** = the output exists in the device folder
  (matched by relative-path suffix). No completion polling, no markers.
- **Profiles** (`animation` smaller / `standard` higher-quality, the default) are chosen from
  Jellyfin genres; unknown/uncertain → `standard`. The profile rides in the input path
  (`<input_dir>/<segment>/...`), which is the only per-file channel Tdarr exposes.
- **Pickup** is short-interval polling (Jellyfin emits no playlist events), plus `sync --once` for
  last-minute trips.

## Tdarr setup (you own this)

Recommended topology: **one Tdarr library per device, all sharing one flow**. Each library:

- watches that device's `input_dir`,
- sets a library variable `output_dir` = the device's SMB folder,
- runs the shared flow, which branches encode settings on the `/<profile>/` path segment
  (`Check File Name Includes`, "include file directory") and writes output with **Keep Relative
  Path** to `{{{args.userVariables.library.output_dir}}}`.

The glue only needs, per device, an `input_dir` to hardlink into and a `library_id` to scan — so
N×2-libraries or a single-library topology work too without code changes.

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
