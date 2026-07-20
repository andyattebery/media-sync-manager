"""The reconciliation brain: compute a TargetPlan per target from playlists + the filesystem.

Pure planning — reads state (Jellyfin + filesystem), returns actions, executes nothing (sync.py
does). Per target: keep each library's input folder mirrored to its playlists (add missing hardlinks,
remove un-listed ones) and sweep the `sync/` output folder to match (segment-aware), so an item that
leaves a playlist loses both its input and its transcoded output. Tdarr owns transcode tracking.
"""

from __future__ import annotations

import posixpath

from . import fsops, paths
from .errors import PathRemapError, TransientError
from .jellyfin import JellyfinClient
from .models import AddInput, Config, DeleteOutput, MediaItem, RemoveInput, Target, TargetPlan


def pick_media_source(item: MediaItem) -> str | None:
    """Choose one MediaSource path deterministically: largest by Size, tie-break by Path."""
    candidates = [ms for ms in item.media_sources if ms.path]
    if not candidates:
        return None
    best = max(candidates, key=lambda ms: (ms.size if ms.size is not None else -1, ms.path))
    return best.path


def _claimed(outrel: str, keep: set[str]) -> bool:
    """True when a sync file (`outrel` = path under sync, ext-stripped) is a wanted output.

    `keep` holds `<segment>/<rel_key>` for every desired input. Segment-aware, so a stale output
    under a different segment is not claimed and gets swept.
    """
    return any(outrel == k or outrel.endswith("/" + k) for k in keep)


def plan_all(config: Config, jellyfin: JellyfinClient) -> list[TargetPlan]:
    return [plan_target(t, config, jellyfin) for t in config.targets]


def plan_target(target: Target, config: Config, jellyfin: JellyfinClient) -> TargetPlan:
    base = posixpath.join(config.transcode_root, target.name)
    output_dir = posixpath.join(base, "sync")

    desired: dict[str, AddInput] = {}  # input_path -> AddInput
    keep: set[str] = set()  # {<segment>/<rel_key>}
    skipped: list[str] = []
    incomplete = False
    error: str | None = None

    for pl in target.playlists:
        input_seg_dir = posixpath.join(base, pl.segment)
        library_id = pl.library_id or target.library_id
        try:
            playlist_id = jellyfin.find_playlist(pl.playlist_name)
            items = jellyfin.playlist_items(playlist_id)
        except TransientError as exc:
            incomplete = True
            error = str(exc)
            skipped.append(f"{pl.playlist_name}: {exc}")
            continue
        for item in items:
            jf_path = pick_media_source(item)
            if not jf_path:
                skipped.append(f"{pl.playlist_name}/{item.name}: no usable media source")
                continue
            try:
                source = paths.to_glue(jf_path, config)
                srel = paths.source_rel(source, config.media_root)
            except PathRemapError as exc:
                skipped.append(f"{pl.playlist_name}/{item.name}: {exc}")
                continue
            relkey = paths.rel_key(srel)
            input_path = posixpath.join(input_seg_dir, srel)
            keep.add(f"{pl.segment}/{relkey}")
            desired[input_path] = AddInput(
                relkey=relkey,
                segment=pl.segment,
                playlist=pl.playlist_name,
                source=source,
                input_path=input_path,
                tdarr_path=paths.to_tdarr(input_path, config),
                library_id=library_id,
            )

    # Present inputs across the target's segment folders.
    try:
        present = {
            full
            for seg_dir in {posixpath.join(base, p.segment) for p in target.playlists}
            for full, _rel in fsops.index_files(seg_dir)
        }
    except TransientError as exc:
        return TargetPlan(target=target.name, skipped=(*skipped, str(exc)), error=str(exc))

    adds = tuple(a for ip, a in desired.items() if ip not in present)

    removes: tuple[RemoveInput, ...] = ()
    deletes: tuple[DeleteOutput, ...] = ()
    if not incomplete:
        removes = tuple(RemoveInput(ip) for ip in present if ip not in desired)
        try:
            deletes = tuple(
                DeleteOutput(full)
                for full, outrel in fsops.index_files(output_dir)
                if not _claimed(outrel, keep)
            )
        except TransientError as exc:  # sync/ offline -> skip the sweep, keep everything
            skipped.append(str(exc))

    return TargetPlan(
        target=target.name,
        adds=adds,
        removes=removes,
        deletes=deletes,
        skipped=tuple(skipped),
        error=error,
    )
