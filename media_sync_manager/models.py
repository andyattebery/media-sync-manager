"""Immutable data model: config, Jellyfin items, and the reconcile plan.

The quality flow ("segment") is chosen by which playlist an item is in, not by genre — each config
target maps one playlist to one segment + output + Tdarr library.
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
    src: str  # "from": how the source system names a path
    dst: str  # "to": how this system names the same bytes


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
class Target:
    """One playlist -> one quality segment + one device output + one Tdarr library.

    Targets that share an `output_dir` are one device (e.g. its '2D Animation' + 'Standard'
    playlists); reconcile groups them so neither deletes the other's files.
    """

    playlist_name: str
    segment: str
    output_dir: str
    library_id: str
    input_dir: str


@dataclass(frozen=True)
class Config:
    jellyfin: JellyfinConfig
    tdarr: TdarrConfig
    media_root: str
    targets: tuple[Target, ...]
    path_maps: tuple[PathMap, ...] = ()
    tdarr_path_maps: tuple[PathMap, ...] = ()
    poll_interval_seconds: int = 45


# --- Reconcile plan ----------------------------------------------------------

@dataclass(frozen=True)
class Submit:
    """A new item to push: hardlink the original into the input folder, then scan."""

    relkey: str  # source path relative to media_root, minus extension
    segment: str
    playlist: str
    source: str  # glue-view path of the original
    input_path: str  # glue-view path of the hardlink to create
    tdarr_path: str  # tdarr-view of input_path, passed to scan-files
    library_id: str

    @property
    def match_key(self) -> str:
        """The output identity: <segment>/<relkey> (the flow prepends the segment folder)."""
        return f"{self.segment}/{self.relkey}"


@dataclass(frozen=True)
class DeleteOutput:
    """An orphan output to remove from a device folder (never a source/original)."""

    path: str  # glue-view path of the file to delete
    match_key: str


@dataclass(frozen=True)
class GroupPlan:
    """Reconcile result for one output_dir (all targets that write to it)."""

    output_dir: str
    submits: tuple[Submit, ...] = ()
    deletes: tuple[DeleteOutput, ...] = ()
    skipped: tuple[str, ...] = ()  # human-readable reasons (no source, in-flight, ...)
    error: str | None = None  # a target in this group failed to fetch -> deletes suppressed

    @property
    def touched(self) -> bool:
        return bool(self.submits or self.deletes)
