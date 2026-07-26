"""Grouping and sort order, across the case set the UI has to render."""

from __future__ import annotations

from media_sync_manager.models import PlaylistEntry
from media_sync_manager.playlists import duplicate_ids, group_entries


def ep(pid, name, *, series=None, sid=None, snum=None, enum=None, sname=None):
    return PlaylistEntry(
        playlist_item_id=pid,
        item_id=pid,
        name=name,
        type="Episode",
        series_id=sid,
        series_name=series,
        season_name=sname,
        season_number=snum,
        episode_number=enum,
    )


def other(pid, name, type_="Movie"):
    return PlaylistEntry(playlist_item_id=pid, item_id=pid, name=name, type=type_)


def test_empty_input():
    assert group_entries([]) == []


def test_episodes_group_by_series_id():
    groups = group_entries(
        [ep("a", "One", series="Meadowlark", sid="s1", snum=1, enum=1),
         ep("b", "Two", series="Meadowlark", sid="s1", snum=1, enum=2)]
    )
    assert [g.key for g in groups] == ["series:s1"]
    assert groups[0].title == "Meadowlark" and groups[0].count == 2


def test_series_name_fallback_when_id_missing():
    groups = group_entries([ep("a", "One", series="Meadowlark", snum=1, enum=1)])
    assert groups[0].key == "series:name:meadowlark"
    assert groups[0].kind == "series"


def test_series_id_and_name_fallback_do_not_merge():
    """Different keys, so an entry with an id and one without stay in separate groups."""
    groups = group_entries(
        [ep("a", "One", series="Meadowlark", sid="s1", snum=1, enum=1),
         ep("b", "Two", series="Meadowlark", snum=1, enum=2)]
    )
    assert {g.key for g in groups} == {"series:s1", "series:name:meadowlark"}


def test_series_less_episodes_keep_their_real_seasons():
    """The degenerate single-season bucket is for non-episodes only: an episode with no series can
    still carry a ParentIndexNumber, and flattening it would lose that."""
    groups = group_entries(
        [ep("a", "One", snum=3, enum=7), ep("b", "Two", snum=4, enum=1)]
    )
    assert groups[0].key == "type:Episode"
    assert groups[0].title == "Episodes (no series)"
    assert [s.title for s in groups[0].seasons] == ["Season 3", "Season 4"]
    assert [s.number for s in groups[0].seasons] == [3, 4]


def test_non_episodes_get_one_degenerate_season():
    groups = group_entries([other("m1", "Slow Water"), other("m2", "The Cartographer")])
    assert groups[0].key == "type:Movie" and groups[0].title == "Movies"
    assert len(groups[0].seasons) == 1
    season = groups[0].seasons[0]
    assert season.number is None and season.title == "All"
    assert season.key == "type:Movie|none"


def test_unknown_and_empty_types():
    groups = group_entries([other("a", "x", "Audio"), other("b", "y", "")])
    titles = {g.key: g.title for g in groups}
    assert titles["type:Audio"] == "Music"
    assert titles["type:"] == "Other items"


def test_season_titles_and_keys():
    groups = group_entries(
        [ep("a", "n", series="S", sid="s1", snum=1, enum=1),
         ep("b", "s", series="S", sid="s1", snum=0, enum=1),
         ep("c", "u", series="S", sid="s1", snum=None, enum=1),
         ep("d", "l", series="S", sid="s1", snum=2, enum=1, sname="Book Two")]
    )
    seasons = groups[0].seasons
    assert [s.title for s in seasons] == ["Season 1", "Book Two", "Specials", "Unknown Season"]
    assert [s.key for s in seasons] == [
        "series:s1|1", "series:s1|2", "series:s1|0", "series:s1|none"
    ]


def test_season_order_numbered_then_specials_then_unknown():
    groups = group_entries(
        [ep("u", "u", series="S", sid="s1", snum=None, enum=1),
         ep("s", "s", series="S", sid="s1", snum=0, enum=1),
         ep("b", "b", series="S", sid="s1", snum=2, enum=1),
         ep("a", "a", series="S", sid="s1", snum=1, enum=1)]
    )
    assert [s.number for s in groups[0].seasons] == [1, 2, 0, None]


def test_episodes_sort_by_number_with_unnumbered_last():
    groups = group_entries(
        [ep("c", "Zed", series="S", sid="s1", snum=1, enum=None),
         ep("b", "Bee", series="S", sid="s1", snum=1, enum=2),
         ep("a", "Aye", series="S", sid="s1", snum=1, enum=1)]
    )
    assert [e.name for e in groups[0].seasons[0].entries] == ["Aye", "Bee", "Zed"]


def test_shows_sort_alphabetically_then_type_groups_last():
    groups = group_entries(
        [other("m", "A Movie"),
         ep("z", "z", series="Zebra", sid="s2", snum=1, enum=1),
         ep("a", "a", series="Aardvark", sid="s1", snum=1, enum=1),
         ep("o", "o", snum=1, enum=1)]
    )
    assert [g.title for g in groups] == [
        "Aardvark", "Zebra", "Episodes (no series)", "Movies"
    ]


def test_identical_titles_sort_deterministically():
    a = group_entries(
        [ep("b", "Same", series="S", sid="s1", snum=1, enum=1),
         ep("a", "Same", series="S", sid="s1", snum=1, enum=1)]
    )
    b = group_entries(
        [ep("a", "Same", series="S", sid="s1", snum=1, enum=1),
         ep("b", "Same", series="S", sid="s1", snum=1, enum=1)]
    )
    ids = lambda gs: [e.playlist_item_id for e in gs[0].seasons[0].entries]  # noqa: E731
    assert ids(a) == ids(b) == ["a", "b"]


def test_counts_roll_up():
    groups = group_entries(
        [ep("a", "a", series="S", sid="s1", snum=1, enum=1),
         ep("b", "b", series="S", sid="s1", snum=1, enum=2),
         ep("c", "c", series="S", sid="s1", snum=2, enum=1)]
    )
    assert groups[0].count == 3
    assert [s.count for s in groups[0].seasons] == [2, 1]


def test_grouping_does_not_dedupe():
    """The playlist is rendered faithfully; duplicate_ids flags the repeats instead."""
    entries = [ep("a", "One", series="S", sid="s1", snum=1, enum=1)] * 2
    groups = group_entries(entries)
    assert groups[0].count == 2


def test_duplicate_ids():
    entries = [
        ep("a", "One", series="S", sid="s1", snum=1, enum=1),
        ep("a", "One", series="S", sid="s1", snum=1, enum=1),
        ep("b", "Two", series="S", sid="s1", snum=1, enum=2),
    ]
    assert duplicate_ids(entries) == {"a"}


def test_duplicate_ids_ignores_unaddressable_entries():
    """Every non-removable entry shares the empty-string id; that is not a duplicate."""
    entries = [other("", "Broken"), other("", "Also broken")]
    assert duplicate_ids(entries) == set()



def test_empty_season_name_falls_through_to_the_number():
    """SeasonName can come back as "" rather than absent; a truthiness check must catch both, or
    the season renders with a blank title."""
    groups = group_entries([ep("a", "n", series="S", sid="s1", snum=0, enum=1, sname="")])
    assert groups[0].seasons[0].title == "Specials"


def test_empty_series_name_does_not_create_a_nameless_group():
    """SeriesName "" with no SeriesId must fall to the no-series bucket, not key on an empty
    string and render a group with a blank header."""
    groups = group_entries([ep("a", "n", series="", snum=1, enum=1)])
    assert groups[0].key == "type:Episode"
    assert groups[0].title == "Episodes (no series)"


def test_series_id_present_but_name_missing():
    groups = group_entries([ep("a", "n", sid="s1", snum=1, enum=1)])
    assert groups[0].key == "series:s1" and groups[0].title == "Unknown Series"
