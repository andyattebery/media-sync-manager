> **Superseded — historical record.** The original handoff brief, kept for provenance. Current
> design lives in [docs/](docs/); how to work on the project is [docs/development.md](docs/development.md).

# media-sync-manager — Requirements (handoff for Claude Code)

You have access to the Jellyfin and Tdarr APIs and can explore them directly. This document is
**what I want**, not how to build it — figure out endpoints, fields, flow mechanics, and structure
yourself. Ask me if a requirement is ambiguous.

## Goal
A Plex-Sync equivalent for Jellyfin: I select content in Jellyfin and get space-efficient
**transcoded** copies onto my iOS devices for offline/travel viewing. Build the glue that connects
my Jellyfin selection to my **existing Tdarr** instance and keeps each device's copy set in sync
with what I picked.

## Pipeline (the shape I've decided on — keep it)
Jellyfin playlist (selection) → **togo-sync** (the glue you're building) → Tdarr (transcode, already
set up) → SMB share → Infuse on the device.

## Requirements
1. **Selection lives in Jellyfin.** I curate a playlist per device in the Jellyfin UI (I can add whole
   seasons/series at once). Day-to-day selection must stay GUI-only; CLI is fine for setup.
2. **Multi-device.** Multiple playlists, each mapping to its own output destination, all defined in a
   **config file**. Devices are independent — the same item may be on several, handled separately.
3. **Transcoding is mandatory.** My sources are mostly BDMV remuxes, so downloading originals isn't
   viable. I own the ffmpeg recipe (quality-for-size). Output must play well in Infuse: **MKV**, keep
   image/PGS subtitles, downmix lossless audio (TrueHD/DTS-HD) to a sane lossy track.
4. **Use my existing Tdarr as the transcode engine** — don't replace it or reimplement queuing/
   encoding. The glue feeds Tdarr; Tdarr does the work.
5. **Never modify or delete my original media.**
6. **Output to a per-device SMB-served location** that Infuse can download from. Infuse isn't strictly
   required, but it's the target client.
7. **Genre-based quality profiles.** Animation/kids content can be transcoded more aggressively
   (smaller); everything else gets my standard/higher-quality settings. Decide the profile from
   Jellyfin metadata (genres, with official rating/tags as backup).
   - **Fail safe toward quality:** genre data is crowd-sourced and unreliable. When classification is
     missing or uncertain, default to the **higher-quality** profile. A false negative (a bigger file)
     is acceptable; never over-compress something by mistake.
   - Genre strings are **provider-dependent** (my libraries use TheTVDB), so build the match list from
     my actual library, not a hardcoded taxonomy.
8. **Responsiveness.** "Sync before a trip" is often last-minute, so new playlist additions should move
   through the pipeline quickly — minimal pickup latency, not a slow cron. (The encode time itself is
   the only unavoidable wait.)
9. **Lifecycle = playlist is source of truth.** Removing an item from a playlist should remove it from
   that device's set.
10. **Keep the glue small.** Its only jobs: read the playlists, decide each item's profile, get files
    to Tdarr, and keep each destination in sync with its playlist. Don't add machinery it doesn't need
    (e.g. no database if the filesystem already tells you the state).

## Things I already confirmed (use as hints; verify against the live APIs)
- Tdarr can't read Jellyfin's genre, so classification has to happen in the glue / be expressed as
  routing into the right Tdarr library or flow.
- Adding a season/series to a Jellyfin playlist expands it into individual episode items with file
  paths — no expansion logic needed on your side.
- Episodes usually don't carry genres; genres live on the parent **series** — resolve from there.
- The Jellyfin playlist-items endpoint needs a `userId` even when using an API key.
- iOS downloads only run in the foreground with the screen on — that's accepted device behavior, not
  something to engineer around.

## What I own (not your job)
- The ffmpeg recipe(s) / Tdarr flow and the actual quality settings per profile.
- Creating the Jellyfin playlists and the SMB shares.
- Tuning the final genre→profile match lists (scaffold sensible defaults; I'll adjust to my library).
