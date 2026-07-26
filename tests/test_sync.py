"""execute() was only covered indirectly through test_cli. These pin the isolation behaviour."""

from __future__ import annotations

import errno
import os
from pathlib import Path

from fakes import FakeTdarrClient

from media_sync_manager import fsops, sync
from media_sync_manager.models import AddInput, DeleteOutput, RemoveInput, TargetPlan


def _add(env, rel: str, *, library_id: str = "lib") -> AddInput:
    src = env.media / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x")
    input_path = str(env.transcode / "iphone" / "standard" / rel)
    return AddInput(
        relkey=os.path.splitext(rel)[0],
        segment="standard",
        playlist="PL",
        source=str(src),
        input_path=input_path,
        tdarr_path=input_path,
        library_id=library_id,
    )


def test_one_failing_input_does_not_abort_the_rest(env, monkeypatch):
    """A single EXDEV used to kill the remaining adds, every scan, every remove and the sweep."""
    good = _add(env, "TV Shows/Good/E01.mkv")
    bad = _add(env, "TV Shows/Bad/E01.mkv")

    stale_input = env.transcode / "iphone" / "standard" / "TV Shows/Old/E01.mkv"
    stale_input.parent.mkdir(parents=True, exist_ok=True)
    stale_input.write_bytes(b"old")
    orphan_output = env.transcode / "iphone" / "sync" / "standard" / "TV Shows/Gone/E01.mkv"
    orphan_output.parent.mkdir(parents=True, exist_ok=True)
    orphan_output.write_bytes(b"out")

    plan = TargetPlan(
        target="iphone",
        adds=(bad, good),  # the failing one first, so it would have aborted everything after
        removes=(RemoveInput(str(stale_input)),),
        deletes=(DeleteOutput(str(orphan_output)),),
    )

    real_link = os.link

    def _selective(src, dst, **kwargs):
        if "Bad" in str(dst):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", _selective)
    td = FakeTdarrClient()

    failures = sync.execute(plan, td, fsops.HARDLINK)

    assert len(failures) == 1
    assert "TV Shows/Bad/E01" in failures[0]
    assert Path(good.input_path).exists()          # the other add still happened
    assert td.scans == [("lib", [good.tdarr_path], "scanFolderWatcher")]  # and was still scanned
    assert not stale_input.exists()                # removes still ran
    assert not orphan_output.exists()              # and so did the sweep


def test_scan_is_grouped_by_library(env):
    a = _add(env, "TV Shows/A/E01.mkv", library_id="lib_a")
    b = _add(env, "TV Shows/B/E01.mkv", library_id="lib_b")
    c = _add(env, "TV Shows/C/E01.mkv", library_id="lib_a")
    td = FakeTdarrClient()

    assert sync.execute(TargetPlan(target="iphone", adds=(a, b, c)), td, fsops.HARDLINK) == []

    assert [lib for lib, _paths, _mode in td.scans] == ["lib_a", "lib_b"]
    assert td.scans[0][1] == [a.tdarr_path, c.tdarr_path]


def test_symlink_mode_creates_symlinks(env):
    a = _add(env, "TV Shows/A/E01.mkv")
    assert sync.execute(TargetPlan(target="iphone", adds=(a,)), FakeTdarrClient(), fsops.SYMLINK) == []
    assert Path(a.input_path).is_symlink()
    assert Path(a.input_path).read_bytes() == b"x"
