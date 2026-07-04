# media-sync-manager — Specification

## Context

Jellyfin (libraries sourced from TheTVDB) plus an existing Tdarr transcode setup. Goal: a "Plex-Sync
equivalent for Jellyfin" — curate Jellyfin playlists, and get space-efficient **transcoded** copies
pushed to a per-device SMB folder that Infuse downloads for offline/travel viewing. The playlist is
the source of truth — remove an item, it leaves that folder.

This tool is **only the glue**: read playlists → route each item to its transcode flow → get files
to the user's Tdarr → keep each output folder in sync. It does not transcode, does not own the
ffmpeg recipes, and never touches originals.

Pipeline: `Jellyfin playlist → media-sync-manager → Tdarr → SMB share → Infuse`.

**The quality flow is chosen by which playlist an item is in, not by genre.** Only 2D animation
needs a more aggressive flow, and genre metadata (crowd-sourced TheTVDB) is unreliable — so the user
sorts content into two playlists (e.g. "2D Animation" and "Standard") and the config maps each
playlist to a **segment**. No genre inference, no fail-safe heuristics.

## 1. Responsibilities (and non-responsibilities)

The glue: (a) reads each configured playlist; (b) for every item, hardlinks the original into that
target's Tdarr input folder under the target's `segment`, preserving the source's relative path, and
triggers a scan; (c) removes output-folder files whose playlist entry was removed.

Out of scope (user-owned): the ffmpeg recipe / Tdarr **flows** and per-segment quality settings; the
Tdarr **library/flow topology** (§3); creating Jellyfin playlists and SMB shares. Infuse and iOS
foreground-download behavior are accepted as-is.

## 2. Architecture & module layout

Small Python package: one long-lived poller plus a CLI. Plain blocking loop — one user, a handful of
targets, ~30–45s cadence; the encode is never awaited (submit and move on).

```
media-sync-manager/
  pyproject.toml          # setuptools; deps: requests, pyyaml; [test]: pytest, responses
  Dockerfile              # python:3.12-slim; ENTRYPOINT python -m media_sync_manager
  docker-compose.yml      # primary deployment
  config.example.yaml
  README.md
  docs/media-sync-manager-spec.md
  .github/workflows/ci.yaml   # test -> docker build/push on tags
  media_sync_manager/     # flat package
    __main__.py   # `python -m media_sync_manager` -> cli
    cli.py        # subcommands: run | sync [--once] [--dry-run] | status | doctor
    config.py     # YAML -> frozen dataclasses; ${ENV} expansion; hard-fail validation at startup
    models.py     # MediaItem, Target, Submit, DeleteOutput, GroupPlan
    jellyfin.py   # JellyfinClient: find playlist, list items
    tdarr.py      # TdarrClient: login, list libraries (cruddb), scan-files
    paths.py      # path remapping (jellyfin->glue->tdarr); source_rel + rel_key derivation
    fsops.py      # hardlink + EXDEV guard; recursive output listing; orphan delete
    reconcile.py  # the algorithm: per-output-group diff -> actions (clients injected, testable)
    sync.py       # execute plans; one full cycle across all output groups
    poller.py     # interval loop; flock (single instance); SIGTERM/SIGINT
    log.py / errors.py   # structured logging; Transient vs Permanent exceptions
  tests/          # unit tests + in-memory FakeJellyfinClient/FakeTdarrClient + real tmp filesystem
```

`jellyfin.py`/`tdarr.py` are dumb transports (HTTP in, dataclasses out). `reconcile.py` is the brain
and takes clients + fsops + config as arguments, so the whole algorithm is testable against fakes on
a real tmp filesystem. Run modes: `run` (daemon loop; the container entrypoint), `sync --once`
(single pass; trip button & cron), `sync --once --dry-run` (print plan, touch nothing), `status`
(read-only), `doctor` (validate config, connectivity, library IDs, same-volume hardlink precondition).

## 3. Tdarr topology (user-owned) — and why the glue is decoupled from it

Two independent dimensions: **segment** (encode settings) and **output** (device destination).
Verified Tdarr facts that shape this:

- **Segment can be branched inside ONE flow** via `Check File Name Includes` (enable "include file
  directory in check") matching a `/<segment>/` segment in the input path → different transcode
  branches. Documented; community-standard.
- **Output destination is NOT per-file.** `Move To Directory`/`Copy to Directory` take a static path
  or a **library/global variable** — Tdarr has no per-file variable. So a single library cannot fan
  out to many device folders cleanly; the per-file channel is the **input path** only.

**Recommended topology: one library per device + one shared flow** (proven by samssausages/
Tdarr-One-Flow). Each device's library: source = that device's `input_dir`; sets its own
`output_dir` library-variable = the device's SMB folder; assigned the one shared flow. The shared
flow branches segment by the `/<segment>/` path segment, then `Move To Directory →
{{{args.userVariables.library.output_dir}}}`, **Keep Relative Path** on. Net: **N libraries +
1 flow**.

**Output layout the glue assumes:** because the glue hardlinks to `input_dir/<segment>/<source_rel>`
and the flow Keeps Relative Path, the output lands at `output_dir/<segment>/<source_rel>`. The glue's
matching is segment-aware (`<segment>/<relkey>`), so this layout is the contract; a flow that
flattens output or drops the segment folder breaks orphan/done detection (`doctor` probes this).

**The glue doesn't depend on the library count.** Config maps each `(playlist → segment, output_dir,
library_id, input_dir)` target; the glue hardlinks to `input_dir/<segment>/source_rel` and scans
`library_id`. `doctor` verifies every configured `library_id` exists and its source folder equals
`tdarr_view(input_dir)` (comparing in **tdarr-view** — Tdarr reports its source path in its own
namespace, so the remap must be applied before matching).

## 4. Sync reconciliation algorithm (one cycle)

Targets are grouped by `output_dir` (targets sharing one are the same device — e.g. its "2D
Animation" + "Standard" playlists). Each group is reconciled together so one target never treats
another's outputs as orphans. For each group:

1. For each target in the group: `find_playlist(playlist_name)` then `playlist_items(...)`.
   A `TransientError` (missing playlist / Jellyfin unreachable) marks the group **incomplete** →
   **deletes are suppressed** this cycle (never wipe a folder because one playlist failed); other
   targets' additions still proceed.
2. Build the group's `desired`, keyed by `match_key = <segment>/<relkey>`:
   - `source = remap(pick_media_source(item))` → glue-local (`pick_media_source` = largest
     `MediaSources[].Size`, stable tie-break by `Path`). No source → skip item.
   - `source_rel = relpath(source, media_root)`; `relkey = source_rel` minus extension (dir + stem,
     NOT bare basename — same-named episodes across series must not collide).
3. `present` = recursive scan of `output_dir`, each file as its output-relative path minus extension.
   A `match_key` is **satisfied** if some present path equals it **or ends with `/match_key`**
   (absorbs an extra prefix a flow might add). For each unsatisfied `match_key`:
   - `input_path = input_dir/<segment>/source_rel`.
   - if `input_path` exists → in-flight (already submitted), wait.
   - else → `hardlink(source, input_path)` (parent dirs `0o755`) then
     `scan_files(library_id, [tdarr_view(input_path)], mode=scan_mode)` (mode resolved by `doctor`).
     Submits are grouped by `library_id` so each library scans once.
4. **Orphans** (unless the group is incomplete): each present file claimed by no desired `match_key`
   is deleted — that file only, never input folders, libraries, or originals.

Because `match_key` includes the segment, moving an item between the "2D Animation" and "Standard"
playlists re-encodes it under the new segment and retires the old output as an orphan.

Every branch reads only the filesystem (input hardlink, output scan) and Jellyfin. No cache, no
completion signal, no glue-written state, no Tdarr status polling.

## 5. Segment routing (from the playlist, not genre)

There is no classification step. Each `target` names its `segment` explicitly, and every item in that
target's playlist gets that segment. The segment is the folder the flow branches on
(`input_dir/<segment>/...`) and the subfolder the output lands under (`output_dir/<segment>/...`).
Two segments are expected today — `animation` (aggressive/smaller) and `standard` — but the glue
treats `segment` as an opaque string, so more can be added purely in config + the flow.

## 6. Edge cases & failure modes

| Case | Handling |
|---|---|
| Playlist not found / Jellyfin unreachable | Group marked incomplete: **deletes suppressed**; other targets' additions proceed. |
| Empty playlist vs failed fetch | Orphan diff runs only when no target in the group failed to fetch; 0 items → orphans removed; fetch error → no deletes. |
| Item has no MediaSource / Path | Skip item, note in `skipped`. |
| Multi-version item | Deterministic pick (largest by Size, tie-break by Path) — stable across cycles. |
| Two targets share an output_dir | Reconciled as one group; the union of their desired sets protects each from the other's files. |
| Item moved between playlists | Segment-aware `match_key` → re-encoded under the new segment, old output retired as an orphan. |
| Same basename in different series (`01.mkv`) | Matching is on the relative-path `relkey` (dir + stem), so they don't collide. Only a flow that flattens output can collide — `doctor` flags it. |
| SMB / output folder offline | `output_dir` unreadable → group skipped, no deletes. |
| Hardlink across filesystems (EXDEV) | Hard error: media and `input_dir` must share a filesystem (esp. in Docker — §9). |
| Encode in progress writes a partial output | **Accepted**: once an output appears (matched), the item is treated satisfied. Mitigation is Tdarr's domain; the glue is out of the output path. |
| Encode failed | No output appears; input hardlink lingers → in-flight, won't resubmit. Surfaced by `status`. |
| Tdarr unreachable | New submissions stall, retried next cycle; existing outputs untouched. |
| Restart mid-encode | Stateless recovery: input hardlink present → in-flight; output present → satisfied. No double-submit, no DB. |
| Two daemons launched | Startup `flock` — second instance refuses. |

## 7. Path remapping (three coordinate systems)

The glue may run on a different host/container than Jellyfin/Tdarr:

- **Jellyfin-view** (`MediaSources.Path`) → `path_maps` → **glue-view** (where the glue reads
  originals and writes hardlinks; `media_root` lives here and defines `source_rel`) →
  `tdarr_path_maps` → **Tdarr-view** (what `scan-files` receives).

Hardlinks operate in glue-view and require source and `input_dir` to share a filesystem (`st_dev`).
`paths.py` provides `to_glue`, `to_tdarr`, `source_rel`, `rel_key` (= `source_rel` minus extension;
longest-prefix; fail loud on no-match). `doctor` takes one real item, remaps it, `stat`s it, asserts
same-`st_dev` as `input_dir`.

## 8. Config schema (YAML)

```yaml
poll_interval_seconds: 45

jellyfin:
  url: http://jellyfin.example.com:8096
  api_key: "${JELLYFIN_API_KEY}"      # env-expanded; commit-safe YAML
  user_id: "a1b2c3..."                # REQUIRED by the playlist-items endpoint

tdarr:
  url: http://tdarr.example.com:8265
  username: "${TDARR_USER}"           # omit username+password if the instance has auth disabled
  password: "${TDARR_PASS}"
  request_timeout_seconds: 20
  submit_timeout_seconds: 21600
  # scan_mode is auto-detected by doctor (scanFolderWatcher vs scanFindNew), not set here

media_root: /mnt/pool/media           # glue-view root; defines source_rel + relkey
path_maps:                            # jellyfin-view -> glue-view (longest prefix wins)
  - { from: /data/media, to: /mnt/pool/media }
tdarr_path_maps:                      # glue-view -> tdarr-view (omit if co-located)
  - { from: /mnt/pool/tdarr, to: /mnt/tdarr }

# Each target maps ONE playlist to ONE segment + output + Tdarr library. Targets sharing an
# output_dir are one device. Which playlist an item is in decides its segment/flow.
targets:
  - playlist_name: "2D Animation"
    segment: animation
    output_dir: /mnt/smb/togo/iphone
    library_id: lib_iphone
    input_dir: /mnt/pool/tdarr/in/iphone
  - playlist_name: "Standard"
    segment: standard
    output_dir: /mnt/smb/togo/iphone
    library_id: lib_iphone
    input_dir: /mnt/pool/tdarr/in/iphone
```

## 9. Deployment (Docker-first)

**Dockerfile** (no ffmpeg — Tdarr transcodes):

```dockerfile
FROM python:3.12-slim
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
ENTRYPOINT ["python", "-m", "media_sync_manager"]
CMD ["run", "--config", "/etc/media-sync-manager/config.yaml"]
```

**docker-compose.yml** — the critical constraint is the hardlink filesystem:

```yaml
services:
  media-sync-manager:
    build: .
    image: ghcr.io/andyattebery/media-sync-manager:latest
    restart: unless-stopped
    volumes:
      - /etc/media-sync-manager/config.yaml:/etc/media-sync-manager/config.yaml:ro
      # media (read) + Tdarr input (hardlink target) MUST be ONE shared-filesystem mount,
      # else os.link() fails EXDEV. Mount the common parent, not two separate volumes:
      - /mnt/pool:/mnt/pool
      # per-device SMB output folders (glue reads to diff, deletes orphans) - may be separate fs:
      - /mnt/smb/togo:/mnt/smb/togo
```

The glue only ever **reads** media and **creates hardlinks** + **deletes output-folder orphans** —
it never writes to originals (enforced in code, not by mount flags). `doctor` verifies the
media↔input `st_dev` match *inside the container*, catching the #1 Docker misconfiguration. One-shot
commands run via `docker compose run --rm media-sync-manager sync --once` or `... doctor`.

**CI (`.github/workflows/ci.yaml`)**: `test` job (checkout → setup-python 3.12 →
`pip install ".[test]"` → `pytest tests/ -v`) on push/PR; `docker` job (needs test, tags only) builds
multi-arch and pushes `ghcr.io/andyattebery/media-sync-manager:<version>`, promoting `:latest` for
non-prerelease tags.

## 10. Tests

`pytest`, no network, no real Tdarr/Jellyfin. Fakes + a real tmp filesystem do the work. Coverage
intent: `reconcile`, `paths`, `fsops` at/near 100% of branches; clients tested at the request-shaping
layer; `cli` smoke-tested for dry-run safety. `doctor` and full round-trips are validated live.

Reconcile scenarios: cold-add; already-present; in-flight (no double-submit); restart safety; output
appears mid-run; orphan removal; transient-no-purge; empty playlist; segment-from-playlist;
shared-output no-cross-delete; re-categorisation (move between playlists); no-MediaSource skip;
multi-version deterministic pick.

## 11. Open items (resolved by `doctor`/at build) & deployment assumptions

- **Tdarr `scan_mode`** — `doctor` probes whether `scanFolderWatcher` or `scanFindNew` enqueues a
  single hardlinked file; the resolved mode is what `reconcile` uses.
- **Output layout** — the flow must Keep Relative Path so output = `output_dir/<segment>/<source_rel>`
  (segment-aware matching depends on it); `doctor` probes a file to confirm.
- **Tdarr auth** — the client supports token login (`/public/auth/login` → Bearer) and an
  auth-disabled instance; `doctor` reports which is in effect.
- **Shared storage** — `input_dir` and `output_dir` must be the same underlying storage seen by both
  the glue container and Tdarr (typically one NAS export). `doctor` verifies the glue's side.
- **Process/permissions** — the container runs as root; the glue never writes originals. Input
  subdirs are mode `0o755` so a non-root Tdarr can traverse. The single-instance `flock` is a
  container-local file (e.g. `/run/media-sync-manager.lock`).
