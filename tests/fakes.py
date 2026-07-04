"""In-memory fakes that duck-type the real clients (no HTTP)."""

from __future__ import annotations

from media_sync_manager.errors import TransientError
from media_sync_manager.models import MediaItem


class FakeJellyfinClient:
    def __init__(
        self,
        playlists: dict[str, list[MediaItem]] | None = None,
        *,
        find_error: Exception | None = None,
        find_error_for: set[str] | None = None,
    ) -> None:
        # Keyed by playlist Name; the Name doubles as its id for simplicity.
        self._playlists = playlists or {}
        self._find_error = find_error
        self._find_error_for = find_error_for or set()

    def find_playlist(self, name: str) -> str:
        if self._find_error is not None or name in self._find_error_for:
            raise self._find_error or TransientError(f"jellyfin down for {name!r}")
        if name not in self._playlists:
            raise TransientError(f"playlist not found: {name!r}")
        return name

    def playlist_items(self, playlist_id: str) -> list[MediaItem]:
        return list(self._playlists.get(playlist_id, []))


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
