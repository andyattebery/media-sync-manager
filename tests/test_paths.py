from __future__ import annotations

import pytest

from media_sync_manager import paths
from media_sync_manager.errors import PathRemapError
from media_sync_manager.models import PathMap


def test_remap_identity_without_maps():
    assert paths.remap("/data/media/x.mkv", ()) == "/data/media/x.mkv"


def test_remap_longest_prefix_wins():
    maps = (
        PathMap("/data", "/A"),
        PathMap("/data/media", "/B"),  # longer, should win
    )
    assert paths.remap("/data/media/Show/ep.mkv", maps) == "/B/Show/ep.mkv"


def test_remap_no_match_raises_when_maps_configured():
    maps = (PathMap("/data/media", "/B"),)
    with pytest.raises(PathRemapError):
        paths.remap("/other/x.mkv", maps)


def test_remap_exact_prefix_dir():
    maps = (PathMap("/data/media", "/B"),)
    assert paths.remap("/data/media", maps) == "/B"


def test_to_glue_and_to_tdarr_round_trip():
    from media_sync_manager.models import Config, JellyfinConfig, TdarrConfig

    cfg = Config(
        jellyfin=JellyfinConfig("http://jf", "k", "u"),
        tdarr=TdarrConfig("http://td"),
        media_root="/mnt/pool/media",
        targets=(),
        path_maps=(PathMap("/data/media", "/mnt/pool/media"),),
        tdarr_path_maps=(PathMap("/mnt/pool/tdarr", "/mnt/tdarr"),),
    )
    glue = paths.to_glue("/data/media/TV/ep.mkv", cfg)
    assert glue == "/mnt/pool/media/TV/ep.mkv"
    tdarr = paths.to_tdarr("/mnt/pool/tdarr/in/iphone/standard/TV/ep.mkv", cfg)
    assert tdarr == "/mnt/tdarr/in/iphone/standard/TV/ep.mkv"


def test_source_rel_under_root():
    assert paths.source_rel("/mnt/pool/media/TV/Show/ep.mkv", "/mnt/pool/media") == "TV/Show/ep.mkv"


def test_source_rel_not_under_root_raises():
    with pytest.raises(PathRemapError):
        paths.source_rel("/elsewhere/ep.mkv", "/mnt/pool/media")


def test_rel_key_strips_extension_keeps_dirs():
    assert paths.rel_key("TV/Show/S01/ep.mkv") == "TV/Show/S01/ep"
    # different series, same basename -> different keys
    assert paths.rel_key("A/S01/01.mkv") != paths.rel_key("B/S01/01.mkv")
