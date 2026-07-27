# User guide

How to install, configure, run and troubleshoot media-sync-manager. For *why* it is built this way
see [the sync spec](media-sync-manager-spec.md) and [the playlist editor design](playlist-editor.md);
to work on the code see [the development guide](development.md).

## 1. What it does, and what it never does

You curate Jellyfin **playlists**. This keeps a per-device folder of space-efficient **transcoded**
copies in sync with them, using your existing **Tdarr** to do the transcoding.

```
Jellyfin playlist  ->  media-sync-manager  ->  Tdarr (transcode)  ->  sync/ folder  ->  your player
```

Each cycle, per device: add an input for every item in the playlist, remove inputs for items that
left, and sweep the output folder to match.

**The guarantees, because this tool deletes things:**

- **It never modifies or deletes your originals.** It only ever creates links inside
  `transcode_root`, and deletes files inside `transcode_root`. Nothing under `media_root` is written.
- **It keeps no database.** The filesystem and Jellyfin are the source of truth, so there is no state
  to corrupt, migrate, or get out of step.
- **A Jellyfin problem cannot wipe a device.** If a playlist fails to fetch, that target is marked
  *incomplete* and **removals and the sweep are suppressed** for it — an empty response is never
  mistaken for "the playlist is empty". Adds for the playlists that did work still proceed.
- **There is no undo** on a removal in the editor. The confirm dialog is the only gate.

## 2. Before you start

You need:

- **Jellyfin**, with a playlist per (device, quality) pair — e.g. "2D Animation" and "Standard".
- **Tdarr**, with a library and a flow you own. This tool never transcodes; it only feeds Tdarr
  inputs and cleans up after it. See §5.
- **One shared storage mount** that Jellyfin, Tdarr and this tool all see at the same path
  (e.g. `/mnt/storage:/media` in every container). Then no path maps are needed.

### Where to run it

**Run it on the host where the media filesystem is local** — not mounted over CIFS/SMB.

To feed Tdarr, this tool creates a file in the input folder that points at each original — a hardlink
or a symlink. Over SMB it can create neither: the protocol has no symlink-create operation (`ln -s`
returns `EIO`), and `mfsymlinks` only writes a marker file the server will not resolve, so Tdarr
would be handed the marker instead of media.

Which primitive it uses is **detected, not configured**, and `doctor` reports which:

- **hardlink** where it works — cheapest, and the input is genuinely the same bytes.
- **symlink** where it does not. A union filesystem such as **mergerfs** can only hardlink within one
  underlying disk, so `os.link` fails `EXDEV` even though both paths are inside one mount and report
  the same `st_dev`. Symlinks store a path rather than an inode reference, so they have no such
  constraint. When Tdarr reaches the media over a share, the server resolves the link during path
  lookup and Tdarr sees an ordinary file.

## 3. Getting `api_key` and `user_id`

Both are required. This is the step with the least signposting in Jellyfin itself.

**API key** — Jellyfin **Dashboard → Advanced → API Keys → +**. Name it anything; copy the key. It is
a server-wide credential: treat it like a password and keep it out of your config file (§4).

**User ID** — a GUID, required by the playlist-items endpoint even when you authenticate with an API
key. Two ways, either is fine:

- **Dashboard → Users →** click the user. The URL ends `.../useredit.html?userId=<guid>`.
- **Ask the API** (verified against Jellyfin 10.11):

  ```sh
  export JF=https://jellyfin.example.com
  export KEY=<your api key>
  curl -s -H "X-Emby-Token: $KEY" "$JF/Users" | python -m json.tool | grep -E '"(Name|Id)"'
  ```

  It returns a JSON list; take the `Id` of the user whose `Name` you want.

> `GET /Users/Me` does **not** work with an API key — it returns `400`, because an API key is not
> tied to a user. Use `/Users` as above.

## 4. Configuration

Copy `config.example.yaml` and edit it. **Keep secrets out of the file**: any `${VAR}` is expanded
from the environment at load time, and a missing variable is a startup error.

```yaml
media_root: /media
transcode_root: /media/Transcoded Videos
jellyfin:
  url: https://jellyfin.example.com
  api_key: "${JELLYFIN_API_KEY}"
  user_id: "<guid from §3>"
tdarr:
  url: https://tdarr.example.com
  username: "${TDARR_USER}"
  password: "${TDARR_PASS}"
targets:
  - name: tablet
    library_id: <tdarr library id>
    playlists:
      - { playlist: "2D Animation", segment: animation }
      - { playlist: "Standard",     segment: standard }
```

### Reference

| Key | Required | Default | What it does |
|---|---|---|---|
| `media_root` | **yes** | — | Where your originals live. Never written to. |
| `transcode_root` | **yes** | — | Everything this tool creates lives here. **Keep it under `media_root`** (see below). |
| `poll_interval_seconds` | no | `45` | How often `run` reconciles. Jellyfin emits no playlist events, so this is polling. |
| `input_mode` | no | `auto` | `auto` \| `hardlink` \| `symlink`. `auto` probes once at startup. Naming one explicitly means a hard failure instead of a fallback. |
| `jellyfin.url` | **yes** | — | Base URL. Trailing slash is stripped. |
| `jellyfin.api_key` | **yes** | — | §3. Sent as `X-Emby-Token`. |
| `jellyfin.user_id` | **yes** | — | §3. Required by the playlist-items endpoint. |
| `tdarr.url` | **yes** | — | Base URL. |
| `tdarr.username` / `.password` | no | unset | Omit **both** if your Tdarr has auth disabled. |
| `tdarr.request_timeout_seconds` | no | `20` | Per-request timeout. |
| `tdarr.submit_timeout_seconds` | no | `21600` | **Accepted but unused.** Setting it does nothing. |
| `targets[].name` | **yes** | — | Device name; keys the directories. Must be unique. |
| `targets[].library_id` | **yes** | — | Default Tdarr library for this target's playlists. |
| `targets[].playlists[].playlist` | **yes** | — | Jellyfin playlist **name**, matched case-insensitively. |
| `targets[].playlists[].segment` | **yes** | — | Quality segment; becomes a path component your Tdarr flow branches on. |
| `targets[].playlists[].library_id` | no | target's | Override, if one library cannot watch both segment folders. |
| `path_maps` | no | none | `[{local, jellyfin}]` — only if the tool sees a folder at a different path than Jellyfin does. |
| `tdarr_path_maps` | no | none | `[{local, tdarr}]` — same, for Tdarr's view. |

**Directories are derived, not configured.** For target `T` and segment `S`:

```
input   <transcode_root>/<T>/<S>/<path relative to media_root>
output  <transcode_root>/<T>/sync/<S>/<same>      <- your Tdarr flow writes here
```

**Keep `transcode_root` under `media_root`.** In symlink mode the links are relative; between two
*sibling* roots a relative link resolves outside an exported SMB share, and Samba's default
`wide links = no` then hides the file from Tdarr entirely rather than showing a broken link. `doctor`
checks this.

## 5. Tdarr setup (yours to own)

Recommended: **one Tdarr library per device**, scanning that device's segment input folders. The flow
branches encode settings on the `/<segment>/` path component (`Check File Name Includes`, with
"include file directory"), and writes with **Keep Relative Path** to `<transcode_root>/<T>/sync`.

Four requirements this tool depends on:

- **The flow must keep its input** after transcoding. The input folders *are* the playlist mirror; if
  the flow deletes them, every cycle re-adds the file and Tdarr re-transcodes forever.
- **Enable Folder Watch on each library** (Library settings → Folder Watch). This is what notices new
  inputs *and* deleted ones — **retiring an item depends on it**. The tool's `scan-files` call only
  makes pickup of new inputs immediate; it is best-effort and never blocks anything.
- **The library must not process `<transcode_root>/<T>/sync`.** Point it at the segment folders, or
  filter out `/sync/`. Otherwise Tdarr transcodes its own output.
- **All three see the same paths**, or you need `path_maps` / `tdarr_path_maps`.

## 6. First run

```sh
# 1. secrets in the environment, never in the file
export JELLYFIN_API_KEY=... TDARR_USER=... TDARR_PASS=...

# 2. check everything before it touches anything
docker compose run --rm media-sync-manager doctor

# 3. see the plan without acting on it
docker compose run --rm media-sync-manager sync --dry-run

# 4. start the poller and the editor
docker compose up -d
```

Do not skip step 3. `--dry-run` prints exactly what would be added, removed and swept.

> **Argument order matters:** `--config` is a top-level option, so it goes *before* the subcommand —
> `media-sync-manager --config ./config.yaml web`, not `... web --config ./config.yaml`. The latter
> exits 2. (The `docker compose run` forms above omit it and use the default
> `/etc/media-sync-manager/config.yaml`.)

### Commands

| Command | What it does |
|---|---|
| `run` | The poller daemon. Reconciles every `poll_interval_seconds`. The container default. |
| `sync [--dry-run]` | One reconcile pass. `--dry-run` changes nothing. `--once` is accepted and is a no-op — `sync` is always a single pass. |
| `status` | Print the planned actions per target. Read-only; touches nothing. |
| `doctor` | Validate config, connectivity and library IDs, and probe how inputs can be created. **Writes and removes temp files under `transcode_root`.** |
| `web [--host H] [--port P]` | Serve the playlist editor. Default `0.0.0.0:8087`. |

Exit codes: `0` all good · `1` something failed (a target error, or an input/scan/unlink failure) ·
`2` bad config or bad arguments.

## 7. Reading `doctor`

Every line is `[OK ]` or `[FAIL]`. A `[FAIL]` anywhere makes the command exit `1`.

| Line | Means | If it fails |
|---|---|---|
| `jellyfin playlist '<name>'` | The playlist exists and Jellyfin answered. | Check the name matches exactly (matching is case-insensitive but not fuzzy), and that `url`/`api_key`/`user_id` are right. |
| `tdarr reachable + libraries listed: N libraries` | Tdarr answered and auth worked. | Check `tdarr.url`; omit `username`/`password` entirely if Tdarr has auth disabled. |
| `library_id '<id>': not found` | A configured `library_id` is not in Tdarr's list. | Copy the id from Tdarr; it is the library's `_id`, not its name. |
| `library '<id>' watches '<dir>'` | That library's source folder covers the input dir. Prints `tdarr_source=…` and `input(tdarr-view)=…` either way, so you can compare them. | If the two disagree, either point the library at the segment folders or add `tdarr_path_maps`. |
| `input mode: hardlink (…)` / `symlink (…)` | The probe result and the reason. `set explicitly; not probed` means you pinned `input_mode`. | If it fails, the message says whether neither primitive works — usually you are running over CIFS/SMB. See §2. |
| `transcode_root is under media_root` | Only checked in symlink mode. | Move `transcode_root` inside `media_root`. The failure text explains the SMB consequence. |

It also prints up to four `NOTE:` blocks. They are reminders, not failures:

- **probing coverage** (hardlink mode) — the probe covers `transcode_root` → each input dir, which is
  where union placement fails; it does not separately prove `media_root` → input dir.
- **symlink caveat** (symlink mode) — symlink inputs assume Tdarr reaches the media over a share that
  resolves links server-side. A Tdarr with local filesystem access would see real symlinks, which is
  untested.
- **flow must keep its input**, and must not process `<target>/sync`.
- **enable Folder Watch** — the one that bites most often. See §5 and §10.

## 8. Reading `status` and `sync`

Both print one line per planned action, prefixed with the target:

| Line | Means |
|---|---|
| `[T] add (<segment> <- <playlist>): <path>` | An input will be created and queued with Tdarr. |
| `[T] remove input: <path>` | That item left the playlist; its input goes. |
| `[T] delete output: <path>` | A transcoded file no longer backed by a playlist item; it gets swept. |
| `[T] skip: <reason>` | One item was skipped — no usable media source, or a path that no map covers. Everything else still proceeds. |
| `[T] in sync` | Nothing to do. |
| `[T] INCOMPLETE: <error> (removes + sweep suppressed)` | **A playlist failed to fetch.** Adds for the playlists that worked still ran; nothing was removed or swept for this target. This is the guard that stops a Jellyfin blip from emptying a device. |
| `[T] FAILED: <what>` | Something failed while applying. The message names its phase: `input …`, `scan-files lib=… `, `remove input …`, `delete output …`. |

A `FAILED: scan-files …` line is the mildest kind: the scan is best-effort and Folder Watch picks the
files up within ~30s. It still makes the command exit `1`, because whether it is harmless depends on
Folder Watch being enabled — which this tool cannot see.

## 9. The playlist editor

Jellyfin's own UI removes playlist items one at a time. This serves a page that does it in bulk.

`docker compose up -d` starts it on **port 8087**. Open `http://<host>:8087`.

> **There is no authentication.** Anyone who can reach the port can edit your playlists. Keep it on
> your LAN and never port-forward it.

**Using it:**

1. Pick a playlist. It renders grouped **show → season → episode**, sorted.
2. Select, by whichever is fewest clicks:
   - tick an **episode**;
   - tick a **season** or a **show** to take everything under it — these are tri-state, so a partial
     selection shows a dash rather than looking empty;
   - **Select first N** on any row takes that row and everything above it *within its group*. For the
     usual "watched S02E01–E10 and stopped", that is one click instead of ten. The label states the
     count and flips to **Clear first N** once that range is selected;
   - **Select all**.
3. **Remove selected (N)**, then confirm. There is no undo.

**Reading the result.** Jellyfin returns `204` even when nothing matched, so the page re-reads the
playlist and reports the change: `Removed 12 — list went 154 → 142.` If the server accepts a removal
and the list does not shrink, it says so instead of claiming success. Partial failures list the
errors and still refresh.

**Two things that surprise people:**

- A row badged **`×2`** appears twice in the playlist and shares one entry id with its twin, because
  Jellyfin's `PlaylistItemId` currently caches the media item's Guid. Removing it removes both copies.
  This is why a show's badge can read one higher than the number selecting it gives.
- A greyed-out row has no usable entry id and cannot be removed from here. Remove it in Jellyfin.

**What happens next.** The removal hits Jellyfin immediately. The poller retires that item's input
and its transcoded `sync/` output within `poll_interval_seconds`. Originals are never touched.

## 10. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Container restarts in a loop, logs show `config error: environment variable 'X' … is not set` | The whole config is expanded before any command runs, so **every** `${VAR}` must be set — including `TDARR_*` for the editor, which never calls Tdarr. | `docker compose logs media-sync-manager-web \| head` |
| Nothing is ever transcoded | Folder Watch is off, or the library is watching the wrong folder. | `doctor` — the `library '<id>' watches …` line. |
| Items removed from a playlist are never removed from the device | Folder Watch is off. It is what notices a *deleted* input; `scan-files` only helps new ones. | Tdarr → Library settings → Folder Watch |
| Files come back after every cycle, Tdarr re-transcodes forever | Your flow deletes its input. It must keep it. | §5 |
| Tdarr transcodes its own output | The library is scanning `<target>/sync`. | Point it at the segment folders, or filter `/sync/`. |
| `sync` exits 1 but everything looks fine | A `scan-files` call failed. Best-effort; Folder Watch covers it. | Look for `FAILED: scan-files` in the output. |
| One target says `INCOMPLETE` | A playlist failed to fetch — renamed, deleted, or Jellyfin blipped. Removals were suppressed **on purpose**. | The error is on the same line. Fix the name and re-run. |
| `cannot hardlink … EXDEV` | A union pool placed the destination on a different disk. | Leave `input_mode: auto`; it falls back to symlinks. |
| `neither hardlink nor symlink works` | You are running over CIFS/SMB. | Run it where the filesystem is local. §2 |
| Editor page loads but says `502` | Jellyfin is unreachable or the API key is wrong. | The message quotes the underlying error. |
| Editor page renders unstyled | Static assets are missing from the image. | Check `/bootstrap.min.css` and `/app.css` return `200`, not `404`. |
| A device folder went empty unexpectedly | This should not happen — an incomplete fetch suppresses removals. | Check for `INCOMPLETE` lines, and confirm the playlist is not actually empty. |

## 11. Upgrading and removing

**Upgrade**: images are published per tag. Pull and recreate:

```sh
docker compose pull && docker compose up -d
```

A version containing `-` (e.g. `1.2.0-rc1`) is published under its own tag but does **not** move
`:latest`.

**Removing the tool** leaves your library exactly as it was. What remains is everything under
`transcode_root` — the input links and the transcoded `sync/` outputs — which you can delete freely;
they are links and derived copies, never originals. Nothing under `media_root` was ever written.
