from __future__ import annotations

import json

import responses

from media_sync_manager.models import TdarrConfig
from media_sync_manager.tdarr import TdarrClient


def test_auth_disabled_when_no_credentials():
    client = TdarrClient(TdarrConfig(url="http://td"))
    assert client.auth_enabled is False


@responses.activate
def test_login_then_bearer_reused():
    responses.add(responses.POST, "http://td/api/v2/public/auth/login", json={"token": "TOK"})
    responses.add(responses.POST, "http://td/api/v2/cruddb", json=[])
    responses.add(responses.POST, "http://td/api/v2/cruddb", json=[])

    client = TdarrClient(TdarrConfig(url="http://td", username="user", password="pw"))
    client.list_libraries()
    client.list_libraries()

    login_calls = [c for c in responses.calls if c.request.url.endswith("/auth/login")]
    assert len(login_calls) == 1  # logged in once
    cruddb_calls = [c for c in responses.calls if c.request.url.endswith("/cruddb")]
    assert all(c.request.headers["Authorization"] == "Bearer TOK" for c in cruddb_calls)


@responses.activate
def test_scan_files_posts_scanconfig_body():
    responses.add(responses.POST, "http://td/api/v2/scan-files", json={})
    client = TdarrClient(TdarrConfig(url="http://td"))  # auth disabled
    client.scan_files("lib1", ["/mnt/tdarr/in/iphone/standard/Show/ep.mkv"], mode="scanFolderWatcher")

    body = json.loads(responses.calls[0].request.body)
    cfg = body["data"]["scanConfig"]
    assert cfg["dbID"] == "lib1"
    assert cfg["mode"] == "scanFolderWatcher"
    assert cfg["arrayOrPath"] == ["/mnt/tdarr/in/iphone/standard/Show/ep.mkv"]


@responses.activate
def test_list_libraries_parses_list():
    responses.add(
        responses.POST,
        "http://td/api/v2/cruddb",
        json=[{"_id": "lib1", "folder": "/mnt/tdarr/in/iphone"}],
    )
    libs = TdarrClient(TdarrConfig(url="http://td")).list_libraries()
    assert libs[0]["_id"] == "lib1"
