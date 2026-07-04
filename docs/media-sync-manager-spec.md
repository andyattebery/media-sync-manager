# media-sync-manager — Specification

## Context

Jellyfin (libraries sourced from TheTVDB) plus an existing Tdarr transcode setup. Goal: a "Plex-Sync
equivalent for Jellyfin" — curate one Jellyfin playlist per iOS device in the GUI, and get
space-efficient **transcoded** copies pushed to a per-device SMB folder that Infuse downloads for
offline/travel viewing. The playlist is the source of truth — remove an item, it leaves that
device's folder.

This tool is **only the glue**: read playlists → decide each item's quality profile → get files to
the user's Tdarr → keep each device folder in sync. It does not transcode, does not own the ffmpeg
recipes, and never touches originals.

Pipeline: `Jellyfin playlist → media-sync-manager → Tdarr → SMB share → Infuse`.

## 1. Responsibilities (and non-responsibilities)

The glue: (a) reads each device's Jellyfin playlist; (b) resolves each item's quality profile from
genre metadata; (c) hardlinks the original into that device's Tdarr input folder, under the
profile's path segment, preserving the source's relative path, and triggers a scan; (d) removes
device-folder outputs whose playlist entry was removed.

Out of scope (user-owned): the ffmpeg recipe / Tdarr **flows** and per-profile quality settings; the
Tdarr **library/flow topology** (§3); creating Jellyfin playlists and SMB shares; final
genre→profile match tuning. Infuse and iOS foreground-download behavior are accepted as-is.

## 2. Architecture & module layout

Small Python package: one long-lived poller plus a CLI. Plain blocking loop — one user, a handful
of devices, ~30–45s cadence; the encode is never awaited (submit and move on).

```
media-sync-manager/
  pyproject.toml          # setuptools; deps: requests, pyyaml; [test]: pytest, responses
  Dockerfile              # python:3.12-slim; ENTRYPOINT python -m media_sync_manager
  docker-compose.yml      # primary deployment
  config.example.yaml
  README.md
  docs/media-sync-manager-spec.md
  .github/workflows/ci.yaml   # test -> docker build/push on tags
  media_sync_manager/              # flat package
    __main__.py   # `python -m media_sync_manager` -> cli
    cli.py        # subcommands: run | sync [--once] [--dry-run] | status | doctor
    config.py     # YAML -> frozen dataclasses; ${ENV} expansion; hard-fail validation at startup
    models.py     # MediaItem, DesiredEntry, Profile, Device
    jellyfin.py   # JellyfinClient: find playlist, list items, resolve series genres (memoized)
    tdarr.py      # TdarrClient: login, list libraries (cruddb), scan-files
    profile.py    # classify(item) -> profile; fail-safe to default (pure)
    paths.py      # path remapping (jellyfin->glue->tdarr); source_rel + rel_key derivation
    fsops.py      # hardlink + EXDEV guard; recursive device-folder listing; rel-path index; orphan delete
    reconcile.py  # the algorithm: per-device diff -> actions (clients injected, unit-testable)
    sync.py       # one full cycle across all devices
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

Two independent dimensions: **profile** (encode settings) and **device** (output destination).
Verified Tdarr facts that shape this:

- **Profile can be branched inside ONE flow** via `Check File Name Includes` (enable "include file
  directory in check") matching a `/<profile>/` segment in the input path → different transcode
  branches. Documented; community-standard.
- **Output destination is NOT per-file.** `Move To Directory`/`Copy to Directory` take a static path
  or a **library/global variable** — Tdarr has no per-file variable. So a single library cannot fan
  out to many device folders cleanly; the per-file channel is the **input path** only.

**Recommended topology: one library per device + one shared flow** (proven by samssausages/
Tdarr-One-Flow). Each device's library: source = that device's `input_dir`; sets its own
`output_dir` library-variable = the device's SMB folder; assigned the one shared flow. The shared
flow branches profile by the `/<profile>/` path segment, then `Move To Directory →
{{{args.userVariables.library.output_dir}}}`, **Keep Relative Path** on. Net: **N libraries +
1 flow** (down from N×2 libraries + 2 flows). Simpler alternative: N×2 single-purpose libraries +
2 flows. Fewest (1 library) is only reachable via a `Run CLI`-driven recipe with a templated output
path (no community precedent); not recommended.

With the recommended topology + Keep Relative Path, the device output inherits a `<segment>/` top
folder (e.g. `…/iphone/animation/Show/…`); the §4 suffix match absorbs it, so the glue is unaffected.
A device that wants no segment folder uses the N×2-library variant (clean output, more libraries).

**The glue doesn't depend on the choice.** Config maps each `(device, profile)` to an `input_dir`
and a `library_id`; the glue hardlinks to `input_dir/<profile_segment>/source_rel` and scans
`library_id`. All three topologies are just different fillings of that map. `doctor` lists Tdarr
libraries (`cruddb getAll LibrarySettingsJSONDB`) and verifies every configured `library_id` exists
and its source folder equals `tdarr_view(input_dir)` (comparing in **tdarr-view** — Tdarr reports
its source path in its own namespace, so the remap must be applied before matching).

## 4. Sync reconciliation algorithm (one cycle)

For each `device` in config:

1. `playlist = jellyfin.find_playlist(device.playlist_name)`. Not found / Jellyfin unreachable →
   `TransientError`: skip device, **do not** compute orphans (never wipe a folder on a transient
   lookup miss — §6).
2. `items = jellyfin.playlist_items(playlist.id, userId, fields=Path,MediaSources,SeriesId,Genres,Tags,OfficialRating)`.
3. Build `desired: {relkey → (profile, source_path, source_rel)}`:
   - `source = remap(pick_media_source(item))` → glue-local (`pick_media_source` = largest
     `MediaSources[].Size`, stable tie-break by `Path`; the same picker §6 names). No source →
     warn, skip item.
   - `profile = classify(item)` (§5); `source_rel = relpath(source, media_root)`;
     `relkey = source_rel` **with the extension stripped** (dir + stem — NOT bare basename, which
     would collide between same-named episodes across series, e.g. `01.mkv`).
4. **Additions** — `present` = recursive scan of `device.output_dir`, each file as its
   output-relative path minus extension. A desired `relkey` is **satisfied** if some present path
   equals `relkey` **or ends with `/relkey`** (suffix match absorbs a `<segment>/` prefix that the
   recommended Keep-Relative-Path flow prepends — see §3). For each unsatisfied `relkey`:
   - `input_path = device.input_dir/<profile.segment>/source_rel` (what the shared flow branches on).
   - if `input_path` exists → in-flight (already submitted), wait.
   - else → `fsops.hardlink(source, input_path)` (parent dirs created mode `0o755` so a non-root
     Tdarr can traverse) then `tdarr.scan_files(device.library_id, [tdarr_view(input_path)],
     mode=tdarr.scan_mode)` — `scan_mode` resolved once by `doctor` (scanFolderWatcher vs
     scanFindNew; see §11), not assumed.
5. **Orphans** — for each present file under `device.output_dir` whose path-minus-ext satisfies no
   desired `relkey` (same suffix rule): delete that file only. Never touch input folders, libraries,
   or originals.
6. **Input cleanup** (optional) — an `input_path` whose output is now satisfied, and which the flow
   didn't delete, may be removed.

Every branch reads only the filesystem (input hardlink, device-folder scan) and Jellyfin. No cache,
no completion signal, no glue-written state, no Tdarr status polling. Suffix matching on the
relative path keeps the diff robust to a `<segment>/` prefix or season folders while staying
collision-safe (full-flatten output is the one layout that can collide — `doctor` flags it).

## 5. Profile classification (fail-safe toward quality)

`classify(item)`:

- Episode genres if present; else fetch parent series genres via
  `GET /Items/{SeriesId}?fields=Genres,Tags,OfficialRating` (cached by SeriesId with TTL
  `genre_cache_ttl_seconds`, shared across cycles — not refetched every poll). Movies carry their
  own genres. Read `Tags`/`OfficialRating` as backup signals.
- Walk `profile_priority` in order; the first profile whose `match` (genres/tags, case-insensitive)
  hits wins. (`standard` carries no `match`, so it never wins by matching — it is reached only as
  the fallthrough below; listing it in `profile_priority` is optional/cosmetic.)
- No match → `default_profile` (`standard`). Includes **missing, empty, or unresolvable** genres and
  a series genre-fetch error (a bigger file is acceptable; over-compression by mistake is not).
- Match lists from config, seeded from the user's actual library strings (e.g. `Animation`, `Anime`,
  `Children`, `Cartoon`), not a hardcoded taxonomy. A device that doesn't define a given profile
  falls back to its default profile (each device must define `standard`).

## 6. Edge cases & failure modes

| Case | Handling |
|---|---|
| Playlist not found / Jellyfin unreachable | Warn, skip device. **No orphan deletion** (avoids wiping a folder on a transient miss). |
| Empty playlist vs failed fetch | Orphan diff runs only after a confirmed-successful items fetch; 0 items → remove orphans; fetch error → touch nothing. |
| Item has no MediaSource / Path | Warn, skip item. |
| Multi-version item | Deterministic pick (largest by Size, stable tie-break by Path) so the choice doesn't flap between cycles. |
| Genre on episode vs series | Episode genres preferred if present, else series; movies use their own. |
| SMB / device folder offline | Unreadable/unwritable `output_dir` → skip device, warn; no deletes. |
| Hardlink across filesystems (EXDEV) | Hard error in `doctor`/startup: media and `input_dir` must share a filesystem (esp. in Docker — §9). |
| Same basename in different series (`01.mkv`) | Handled: matching is on the relative-path `relkey` (dir + stem), so `ShowA/S01/01` ≠ `ShowB/S01/01`. Only a flow that **flattens** output to bare basenames can collide — `doctor` flags duplicate basenames per playlist when flatten is detected. |
| Encode in progress writes a partial output | **Accepted edge case**: once an output appears (suffix match), the glue treats it satisfied. Mitigation is Tdarr's domain; the glue is out of the output path. |
| Encode failed | No output appears; input hardlink lingers → glue sees in-flight, won't resubmit. Surfaced by `status` (submitted but no output after `submit_timeout_seconds`). Optional: flow deletes input on failure to force a clean re-submit. |
| Tdarr unreachable | New submissions stall, retried next cycle; existing device folders untouched. |
| Restart mid-encode | Stateless recovery: input hardlink present → in-flight; output present (suffix match) → satisfied. No double-submit, no DB. |
| Two daemons launched | Startup `flock` — second instance refuses. |
| Source file changed (re-grab) | Same `relkey` → still satisfied if an output exists. (No content hashing; remove+re-add forces a refresh.) |

## 7. Path remapping (three coordinate systems)

The glue may run on a different host/container than Jellyfin/Tdarr:

- **Jellyfin-view** (`MediaSources.Path`) → `path_maps` → **glue-view** (where the glue reads
  originals and writes hardlinks; `media_root` lives here and defines `source_rel`) →
  `tdarr_path_maps` → **Tdarr-view** (what `scan-files` receives).

Hardlinks operate in glue-view and require source and `input_dir` to share a filesystem (`st_dev`).
`paths.py` provides `to_glue`, `to_tdarr`, `source_rel`, `rel_key` (= `source_rel` minus extension;
longest-prefix; fail loud on no-match). `doctor` takes one real item, remaps it, `stat`s it, asserts
same-`st_dev` as `input_dir`.

## 8. Config schema (YAML) — recommended N-libraries + 1-flow topology

```yaml
poll_interval_seconds: 45
genre_cache_ttl_seconds: 900

jellyfin:
  url: http://jellyfin.example.com:8096
  api_key: "${JELLYFIN_API_KEY}"      # env-expanded; commit-safe YAML
  user_id: "a1b2c3..."                # REQUIRED by the playlist-items endpoint

tdarr:
  url: http://tdarr.example.com:8265
  username: "${TDARR_USER}"           # omit username+password if the instance has auth disabled
  password: "${TDARR_PASS}"
  request_timeout_seconds: 20
  submit_timeout_seconds: 21600       # 6h: input link present this long with no output -> status flags it
  # scan_mode is auto-detected by doctor (scanFolderWatcher vs scanFindNew), not set here

media_root: /mnt/pool/media           # glue-view root; defines source_rel + relkey
path_maps:                            # jellyfin-view -> glue-view (longest prefix wins)
  - { from: /data/media, to: /mnt/pool/media }
tdarr_path_maps:                      # glue-view -> tdarr-view (omit if co-located)
  - { from: /mnt/pool/tdarr, to: /mnt/tdarr }

profiles:                             # classification (match lists) + path segment for flow branching
  standard:  { segment: standard }                 # DEFAULT / fail-safe; no match needed
  animation: { segment: animation, match: { genres: [Animation, Anime, Children, Cartoon, Family], tags: [anime, kids] } }
default_profile: standard
profile_priority: [animation, standard]   # first match wins; standard last = fallthrough

devices:
  - name: iphone
    playlist_name: "Travel - Phone"    # matched client-side by Name
    output_dir: /mnt/smb/togo/iphone  # Tdarr flow writes here; glue reads it for done/orphan diff
    library_id: lib_iphone            # the device's Tdarr library (shared flow); glue writes
    input_dir: /mnt/pool/tdarr/in/iphone   #   <input_dir>/<profile.segment>/<source_rel>, scans library_id
  - name: kids-ipad
    playlist_name: "Togo - Kids iPad"
    output_dir: /mnt/smb/togo/kids
    library_id: lib_kids
    input_dir: /mnt/pool/tdarr/in/kids
# (N×2-library topology: give each device per-profile {library_id,input_dir}. The glue only needs,
#  per (device,profile), an input_dir to hardlink into and a library_id to scan.)
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

The glue only ever **reads** media and **creates hardlinks** + **deletes device-folder orphans** —
it never writes to originals (enforced in code, not by mount flags). `doctor` verifies the
media↔input `st_dev` match *inside the container*, catching the #1 Docker misconfiguration (media
and input bind-mounted from different filesystems). One-shot commands run via
`docker compose run --rm media-sync-manager sync --once` or `... doctor`.

**CI (`.github/workflows/ci.yaml`)**: `test` job (checkout → setup-python 3.12 →
`pip install ".[test]"` → `pytest tests/ -v`) on push/PR; `docker` job (needs test,
`if: startsWith(github.ref, 'refs/tags/')`) builds multi-arch (`linux/amd64,linux/arm64`) via buildx
and pushes `ghcr.io/andyattebery/media-sync-manager:<version>` (stripping a leading `v`), promoting
`:latest` for non-prerelease tags.

## 10. Tests

`pytest`, no network, no real Tdarr/Jellyfin. Fakes + a real tmp filesystem do the work. Coverage
intent: `reconcile`, `profile`, `paths`, `fsops` at/near 100% of branches (they hold the correctness
and the safety rules); clients tested at the request-shaping layer; `cli` smoke-tested for dry-run
safety. `doctor` and full Tdarr/Jellyfin round-trips are validated live, not in CI.

Reconcile scenarios (against fakes + tmp fs): cold-add; already-present (suffix-matched incl. a
`<segment>/` prefix); in-flight (no double-submit); restart safety; output appears mid-run; orphan
(only the device file deleted, input + original untouched); transient safety (no purge on lookup
failure); empty playlist; fail-safe routing; no-MediaSource skip; multi-version deterministic pick.

## 11. Open items (resolved by `doctor`/at build) & deployment assumptions

- **Tdarr `scan_mode`** — `doctor` probes one file to confirm whether `scanFolderWatcher` (preferred;
  may require folder-watch enabled, toggled via `/api/v2/toggle-folder-watch`) or `scanFindNew`
  actually enqueues a single hardlinked file; the resolved mode is what `reconcile` uses.
- **Tdarr auth** — the client supports both token login (`/public/auth/login` → Bearer) and an
  auth-disabled instance (no credentials); `doctor` reports which is in effect.
- **Shared storage** — `input_dir` and `output_dir` must be the *same underlying storage* seen by
  both the glue container and Tdarr (typically one NAS export), since the glue writes the hardlink
  and reads the output that Tdarr wrote. `doctor` can only verify the glue's side; the Tdarr-side
  visibility is a deployment precondition stated in the README.
- **Process/permissions** — the container runs as root; the glue never writes originals (code-
  enforced). Created input subdirs are mode `0o755` so a non-root Tdarr can traverse to the hardlink
  (which itself keeps the original's perms). The single-instance `flock` is a container-local file
  (e.g. `/run/media-sync-manager.lock`), not on shared storage.
