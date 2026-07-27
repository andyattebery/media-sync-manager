"""execute() was only covered indirectly through test_cli. These pin the isolation behaviour."""

from __future__ import annotations

import errno
import os
from pathlib import Path

from fakes import FakeTdarrClient

from media_sync_manager import fsops, sync
from media_sync_manager.errors import TransientError
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


def _stale_and_orphan(env) -> tuple[Path, Path]:
    """A stale input to remove and an orphaned output to sweep — the two things an abort skips."""
    stale_input = env.transcode / "iphone" / "standard" / "TV Shows/Old/E01.mkv"
    stale_input.parent.mkdir(parents=True, exist_ok=True)
    stale_input.write_bytes(b"old")
    orphan_output = env.transcode / "iphone" / "sync" / "standard" / "TV Shows/Gone/E01.mkv"
    orphan_output.parent.mkdir(parents=True, exist_ok=True)
    orphan_output.write_bytes(b"out")
    return stale_input, orphan_output


def test_one_failing_input_does_not_abort_the_rest(env, monkeypatch):
    """A single EXDEV used to kill the remaining adds, every scan, every remove and the sweep."""
    good = _add(env, "TV Shows/Good/E01.mkv")
    bad = _add(env, "TV Shows/Bad/E01.mkv")
    stale_input, orphan_output = _stale_and_orphan(env)

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


# --- a best-effort call must not be able to abort retirement -------------------


def test_scan_failure_does_not_abort_removes_or_sweep(env):
    """The reported bug: on the deployment host a cycle created 154 symlinks, then enqueued 0 scans and ran 0
    removes and 0 deletes, because an unguarded scan_files raised straight out of execute().

    Folder Watch is what actually retires a deleted input; scan-files only makes pickup of new ones
    immediate. A latency optimisation must never be able to block retirement and the sweep.
    """
    good = _add(env, "TV Shows/Good/E01.mkv")
    stale_input, orphan_output = _stale_and_orphan(env)
    plan = TargetPlan(
        target="iphone",
        adds=(good,),
        removes=(RemoveInput(str(stale_input)),),
        deletes=(DeleteOutput(str(orphan_output)),),
    )

    failures = sync.execute(plan, FakeTdarrClient(scan_error=TransientError("boom")), fsops.HARDLINK)

    assert Path(good.input_path).exists()      # the add still happened
    assert not stale_input.exists(), "a failed scan aborted the removes"
    assert not orphan_output.exists(), "a failed scan aborted the sweep"
    # Reported, not swallowed: whether this is harmless depends on Folder Watch being enabled, which
    # the glue cannot see. `sync --once` exits non-zero via CycleResult.ok.
    assert len(failures) == 1 and "scan-files" in failures[0]


def test_one_library_failing_still_scans_the_others(env):
    """Pins the guard INSIDE the per-library loop. Wrapping the whole loop is the plausible wrong
    version and would leave lib_b unscanned.

    lib_a is deliberately FIRST in plan.adds — by_library is an OrderedDict in adds order, so with
    the failing library second a whole-loop guard would still reach lib_b and this test would pass
    while constraining nothing.
    """
    a = _add(env, "TV Shows/A/E01.mkv", library_id="lib_a")
    b = _add(env, "TV Shows/B/E01.mkv", library_id="lib_b")
    td = FakeTdarrClient(scan_error_for={"lib_a"})

    failures = sync.execute(TargetPlan(target="iphone", adds=(a, b)), td, fsops.HARDLINK)

    assert [lib for lib, _paths, _mode in td.scans] == ["lib_b"], "lib_b lost its scan to lib_a"
    assert len(failures) == 1 and "lib_a" in failures[0]


def test_a_failed_unlink_does_not_abort_the_rest(env, monkeypatch):
    """fsops.unlink lets every OSError but FileNotFoundError through UNWRAPPED — it is not a
    MediaSyncError, so run_cycle's handlers and main()'s both miss it and one unreadable file used
    to take down every remaining target with a traceback.

    The failing remove is FIRST, so an unguarded loop would abort before the second one and the
    sweep.
    """
    first = env.transcode / "iphone" / "standard" / "TV Shows/Locked/E01.mkv"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"x")
    second, orphan_output = _stale_and_orphan(env)

    real_remove = os.remove

    def _selective(path, **kwargs):
        if "Locked" in str(path):
            raise OSError(errno.EACCES, "permission denied")
        return real_remove(path, **kwargs)

    monkeypatch.setattr(os, "remove", _selective)
    plan = TargetPlan(
        target="iphone",
        removes=(RemoveInput(str(first)), RemoveInput(str(second))),
        deletes=(DeleteOutput(str(orphan_output)),),
    )

    failures = sync.execute(plan, FakeTdarrClient(), fsops.HARDLINK)

    assert first.exists()                       # this one genuinely could not be removed
    assert not second.exists(), "one failing unlink aborted the remaining removes"
    assert not orphan_output.exists(), "one failing unlink aborted the sweep"
    assert len(failures) == 1 and "remove input" in failures[0]
