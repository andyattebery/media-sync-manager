"""Filesystem operations: hardlink (with EXDEV guard), directory indexing, unlink.

The glue only ever reads originals, creates hardlinks into input folders, and deletes files inside
the input/output folders under transcode_root. It never writes to or deletes originals.
"""

from __future__ import annotations

import errno
import os

from . import log
from .errors import PermanentError, TransientError

_log = log.get("fsops")

# Mode for directories we create under input_dir, so a non-root Tdarr can traverse to the hardlink.
_DIR_MODE = 0o755


def exists(path: str) -> bool:
    return os.path.lexists(path)


def hardlink(source: str, dest: str) -> None:
    """Hardlink `source` -> `dest`, creating parent dirs. No-op if `dest` already exists.

    Raises PermanentError on EXDEV: source and dest are on different filesystems, which defeats the
    design (media_root and transcode_root must be one filesystem).
    """
    if exists(dest):
        return
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, mode=_DIR_MODE, exist_ok=True)
    try:
        os.link(source, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise PermanentError(
                f"cannot hardlink across filesystems: {source!r} -> {dest!r}. "
                "media_root and transcode_root must share one filesystem "
                "(in Docker, mount their common parent as a single volume)."
            ) from exc
        raise


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


def unlink(path: str) -> None:
    """Delete a single file (an input hardlink or a swept output — never a real original)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # already gone; fine
    _log.info("deleted %s", path)
