"""Genre -> quality profile, failing safe toward quality (unknown/uncertain -> default)."""

from __future__ import annotations

from typing import Callable

from . import log
from .models import Config, MediaItem, Profile

_log = log.get("profile")

# Resolve a series' genres from its id. May raise; classify() treats any failure as "no genres".
SeriesGenreResolver = Callable[[str], list[str]]


def _effective_genres(item: MediaItem, resolve: SeriesGenreResolver) -> list[str]:
    if item.genres:
        return list(item.genres)
    if item.series_id:
        try:
            return list(resolve(item.series_id))
        except Exception as exc:  # fail-safe: a fetch failure must not over-compress
            _log.warning("series-genre lookup failed for %s (%s); failing safe", item.series_id, exc)
            return []
    return []


def classify(item: MediaItem, config: Config, resolve: SeriesGenreResolver) -> Profile:
    """Pick the profile for an item.

    Walk profile_priority; the first profile whose match genres/tags hit wins. Missing, empty, or
    unresolvable genres -> default_profile (fail safe toward quality).
    """
    genres = {g.casefold() for g in _effective_genres(item, resolve)}
    tags = {t.casefold() for t in item.tags}

    for name in config.profile_priority:
        prof = config.profiles.get(name)
        if prof is None:
            continue
        if (prof.match_genres and prof.match_genres & genres) or (
            prof.match_tags and prof.match_tags & tags
        ):
            return prof

    return config.profiles[config.default_profile]
