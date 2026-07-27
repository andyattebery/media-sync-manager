"""Jellyfin REST client. Dumb transport: HTTP in, dataclasses out, no business logic.

Auth uses the single-header `X-Emby-Token` scheme (avoids the `MediaBrowser` scheme's required
Client/DeviceId fields). The playlist-items endpoint requires `userId` even with an API key.
"""

from __future__ import annotations

from typing import Sequence

import requests

from . import log
from .errors import TransientError
from .models import (
    JellyfinConfig,
    MediaItem,
    MediaSource,
    PlaylistEntry,
    PlaylistSummary,
    RemovalResult,
)

_log = log.get("jellyfin")

_ITEM_FIELDS = "Path,MediaSources"

# entryIds go in the query string. Ids are 32 hex chars (33 joined), so 50 keeps the URL near 1.7 kB
# — well under the conservative 2048-char limit and Kestrel's 8192-byte request-line cap.
_ENTRY_CHUNK = 50


def _guid(value: object) -> str:
    """Normalise an id to the dashless "N" form the server compares entryIds against."""
    return str(value).replace("-", "").strip() if value else ""


def _opt_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_str(value: object) -> str | None:
    return str(value) if value else None


def _parse_entry(raw: dict) -> PlaylistEntry:
    """Map a playlist item DTO to a PlaylistEntry.

    `PlaylistItemId` is set unconditionally by PlaylistsController (it is not an ItemFields option),
    but fall back to the normalised `Id` if a server omits it. If neither is present the entry is
    left unaddressable rather than given a value we can't verify.
    """
    item_id = _guid(raw.get("Id"))
    return PlaylistEntry(
        playlist_item_id=_guid(raw.get("PlaylistItemId")) or item_id,
        item_id=item_id,
        name=str(raw.get("Name") or ""),
        type=str(raw.get("Type") or ""),
        series_id=_guid(raw.get("SeriesId")) or None,
        series_name=_opt_str(raw.get("SeriesName")),
        season_id=_guid(raw.get("SeasonId")) or None,
        season_name=_opt_str(raw.get("SeasonName")),
        season_number=_opt_int(raw.get("ParentIndexNumber")),
        episode_number=_opt_int(raw.get("IndexNumber")),
    )


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
        media_sources=sources,
    )


class JellyfinClient:
    def __init__(
        self,
        config: JellyfinConfig,
        *,
        timeout: int = 20,
        session: requests.Session | None = None,
    ) -> None:
        self._cfg = config
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update(
            {"X-Emby-Token": config.api_key, "Accept": "application/json"}
        )

    @property
    def base_url(self) -> str:
        """The Jellyfin server's base URL, for linking back to it from the editor UI."""
        return self._cfg.url

    def _request(self, method: str, path: str, params: dict | None = None) -> dict | None:
        url = f"{self._cfg.url}{path}"
        try:
            resp = self._session.request(method, url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            # The removal endpoint answers 204 with an empty body, so skip the decode rather than
            # raise and catch for nothing. (An earlier comment here claimed JSONDecodeError is not a
            # RequestException and would escape uncaught — it is one, via InvalidJSONError, so it
            # would have been caught and mislabelled a transport failure. Same wrong assumption, in
            # the opposite direction, cost the Tdarr client a real bug: see tdarr.py::_post.)
            return resp.json() if resp.content else None
        except requests.RequestException as exc:
            raise TransientError(f"jellyfin {method} {path} failed: {exc}") from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params) or {}

    def _delete(self, path: str, params: dict | None = None) -> None:
        self._request("DELETE", path, params)

    def _all_playlists(self) -> list[dict]:
        """The one GET that returns every playlist visible to the configured user."""
        data = self._get(
            f"/Users/{self._cfg.user_id}/Items",
            {"IncludeItemTypes": "Playlist", "Recursive": "true"},
        )
        return data.get("Items") or []

    def list_playlists(self) -> list[PlaylistSummary]:
        """Every playlist, sorted by name (case-insensitive, id as a deterministic tie-break)."""
        return sorted(
            (
                PlaylistSummary(id=str(it["Id"]), name=str(it.get("Name", "")))
                for it in self._all_playlists()
                if it.get("Id")
            ),
            key=lambda p: (p.name.casefold(), p.id),
        )

    def find_playlist(self, name: str) -> str:
        """Return the Id of the playlist whose Name matches. Raise TransientError if missing.

        Treating "not found" as transient is deliberate: a playlist mid-rename must not trigger a
        device-folder purge (see reconcile).
        """
        items = self._all_playlists()
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

    # --- playlist editing (web UI) -------------------------------------------

    def playlist_entries(self, playlist_id: str) -> list[PlaylistEntry]:
        """Playlist rows with the metadata the editor groups on.

        Sends no `fields=`: SeriesName/SeriesId/SeasonName/SeasonId/IndexNumber/ParentIndexNumber are
        assigned ungated by DtoService for Episodes, and PlaylistItemId is set by the controller.
        In particular do NOT reuse _ITEM_FIELDS — MediaSources is its expensive part and is useless
        here. `enableUserData=false` is deliberate and not a payload tweak: the transcoded copies are
        watched off-device in a non-Jellyfin player, so UserData.Played is false for exactly the
        items you want to remove, and a UI built on it would look authoritative while selecting
        nothing.
        """
        data = self._get(
            f"/Playlists/{playlist_id}/Items",
            {
                "userId": self._cfg.user_id,
                "enableImages": "false",
                "enableUserData": "false",
            },
        )
        return [_parse_entry(it) for it in (data.get("Items") or [])]

    def remove_playlist_entries(
        self,
        playlist_id: str,
        entry_ids: Sequence[str],
        *,
        chunk_size: int = _ENTRY_CHUNK,
    ) -> RemovalResult:
        """DELETE /Playlists/{id}/Items?entryIds=a,b,c — chunked, tolerating partial failure.

        `entry_ids` MUST be PlaylistItemId values (see PlaylistEntry). Empty ids are dropped and
        duplicates collapsed, preserving order. Returns a result rather than raising, because
        "40 of 60 removed" is information the caller has to surface; one bad chunk must not abort
        the rest.

        Note: Jellyfin answers 204 even when an entryId matched nothing, so `removed` means
        "accepted by the server". Callers must re-read the playlist to know what actually happened.
        """
        wanted = list(dict.fromkeys(i for i in entry_ids if i))
        if not wanted:
            return RemovalResult(requested=0, removed=0, failed=0)

        removed = failed = 0
        errors: list[str] = []
        for start in range(0, len(wanted), chunk_size):
            chunk = wanted[start : start + chunk_size]
            try:
                # Comma-joined is what Jellyfin's own web client sends. If a proxy mangles it,
                # passing the list instead makes requests emit repeated entryIds= params, which
                # the same model binder accepts.
                self._delete(
                    f"/Playlists/{playlist_id}/Items", {"entryIds": ",".join(chunk)}
                )
                removed += len(chunk)
            except TransientError as exc:
                failed += len(chunk)
                errors.append(str(exc))
        _log.info("playlist %s: removed %d, failed %d", playlist_id, removed, failed)
        return RemovalResult(len(wanted), removed, failed, tuple(errors))
