"""Filesystem operations: create an input pointing at an original, index dirs, unlink.

The glue only ever reads originals, creates inputs (hardlink or symlink) into input folders, and
deletes files inside the input/output folders under transcode_root. It never writes to or deletes
originals.

Which primitive to use is a property of the filesystem topology, not something an operator should
have to reason about, so `detect_mode` probes for it — see the module's MODES and `detect_mode`.
"""

from __future__ import annotations

import errno
import os
import tempfile
from typing import Iterable

from . import log
from .errors import PermanentError, TransientError

_log = log.get("fsops")

# Mode for directories we create under input_dir, so a non-root Tdarr can traverse to the input.
_DIR_MODE = 0o755

AUTO = "auto"
HARDLINK = "hardlink"
SYMLINK = "symlink"
MODES = (AUTO, HARDLINK, SYMLINK)

_PROBE_PREFIX = ".msm-probe-"

# Memoised result of detect_mode(). Reset between tests via _reset_detected().
_detected: str | None = None

# errnos that mean "this will not fix itself" -> PermanentError rather than retry-forever.
_PERMANENT = frozenset({errno.EXDEV, errno.EACCES, errno.EPERM, errno.EROFS})


def exists(path: str) -> bool:
    return os.path.lexists(path)


def _rm(path: str) -> None:
    """Remove a file, tolerating that it is already gone. No logging (used by probes)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def unlink(path: str) -> None:
    """Delete a single file (an input, or a swept output — never a real original)."""
    _rm(path)
    _log.info("deleted %s", path)


# --- creating an input --------------------------------------------------------


def link_target(source: str, dest: str) -> str:
    """The *relative* target for a symlink at `dest` pointing to `source`.

    Relative, never absolute, for two reasons: it resolves identically in every mount namespace that
    sees the same tree (so the glue's view and Tdarr's view need not agree), and Samba's default
    `wide links = no` refuses to follow a link that resolves outside the exported share — which it
    silently does by omitting the entry entirely, not by showing a broken link.
    """
    return os.path.relpath(source, os.path.dirname(dest))


def is_current(source: str, dest: str) -> bool:
    """True when `dest` is a working input for `source`, by either mechanism.

    Deliberately mode-free. An entry created under one mode stays valid under the other, so changing
    the input mode causes no churn, and `reconcile` never needs to know the mode — which is what
    keeps `status` and `--dry-run` write-free.

    False for: nothing there; a hardlink to a stale inode (the original was replaced in place); a
    symlink pointing somewhere else; a dangling symlink (invisible to SMB clients, so it is broken);
    and a plain copy left at that path (e.g. mergerfs `moveonenospc` relocating a file and breaking
    the link). Each of those gets repaired rather than trusted forever.
    """
    if not exists(dest):
        return False
    if os.path.islink(dest):
        target = os.path.join(os.path.dirname(dest), os.readlink(dest))
        if os.path.normpath(target) != os.path.normpath(source):
            return False
        return os.path.exists(dest)  # follows the link: False when dangling
    try:
        return os.path.samefile(source, dest)
    except OSError:
        return False


def _create(source: str, dest: str, mode: str) -> None:
    if mode == SYMLINK:
        os.symlink(link_target(source, dest), dest)
    else:
        os.link(source, dest)


def _wrap(exc: OSError, source: str, dest: str, mode: str) -> Exception:
    if exc.errno == errno.EXDEV:
        return PermanentError(
            f"cannot hardlink {source!r} -> {dest!r}: the destination directory is on a different "
            f"underlying disk than the source. Note both paths can be inside one mount and still "
            f"fail this — a union filesystem (mergerfs) reports a single st_dev for the whole pool. "
            f"Leave input_mode at {AUTO!r} to fall back to symlinks."
        )
    if exc.errno in _PERMANENT:
        return PermanentError(f"cannot create {mode} input {dest!r}: {exc}")
    return TransientError(f"cannot create {mode} input {dest!r}: {exc}")


def materialize(source: str, dest: str, mode: str) -> None:
    """Create an input at `dest` pointing at `source`, replacing anything stale already there."""
    if is_current(source, dest):
        return
    if exists(dest):
        # Present but not current: a stale inode, a dangling link, or a stray copy. os.link and
        # os.symlink both raise FileExistsError on an occupied path, so clear it first.
        unlink(dest)
    try:
        try:
            _create(source, dest, mode)
        except FileNotFoundError:
            # Parent missing — create it only *now*, never up front. On a union filesystem creating
            # the destination directory before attempting the link pins it to whichever branch the
            # create policy picks, which is usually not the source's branch, guaranteeing EXDEV.
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, mode=_DIR_MODE, exist_ok=True)
            _create(source, dest, mode)
    except OSError as exc:
        raise _wrap(exc, source, dest, mode) from exc


# --- choosing the mode --------------------------------------------------------


def _reset_detected() -> None:
    """Clear the memoised probe result. For tests only — otherwise suite order fixes the mode."""
    global _detected
    _detected = None


def probe(transcode_root: str, input_dirs: Iterable[str]) -> tuple[str, str]:
    """Work out which primitive this filesystem supports. Returns (mode, human-readable reason).

    Probes *every* input dir and picks SYMLINK if hardlinking into any one of them fails: under a
    path-preserving mergerfs policy, success depends on the branch placement of that specific
    destination directory, so one dir proves nothing about another. A single global mode keeps the
    input tree homogeneous.

    Writes only inside transcode_root — never under media_root, which the glue must not touch.

    So this is a screen, not a guarantee: on a union filesystem whether a hardlink succeeds also
    depends on which underlying disk the *source* sits on, and the probe's source is a temp file, not
    a real original. HARDLINK can therefore be selected and individual files still fail EXDEV later.
    That is survivable because `sync.execute` isolates each input and reports the failures, rather
    than letting one abort the cycle.
    """
    try:
        os.makedirs(transcode_root, mode=_DIR_MODE, exist_ok=True)
        fd, src = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=transcode_root)
        os.close(fd)
    except OSError as exc:
        raise PermanentError(f"cannot write a probe file in {transcode_root!r}: {exc}") from exc
    try:
        for d in input_dirs:
            try:
                os.makedirs(d, mode=_DIR_MODE, exist_ok=True)
            except OSError as exc:
                raise PermanentError(f"cannot create input dir {d!r}: {exc}") from exc
            dest = os.path.join(d, os.path.basename(src))
            try:
                try:
                    os.link(src, dest)
                except OSError as exc:
                    reason = (
                        f"hardlink into {d!r} failed EXDEV (union or cross-device)"
                        if exc.errno == errno.EXDEV
                        else f"hardlink into {d!r} failed: {exc}"
                    )
                    # Do not select symlink without confirming it actually works: on a CIFS/SMB
                    # mount the SMB protocol has no symlink-creation operation and this fails too.
                    try:
                        os.symlink(link_target(src, dest), dest)
                    except OSError as sym_exc:
                        raise PermanentError(
                            f"neither hardlink nor symlink works from {transcode_root!r} into "
                            f"{d!r}: link failed ({exc}), symlink failed ({sym_exc}). If this is a "
                            f"CIFS/SMB mount, symlink creation is not supported by the protocol — "
                            f"run the glue where the filesystem is local."
                        ) from sym_exc
                    return SYMLINK, reason
            finally:
                _rm(dest)
        return HARDLINK, "hardlink works into every input dir"
    finally:
        _rm(src)


def detect_mode(transcode_root: str, input_dirs: Iterable[str], configured: str = AUTO) -> str:
    """Resolve the input mode, memoised for the process.

    `configured` other than AUTO is taken as-is and not probed — an operator who names a mode wants
    a hard failure if it does not work, not a silent downgrade.
    """
    global _detected
    if configured != AUTO:
        return configured
    if _detected is None:
        _detected, reason = probe(transcode_root, tuple(input_dirs))
        _log.info("input mode: %s (%s)", _detected, reason)
    return _detected


# --- indexing -----------------------------------------------------------------


def index_files(root: str) -> list[tuple[str, str]]:
    """List every file under `root` as (full_path, rel_no_ext).

    `rel_no_ext` is the path relative to `root`, POSIX-normalised, extension stripped. Used for both
    input folders (compare full paths) and the sync folder (segment-aware output sweep).

    A non-existent `root` means "nothing there yet" -> empty. An existing-but-inaccessible `root`
    (e.g. an offline mount) raises TransientError so the caller can skip without deleting anything.
    """
    if not os.path.exists(root):
        return []
    if not os.access(root, os.R_OK | os.X_OK):
        raise TransientError(f"directory not accessible: {root}")
    out: list[tuple[str, str]] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((full, os.path.splitext(rel)[0]))
    return out
