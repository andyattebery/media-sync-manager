"""Fixture playlists for the browser tests.

The main one is built to contain the whole render case set in a single page, so the assertions and
the screenshots cover it by construction rather than by anyone remembering to add a case.
"""

from __future__ import annotations

from media_sync_manager.models import PlaylistEntry

CASE_SET_ID = "pl-cases"
BULK_ID = "pl-bulk"
EMPTY_ID = "pl-empty"

NAMES = {CASE_SET_ID: "Case Set", BULK_ID: "Bulk (120)", EMPTY_ID: "Empty",
         "pl-wide": "Wide (21 groups)"}


def ep(pid, name, series, sid, snum, enum):
    return PlaylistEntry(
        playlist_item_id=pid, item_id=pid, name=name, type="Episode",
        series_id=sid, series_name=series, season_number=snum, episode_number=enum,
    )


def movie(pid, name):
    return PlaylistEntry(playlist_item_id=pid, item_id=pid, name=name, type="Movie")


MEADOW_S1 = [ep(f"b{i}", n, "Meadowlark", "s-meadow", 1, i) for i, n in enumerate(
    ["First Flight", "The Long Way", "Windfall", "Low Tide", "Snowline"], start=1)]
MEADOW_S2 = [ep(f"c{i}", n, "Meadowlark", "s-meadow", 2, i) for i, n in enumerate(
    ["Nightjar", "Driftwood", "Ninepin", "Harrow"], start=1)]

CASE_SET = [
    *MEADOW_S1,
    *MEADOW_S2,
    ep("sp1", "Meadowlark Special", "Meadowlark", "s-meadow", 0, 1),          # Specials
    ep("loose", "Loose Episode", "Meadowlark", "s-meadow", 2, None),         # no episode number
    *[ep(f"a{i}", n, "Northwind", "s-north", 1, i) for i, n in enumerate(
        ["Cold Open", "Second Wind"], start=1)],
    ep("orphan", "Unmatched File", None, None, 3, 7),                  # series-less, real season
    # Episode number known, season number NOT — the only shape that renders "S??E05".
    ep("nosnum", "No Season Number", "Meadowlark", "s-meadow", None, 5),
    ep("c1", "Nightjar", "Meadowlark", "s-meadow", 2, 1),                  # duplicate of c1
    movie("m1", "The Cartographer"),                                       # degenerate season
    movie("m2", "Slow Water"),
    PlaylistEntry(playlist_item_id="", item_id="", name="Broken Entry", type="Movie"),
    movie("x1", '<img src=x onerror="alert(1)"> & "quotes"'),          # must render as text
]

# Exists solely for the chunk-boundary test: the case set has ~15 items and nothing to select 120
# of, so without this that test silently degrades into "select everything, one chunk".
BULK = [ep(f"z{i}", f"Episode {i}", "Long Show", "s-long", 1, i) for i in range(1, 121)]

# 18 shows plus 3 non-episode buckets. Exists for two branches nothing else reaches: MANY_GROUPS
# (>15 groups -> shows start collapsed) and summarise()'s "N other sections" plural, which only
# appears above two type buckets. The largest other fixture has 4 groups.
WIDE_ID = "pl-wide"
WIDE = (
    [ep(f"w{i}", f"Ep {i}", f"Show {i:02d}", f"s-w{i}", 1, 1) for i in range(18)]
    + [movie("mw1", "A Movie")]
    + [PlaylistEntry(playlist_item_id="aw1", item_id="aw1", name="A Song", type="Audio")]
    + [PlaylistEntry(playlist_item_id="vw1", item_id="vw1", name="A Clip", type="Video")]
)

PLAYLISTS = {CASE_SET_ID: CASE_SET, BULK_ID: BULK, EMPTY_ID: [], WIDE_ID: WIDE}

MEADOW_KEY = "series:s-meadow"
MEADOW_S1_KEY = "series:s-meadow|1"
MEADOW_S2_KEY = "series:s-meadow|2"
