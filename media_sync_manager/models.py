"""Immutable data model: config, Jellyfin items, and the reconcile plan."""

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
    series_id: str | None = None
    genres: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    official_rating: str | None = None
    media_sources: tuple[MediaSource, ...] = ()


# --- Config ------------------------------------------------------------------

@dataclass(frozen=True)
class PathMap:
    src: str  # "from": how the source system names a path
    dst: str  # "to": how this system names the same bytes


@dataclass(frozen=True)
class Profile:
    name: str
    segment: str
    match_genres: frozenset[str] = frozenset()  # casefolded
    match_tags: frozenset[str] = frozenset()  # casefolded


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
class Device:
    name: str
    playlist_name: str
    output_dir: str
    library_id: str
    input_dir: str


@dataclass(frozen=True)
class Config:
    jellyfin: JellyfinConfig
    tdarr: TdarrConfig
    media_root: str
    profiles: dict[str, Profile]
    default_profile: str
    profile_priority: tuple[str, ...]
    devices: tuple[Device, ...]
    path_maps: tuple[PathMap, ...] = ()
    tdarr_path_maps: tuple[PathMap, ...] = ()
    poll_interval_seconds: int = 45
    genre_cache_ttl_seconds: int = 900


# --- Reconcile plan ----------------------------------------------------------

@dataclass(frozen=True)
class Submit:
    """A new item to push: hardlink the original into the input folder, then scan."""

    relkey: str
    source: str  # glue-view path of the original
    input_path: str  # glue-view path of the hardlink to create
    tdarr_path: str  # tdarr-view of input_path, passed to scan-files
    library_id: str
    profile: str


@dataclass(frozen=True)
class DeleteOutput:
    """An orphan output to remove from a device folder (never a source/original)."""

    path: str  # glue-view path of the file to delete
    relkey: str


@dataclass(frozen=True)
class DevicePlan:
    device: str
    submits: tuple[Submit, ...] = ()
    deletes: tuple[DeleteOutput, ...] = ()
    skipped: tuple[str, ...] = ()  # human-readable reasons (no source, in-flight, ...)
    error: str | None = None  # set when the device was skipped wholesale (transient)

    @property
    def touched(self) -> bool:
        return bool(self.submits or self.deletes)
