"""Shared fixtures: a tmp media/transcode tree (one fs), Config + Target/Playlist/MediaItem factories."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_sync_manager.models import (
    Config,
    JellyfinConfig,
    MediaItem,
    MediaSource,
    Playlist,
    Target,
    TdarrConfig,
)


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    """media/ and transcode/ under one tmp_path (same filesystem -> hardlinks work)."""
    media = tmp_path / "media"
    transcode = tmp_path / "transcode"
    media.mkdir()
    transcode.mkdir()
    return SimpleNamespace(tmp=tmp_path, media=media, transcode=transcode)


@pytest.fixture
def make_playlist():
    def _make(playlist_name: str, segment: str, library_id: str | None = None) -> Playlist:
        return Playlist(playlist_name=playlist_name, segment=segment, library_id=library_id)

    return _make


@pytest.fixture
def make_target():
    def _make(name: str = "iphone", *, playlists, library_id: str | None = None) -> Target:
        return Target(name=name, library_id=library_id or f"lib_{name}", playlists=tuple(playlists))

    return _make


@pytest.fixture
def make_config(env: SimpleNamespace):
    def _make(targets, *, path_maps=(), tdarr_path_maps=()) -> Config:
        return Config(
            jellyfin=JellyfinConfig(url="http://jf", api_key="k", user_id="u"),
            tdarr=TdarrConfig(url="http://td"),
            media_root=str(env.media),
            transcode_root=str(env.transcode),
            targets=tuple(targets),
            path_maps=path_maps,
            tdarr_path_maps=tdarr_path_maps,
        )

    return _make


@pytest.fixture
def write_source(env: SimpleNamespace):
    """Create a source file under media_root; return its absolute (glue-view) path."""

    def _write(rel: str, content: bytes = b"x") -> str:
        p = env.media / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return str(p)

    return _write


@pytest.fixture
def make_episode():
    def _make(source_path: str, *, size: int | None = 100, name: str | None = None) -> MediaItem:
        return MediaItem(
            id=source_path,
            name=name or Path(source_path).stem,
            type="Episode",
            media_sources=(MediaSource(path=source_path, size=size),),
        )

    return _make
