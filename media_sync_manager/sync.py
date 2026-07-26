"""Execute reconcile plans and run one full sync cycle across all targets."""

from __future__ import annotations

from collections import OrderedDict

from . import fsops, log, paths, reconcile
from .errors import MediaSyncError, PermanentError, TransientError
from .jellyfin import JellyfinClient
from .models import Config, CycleResult, TargetPlan
from .tdarr import TdarrClient

_log = log.get("sync")


def describe(plan: TargetPlan) -> list[str]:
    """Human-readable lines describing the planned actions (used by --dry-run and status)."""
    lines: list[str] = []
    if plan.error:
        lines.append(f"[{plan.target}] INCOMPLETE: {plan.error} (removes + sweep suppressed)")
    for a in plan.adds:
        lines.append(f"[{plan.target}] add ({a.segment} <- {a.playlist}): {a.relkey}")
    for r in plan.removes:
        lines.append(f"[{plan.target}] remove input: {r.input_path}")
    for d in plan.deletes:
        lines.append(f"[{plan.target}] delete output: {d.path}")
    for reason in plan.skipped:
        lines.append(f"[{plan.target}] skip: {reason}")
    if not plan.touched and not plan.skipped and not plan.error:
        lines.append(f"[{plan.target}] in sync")
    return lines


def execute(plan: TargetPlan, tdarr: TdarrClient, mode: str) -> list[str]:
    """Apply a plan: create + scan new inputs (grouped by library_id), unlink removed inputs and
    swept outputs. Returns one message per input that could not be created.

    Each input is isolated. Previously a single failure aborted the remaining adds, *every* Tdarr
    scan, *every* remove and the entire sweep for that target — which is why a pool-wide EXDEV
    queued zero files rather than syncing what it could.
    """
    failures: list[str] = []
    by_library: "OrderedDict[str, list[str]]" = OrderedDict()
    for a in plan.adds:
        try:
            fsops.materialize(a.source, a.input_path, mode)
        except MediaSyncError as exc:
            _log.error("target %s: %s: %s", plan.target, a.relkey, exc)
            failures.append(f"{a.relkey}: {exc}")
            continue
        by_library.setdefault(a.library_id, []).append(a.tdarr_path)
    for library_id, tdarr_paths in by_library.items():
        tdarr.scan_files(library_id, tdarr_paths)
    for r in plan.removes:
        fsops.unlink(r.input_path)
    for d in plan.deletes:
        fsops.unlink(d.path)
    return failures


def run_cycle(
    config: Config,
    jellyfin: JellyfinClient,
    tdarr: TdarrClient,
    *,
    dry_run: bool = False,
) -> list[CycleResult]:
    """Reconcile and (unless dry_run) apply every target. Returns plan + outcome for reporting.

    Per-target errors are logged and do not abort the cycle; the daemon stays up.
    """
    plans = reconcile.plan_all(config, jellyfin)
    if dry_run:
        return [CycleResult(plan=p) for p in plans]
    mode = fsops.detect_mode(
        config.transcode_root, paths.all_input_dirs(config), config.input_mode
    )
    results: list[CycleResult] = []
    for plan in plans:
        try:
            failures = execute(plan, tdarr, mode)
        except TransientError as exc:
            _log.warning("target %s: %s (will retry next cycle)", plan.target, exc)
            failures = [str(exc)]
        except PermanentError as exc:
            _log.error("target %s: %s", plan.target, exc)
            failures = [str(exc)]
        results.append(CycleResult(plan=plan, failures=tuple(failures)))
    return results
