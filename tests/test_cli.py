from __future__ import annotations

import errno
import os
from pathlib import Path

from fakes import FakeJellyfinClient, FakeTdarrClient

from media_sync_manager import cli
from media_sync_manager.errors import TransientError

REL = "TV Shows/Meadowlark/S01/E01.mkv"


def _exdev(*_args, **_kwargs):
    raise OSError(errno.EXDEV, "cross-device link")


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


def test_doctor_reports_the_probed_input_mode(env, make_target, make_playlist, make_config):
    """doctor had no coverage at all, which is how the bogus st_dev check survived."""
    config = make_config([_target(make_target, make_playlist)])
    lines: list[str] = []

    cli.cmd_doctor(config, _jf([]), FakeTdarrClient(), out=lines.append)

    assert any("input mode" in ln and "hardlink" in ln for ln in lines)
    assert not any("same filesystem" in ln for ln in lines)


def test_doctor_flags_transcode_root_outside_media_root(
    env, make_target, make_playlist, make_config, monkeypatch
):
    """Siblings make a relative symlink resolve outside an SMB share, where Samba hides the file."""
    monkeypatch.setattr(os, "link", _exdev)  # force symlink mode
    config = make_config([_target(make_target, make_playlist)])
    lines: list[str] = []

    rc = cli.cmd_doctor(config, _jf([]), FakeTdarrClient(), out=lines.append)

    # conftest deliberately puts media/ and transcode/ side by side.
    assert any("FAIL" in ln and "transcode_root is under media_root" in ln for ln in lines)
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
