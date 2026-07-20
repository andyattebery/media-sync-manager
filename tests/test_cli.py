from __future__ import annotations

from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient

from media_sync_manager import cli
from media_sync_manager.errors import TransientError

REL = "TV Shows/Meadowlark/S01/E01.mkv"


def _jf(items):
    return FakeJellyfinClient(playlists={"PL": items})


def _target(make_target, make_playlist):
    return make_target(playlists=[make_playlist("PL", "standard")])


def _input_path(env) -> str:
    return str(Path(env.transcode) / "iphone" / "standard" / REL)


def test_dry_run_changes_nothing(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=True, out=lines.append)

    assert rc == 0
    assert td.scans == []
    assert not Path(_input_path(env)).exists()
    assert any("add" in ln for ln in lines)


def test_real_sync_executes(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=False, out=lambda _l: None)

    assert rc == 0
    assert Path(_input_path(env)).exists()
    assert len(td.scans) == 1
    library_id, paths, _mode = td.scans[0]
    assert library_id == "lib_iphone"
    assert paths == [_input_path(env)]


def test_transient_error_returns_nonzero(make_target, make_playlist, make_config):
    config = make_config([_target(make_target, make_playlist)])
    jf = FakeJellyfinClient(find_error=TransientError("down"))
    rc = cli.cmd_sync(config, jf, FakeTdarrClient(), dry_run=False, out=lambda _l: None)
    assert rc == 1


def test_status_is_read_only(env, make_target, make_playlist, make_config, write_source, make_episode):
    config = make_config([_target(make_target, make_playlist)])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_status(config, _jf([make_episode(src)]), td, out=lines.append)

    assert rc == 0
    assert td.scans == []
    assert not Path(_input_path(env)).exists()
