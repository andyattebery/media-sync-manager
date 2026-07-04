"""Shared fixtures: a tmp media/in/out tree, Config builder, Target + MediaItem factories."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from media_sync_manager.models import (
    Config,
    JellyfinConfig,
    MediaItem,
    MediaSource,
    Target,
    TdarrConfig,
)


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
def make_target(env: SimpleNamespace):
    def _make(
        *,
        playlist: str = "PL",
        segment: str = "standard",
        device: str = "iphone",
        library_id: str | None = None,
        input_device: str | None = None,
    ) -> Target:
        return Target(
            playlist_name=playlist,
            segment=segment,
            output_dir=str(env.outdir / device),
            library_id=library_id or f"lib_{device}",
            input_dir=str(env.indir / (input_device or device)),
        )

    return _make


@pytest.fixture
def make_config(env: SimpleNamespace):
    def _make(targets, *, path_maps=(), tdarr_path_maps=()) -> Config:
        return Config(
            jellyfin=JellyfinConfig(url="http://jf", api_key="k", user_id="u"),
            tdarr=TdarrConfig(url="http://td"),
            media_root=str(env.media),
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
