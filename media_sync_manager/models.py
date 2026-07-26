"""Immutable data model: config, Jellyfin items, and the reconcile plan.

The quality flow ("segment") is chosen by which playlist an item is in, not by genre. The glue keeps
each Tdarr library's input folder mirrored to its playlist and lets Tdarr own transcode tracking;
input + output dirs are derived from `transcode_root` by convention (see paths.py).
"""

from __future__ import annotations

from dataclasses import dataclass


# --- Jellyfin domain ---------------------------------------------------------

@dataclass(frozen=True)
class MediaSource:
    path: str | None
    size: int | None = None


@dataclass(frozen=True)
class MediaItem:
    id: str
    name: str
    type: str  # "Episode" | "Movie" | ...
    media_sources: tuple[MediaSource, ...] = ()


# --- Playlist editing (web UI only; never consumed by reconcile) -------------
#
# Deliberately separate from MediaItem. The sync path asks Jellyfin for
# `fields=Path,MediaSources`; the editor asks for neither and needs series/season
# metadata instead, so one class would make "series_name is None" ambiguous
# between "this is a Movie" and "we didn't request that field".

@dataclass(frozen=True)
class PlaylistSummary:
    """A playlist as the picker sees it. Distinct from `Playlist` below, which is a config row."""

    id: str
    name: str


@dataclass(frozen=True)
class PlaylistEntry:
    """One row of a Jellyfin playlist, for browsing and removal only.

    `playlist_item_id` is the ONLY value valid as an `entryIds` argument to the removal endpoint.
    There is deliberately no attribute named `id`: passing the *media item's* id to the delete call
    is the obvious bug here, and it must not be spellable. It happens to work on today's Jellyfin
    (PlaylistItemId caches the item Guid) and would break silently the day that changes.
    """

    playlist_item_id: str  # PlaylistItemId, falling back to Id; "" when unaddressable
    item_id: str  # display/debug only
    name: str
    type: str  # "Episode" | "Movie" | ...
    series_id: str | None = None
    series_name: str | None = None
    season_id: str | None = None
    season_name: str | None = None
    season_number: int | None = None  # ParentIndexNumber
    episode_number: int | None = None  # IndexNumber

    @property
    def removable(self) -> bool:
        return bool(self.playlist_item_id)


@dataclass(frozen=True)
class RemovalResult:
    """Outcome of a (chunked) playlist removal.

    Distinct from `CycleResult` below: that one is about applying a sync plan to the filesystem,
    this one is about one HTTP mutation against Jellyfin. `removed` means "the server accepted it",
    not "it is verifiably gone" — Jellyfin answers 204 even when no entryId matched.
    """

    requested: int
    removed: int
    failed: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeasonGroup:
    key: str  # "<show_key>|<n>" or "<show_key>|none"
    title: str
    number: int | None
    entries: tuple[PlaylistEntry, ...] = ()

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ShowGroup:
    key: str  # "series:<id>" | "series:name:<casefold>" | "type:<Type>"
    title: str
    kind: str  # "series" | "type"
    seasons: tuple[SeasonGroup, ...] = ()

    @property
    def count(self) -> int:
        return sum(s.count for s in self.seasons)


# --- Config ------------------------------------------------------------------

@dataclass(frozen=True)
class PathMap:
    src: str  # the namespace a path arrives in
    dst: str  # the namespace we want it in (config.py fills these from the friendly YAML)


@dataclass(frozen=True)
class JellyfinConfig:
    url: str
    api_key: str
    user_id: str


@dataclass(frozen=True)
class TdarrConfig:
    url: str
    username: str | None = None
    password: str | None = None
    request_timeout_seconds: int = 20
    submit_timeout_seconds: int = 21600


@dataclass(frozen=True)
class Playlist:
    """One Jellyfin playlist -> one quality segment (and optionally a specific Tdarr library)."""

    playlist_name: str
    segment: str
    library_id: str | None = None  # falls back to the target's library_id


@dataclass(frozen=True)
class Target:
    """A device: a name (keys `<transcode_root>/<name>/...`), a default library, and its playlists.

    Derived dirs: input = `<transcode_root>/<name>/<segment>`; output = `<transcode_root>/<name>/sync`.
    """

    name: str
    library_id: str
    playlists: tuple[Playlist, ...]


@dataclass(frozen=True)
class Config:
    jellyfin: JellyfinConfig
    tdarr: TdarrConfig
    media_root: str
    transcode_root: str
    targets: tuple[Target, ...]
    path_maps: tuple[PathMap, ...] = ()
    tdarr_path_maps: tuple[PathMap, ...] = ()
    poll_interval_seconds: int = 45
    # "auto" (probe the filesystem) | "hardlink" | "symlink". See fsops.detect_mode.
    input_mode: str = "auto"


# --- Reconcile plan ----------------------------------------------------------

@dataclass(frozen=True)
class AddInput:
    """Point an input at an original in a library input folder, then scan so Tdarr transcodes it."""

    relkey: str  # source_rel minus extension
    segment: str
    playlist: str
    source: str  # glue-view path of the original
    input_path: str  # glue-view path of the input to create (hardlink or symlink)
    tdarr_path: str  # tdarr-view of input_path, passed to scan-files
    library_id: str


@dataclass(frozen=True)
class RemoveInput:
    """Delete an input whose item left the playlist (never a real original)."""

    input_path: str


@dataclass(frozen=True)
class DeleteOutput:
    """Delete a `sync/` output no longer backed by a desired item (req 9)."""

    path: str


@dataclass(frozen=True)
class TargetPlan:
    target: str  # target name
    adds: tuple[AddInput, ...] = ()
    removes: tuple[RemoveInput, ...] = ()
    deletes: tuple[DeleteOutput, ...] = ()
    skipped: tuple[str, ...] = ()  # human-readable reasons (no source, unmappable path, ...)
    error: str | None = None  # a playlist failed to fetch -> removes + sweep suppressed

    @property
    def touched(self) -> bool:
        return bool(self.adds or self.removes or self.deletes)


@dataclass(frozen=True)
class CycleResult:
    """A plan plus what happened when it was applied.

    TargetPlan is built by reconcile *before* anything is executed, so execute-time failures have
    nowhere to live on it. They land here instead, which is what lets `sync` report (and exit
    non-zero on) inputs that could not be created.
    """

    plan: TargetPlan
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.plan.error is None and not self.failures
