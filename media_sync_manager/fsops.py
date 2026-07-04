"""Filesystem operations: hardlink (with EXDEV guard), output indexing, orphan delete.

The glue only ever reads originals, creates hardlinks into input folders, and deletes files inside
device output folders. It never writes to or deletes originals.
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
    design (and, in Docker, means media and input_dir were bind-mounted from different volumes).
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
                "media and input_dir must share one filesystem "
                "(in Docker, mount their common parent as a single volume)."
            ) from exc
        raise


def output_index(output_dir: str) -> list[tuple[str, str]]:
    """List every file under `output_dir` as (full_path, rel_no_ext).

    `rel_no_ext` is the path relative to output_dir, POSIX-normalised, with the extension stripped —
    the same shape as a desired `relkey`, so matching is a direct comparison.

    A non-existent output_dir means "nothing synced yet" -> empty. An existing-but-inaccessible
    output_dir (e.g. an SMB mount that's offline) raises TransientError so the caller skips the
    device without computing orphans.
    """
    if not os.path.exists(output_dir):
        return []
    if not os.access(output_dir, os.R_OK | os.X_OK):
        raise TransientError(f"output_dir not accessible: {output_dir}")
    out: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(output_dir):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, output_dir).replace("\\", "/")
            rel_no_ext = os.path.splitext(rel)[0]
            out.append((full, rel_no_ext))
    return out


def delete_output(path: str) -> None:
    """Delete a single output file (only ever called for orphans inside a device folder)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # already gone; fine
    _log.info("deleted orphan output %s", path)
