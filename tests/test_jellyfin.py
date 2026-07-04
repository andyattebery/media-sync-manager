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
