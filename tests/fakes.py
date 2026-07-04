"""In-memory fakes that duck-type the real clients (no HTTP)."""

from __future__ import annotations

from media_sync_manager.errors import TransientError
from media_sync_manager.models import MediaItem


class FakeJellyfinClient:
    def __init__(
        self,
        playlists: dict[str, list[MediaItem]] | None = None,
        series_genres: dict[str, list[str]] | None = None,
        *,
        find_error: Exception | None = None,
    ) -> None:
        # Keyed by playlist Name; the Name doubles as its id for simplicity.
        self._playlists = playlists or {}
        self._series = series_genres or {}
        self._find_error = find_error
        self.series_calls: list[str] = []

    def find_playlist(self, name: str) -> str:
        if self._find_error is not None:
            raise self._find_error
        if name not in self._playlists:
            raise TransientError(f"playlist not found: {name!r}")
        return name

    def playlist_items(self, playlist_id: str) -> list[MediaItem]:
        return list(self._playlists.get(playlist_id, []))

    def series_genres(self, series_id: str) -> list[str]:
        self.series_calls.append(series_id)
        return list(self._series.get(series_id, []))


class FakeTdarrClient:
    def __init__(
        self, libraries: list[dict] | None = None, *, scan_mode: str = "scanFolderWatcher"
    ) -> None:
        self.scan_mode = scan_mode
        self._libraries = libraries or []
        self.scans: list[tuple[str, list[str], str]] = []

    def list_libraries(self) -> list[dict]:
        return list(self._libraries)

    def scan_files(self, library_id: str, paths: list[str], mode: str | None = None) -> None:
        self.scans.append((library_id, list(paths), mode or self.scan_mode))
