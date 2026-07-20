"""Immutable data model: config, Jellyfin items, and the reconcile plan.

The quality flow ("segment") is chosen by which playlist an item is in, not by genre. The glue keeps
each Tdarr library's input folder mirrored to its playlist and lets Tdarr own transcode tracking;
input + output dirs are derived from `transcode_root` by convention.
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


# --- Reconcile plan ----------------------------------------------------------

@dataclass(frozen=True)
class AddInput:
    """Hardlink an original into a library input folder, then scan so Tdarr transcodes it."""

    relkey: str  # source_rel minus extension
    segment: str
    playlist: str
    source: str  # glue-view path of the original
    input_path: str  # glue-view path of the hardlink to create
    tdarr_path: str  # tdarr-view of input_path, passed to scan-files
    library_id: str


@dataclass(frozen=True)
class RemoveInput:
    """Delete an input hardlink whose item left the playlist (never a real original)."""

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
