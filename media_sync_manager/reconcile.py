"""The reconciliation brain: compute a GroupPlan per output_dir from playlists + the filesystem.

Pure planning — it reads state (Jellyfin + the filesystem) and returns the actions to take, but does
not execute them (sync.py does). Targets that share an `output_dir` are reconciled together so one
target never treats another's outputs as orphans; matching is segment-aware
(`<segment>/<relkey>`), so moving an item between playlists re-encodes it and retires the old output.
"""

from __future__ import annotations

import posixpath
from collections import OrderedDict

from . import fsops, paths
from .errors import PathRemapError, TransientError
from .jellyfin import JellyfinClient
from .models import Config, DeleteOutput, GroupPlan, MediaItem, Submit, Target


def pick_media_source(item: MediaItem) -> str | None:
    """Choose one MediaSource path deterministically: largest by Size, tie-break by Path."""
    candidates = [ms for ms in item.media_sources if ms.path]
    if not candidates:
        return None
    best = max(candidates, key=lambda ms: (ms.size if ms.size is not None else -1, ms.path))
    return best.path


def _claims(present_rel: str, match_key: str) -> bool:
    """True when an output path (minus ext) is the output for `match_key` (= <segment>/<relkey>).

    Exact match, or the present path ends with `/match_key` — the latter absorbs an extra prefix a
    flow might add above the segment. The segment is part of the key, so a stale output under a
    different segment does NOT match (re-categorised items get re-encoded, old outputs retired).
    """
    return present_rel == match_key or present_rel.endswith("/" + match_key)


def plan_all(config: Config, jellyfin: JellyfinClient) -> list[GroupPlan]:
    """One GroupPlan per distinct output_dir, in first-seen order."""
    groups: "OrderedDict[str, list[Target]]" = OrderedDict()
    for target in config.targets:
        groups.setdefault(target.output_dir, []).append(target)
    return [_plan_group(out, targets, config, jellyfin) for out, targets in groups.items()]


def _plan_group(
    output_dir: str, targets: list[Target], config: Config, jellyfin: JellyfinClient
) -> GroupPlan:
    desired: "OrderedDict[str, tuple[Target, str, str, str]]" = OrderedDict()
    skipped: list[str] = []
    incomplete = False  # a target failed to fetch -> we must not delete (its items look orphaned)
    error: str | None = None

    for target in targets:
        try:
            playlist_id = jellyfin.find_playlist(target.playlist_name)
            items = jellyfin.playlist_items(playlist_id)
        except TransientError as exc:
            incomplete = True
            error = str(exc)
            skipped.append(f"{target.playlist_name}: {exc}")
            continue
        for item in items:
            jf_path = pick_media_source(item)
            if not jf_path:
                skipped.append(f"{target.playlist_name}/{item.name}: no usable media source")
                continue
            try:
                source = paths.to_glue(jf_path, config)
                srel = paths.source_rel(source, config.media_root)
            except PathRemapError as exc:
                skipped.append(f"{target.playlist_name}/{item.name}: {exc}")
                continue
            relkey = paths.rel_key(srel)
            match_key = f"{target.segment}/{relkey}"
            if match_key not in desired:  # same item in two same-segment playlists -> first wins
                desired[match_key] = (target, source, srel, relkey)

    try:
        present = fsops.output_index(output_dir)
    except TransientError as exc:
        return GroupPlan(output_dir=output_dir, skipped=(*skipped, str(exc)), error=str(exc))
    present_rels = [rel for _full, rel in present]

    submits: list[Submit] = []
    for match_key, (target, source, srel, relkey) in desired.items():
        if any(_claims(p, match_key) for p in present_rels):
            continue
        input_path = posixpath.join(target.input_dir, target.segment, srel)
        if fsops.exists(input_path):
            skipped.append(f"{match_key}: in-flight")
            continue
        submits.append(
            Submit(
                relkey=relkey,
                segment=target.segment,
                playlist=target.playlist_name,
                source=source,
                input_path=input_path,
                tdarr_path=paths.to_tdarr(input_path, config),
                library_id=target.library_id,
            )
        )

    # Orphans: present outputs claimed by no desired key. Suppressed when the group is incomplete.
    deletes: tuple[DeleteOutput, ...] = ()
    if not incomplete:
        deletes = tuple(
            DeleteOutput(path=full, match_key=rel)
            for full, rel in present
            if not any(_claims(rel, key) for key in desired)
        )

    return GroupPlan(
        output_dir=output_dir,
        submits=tuple(submits),
        deletes=deletes,
        skipped=tuple(skipped),
        error=error,
    )
