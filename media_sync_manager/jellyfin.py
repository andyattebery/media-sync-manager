"""Jellyfin REST client. Dumb transport: HTTP in, dataclasses out, no business logic.

Auth uses the single-header `X-Emby-Token` scheme (avoids the `MediaBrowser` scheme's required
Client/DeviceId fields). The playlist-items endpoint requires `userId` even with an API key.
"""

from __future__ import annotations

import time
from typing import Callable

import requests

from . import log
from .errors import TransientError
from .models import JellyfinConfig, MediaItem, MediaSource

_log = log.get("jellyfin")

_ITEM_FIELDS = "Path,MediaSources,SeriesId,SeasonId,Genres,Tags,OfficialRating"


def _parse_item(raw: dict) -> MediaItem:
    sources = tuple(
        MediaSource(path=ms.get("Path"), size=ms.get("Size"))
        for ms in (raw.get("MediaSources") or [])
    )
    # Fall back to the top-level Path as a single source when MediaSources is absent.
    if not sources and raw.get("Path"):
        sources = (MediaSource(path=raw["Path"], size=raw.get("Size")),)
    return MediaItem(
        id=str(raw.get("Id", "")),
        name=str(raw.get("Name", "")),
        type=str(raw.get("Type", "")),
        series_id=raw.get("SeriesId"),
        genres=tuple(raw.get("Genres") or ()),
        tags=tuple(raw.get("Tags") or ()),
        official_rating=raw.get("OfficialRating"),
        media_sources=sources,
    )


class JellyfinClient:
    def __init__(
        self,
        config: JellyfinConfig,
        *,
        timeout: int = 20,
        genre_cache_ttl: int = 900,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._timeout = timeout
        self._ttl = genre_cache_ttl
        self._clock = clock
        self._session = session or requests.Session()
        self._session.headers.update(
            {"X-Emby-Token": config.api_key, "Accept": "application/json"}
        )
        self._genre_cache: dict[str, tuple[float, list[str]]] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._cfg.url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise TransientError(f"jellyfin GET {path} failed: {exc}") from exc

    def find_playlist(self, name: str) -> str:
        """Return the Id of the playlist whose Name matches. Raise TransientError if missing.

        Treating "not found" as transient is deliberate: a playlist mid-rename must not trigger a
        device-folder purge (see reconcile).
        """
        data = self._get(
            f"/Users/{self._cfg.user_id}/Items",
            {"IncludeItemTypes": "Playlist", "Recursive": "true"},
        )
        items = data.get("Items") or []
        for it in items:
            if it.get("Name") == name:
                return str(it["Id"])
        for it in items:  # case-insensitive fallback
            if str(it.get("Name", "")).casefold() == name.casefold():
                return str(it["Id"])
        raise TransientError(f"playlist not found: {name!r}")

    def playlist_items(self, playlist_id: str) -> list[MediaItem]:
        data = self._get(
            f"/Playlists/{playlist_id}/Items",
            {"userId": self._cfg.user_id, "fields": _ITEM_FIELDS},
        )
        return [_parse_item(it) for it in (data.get("Items") or [])]

    def series_genres(self, series_id: str) -> list[str]:
        """Genres of a series, memoised with TTL across cycles."""
        now = self._clock()
        cached = self._genre_cache.get(series_id)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        data = self._get(
            f"/Items/{series_id}",
            {"userId": self._cfg.user_id, "fields": "Genres,Tags,OfficialRating"},
        )
        genres = list(data.get("Genres") or [])
        self._genre_cache[series_id] = (now, genres)
        return genres
