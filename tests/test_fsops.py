from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from media_sync_manager import fsops
from media_sync_manager.errors import PermanentError, TransientError


def _exdev(*_args, **_kwargs):
    raise OSError(errno.EXDEV, "cross-device link")


# --- creating an input -------------------------------------------------------


def test_hardlink_shares_inode_and_creates_parents(tmp_path: Path):
    src = tmp_path / "media" / "Show" / "ep.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data")
    dest = tmp_path / "in" / "iphone" / "standard" / "Show" / "ep.mkv"

    fsops.materialize(str(src), str(dest), fsops.HARDLINK)

    assert dest.exists()
    assert os.stat(src).st_ino == os.stat(dest).st_ino
    assert os.stat(src).st_nlink >= 2
    # parent dirs created traversable
    assert os.stat(dest.parent).st_mode & 0o755 == 0o755


def test_symlink_is_relative_and_creates_parents(tmp_path: Path):
    src = tmp_path / "media" / "Show" / "ep.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data")
    dest = tmp_path / "media" / "transcode" / "iphone" / "standard" / "Show" / "ep.mkv"

    fsops.materialize(str(src), str(dest), fsops.SYMLINK)

    assert dest.is_symlink()
    assert not os.path.isabs(os.readlink(dest))  # relative, so it survives a mount-prefix change
    assert dest.read_bytes() == b"data"
    assert os.stat(dest.parent).st_mode & 0o755 == 0o755


def test_relative_symlink_resolves_under_a_different_mount_prefix(tmp_path: Path):
    """The link is created under one root and read back through another path to the same tree."""
    real = tmp_path / "real"
    (real / "media" / "Show").mkdir(parents=True)
    (real / "media" / "Show" / "ep.mkv").write_bytes(b"data")
    dest = real / "media" / "transcode" / "standard" / "Show" / "ep.mkv"
    fsops.materialize(str(real / "media" / "Show" / "ep.mkv"), str(dest), fsops.SYMLINK)

    alias = tmp_path / "alias"
    alias.symlink_to(real)  # a second name for the same tree, as a second mount would be
    via_alias = alias / "media" / "transcode" / "standard" / "Show" / "ep.mkv"
    assert via_alias.read_bytes() == b"data"


@pytest.mark.parametrize("mode", [fsops.HARDLINK, fsops.SYMLINK])
def test_materialize_is_idempotent(tmp_path: Path, mode: str):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    dest = tmp_path / "sub" / "b.mkv"

    fsops.materialize(str(src), str(dest), mode)
    before = os.lstat(dest)
    fsops.materialize(str(src), str(dest), mode)  # no error, no churn

    assert fsops.is_current(str(src), str(dest))
    assert os.lstat(dest).st_ino == before.st_ino


@pytest.mark.parametrize("mode", [fsops.HARDLINK, fsops.SYMLINK])
def test_materialize_does_not_modify_source(tmp_path: Path, mode: str):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"original")
    fsops.materialize(str(src), str(tmp_path / "sub" / "b.mkv"), mode)
    assert src.read_bytes() == b"original"


def test_materialize_replaces_a_stale_inode(tmp_path: Path):
    """The original was replaced in place: same path, new inode. The old link must be repaired."""
    src = tmp_path / "a.mkv"
    src.write_bytes(b"v1")
    dest = tmp_path / "sub" / "b.mkv"
    fsops.materialize(str(src), str(dest), fsops.HARDLINK)

    src.unlink()
    src.write_bytes(b"v2")  # new inode at the same path
    assert not fsops.is_current(str(src), str(dest))

    fsops.materialize(str(src), str(dest), fsops.HARDLINK)

    assert dest.read_bytes() == b"v2"
    assert os.stat(src).st_ino == os.stat(dest).st_ino


def test_materialize_replaces_a_plain_copy(tmp_path: Path):
    """mergerfs moveonenospc (or a restore) can silently turn a link into an independent copy."""
    src = tmp_path / "a.mkv"
    src.write_bytes(b"data")
    dest = tmp_path / "sub" / "b.mkv"
    dest.parent.mkdir()
    dest.write_bytes(b"data")  # right path, right content, not linked

    assert not fsops.is_current(str(src), str(dest))
    fsops.materialize(str(src), str(dest), fsops.HARDLINK)
    assert os.stat(src).st_ino == os.stat(dest).st_ino


def test_materialize_replaces_a_dangling_symlink(tmp_path: Path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    dest = tmp_path / "sub" / "b.mkv"
    dest.parent.mkdir()
    dest.symlink_to("nowhere.mkv")

    assert not fsops.is_current(str(src), str(dest))
    fsops.materialize(str(src), str(dest), fsops.SYMLINK)
    assert dest.read_bytes() == b"x"


def test_materialize_replaces_the_other_mode(tmp_path: Path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    dest = tmp_path / "sub" / "b.mkv"
    fsops.materialize(str(src), str(dest), fsops.HARDLINK)

    # Already a working input, so switching mode is a no-op — no churn on an existing tree.
    fsops.materialize(str(src), str(dest), fsops.SYMLINK)
    assert not dest.is_symlink()


def test_hardlink_exdev_raises_permanent(tmp_path: Path, monkeypatch):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    monkeypatch.setattr(os, "link", _exdev)
    with pytest.raises(PermanentError):
        fsops.materialize(str(src), str(tmp_path / "b.mkv"), fsops.HARDLINK)


def test_non_exdev_oserror_is_typed(tmp_path: Path, monkeypatch):
    """EACCES/EIO/... used to escape the typed-error contract and kill the whole cycle."""
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")

    def _eio(*_args, **_kwargs):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "link", _eio)
    with pytest.raises(TransientError):
        fsops.materialize(str(src), str(tmp_path / "b.mkv"), fsops.HARDLINK)

    def _eacces(*_args, **_kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(os, "link", _eacces)
    with pytest.raises(PermanentError):
        fsops.materialize(str(src), str(tmp_path / "c.mkv"), fsops.HARDLINK)


# --- is_current --------------------------------------------------------------


def test_is_current_accepts_either_mechanism(tmp_path: Path):
    """Mode-free by design: a tree may hold both, and changing mode must not force a rewrite."""
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    hard = tmp_path / "hard.mkv"
    soft = tmp_path / "soft.mkv"
    fsops.materialize(str(src), str(hard), fsops.HARDLINK)
    fsops.materialize(str(src), str(soft), fsops.SYMLINK)

    assert fsops.is_current(str(src), str(hard))
    assert fsops.is_current(str(src), str(soft))


def test_is_current_false_for_missing_and_wrong_target(tmp_path: Path):
    src = tmp_path / "a.mkv"
    other = tmp_path / "other.mkv"
    src.write_bytes(b"x")
    other.write_bytes(b"y")

    assert not fsops.is_current(str(src), str(tmp_path / "nope.mkv"))

    wrong = tmp_path / "wrong.mkv"
    fsops.materialize(str(other), str(wrong), fsops.SYMLINK)
    assert not fsops.is_current(str(src), str(wrong))


# --- mode detection ----------------------------------------------------------


def test_probe_picks_hardlink_when_link_works(tmp_path: Path):
    mode, reason = fsops.probe(str(tmp_path / "transcode"), [str(tmp_path / "transcode" / "seg")])
    assert mode == fsops.HARDLINK
    assert reason


def test_probe_picks_symlink_on_exdev(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(os, "link", _exdev)
    mode, reason = fsops.probe(str(tmp_path / "transcode"), [str(tmp_path / "transcode" / "seg")])
    assert mode == fsops.SYMLINK
    assert "EXDEV" in reason


def test_probe_picks_symlink_when_only_one_dir_fails(tmp_path: Path, monkeypatch):
    """Union branch placement is per-directory, so one good dir proves nothing about another."""
    root = tmp_path / "transcode"
    good = str(root / "2d-animation")
    bad = str(root / "standard")
    real_link = os.link

    def _selective(src, dst, **kwargs):
        if os.path.dirname(dst) == bad:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", _selective)
    mode, reason = fsops.probe(str(root), [good, bad])
    assert mode == fsops.SYMLINK
    assert "standard" in reason


def test_probe_fails_when_neither_primitive_works(tmp_path: Path, monkeypatch):
    """A CIFS mount supports neither: SMB has no symlink-create op. Must not silently pick one."""
    monkeypatch.setattr(os, "link", _exdev)

    def _eio(*_args, **_kwargs):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "symlink", _eio)
    with pytest.raises(PermanentError, match="neither hardlink nor symlink"):
        fsops.probe(str(tmp_path / "transcode"), [str(tmp_path / "transcode" / "seg")])


def test_probe_leaves_nothing_behind(tmp_path: Path):
    root = tmp_path / "transcode"
    seg = root / "seg"
    fsops.probe(str(root), [str(seg)])
    assert list(root.iterdir()) == [seg]
    assert list(seg.iterdir()) == []


def test_detect_mode_is_memoised_and_honours_explicit(tmp_path: Path, monkeypatch):
    root = str(tmp_path / "transcode")
    dirs = [str(tmp_path / "transcode" / "seg")]

    assert fsops.detect_mode(root, dirs) == fsops.HARDLINK
    monkeypatch.setattr(os, "link", _exdev)  # would flip the answer if it re-probed
    assert fsops.detect_mode(root, dirs) == fsops.HARDLINK

    # An explicitly configured mode is never probed — the operator wants a hard failure, not a
    # silent downgrade.
    assert fsops.detect_mode(root, dirs, fsops.SYMLINK) == fsops.SYMLINK


# --- indexing ----------------------------------------------------------------


def test_index_files_strips_ext_and_is_relative(tmp_path: Path):
    out = tmp_path / "out"
    (out / "animation" / "Show" / "S01").mkdir(parents=True)
    (out / "animation" / "Show" / "S01" / "ep.mkv").write_bytes(b"x")
    index = fsops.index_files(str(out))
    rels = [rel for _full, rel in index]
    assert rels == ["animation/Show/S01/ep"]


def test_index_files_missing_dir_is_empty(tmp_path: Path):
    assert fsops.index_files(str(tmp_path / "nope")) == []


def test_index_files_unreadable_raises_transient(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    os.chmod(out, 0o000)
    try:
        with pytest.raises(TransientError):
            fsops.index_files(str(out))
    finally:
        os.chmod(out, 0o755)  # restore so tmp cleanup works


def test_unlink_removes_only_target(tmp_path: Path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    fsops.unlink(str(a))
    assert not a.exists()
    assert b.exists()
