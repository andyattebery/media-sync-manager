from __future__ import annotations

import copy

import pytest

from media_sync_manager import config as config_mod
from media_sync_manager.errors import ConfigError

RAW = {
    "jellyfin": {"url": "http://jf", "api_key": "k", "user_id": "u"},
    "tdarr": {"url": "http://td"},
    "media_root": "/mnt/pool/media",
    "profiles": {
        "standard": {"segment": "standard"},
        "animation": {"segment": "animation", "match": {"genres": ["Animation"], "tags": ["kids"]}},
    },
    "default_profile": "standard",
    "profile_priority": ["animation", "standard"],
    "devices": [
        {
            "name": "iphone",
            "playlist_name": "Travel - Phone",
            "output_dir": "/out/iphone",
            "library_id": "lib_iphone",
            "input_dir": "/in/iphone",
        }
    ],
}


def _raw(**overrides):
    d = copy.deepcopy(RAW)
    d.update(overrides)
    return d


def test_parse_valid():
    cfg = config_mod.parse(_raw())
    assert cfg.default_profile == "standard"
    assert cfg.profiles["animation"].match_genres == frozenset({"animation"})
    assert cfg.profiles["animation"].match_tags == frozenset({"kids"})
    assert cfg.devices[0].library_id == "lib_iphone"


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "secret123")
    p = tmp_path / "config.yaml"
    p.write_text(
        """
jellyfin: {url: "http://jf", api_key: "${JELLYFIN_API_KEY}", user_id: "u"}
tdarr: {url: "http://td"}
media_root: /m
profiles: {standard: {segment: standard}}
default_profile: standard
profile_priority: [standard]
devices:
  - {name: a, playlist_name: PL, output_dir: /o, library_id: lib, input_dir: /i}
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
profiles: {standard: {segment: standard}}
default_profile: standard
profile_priority: [standard]
devices:
  - {name: a, playlist_name: PL, output_dir: /o, library_id: lib, input_dir: /i}
"""
    )
    with pytest.raises(ConfigError):
        config_mod.load(p)


def test_default_profile_must_exist():
    with pytest.raises(ConfigError):
        config_mod.parse(_raw(default_profile="missing"))


def test_device_missing_input_dir_raises():
    bad = _raw()
    del bad["devices"][0]["input_dir"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_device_missing_library_id_raises():
    bad = _raw()
    del bad["devices"][0]["library_id"]
    with pytest.raises(ConfigError):
        config_mod.parse(bad)


def test_unknown_profile_in_priority_raises():
    with pytest.raises(ConfigError):
        config_mod.parse(_raw(profile_priority=["animation", "ghost"]))


def test_empty_devices_raises():
    with pytest.raises(ConfigError):
        config_mod.parse(_raw(devices=[]))
