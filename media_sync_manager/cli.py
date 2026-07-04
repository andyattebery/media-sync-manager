"""Command-line entry point: run | sync [--once] [--dry-run] | status | doctor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import log, paths, poller, reconcile, sync
from .errors import MediaSyncError, TransientError
from .jellyfin import JellyfinClient
from .models import Config
from .tdarr import TdarrClient

Out = Callable[[str], None]


def _build_clients(config: Config) -> tuple[JellyfinClient, TdarrClient]:
    jellyfin = JellyfinClient(config.jellyfin)
    tdarr = TdarrClient(config.tdarr)
    return jellyfin, tdarr


def cmd_sync(
    config: Config, jellyfin: JellyfinClient, tdarr: TdarrClient, *, dry_run: bool, out: Out = print
) -> int:
    if dry_run:
        out("# dry-run: no files will be linked, scanned, or deleted")
    plans = sync.run_cycle(config, jellyfin, tdarr, dry_run=dry_run)
    for plan in plans:
        for line in sync.describe(plan):
            out(line)
    return 1 if any(p.error for p in plans) else 0


def cmd_status(
    config: Config, jellyfin: JellyfinClient, tdarr: TdarrClient, *, out: Out = print
) -> int:
    plans = reconcile.plan_all(config, jellyfin)
    for plan in plans:
        for line in sync.describe(plan):
            out(line)
    return 1 if any(p.error for p in plans) else 0


def cmd_run(config: Config, jellyfin: JellyfinClient, tdarr: TdarrClient) -> int:
    poller.run_forever(config, jellyfin, tdarr)
    return 0


def _st_dev(path: str) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def cmd_doctor(
    config: Config, jellyfin: JellyfinClient, tdarr: TdarrClient, *, out: Out = print
) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        out(f"[{'OK ' if passed else 'FAIL'}] {label}{': ' + detail if detail else ''}")

    # Jellyfin reachability + auth + playlists exist (one check per distinct playlist).
    for name in dict.fromkeys(t.playlist_name for t in config.targets):
        try:
            jellyfin.find_playlist(name)
            check(f"jellyfin playlist '{name}'", True)
        except TransientError as exc:
            check(f"jellyfin playlist '{name}'", False, str(exc))

    # Tdarr reachability + auth + libraries exist; compare source folder in tdarr-view.
    try:
        libs = tdarr.list_libraries()
        by_id = {str(lib.get("_id")): lib for lib in libs}
        check("tdarr reachable + libraries listed", True, f"{len(libs)} libraries")
        for lib_id, input_dir in dict.fromkeys((t.library_id, t.input_dir) for t in config.targets):
            lib = by_id.get(lib_id)
            if lib is None:
                check(f"library_id '{lib_id}'", False, "not found")
                continue
            want = paths.to_tdarr(input_dir, config)
            src = _library_source(lib)
            matches = src is not None and (src.rstrip("/") == want.rstrip("/"))
            check(
                f"library '{lib_id}' source == tdarr_view(input_dir)",
                matches,
                f"tdarr={src!r} want={want!r}",
            )
    except TransientError as exc:
        check("tdarr reachable", False, str(exc))

    # Hardlink precondition: media_root and each input_dir share a filesystem.
    media_dev = _st_dev(config.media_root)
    for input_dir in dict.fromkeys(t.input_dir for t in config.targets):
        same = _st_dev(input_dir) == media_dev
        check(f"media_root <-> input_dir '{input_dir}' same filesystem", same)

    out("NOTE: scan_mode assumed 'scanFolderWatcher'; confirm a single file enqueues on this instance.")
    return 0 if ok else 1


def _library_source(lib: dict) -> str | None:
    """Best-effort extraction of a Tdarr library's source folder (field name varies by version)."""
    for key in ("folder", "path", "sourceFolder", "source"):
        val = lib.get(key)
        if isinstance(val, str) and val:
            return val
    folders = lib.get("folders") or lib.get("scannerThreads")
    if isinstance(folders, list) and folders:
        first = folders[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            for key in ("path", "folder"):
                if isinstance(first.get(key), str):
                    return first[key]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-sync-manager")
    parser.add_argument("--config", default="/etc/media-sync-manager/config.yaml", help="path to config YAML")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the poller daemon")
    p_sync = sub.add_parser("sync", help="run one reconcile cycle")
    p_sync.add_argument("--once", action="store_true", help="single pass (default for sync)")
    p_sync.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    sub.add_parser("status", help="show desired vs present per output group (read-only)")
    sub.add_parser("doctor", help="validate config, connectivity, and preconditions")
    return parser


def main(argv: list[str] | None = None) -> int:
    log.setup()
    args = build_parser().parse_args(argv)
    try:
        config = config_mod.load(args.config)
    except MediaSyncError as exc:
        print(f"config error: {exc}")
        return 2
    jellyfin, tdarr = _build_clients(config)

    if args.command == "run":
        return cmd_run(config, jellyfin, tdarr)
    if args.command == "sync":
        return cmd_sync(config, jellyfin, tdarr, dry_run=args.dry_run)
    if args.command == "status":
        return cmd_status(config, jellyfin, tdarr)
    if args.command == "doctor":
        return cmd_doctor(config, jellyfin, tdarr)
    return 2
