from __future__ import annotations

import copy

import pytest

from media_sync_manager import config as config_mod
from media_sync_manager.errors import ConfigError

RAW = {
    "jellyfin": {"url": "http://jf", "api_key": "k", "user_id": "u"},
    "tdarr": {"url": "http://td"},
    "media_root": "/mnt/pool/media",
    "targets": [
        {
            "playlist_name": "2D Animation",
            "segment": "animation",
            "output_dir": "/out/iphone",
            "library_id": "lib_iphone",
            "input_dir": "/in/iphone",
        },
        {
            "playlist_name": "Standard",
            "segment": "standard",
            "output_dir": "/out/iphone",
            "library_id": "lib_iphone",
            "input_dir": "/in/iphone",
        },
    ],
}


def _raw(**overrides):
    d = copy.deepcopy(RAW)
    d.update(overrides)
    return d


def test_parse_valid():
    cfg = config_mod.parse(_raw())
    assert len(cfg.targets) == 2
    assert cfg.targets[0].segment == "animation"
    assert cfg.targets[0].playlist_name == "2D Animation"
    assert cfg.targets[1].segment == "standard"


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "secret123")
    p = tmp_path / "config.yaml"
    p.write_text(
        """
jellyfin: {url: "http://jf", api_key: "${JELLYFIN_API_KEY}", user_id: "u"}
tdarr: {url: "http://td"}
media_root: /m
targets:
  - {playlist_name: PL, segment: standard, output_dir: /o, library_id: lib, input_dir: /i}
"""
    )
    cfg = config_mod.load(p)
    assert cfg.jellyfin.api_key == "secret123"


def test_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    p = tmp_path / "config.yaml"
    p.write_text(
        """
jellyfin: {url: "http://jf", api_key: "${NOPE}", user_id: "u"}
tdarr: {url: "http://td"}
media_root: /m
targets:
  - {playlist_name: PL, segment: standard, output_dir: /o, library_id: lib, input_dir: /i}
"""
    )
    with pytest.raises(ConfigError):
        config_mod.load(p)


def test_target_missing_input_dir_raises():
    bad = _raw()
    del bad["targets"][0]["input_dir"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_target_missing_library_id_raises():
    bad = _raw()
    del bad["targets"][0]["library_id"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_target_missing_segment_raises():
    bad = _raw()
    del bad["targets"][0]["segment"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_empty_targets_raises():
    with pytest.raises(ConfigError):
        config_mod.parse(_raw(targets=[]))
