"""In-memory fakes that duck-type the real clients (no HTTP)."""

from __future__ import annotations

from media_sync_manager.errors import TransientError
from media_sync_manager.models import MediaItem, PlaylistEntry, PlaylistSummary, RemovalResult


class FakeJellyfinClient:
    def __init__(
        self,
        playlists: dict[str, list[MediaItem]] | None = None,
        *,
        find_error: Exception | None = None,
        find_error_for: set[str] | None = None,
        entries: dict[str, list[PlaylistEntry]] | None = None,
        names: dict[str, str] | None = None,
        load_error: Exception | None = None,
        remove_error: Exception | None = None,
        fail_after: int | None = None,
        pretend_only: bool = False,
    ) -> None:
        # Keyed by playlist Name; the Name doubles as its id for simplicity.
        self._playlists = playlists or {}
        self._find_error = find_error
        self._find_error_for = find_error_for or set()

        # --- playlist-editing surface ---
        self.entries = {k: list(v) for k, v in (entries or {}).items()}
        self._names = names or {}
        # find_error belongs to the SYNC path (find_playlist). The editor reads through
        # list_playlists/playlist_entries, which had no way to fail — so the page's two
        # "could not load" branches were unreachable from a test, not merely untested.
        self._load_error = load_error
        self._remove_error = remove_error
        self._fail_after = fail_after
        # pretend_only reproduces Jellyfin's real misbehaviour: accept the call, return 204, delete
        # nothing. It must be OPT-IN — a record-only fake would make every removal test report "the
        # list didn't shrink", i.e. green suite, broken app, exactly inverted.
        self._pretend_only = pretend_only
        self.removals: list[tuple[str, list[str]]] = []

    @property
    def base_url(self) -> str:
        return "http://jf.test"

    def find_playlist(self, name: str) -> str:
        if self._find_error is not None or name in self._find_error_for:
            raise self._find_error or TransientError(f"jellyfin down for {name!r}")
        if name not in self._playlists:
            raise TransientError(f"playlist not found: {name!r}")
        return name

    def playlist_items(self, playlist_id: str) -> list[MediaItem]:
        return list(self._playlists.get(playlist_id, []))

    # --- playlist editing -----------------------------------------------------

    def list_playlists(self) -> list[PlaylistSummary]:
        if self._load_error is not None:
            raise self._load_error
        return sorted(
            (PlaylistSummary(id=k, name=self._names.get(k, k)) for k in self.entries),
            key=lambda p: (p.name.casefold(), p.id),
        )

    def playlist_entries(self, playlist_id: str) -> list[PlaylistEntry]:
        if self._load_error is not None:
            raise self._load_error
        return list(self.entries.get(playlist_id, []))

    def remove_playlist_entries(
        self, playlist_id: str, entry_ids, *, chunk_size: int = 50
    ) -> RemovalResult:
        # Record what the caller actually sent, BEFORE this fake's own dedupe: a test asserting
        # post-dedupe ids is asserting the double's behaviour, and survives any mutation to the
        # code it claims to cover.
        self.removals.append((playlist_id, list(entry_ids)))
        wanted = list(dict.fromkeys(i for i in entry_ids if i))
        if not wanted:
            return RemovalResult(0, 0, 0)
        if self._remove_error is not None:
            return RemovalResult(len(wanted), 0, len(wanted), (str(self._remove_error),))

        # fail_after=N: the first N ids land, the rest fail -> exercises the partial (207) path.
        ok = wanted if self._fail_after is None else wanted[: self._fail_after]
        bad = [] if self._fail_after is None else wanted[self._fail_after :]
        if not self._pretend_only:
            drop = set(ok)
            self.entries[playlist_id] = [
                e for e in self.entries.get(playlist_id, []) if e.playlist_item_id not in drop
            ]
            # ALSO drop from the sync-side projection. Without this the two surfaces are disjoint
            # and an editor removal is invisible to playlist_items(), which makes the whole point
            # of the feature — remove here, the file leaves the device — untestable.
            if playlist_id in self._playlists:
                self._playlists[playlist_id] = [
                    m for m in self._playlists[playlist_id] if m.id not in drop
                ]
        errors = ("jellyfin DELETE /Playlists/x/Items failed: boom",) if bad else ()
        return RemovalResult(len(wanted), len(ok), len(bad), errors)


class FakeTdarrClient:
    def __init__(
        self,
        libraries: list[dict] | None = None,
        *,
        scan_mode: str = "scanFolderWatcher",
        scan_error: Exception | None = None,
        scan_error_for: set[str] | None = None,
    ) -> None:
        self.scan_mode = scan_mode
        self._libraries = libraries or []
        self.scans: list[tuple[str, list[str], str]] = []
        # scan_files could not fail, so the guard around it was unreachable from a test rather than
        # merely untested. Named after find_error/find_error_for on FakeJellyfinClient: the *_for
        # variant fails one named library, which is what pins the guard INSIDE the per-library loop
        # instead of around it.
        self._scan_error = scan_error
        self._scan_error_for = scan_error_for or set()

    def list_libraries(self) -> list[dict]:
        return list(self._libraries)

    def scan_files(self, library_id: str, paths: list[str], mode: str | None = None) -> None:
        if self._scan_error is not None or library_id in self._scan_error_for:
            raise self._scan_error or TransientError(
                f"tdarr POST /api/v2/scan-files failed: down for {library_id!r}"
            )
        self.scans.append((library_id, list(paths), mode or self.scan_mode))


def linked_playlist(name: str, sources: list[str]) -> dict:
    """Build both projections of ONE playlist from one list of source paths.

    `MediaItem.id` and `PlaylistEntry.playlist_item_id` MUST stay equal: that shared id is the only
    thing connecting the editor's removal to what the sync path subsequently sees. Split them and the
    integration test goes green while testing nothing.

    Returns kwargs for FakeJellyfinClient, e.g. FakeJellyfinClient(**linked_playlist("PL", [src])).
    """
    from pathlib import Path as _Path

    from media_sync_manager.models import MediaSource

    items, entries = [], []
    for i, src in enumerate(sources, start=1):
        entry_id = f"e{i}"
        stem = _Path(src).stem
        items.append(
            MediaItem(id=entry_id, name=stem, type="Episode",
                      media_sources=(MediaSource(path=src, size=100),))
        )
        entries.append(
            PlaylistEntry(playlist_item_id=entry_id, item_id=entry_id, name=stem,
                          type="Episode", series_id="s1", series_name="Show",
                          season_number=1, episode_number=i)
        )
    return {"playlists": {name: items}, "entries": {name: entries}, "names": {name: name}}
