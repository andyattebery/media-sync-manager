from __future__ import annotations

import pytest
import responses

from media_sync_manager.errors import TransientError
from media_sync_manager.jellyfin import JellyfinClient
from media_sync_manager.models import JellyfinConfig

CFG = JellyfinConfig(url="http://jf", api_key="k", user_id="u")


@responses.activate
def test_find_playlist_matches_by_name_and_sends_auth():
    responses.add(
        responses.GET,
        "http://jf/Users/u/Items",
        json={"Items": [{"Id": "p1", "Name": "Other"}, {"Id": "p2", "Name": "Travel - Phone"}]},
    )
    client = JellyfinClient(CFG)
    assert client.find_playlist("Travel - Phone") == "p2"
    assert responses.calls[0].request.headers["X-Emby-Token"] == "k"


@responses.activate
def test_find_playlist_missing_raises_transient():
    responses.add(responses.GET, "http://jf/Users/u/Items", json={"Items": []})
    with pytest.raises(TransientError):
        JellyfinClient(CFG).find_playlist("nope")


@responses.activate
def test_playlist_items_sends_userid_and_fields_and_parses():
    responses.add(
        responses.GET,
        "http://jf/Playlists/p1/Items",
        json={
            "Items": [
                {
                    "Id": "e1",
                    "Name": "Ep 1",
                    "Type": "Episode",
                    "SeriesId": "s1",
                    "MediaSources": [{"Path": "/data/x.mkv", "Size": 123}],
                }
            ]
        },
    )
    items = JellyfinClient(CFG).playlist_items("p1")
    assert len(items) == 1
    assert items[0].type == "Episode"
    assert items[0].media_sources[0].path == "/data/x.mkv"
    q = responses.calls[0].request.params
    assert q["userId"] == "u"
    assert "MediaSources" in q["fields"]


@responses.activate
def test_network_error_raises_transient():
    responses.add(responses.GET, "http://jf/Users/u/Items", status=503)
    with pytest.raises(TransientError):
        JellyfinClient(CFG).find_playlist("x")


# --- playlist editing --------------------------------------------------------


@responses.activate
def test_list_playlists_sorted_and_authed():
    responses.add(
        responses.GET,
        "http://jf/Users/u/Items",
        json={"Items": [{"Id": "p2", "Name": "zeta"}, {"Id": "p1", "Name": "Alpha"}]},
    )
    out = JellyfinClient(CFG).list_playlists()
    assert [(p.id, p.name) for p in out] == [("p1", "Alpha"), ("p2", "zeta")]
    assert responses.calls[0].request.headers["X-Emby-Token"] == "k"


@responses.activate
def test_playlist_entries_maps_metadata_and_sends_no_fields():
    responses.add(
        responses.GET,
        "http://jf/Playlists/p1/Items",
        json={
            "Items": [
                {
                    "Id": "9c0",
                    "PlaylistItemId": "abc",
                    "Name": "First Flight",
                    "Type": "Episode",
                    "SeriesId": "s1",
                    "SeriesName": "Meadowlark",
                    "SeasonId": "se1",
                    "SeasonName": "Season 1",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 2,
                }
            ]
        },
    )
    entries = JellyfinClient(CFG).playlist_entries("p1")
    e = entries[0]
    assert (e.playlist_item_id, e.item_id) == ("abc", "9c0")
    assert (e.series_id, e.series_name) == ("s1", "Meadowlark")
    assert (e.season_number, e.episode_number) == (1, 2)
    q = responses.calls[0].request.params
    assert q["userId"] == "u"
    # MediaSources is the expensive part of the sync call and useless here.
    assert "fields" not in q
    assert q["enableUserData"] == "false"


@responses.activate
def test_playlist_entries_falls_back_to_id_and_strips_dashes():
    responses.add(
        responses.GET,
        "http://jf/Playlists/p1/Items",
        json={"Items": [{"Id": "9c0-11e2-abc", "Name": "x", "Type": "Movie"}]},
    )
    e = JellyfinClient(CFG).playlist_entries("p1")[0]
    assert e.playlist_item_id == "9c011e2abc"  # server compares the dashless "N" form
    assert e.removable


@responses.activate
def test_playlist_entries_unaddressable_when_both_ids_missing():
    responses.add(
        responses.GET,
        "http://jf/Playlists/p1/Items",
        json={"Items": [{"Name": "x", "Type": "Movie"}]},
    )
    e = JellyfinClient(CFG).playlist_entries("p1")[0]
    assert e.playlist_item_id == "" and not e.removable


@responses.activate
def test_remove_sends_comma_joined_entry_ids_and_survives_204_empty_body():
    """204 with an empty body: the `if resp.content` guard skips a decode that would only raise.

    It would NOT escape uncaught — requests makes JSONDecodeError a RequestException — but it would
    be relabelled a transport failure, which is exactly how the Tdarr scan-files bug hid.
    """
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    result = JellyfinClient(CFG).remove_playlist_entries("p1", ["a", "b"])
    assert (result.requested, result.removed, result.failed) == (2, 2, 0)
    assert responses.calls[0].request.params["entryIds"] == "a,b"
    assert responses.calls[0].request.headers["X-Emby-Token"] == "k"


@responses.activate
def test_remove_chunks_at_the_boundary():
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    ids = [f"id{i}" for i in range(120)]
    result = JellyfinClient(CFG).remove_playlist_entries("p1", ids, chunk_size=50)
    assert len(responses.calls) == 3
    sent = [c.request.params["entryIds"].split(",") for c in responses.calls]
    assert [len(c) for c in sent] == [50, 50, 20]
    assert [i for chunk in sent for i in chunk] == ids  # order preserved, nothing dropped
    assert result.removed == 120


@responses.activate
def test_remove_dedupes_and_drops_empty_ids():
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    result = JellyfinClient(CFG).remove_playlist_entries("p1", ["a", "", "a", "b", ""])
    assert result.requested == 2
    assert responses.calls[0].request.params["entryIds"] == "a,b"


@responses.activate
def test_remove_with_no_ids_makes_no_http_call():
    result = JellyfinClient(CFG).remove_playlist_entries("p1", ["", ""])
    assert (result.requested, result.removed, result.failed) == (0, 0, 0)
    assert len(responses.calls) == 0


@responses.activate
def test_remove_continues_after_a_failing_chunk():
    """One bad chunk must not abort the rest: the caller needs '40 of 60', not an exception."""
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=403, body="")
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    ids = [f"id{i}" for i in range(6)]
    result = JellyfinClient(CFG).remove_playlist_entries("p1", ids, chunk_size=2)
    assert len(responses.calls) == 3  # the third chunk was still attempted
    assert (result.requested, result.removed, result.failed) == (6, 4, 2)
    assert len(result.errors) == 1


def test_base_url_is_exposed_for_linking_back():
    """The editor page links to the instance; web.py must not reach into a private attribute."""
    assert JellyfinClient(CFG).base_url == "http://jf"


@responses.activate
def test_empty_and_missing_items_keys_are_tolerated():
    """Jellyfin omits Items on an empty result rather than sending []."""
    responses.add(responses.GET, "http://jf/Playlists/p1/Items", json={})
    responses.add(responses.GET, "http://jf/Users/u/Items", json={})
    client = JellyfinClient(CFG)
    assert client.playlist_entries("p1") == []
    assert client.list_playlists() == []


@responses.activate
def test_list_playlists_skips_entries_without_an_id():
    responses.add(
        responses.GET,
        "http://jf/Users/u/Items",
        json={"Items": [{"Name": "no id"}, {"Id": "p1", "Name": "ok"}]},
    )
    assert [p.id for p in JellyfinClient(CFG).list_playlists()] == ["p1"]


@responses.activate
def test_playlist_entries_tolerates_junk_index_numbers():
    """ParentIndexNumber/IndexNumber are ints in practice, but a null or a string must not raise —
    grouping treats them as 'unknown' rather than blowing up the whole page."""
    responses.add(
        responses.GET,
        "http://jf/Playlists/p1/Items",
        json={"Items": [{"Id": "e1", "Name": "x", "Type": "Episode",
                         "ParentIndexNumber": None, "IndexNumber": "not a number"}]},
    )
    e = JellyfinClient(CFG).playlist_entries("p1")[0]
    assert e.season_number is None and e.episode_number is None


@responses.activate
def test_remove_with_a_single_full_chunk_makes_one_call():
    """Exactly chunk_size is the off-by-one boundary: 50 must be one request, not two."""
    responses.add(responses.DELETE, "http://jf/Playlists/p1/Items", status=204, body="")
    result = JellyfinClient(CFG).remove_playlist_entries(
        "p1", [f"id{i}" for i in range(50)], chunk_size=50
    )
    assert len(responses.calls) == 1 and result.removed == 50
