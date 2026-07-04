from __future__ import annotations

import posixpath
from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient

from media_sync_manager import cli
from media_sync_manager.errors import TransientError

REL = "TV/Show/S01/ep.mkv"


def _jf(items, **kw):
    return FakeJellyfinClient(playlists={"PL": items}, **kw)


def test_dry_run_changes_nothing(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    jf = _jf([make_episode(src)])
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_sync(config, jf, td, dry_run=True, out=lines.append)

    assert rc == 0
    assert td.scans == []  # no scan triggered
    input_path = posixpath.join(device.input_dir, "standard", REL)
    assert not Path(input_path).exists()  # no hardlink created
    assert any("submit" in ln for ln in lines)


def test_real_sync_executes(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    jf = _jf([make_episode(src)])
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, jf, td, dry_run=False, out=lambda _l: None)

    assert rc == 0
    input_path = posixpath.join(device.input_dir, "standard", REL)
    assert Path(input_path).exists()
    assert len(td.scans) == 1
    library_id, paths, _mode = td.scans[0]
    assert library_id == "lib_iphone"
    assert paths == [input_path]


def test_transient_device_error_returns_nonzero(make_device, make_config):
    device = make_device()
    config = make_config([device])
    jf = _jf([], find_error=TransientError("down"))
    td = FakeTdarrClient()

    rc = cli.cmd_sync(config, jf, td, dry_run=False, out=lambda _l: None)
    assert rc == 1


def test_status_is_read_only(make_device, make_config, write_source, make_episode):
    device = make_device()
    config = make_config([device])
    src = write_source(REL)
    jf = _jf([make_episode(src)])
    td = FakeTdarrClient()
    lines: list[str] = []

    rc = cli.cmd_status(config, jf, td, out=lines.append)

    assert rc == 0
    assert td.scans == []
    input_path = posixpath.join(device.input_dir, "standard", REL)
    assert not Path(input_path).exists()
