from __future__ import annotations

import copy

import pytest

from media_sync_manager import config as config_mod
from media_sync_manager.errors import ConfigError
from media_sync_manager.models import PathMap

RAW = {
    "jellyfin": {"url": "http://jf", "api_key": "k", "user_id": "u"},
    "tdarr": {"url": "http://td"},
    "media_root": "/media",
    "transcode_root": "/media/Transcode Videos",
    "targets": [
        {
            "name": "iphone",
            "library_id": "lib_iphone",
            "playlists": [
                {"playlist": "2D Animation", "segment": "animation"},
                {"playlist": "Standard", "segment": "standard"},
            ],
        }
    ],
}


def _raw(**overrides):
    d = copy.deepcopy(RAW)
    d.update(overrides)
    return d


def test_parse_valid():
    cfg = config_mod.parse(_raw())
    assert cfg.transcode_root == "/media/Transcode Videos"
    assert len(cfg.targets) == 1
    t = cfg.targets[0]
    assert t.name == "iphone"
    assert t.library_id == "lib_iphone"
    assert [(p.playlist_name, p.segment) for p in t.playlists] == [
        ("2D Animation", "animation"),
        ("Standard", "standard"),
    ]
    assert t.playlists[0].library_id is None


def test_input_mode_defaults_to_auto_and_validates():
    assert config_mod.parse(_raw()).input_mode == "auto"
    assert config_mod.parse(_raw(input_mode="symlink")).input_mode == "symlink"
    with pytest.raises(ConfigError, match="input_mode"):
        config_mod.parse(_raw(input_mode="reflink"))


def test_per_playlist_library_id_override():
    raw = _raw()
    raw["targets"][0]["playlists"][0]["library_id"] = "lib_anim"
    cfg = config_mod.parse(raw)
    assert cfg.targets[0].playlists[0].library_id == "lib_anim"


def test_friendly_path_maps_translate_direction():
    cfg = config_mod.parse(
        _raw(
            path_maps=[{"local": "/media", "jellyfin": "/data/media"}],
            tdarr_path_maps=[{"local": "/media", "tdarr": "/mnt/tdarr"}],
        )
    )
    assert cfg.path_maps == (PathMap(src="/data/media", dst="/media"),)  # jellyfin -> local
    assert cfg.tdarr_path_maps == (PathMap(src="/media", dst="/mnt/tdarr"),)  # local -> tdarr


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "secret123")
    p = tmp_path / "config.yaml"
    p.write_text(
        """
jellyfin: {url: "http://jf", api_key: "${JELLYFIN_API_KEY}", user_id: "u"}
tdarr: {url: "http://td"}
media_root: /media
transcode_root: /media/Transcode
targets:
  - name: iphone
    library_id: lib
    playlists:
      - {playlist: PL, segment: standard}
"""
    )
    cfg = config_mod.load(p)
    assert cfg.jellyfin.api_key == "secret123"


def test_missing_transcode_root_raises():
    bad = _raw()
    del bad["transcode_root"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_target_missing_library_id_raises():
    bad = _raw()
    del bad["targets"][0]["library_id"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_playlist_missing_segment_raises():
    bad = _raw()
    del bad["targets"][0]["playlists"][0]["segment"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_duplicate_target_name_raises():
    bad = _raw()
    bad["targets"].append(copy.deepcopy(bad["targets"][0]))
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_empty_targets_raises():
    with pytest.raises(ConfigError):
        config_mod.parse(_raw(targets=[]))


def test_empty_playlists_raises():
    bad = _raw()
    bad["targets"][0]["playlists"] = []
    with pytest.raises(ConfigError):
        config_mod.parse(bad)
