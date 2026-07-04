"""The reconciliation brain: compute a DevicePlan from the playlist, the filesystem, and config.

Pure planning — it reads state (Jellyfin + the filesystem) and returns the actions to take, but does
not execute them (sync.py does). This keeps every decision branch unit-testable by asserting the
returned plan, and makes --dry-run trivial.
"""

from __future__ import annotations

import posixpath

from . import fsops, log, paths, profile
from .errors import PathRemapError, TransientError
from .jellyfin import JellyfinClient
from .models import Config, Device, DevicePlan, DeleteOutput, MediaItem, Submit

_log = log.get("reconcile")


def pick_media_source(item: MediaItem) -> str | None:
    """Choose one MediaSource path deterministically: largest by Size, tie-break by Path."""
    candidates = [ms for ms in item.media_sources if ms.path]
    if not candidates:
        return None
    best = max(candidates, key=lambda ms: (ms.size if ms.size is not None else -1, ms.path))
    return best.path


def _suffix_match(present_rel: str, relkey: str) -> bool:
    """True when an output path (minus ext) corresponds to a desired relkey.

    Exact match, or the present path ends with `/relkey` — the latter absorbs a `<segment>/` (or
    season) prefix that a Keep-Relative-Path flow prepends. The leading slash prevents partial
    path-component matches.
    """
    return present_rel == relkey or present_rel.endswith("/" + relkey)


def plan_device(device: Device, config: Config, jellyfin: JellyfinClient) -> DevicePlan:
    """Reconcile one device. On any transient failure, return a plan with `error` set and NO
    deletes — never purge a device folder because a lookup failed."""
    try:
        playlist_id = jellyfin.find_playlist(device.playlist_name)
        items = jellyfin.playlist_items(playlist_id)
    except TransientError as exc:
        _log.warning("device %s skipped: %s", device.name, exc)
        return DevicePlan(device=device.name, error=str(exc))

    try:
        present = fsops.output_index(device.output_dir)
    except TransientError as exc:
        _log.warning("device %s skipped: %s", device.name, exc)
        return DevicePlan(device=device.name, error=str(exc))

    present_rels = [rel for _full, rel in present]

    desired: dict[str, tuple[object, str, str]] = {}  # relkey -> (profile, source_glue, source_rel)
    skipped: list[str] = []
    for item in items:
        jf_path = pick_media_source(item)
        if not jf_path:
            skipped.append(f"{item.name}: no usable media source")
            continue
        try:
            source = paths.to_glue(jf_path, config)
            srel = paths.source_rel(source, config.media_root)
        except PathRemapError as exc:
            skipped.append(f"{item.name}: {exc}")
            continue
        prof = profile.classify(item, config, jellyfin.series_genres)
        relkey = paths.rel_key(srel)
        desired[relkey] = (prof, source, srel)

    # Additions: desired items with no satisfying output and no in-flight input hardlink.
    submits: list[Submit] = []
    for relkey, (prof, source, srel) in desired.items():
        if any(_suffix_match(p, relkey) for p in present_rels):
            continue
        input_path = posixpath.join(device.input_dir, prof.segment, srel)
        if fsops.exists(input_path):
            skipped.append(f"{relkey}: in-flight")
            continue
        submits.append(
            Submit(
                relkey=relkey,
                source=source,
                input_path=input_path,
                tdarr_path=paths.to_tdarr(input_path, config),
                library_id=device.library_id,
                profile=prof.name,
            )
        )

    # Orphans: present outputs not claimed by any desired item.
    deletes = tuple(
        DeleteOutput(path=full, relkey=rel)
        for full, rel in present
        if not any(_suffix_match(rel, d) for d in desired)
    )

    return DevicePlan(
        device=device.name,
        submits=tuple(submits),
        deletes=deletes,
        skipped=tuple(skipped),
    )
