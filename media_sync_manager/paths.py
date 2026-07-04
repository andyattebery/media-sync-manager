"""Path translation between the three coordinate systems and source-relative key derivation.

Jellyfin-view (MediaSources.Path) --path_maps--> glue-view (where we read/hardlink; media_root
lives here) --tdarr_path_maps--> Tdarr-view (what scan-files receives).

All media paths are POSIX (Linux servers); we normalise to forward slashes throughout.
"""

from __future__ import annotations

import posixpath

from .errors import PathRemapError
from .models import Config, PathMap


def _normalise(path: str) -> str:
    return path.replace("\\", "/")


def remap(path: str, maps: tuple[PathMap, ...]) -> str:
    """Apply the longest-prefix-matching map. Identity when no maps are configured.

    Raises PathRemapError when maps ARE configured but none is a prefix of `path` — a configured
    remap that doesn't match is almost always a misconfiguration we want surfaced, not silently
    passed through.
    """
    path = _normalise(path)
    if not maps:
        return path
    best: PathMap | None = None
    for m in maps:
        src = _normalise(m.src).rstrip("/")
        if path == src or path.startswith(src + "/"):
            if best is None or len(src) > len(_normalise(best.src).rstrip("/")):
                best = m
    if best is None:
        raise PathRemapError(
            f"no path map matched {path!r} (configured: {[m.src for m in maps]})"
        )
    src = _normalise(best.src).rstrip("/")
    dst = _normalise(best.dst).rstrip("/")
    suffix = path[len(src):]  # includes leading slash or empty
    return dst + suffix


def to_glue(jellyfin_path: str, config: Config) -> str:
    """Translate a Jellyfin-reported media path to this host's (glue) view."""
    return remap(jellyfin_path, config.path_maps)


def to_tdarr(glue_path: str, config: Config) -> str:
    """Translate a glue-view path (an input hardlink) to Tdarr's view for scan-files."""
    return remap(glue_path, config.tdarr_path_maps)


def source_rel(glue_path: str, media_root: str) -> str:
    """Path of a source file relative to media_root. Raises if it isn't under media_root."""
    glue_path = _normalise(glue_path)
    root = _normalise(media_root).rstrip("/")
    if glue_path != root and not glue_path.startswith(root + "/"):
        raise PathRemapError(f"{glue_path!r} is not under media_root {media_root!r}")
    return glue_path[len(root):].lstrip("/")


def rel_key(rel: str) -> str:
    """The container-agnostic identity of an item: its relative path with the extension stripped.

    Includes directories (dir + stem), so same-named episodes in different series do not collide.
    """
    rel = _normalise(rel)
    stem, _ext = posixpath.splitext(rel)
    return stem
