"""Group playlist entries into show -> season -> episode for the editor UI.

Pure functions: no HTTP, no flask, no filesystem. Everything the browser renders is decided here so
it is unit-testable without a browser or a server.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from typing import Iterable, Sequence

from .models import PlaylistEntry, SeasonGroup, ShowGroup

# Friendly names for the non-episode buckets; anything else falls back to "<Type> items".
_TYPE_TITLES = {"Movie": "Movies", "Audio": "Music", "Video": "Videos"}

_NO_SERIES_KEY = "type:Episode"
_NO_SERIES_TITLE = "Episodes (no series)"


def _show_key_and_title(entry: PlaylistEntry) -> tuple[str, str, str]:
    """Return (key, title, kind) for the group an entry belongs to."""
    if entry.type == "Episode":
        if entry.series_id:
            return f"series:{entry.series_id}", entry.series_name or "Unknown Series", "series"
        if entry.series_name:
            return (
                f"series:name:{entry.series_name.casefold()}",
                entry.series_name,
                "series",
            )
        # An episode with no series identity at all. It still keeps its real seasons below —
        # the degenerate single-season bucket is for non-episodes only.
        return _NO_SERIES_KEY, _NO_SERIES_TITLE, "type"
    if not entry.type:
        return "type:", "Other items", "type"
    return f"type:{entry.type}", _TYPE_TITLES.get(entry.type, f"{entry.type} items"), "type"


def _has_seasons(show_key: str) -> bool:
    """Series groups and the series-less-episode group are seasoned; other type: groups are not."""
    return show_key.startswith("series:") or show_key == _NO_SERIES_KEY


def _season_key(show_key: str, number: int | None) -> str:
    return f"{show_key}|{number if number is not None else 'none'}"


def _season_title(entry: PlaylistEntry) -> str:
    if entry.season_name:
        return entry.season_name  # preserves localisations and named specials
    if entry.season_number == 0:
        return "Specials"
    if entry.season_number is not None:
        return f"Season {entry.season_number}"
    return "Unknown Season"


def _entry_sort_key(e: PlaylistEntry) -> tuple:
    # Un-numbered items trail the numbered ones; the trailing ids make ties deterministic.
    return (
        e.episode_number is None,
        e.episode_number or 0,
        e.name.casefold(),
        e.playlist_item_id,
        e.item_id,
    )


def _season_sort_key(s: SeasonGroup) -> tuple:
    # 1, 2, 3, ... then Specials (0), then Unknown (None).
    # To put Specials first instead, drop the `s.number == 0` term.
    return (
        s.number is None,
        s.number == 0,
        s.number if s.number is not None else 0,
        s.title.casefold(),
        s.key,
    )


def _show_sort_key(g: ShowGroup) -> tuple:
    # Series alphabetically, then the type: buckets (Movies, Episodes (no series), ...) last.
    return (g.kind == "type", g.title.casefold(), g.key)


def group_entries(entries: Sequence[PlaylistEntry]) -> list[ShowGroup]:
    """Bucket entries into shows and seasons, sorted for display.

    Does not dedupe: the playlist is rendered faithfully, and `duplicate_ids` flags the repeats.
    """
    # show_key -> (title, kind, {season_key: (number, title, [entries])})
    shows: OrderedDict[str, tuple[str, str, OrderedDict[str, tuple[int | None, str, list]]]] = (
        OrderedDict()
    )

    for entry in entries:
        show_key, show_title, kind = _show_key_and_title(entry)
        if show_key not in shows:
            shows[show_key] = (show_title, kind, OrderedDict())
        _, _, seasons = shows[show_key]

        if _has_seasons(show_key):
            number, title = entry.season_number, _season_title(entry)
        else:
            number, title = None, "All"  # degenerate: rendered without its own header
        key = _season_key(show_key, number)
        if key not in seasons:
            seasons[key] = (number, title, [])
        seasons[key][2].append(entry)

    out = [
        ShowGroup(
            key=show_key,
            title=title,
            kind=kind,
            seasons=tuple(
                sorted(
                    (
                        SeasonGroup(
                            key=skey,
                            title=stitle,
                            number=snum,
                            entries=tuple(sorted(sentries, key=_entry_sort_key)),
                        )
                        for skey, (snum, stitle, sentries) in seasons.items()
                    ),
                    key=_season_sort_key,
                )
            ),
        )
        for show_key, (title, kind, seasons) in shows.items()
    ]
    out.sort(key=_show_sort_key)
    return out


def duplicate_ids(entries: Iterable[PlaylistEntry]) -> set[str]:
    """playlist_item_ids appearing more than once.

    Jellyfin's PlaylistItemId currently caches the media item's Guid, so two copies of one episode
    share an entry id and removing it clears both. The UI badges these so that is not a surprise.
    """
    counts = Counter(e.playlist_item_id for e in entries if e.playlist_item_id)
    return {k for k, n in counts.items() if n > 1}
