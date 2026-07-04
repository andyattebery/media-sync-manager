from __future__ import annotations

import pytest

from media_sync_manager import profile
from media_sync_manager.models import (
    Config,
    JellyfinConfig,
    MediaItem,
    Profile,
    TdarrConfig,
)

PROFILES = {
    "standard": Profile(name="standard", segment="standard"),
    "animation": Profile(
        name="animation",
        segment="animation",
        match_genres=frozenset({"animation", "anime", "children"}),
        match_tags=frozenset({"kids"}),
    ),
}


def _config():
    return Config(
        jellyfin=JellyfinConfig("http://jf", "k", "u"),
        tdarr=TdarrConfig("http://td"),
        media_root="/m",
        profiles=PROFILES,
        default_profile="standard",
        profile_priority=("animation", "standard"),
        devices=(),
    )


def _item(genres=(), tags=(), series_id=None) -> MediaItem:
    return MediaItem(id="i", name="i", type="Episode", series_id=series_id, genres=tuple(genres), tags=tuple(tags))


def _no_resolve(_series_id):
    raise AssertionError("resolver should not be called when episode has genres")


def test_animation_genre_match_case_insensitive():
    p = profile.classify(_item(genres=["ANIMATION"]), _config(), _no_resolve)
    assert p.name == "animation"


def test_animation_tag_match():
    p = profile.classify(_item(genres=["Comedy"], tags=["kids"]), _config(), _no_resolve)
    assert p.name == "animation"


def test_non_matching_genre_is_standard():
    p = profile.classify(_item(genres=["Drama", "Crime"]), _config(), _no_resolve)
    assert p.name == "standard"


def test_empty_genres_fails_safe_to_standard():
    p = profile.classify(_item(genres=[]), _config(), lambda s: [])
    assert p.name == "standard"


def test_episode_without_genres_resolves_from_series():
    item = _item(genres=[], series_id="s1")
    p = profile.classify(item, _config(), lambda s: ["Anime"] if s == "s1" else [])
    assert p.name == "animation"


def test_episode_with_own_genres_skips_series_lookup():
    item = _item(genres=["Animation"], series_id="s1")
    p = profile.classify(item, _config(), _no_resolve)  # would raise if called
    assert p.name == "animation"


def test_series_lookup_failure_fails_safe_to_standard():
    def boom(_s):
        raise RuntimeError("jellyfin down")

    p = profile.classify(_item(genres=[], series_id="s1"), _config(), boom)
    assert p.name == "standard"


def test_unknown_genre_string_is_standard():
    p = profile.classify(_item(genres=["Totally Made Up"]), _config(), _no_resolve)
    assert p.name == "standard"
