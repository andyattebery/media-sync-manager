"""Shared fixtures: a tmp media/in/out tree, Config builder, and MediaItem factories."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_sync_manager.models import (
    Config,
    Device,
    JellyfinConfig,
    MediaItem,
    MediaSource,
    Profile,
    TdarrConfig,
)

PROFILES = {
    "standard": Profile(name="standard", segment="standard"),
    "animation": Profile(
        name="animation",
        segment="animation",
        match_genres=frozenset({"animation", "anime", "children", "cartoon", "family"}),
        match_tags=frozenset({"anime", "kids"}),
    ),
}


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    """A tmp filesystem with media/, in/, out/ roots (all on one fs -> hardlinks work)."""
    media = tmp_path / "media"
    indir = tmp_path / "in"
    outdir = tmp_path / "out"
    for d in (media, indir, outdir):
        d.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(tmp=tmp_path, media=media, indir=indir, outdir=outdir)


@pytest.fixture
def make_device(env: SimpleNamespace):
    def _make(name: str = "iphone", playlist: str = "PL") -> Device:
        return Device(
            name=name,
            playlist_name=playlist,
            output_dir=str(env.outdir / name),
            library_id=f"lib_{name}",
            input_dir=str(env.indir / name),
        )

    return _make


@pytest.fixture
def make_config(env: SimpleNamespace):
    def _make(devices, *, path_maps=(), tdarr_path_maps=()) -> Config:
        return Config(
            jellyfin=JellyfinConfig(url="http://jf", api_key="k", user_id="u"),
            tdarr=TdarrConfig(url="http://td"),
            media_root=str(env.media),
            profiles=PROFILES,
            default_profile="standard",
            profile_priority=("animation", "standard"),
            devices=tuple(devices),
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
    def _make(
        source_path: str,
        *,
        series_id: str | None = "series1",
        genres=(),
        tags=(),
        size: int | None = 100,
        name: str | None = None,
    ) -> MediaItem:
        return MediaItem(
            id=source_path,
            name=name or Path(source_path).stem,
            type="Episode",
            series_id=series_id,
            genres=tuple(genres),
            tags=tuple(tags),
            media_sources=(MediaSource(path=source_path, size=size),),
        )

    return _make
