# media-sync-manager — Specification

## Context

Jellyfin (libraries sourced from TheTVDB) plus an existing Tdarr transcode setup. Goal: a "Plex-Sync
equivalent for Jellyfin" — curate Jellyfin playlists, and get space-efficient **transcoded** copies
into a per-device folder that Infuse downloads for offline/travel viewing. The playlist is the source
of truth — remove an item, it leaves that device's folder.

This tool is **only the glue**: it keeps each Tdarr library's **input folder mirrored to its
playlist**, and lets Tdarr transcode + track what's done. It does not transcode, does not own the
ffmpeg recipes, and never touches originals.

Pipeline: `Jellyfin playlist → media-sync-manager → Tdarr → sync/ folder → Infuse`.

**The quality flow is chosen by which playlist an item is in, not by genre.** Only 2D animation needs
a more aggressive flow, and genre metadata is unreliable — so the user sorts content into playlists
(e.g. "2D Animation" and "Standard") and each maps to a **segment**.

## 1. Responsibilities

The glue, per target, per cycle: (a) reads each playlist; (b) creates an input pointing at each
item's original at `<transcode_root>/<target>/<segment>/<source_rel>`; (c) removes inputs whose item
left the playlist; (d) sweeps `<transcode_root>/<target>/sync/` to delete outputs no longer wanted
(req 9). Tdarr transcodes the inputs into `sync/` and owns transcode-done tracking.

Out of scope (user-owned): the Tdarr **flows**/recipes and the **library topology**; creating
Jellyfin playlists. **The flow must keep its input after transcode** and must not process `sync/`.

## 2. Architecture & module layout

Small Python package: one long-lived poller plus a CLI. Plain blocking loop.

```
media-sync-manager/
  pyproject.toml          # setuptools; deps: requests, pyyaml; [test]: pytest, responses
  Dockerfile              # python:3.12-slim; ENTRYPOINT python -m media_sync_manager
  docker-compose.yml      # primary deployment (bind-mount /mnt/storage:/media, run where it is local)
  config.example.yaml
  media_sync_manager/     # flat package
    __main__.py / cli.py  # run | sync [--once] [--dry-run] | status | doctor
    config.py             # YAML -> frozen dataclasses; ${ENV} expansion; hard-fail validation
    models.py             # MediaItem, Playlist, Target, Config; AddInput/RemoveInput/DeleteOutput/TargetPlan
    jellyfin.py           # JellyfinClient: find playlist, list items
    tdarr.py              # TdarrClient: login, list libraries (cruddb), scan-files
    paths.py              # remap (jellyfin<->glue<->tdarr); source_rel + rel_key; dir convention
    fsops.py              # materialize/is_current (hardlink|symlink) + probe; index_files; unlink
    reconcile.py          # plan_target/plan_all: input-mirror + segment-aware output sweep
    sync.py               # execute a TargetPlan; run one cycle
    poller.py             # interval loop; flock; SIGTERM/SIGINT
    log.py / errors.py
  tests/                  # fakes + real tmp filesystem
```

`reconcile.py` takes the clients + config and returns a plan (executes nothing) — so the whole
algorithm is unit-testable against fakes on a real tmp filesystem, and `--dry-run` is trivial.

## 3. Dir convention & Tdarr topology (user-owned)

For a target `T` under `transcode_root R`: input per segment `S` = `R/T/S`, output = `R/T/sync`
(derived in `paths.py`, not re-spelled per caller). The glue creates an input at `R/T/S/<source_rel>`
pointing at the original; the flow branches encode settings on the `/<segment>/` path component and
writes (Keep Relative Path) to `R/T/sync/<segment>/<source_rel>`.

Requirements on the user's Tdarr setup (documented, checked by `doctor`):

- **Flow keeps its input** after transcode — else `present` inputs go stale and the glue re-creates
  the input for a done item → re-transcode loop. (The sweep keys on the playlist, so it is
  unaffected.)
- The library scans the segment input folders and **must not process `R/T/sync`** (point the library
  at the segment folders, or filter out `/sync/`). Whether one library can watch both segment folders
  (single `library_id` per target) is Tdarr-version-dependent; if not, use one library per segment via
  the optional per-playlist `library_id`.
- **Enable Folder Watch on each library** (Library settings → Folder Watch). Tdarr polls the folder
  (~30s, or instantly with "Use File System Events") and picks up new inputs on its own; the glue's
  `scanFolderWatcher` call only makes that immediate. It is also what notices a *deleted* input, so
  retiring an item depends on it.
- `transcode_root` should sit **under** `media_root`, so a relative symlink between them never
  resolves outside an exported SMB share (Samba's default `wide links = no` then omits the entry
  entirely rather than showing a broken link). `doctor` checks this when symlink mode is selected.

### Choosing hardlink vs symlink

Detected by probing, not configured (`input_mode: auto`, the default; `doctor` reports the result and
the reason). `fsops.probe` creates a temp file in `transcode_root`, tries `os.link` into **every**
input dir, and selects `symlink` if any one fails — under a path-preserving mergerfs policy success
depends on the branch placement of that specific destination dir, so one dir proves nothing about
another. It confirms `os.symlink` works before selecting it, because a CIFS/SMB mount supports
neither and must fail loudly instead of silently picking a broken primitive.

Why `st_dev` was the wrong test: a union filesystem reports one `st_dev` across every underlying
disk, so the old check passed on exactly the topology that cannot hardlink. mergerfs can only link
within one disk; symlinks store a path rather than an inode reference and have no such constraint.
Where Tdarr reaches the media over a share, the server resolves the link during path lookup and Tdarr
sees an ordinary file — so path-based flow branching and Folder Watch behave identically. A Tdarr
with *local* access would see real symlinks; that case is untested.

## 4. Reconciliation algorithm (per target, one cycle)

1. **Desired inputs** — for each `(playlist, segment)`: `find_playlist` + `playlist_items`. Each item
   → `input_path = R/T/<segment>/<source_rel>` (source remapped to glue-view, relative to
   `media_root`). No usable MediaSource or an unmappable path → skip that item. A whole playlist that
   fails to fetch marks the target **incomplete** (removes + sweep suppressed; adds still proceed).
2. **Present inputs** — files under each `R/T/<segment>/`.
3. **Add** = desired − present, *plus* anything present that fails `is_current(source, input_path)`
   → `materialize(source, input_path, mode)` then `scan_files(library_id,
   [tdarr_view(input_path)])` (grouped by `library_id`, one scan each). `is_current` accepts either a
   hardlink to the same inode or a symlink resolving to the source, so a tree may hold both and
   changing mode causes no churn; it rejects a stale inode (original replaced in place), a dangling
   symlink, and a plain copy left at that path, so those get repaired instead of trusted forever.
   Each input is applied independently — one failure never aborts the scans, removes or sweep.
4. **Remove input** (unless incomplete) = present − desired → delete that input. Deleting a hardlink
   drops one name for the inode and deleting a symlink drops the link; either way the original is
   untouched. (Verified for symlinks over SMB too: Samba unlinks the link, not the target.)
5. **Sweep output** (unless incomplete) — for each file under `R/T/sync`, `outrel` = its path there
   minus extension. Delete unless claimed by `keep = {<segment>/<rel_key> for each desired input}`
   (`rel_key` = `source_rel` minus extension), where claim = `outrel == k` or `outrel.endswith("/"+k)`.
   Segment-aware, so moving an item between playlists retires the old output: once `Show/X` is only in
   the 2D playlist, `standard/Show/X` is no longer claimed → swept, and `animation/Show/X` is kept.
   If `R/T/sync` is unreadable (offline) → skip the sweep.

Inputs mirror the playlists (add + remove); the sweep makes `sync/` mirror them too. No "done"
detection, no Tdarr status polling, no glue state — the filesystem + Jellyfin are the truth.

## 5. Edge cases

| Case | Handling |
|---|---|
| Playlist not found / Jellyfin unreachable | Target incomplete: removes + sweep suppressed; adds for OK playlists proceed. |
| Empty playlist (0 items, success) | That segment's stale inputs removed + outputs swept. |
| Item has no MediaSource / unmappable path | Skip item (noted in `skipped`). |
| Multi-version item | Deterministic pick (largest by Size, tie-break by Path). |
| Item moved between playlists | Segment-aware sweep retires the old segment's output; new input added → Tdarr re-encodes. |
| Same basename across series | Matching is on the relative-path `rel_key` (dir + stem) → no collision. |
| `sync/` offline | Sweep skipped (no deletes); adds/removes on the (local) input dirs still proceed. |
| Hardlink fails EXDEV (union pool, cross-device) | Not an error under `input_mode: auto` — it is the signal to use symlinks. Explicit `hardlink` mode surfaces it as a PermanentError. |
| Neither hardlink nor symlink works (CIFS/SMB mount) | Hard error at startup — run the glue where the filesystem is local. |
| Input broken after creation (stale inode, dangling link, `moveonenospc` copy) | `is_current` rejects it → re-added and replaced next cycle. |
| One input fails to create | Logged, reported, non-zero exit; the other adds, the scans, the removes and the sweep all still run. |
| Flow deletes its input (misconfig) | Re-transcode loop — a hard requirement violated; documented + doctor reminder. Originals are safe either way. |
| Tdarr unreachable | New submissions stall, retried next cycle; existing files untouched. |
| Restart mid-encode | Stateless: input present → no re-add; sweep keys on the playlist. No double-submit, no DB. |
| Two daemons | Startup `flock` — second refuses. |

## 6. Path remapping (three coordinate systems)

Jellyfin-view (`MediaSources.Path`) → `path_maps` → glue-view (`media_root`/`transcode_root` live
here) → `tdarr_path_maps` → Tdarr-view (what `scan-files` receives). Maps are **optional** — needed
only when the glue sees a folder at a different path than Jellyfin/Tdarr (mismatched mounts). Each
config entry is anchored on the glue's `local` path (`{local, jellyfin}` / `{local, tdarr}`), so
there's no direction to reason about; `config.py` translates them into the internal `PathMap(src,
dst)` in the direction `remap` applies (jellyfin→local inbound, local→tdarr outbound).

For the recommended deployment (glue container bind-mounts `/mnt/storage:/media`, same as Jellyfin &
Tdarr), all three agree on `/media/...` and **no maps are needed**.

## 7. Config schema (YAML)

```yaml
media_root: /media                              # covers /media/Movies, /media/TV Shows, ...
transcode_root: /media/Transcode Videos         # keep under media_root
poll_interval_seconds: 45
input_mode: auto                                # optional; auto | hardlink | symlink
jellyfin: { url: ..., api_key: "${JELLYFIN_API_KEY}", user_id: ... }
tdarr:    { url: ..., username: "${TDARR_USER}", password: "${TDARR_PASS}" }
targets:
  - name: iphone                                # keys /media/Transcode Videos/iphone/...
    library_id: lib_iphone
    playlists:
      - { playlist: "2D Animation", segment: animation }
      - { playlist: "Standard",     segment: standard }   # optional per-playlist library_id
```

Point each Jellyfin library at its subdir (`/media/Movies`, `/media/TV Shows`, …), not at `/media`,
so Jellyfin never scans `/media/Transcode Videos`.

## 8. Deployment (Docker-first)

```dockerfile
FROM python:3.12-slim
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
ENTRYPOINT ["python", "-m", "media_sync_manager"]
CMD ["run", "--config", "/etc/media-sync-manager/config.yaml"]
```

```yaml
services:
  media-sync-manager:
    build: .
    image: ghcr.io/andyattebery/media-sync-manager:latest
    restart: unless-stopped
    environment: [JELLYFIN_API_KEY, TDARR_USER, TDARR_PASS]   # compose forwards no host env implicitly
    volumes:
      - /etc/media-sync-manager/config.yaml:/etc/media-sync-manager/config.yaml:ro
      - /mnt/storage:/media        # same mount as Jellyfin/Tdarr; must be LOCAL on this host
```

One-shot commands: `docker compose run --rm media-sync-manager sync --once` / `... doctor`.
**CI** (`.github/workflows/ci.yaml`): `pytest tests/ -v` on push/PR; multi-arch image to GHCR on
semver tags.

## 9. Tests

`pytest`, no network. Fakes + a real tmp filesystem. Reconcile scenarios: cold-add; present-input
no-op; remove un-listed input; sweep orphan output; transient suppresses removes+sweep; empty
playlist; re-categorisation; no-MediaSource skip; multi-version pick; two playlists per target.
`fsops` covers both modes, every `is_current` rejection (stale inode, dangling link, plain copy,
wrong target), the probe (including the one-dir-fails and neither-works cases), and typed errors for
non-EXDEV `OSError`. `sync` covers per-input isolation and library grouping. The probe memoises per
process, so an autouse fixture resets it — otherwise suite order would fix the mode.
`doctor` and full round-trips are validated live.

## 10. Open items (resolved by `doctor`/at build)

- **Tdarr enqueue** — the glue calls `scan-files` with `mode: scanFolderWatcher` and the file path in
  `arrayOrPath` (the API's documented single-file form: *"scanFolderWatcher requires an array of file
  paths"*). Pickup is guaranteed by the library's **Folder Watch** poll (see §3); the API call just
  makes it immediate. (Not auto-probed — the earlier "doctor probes scan_mode" was never real.)
- **Tdarr auth** — token login (`/public/auth/login` → Bearer) or auth-disabled; `doctor` reports.
- **Library topology** — one library watching both segment folders vs one per segment; `doctor`
  reports what each configured `library_id` watches.
- **Process/permissions** — container runs as root; the glue never writes originals; input subdirs
  are `0o755` so a non-root Tdarr can traverse; single-instance `flock` at `/run/media-sync-manager.lock`.
