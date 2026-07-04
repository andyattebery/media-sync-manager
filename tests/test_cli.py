from __future__ import annotations

import posixpath
from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient

from media_sync_manager import cli
from media_sync_manager.errors import TransientError

REL = "TV/Show/S01/ep.mkv"


def _jf(items):
    return FakeJellyfinClient(playlists={"PL": items})


def test_dry_run_changes_nothing(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=True, out=lines.append)

    assert rc == 0
    assert td.scans == []
    input_path = posixpath.join(t.input_dir, "standard", REL)
    assert not Path(input_path).exists()
    assert any("submit" in ln for ln in lines)


def test_real_sync_executes(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, _jf([make_episode(src)]), td, dry_run=False, out=lambda _l: None)

    assert rc == 0
    input_path = posixpath.join(t.input_dir, "standard", REL)
    assert Path(input_path).exists()
    assert len(td.scans) == 1
    library_id, paths, _mode = td.scans[0]
    assert library_id == "lib_iphone"
    assert paths == [input_path]


def test_transient_error_returns_nonzero(make_target, make_config):
    t = make_target(segment="standard")
    config = make_config([t])
    jf = FakeJellyfinClient(find_error=TransientError("down"))
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, jf, td, dry_run=False, out=lambda _l: None)
    assert rc == 1


def test_status_is_read_only(make_target, make_config, write_source, make_episode):
    t = make_target(segment="standard")
    config = make_config([t])
    src = write_source(REL)
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_status(config, _jf([make_episode(src)]), td, out=lines.append)

    assert rc == 0
    assert td.scans == []
    input_path = posixpath.join(t.input_dir, "standard", REL)
    assert not Path(input_path).exists()
