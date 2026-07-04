from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from media_sync_manager import fsops
from media_sync_manager.errors import PermanentError, TransientError


def test_hardlink_shares_inode_and_creates_parents(tmp_path: Path):
    src = tmp_path / "media" / "Show" / "ep.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data")
    dest = tmp_path / "in" / "iphone" / "standard" / "Show" / "ep.mkv"

    fsops.hardlink(str(src), str(dest))

    assert dest.exists()
    assert os.stat(src).st_ino == os.stat(dest).st_ino
    assert os.stat(src).st_nlink >= 2
    # parent dirs created traversable
    assert os.stat(dest.parent).st_mode & 0o755 == 0o755


def test_hardlink_idempotent(tmp_path: Path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    dest = tmp_path / "b.mkv"
    fsops.hardlink(str(src), str(dest))
    fsops.hardlink(str(src), str(dest))  # no error
    assert dest.exists()


def test_hardlink_does_not_modify_source(tmp_path: Path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"original")
    dest = tmp_path / "sub" / "b.mkv"
    fsops.hardlink(str(src), str(dest))
    assert src.read_bytes() == b"original"


def test_hardlink_exdev_raises_permanent(tmp_path: Path, monkeypatch):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    dest = tmp_path / "b.mkv"

    def fake_link(_s, _d):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", fake_link)
    with pytest.raises(PermanentError):
        fsops.hardlink(str(src), str(dest))


def test_output_index_strips_ext_and_is_relative(tmp_path: Path):
    out = tmp_path / "out"
    (out / "animation" / "Show" / "S01").mkdir(parents=True)
    (out / "animation" / "Show" / "S01" / "ep.mkv").write_bytes(b"x")
    index = fsops.output_index(str(out))
    rels = [rel for _full, rel in index]
    assert rels == ["animation/Show/S01/ep"]


def test_output_index_missing_dir_is_empty(tmp_path: Path):
    assert fsops.output_index(str(tmp_path / "nope")) == []


def test_output_index_unreadable_raises_transient(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    os.chmod(out, 0o000)
    try:
        with pytest.raises(TransientError):
            fsops.output_index(str(out))
    finally:
        os.chmod(out, 0o755)  # restore so tmp cleanup works


def test_delete_output_removes_only_target(tmp_path: Path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    fsops.delete_output(str(a))
    assert not a.exists()
    assert b.exists()
