"""Command-line entry point: run | sync [--once] [--dry-run] | status | doctor."""

from __future__ import annotations

import argparse
from typing import Callable

from . import config as config_mod
from . import fsops, log, paths, poller, reconcile, sync
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
        out("# dry-run: nothing will be created, scanned, or deleted")
    results = sync.run_cycle(config, jellyfin, tdarr, dry_run=dry_run)
    for result in results:
        for line in sync.describe(result.plan):
            out(line)
        for failure in result.failures:
            out(f"[{result.plan.target}] FAILED input: {failure}")
    return 0 if all(r.ok for r in results) else 1


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


def _transcode_under_media(config: Config) -> bool:
    """True when transcode_root is nested inside media_root.

    That nesting is what keeps a relative symlink (input -> original) from ever resolving outside a
    share rooted at or above media_root. Samba's default `wide links = no` refuses to follow a link
    that resolves outside the export, and it refuses by *omitting the entry* — Tdarr sees no file at
    all rather than a broken one, which is the worst possible thing to debug.

    Testing for a shared ancestor would be useless: every pair of paths shares one. Nesting is the
    only property that holds regardless of where the share root actually is, which the glue cannot
    see from inside a container.
    """
    media = config.media_root.rstrip("/")
    return config.transcode_root.rstrip("/").startswith(media + "/")


def cmd_doctor(
    config: Config, jellyfin: JellyfinClient, tdarr: TdarrClient, *, out: Out = print
) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        out(f"[{'OK ' if passed else 'FAIL'}] {label}{': ' + detail if detail else ''}")

    # Jellyfin reachability + auth + playlists exist (one check per distinct playlist).
    for name in dict.fromkeys(pl.playlist_name for t in config.targets for pl in t.playlists):
        try:
            jellyfin.find_playlist(name)
            check(f"jellyfin playlist '{name}'", True)
        except TransientError as exc:
            check(f"jellyfin playlist '{name}'", False, str(exc))

    # Tdarr reachability + auth + libraries exist; each library must watch (an ancestor of) its
    # segment input dir, compared in tdarr-view.
    pairs = dict.fromkeys(
        (pl.library_id or t.library_id, paths.input_dir(config, t.name, pl.segment))
        for t in config.targets
        for pl in t.playlists
    )
    try:
        libs = tdarr.list_libraries()
        by_id = {str(lib.get("_id")): lib for lib in libs}
        check("tdarr reachable + libraries listed", True, f"{len(libs)} libraries")
        for lib_id, input_dir in pairs:
            lib = by_id.get(lib_id)
            if lib is None:
                check(f"library_id '{lib_id}'", False, "not found")
                continue
            want = paths.to_tdarr(input_dir, config)
            src = _library_source(lib)
            src_ok = src is not None and (
                want == src.rstrip("/") or want.startswith(src.rstrip("/") + "/")
            )
            check(
                f"library '{lib_id}' watches '{input_dir}'",
                src_ok,
                f"tdarr_source={src!r} input(tdarr-view)={want!r}",
            )
    except TransientError as exc:
        check("tdarr reachable", False, str(exc))

    # How inputs get created. Probe it rather than infer it: the old check compared st_dev, which is
    # identical across every branch of a union mount and so passed on exactly the topology that
    # cannot hardlink. This writes (and removes) temp files under transcode_root, never media_root.
    mode = config.input_mode
    try:
        if mode == fsops.AUTO:
            mode, reason = fsops.probe(config.transcode_root, paths.all_input_dirs(config))
            check("input mode", True, f"{mode} ({reason})")
        else:
            check("input mode", True, f"{mode} (set explicitly; not probed)")
    except MediaSyncError as exc:
        check("input mode", False, str(exc))
        mode = None
    if mode == fsops.HARDLINK:
        out("NOTE: probing covers transcode_root -> each input dir, which is where union branch")
        out("      placement fails; it does not separately prove media_root -> input dir.")
    if mode == fsops.SYMLINK:
        check(
            "transcode_root is under media_root",
            _transcode_under_media(config),
            f"{config.transcode_root!r} is outside {config.media_root!r}, so a relative symlink "
            "between them resolves outside an SMB share; Samba's default 'wide links = no' then "
            "hides the file from Tdarr entirely rather than showing a broken link",
        )
        out("NOTE: symlink inputs rely on Tdarr reaching the media over a share that resolves links")
        out("      server-side (SMB/NFS), where it sees plain files. A Tdarr with local filesystem")
        out("      access would see real symlinks instead, which is untested.")

    out("NOTE: your Tdarr flow must KEEP its input file after transcode, and must NOT process")
    out("      <target>/sync (point the library at the segment folders or filter out /sync/).")
    out("NOTE: Enable Folder Watch on each library (Library settings -> Folder Watch). It is what")
    out("      notices an input has been deleted and retires the file; the glue's scan-files call")
    out("      only makes pickup of new inputs immediate.")
    return 0 if ok else 1


def cmd_web(
    config: Config,
    jellyfin: JellyfinClient,
    tdarr: TdarrClient,
    *,
    host: str,
    port: int,
    out: Out = print,
) -> int:
    """Serve the playlist editor.

    `config` and `tdarr` are inert here — the web layer reads only the Jellyfin client — but the
    signature matches every other command so main()'s dispatch stays uniform.
    """
    try:
        # Probe first so the error names the missing *extra*, not an internal module. Also the
        # seam tests monkeypatch to force the failure.
        import flask  # noqa: F401
    except ImportError:
        out("the 'web' command needs Flask, which is an optional extra.")
        out("  pip install 'media-sync-manager[web]'")
        out("  (the published Docker image already includes it)")
        return 2

    # Imported lazily so run/sync/status/doctor never pull flask into their import path.
    from . import web

    app = web.create_app(jellyfin)
    out(f"playlist editor on http://{host}:{port}")
    out("NOTE: no authentication — keep this on your LAN, never port-forward it.")
    app.run(host=host, port=port)
    return 0


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
    sub.add_parser("status", help="show planned actions per target (read-only)")
    sub.add_parser(
        "doctor",
        help="validate config, connectivity, and preconditions "
        "(writes and removes temp probe files under transcode_root)",
    )
    p_web = sub.add_parser("web", help="serve the playlist editor UI")
    # 0.0.0.0 because the container case is primary: 127.0.0.1 inside a container is unreachable
    # through a port mapping. Host exposure is the compose `ports:` line.
    p_web.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    p_web.add_argument("--port", type=int, default=8087, help="bind port (default 8087)")
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

    try:
        if args.command == "run":
            return cmd_run(config, jellyfin, tdarr)
        if args.command == "sync":
            return cmd_sync(config, jellyfin, tdarr, dry_run=args.dry_run)
        if args.command == "status":
            return cmd_status(config, jellyfin, tdarr)
        if args.command == "doctor":
            return cmd_doctor(config, jellyfin, tdarr)
        if args.command == "web":
            return cmd_web(config, jellyfin, tdarr, host=args.host, port=args.port)
    except MediaSyncError as exc:
        # e.g. detect_mode finding that neither hardlink nor symlink works here.
        print(f"error: {exc}")
        return 1
    return 2
