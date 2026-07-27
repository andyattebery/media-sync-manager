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


def _unlink_each(to_remove: list[str], kind: str, target: str, failures: list[str]) -> None:
    """Unlink each path independently, reporting rather than raising.

    `fsops.unlink` swallows FileNotFoundError and lets every other OSError through *unwrapped* — it
    is not a MediaSyncError, so it would escape run_cycle's handlers and main()'s, taking every
    remaining target down with a traceback because one file was unreadable.

    Not named `paths`: this module imports a `paths` module, and shadowing it inside a function that
    deletes files is a trap worth not setting.
    """
    for path in to_remove:
        try:
            fsops.unlink(path)
        except OSError as exc:
            _log.error("target %s: %s %s: %s", target, kind, path, exc)
            failures.append(f"{kind} {path}: {exc}")


def execute(plan: TargetPlan, tdarr: TdarrClient, mode: str) -> list[str]:
    """Apply a plan: create + scan new inputs (grouped by library_id), unlink removed inputs and
    swept outputs. Returns one message per unit of work that failed.

    Every unit is isolated — each input, each library's scan, each unlink. Previously a single
    failure aborted the remaining adds, *every* Tdarr scan, *every* remove and the entire sweep for
    that target, which is why a pool-wide EXDEV queued zero files rather than syncing what it could.
    1.2.0 isolated the adds; the scan and the unlinks kept the old behaviour, so a Tdarr hiccup still
    reached the removes and the sweep and silently skipped both.

    The scan is explicitly **best-effort**: Folder Watch (~30s) is what actually picks inputs up and
    what notices a deletion — `doctor` tells operators exactly that — so `scan-files` only makes
    pickup immediate and must never be able to block retirement. It is still reported, because
    whether a scan failure is harmless depends on Folder Watch being enabled, which we cannot see.
    """
    failures: list[str] = []
    by_library: "OrderedDict[str, list[str]]" = OrderedDict()
    for a in plan.adds:
        try:
            fsops.materialize(a.source, a.input_path, mode)
        except (MediaSyncError, OSError) as exc:
            # OSError too: materialize clears a stale entry via fsops.unlink *outside* its own
            # except-OSError wrapper, so that one call can still raise unwrapped.
            _log.error("target %s: %s: %s", plan.target, a.relkey, exc)
            failures.append(f"input {a.relkey}: {exc}")
            continue
        by_library.setdefault(a.library_id, []).append(a.tdarr_path)
    for library_id, tdarr_paths in by_library.items():
        # Guarded per library, INSIDE the loop. One unreachable library must not silence the scans
        # for every other library behind it.
        try:
            tdarr.scan_files(library_id, tdarr_paths)
        except (MediaSyncError, OSError) as exc:
            _log.warning(
                "target %s: scan-files lib=%s failed, Folder Watch will pick up: %s",
                plan.target, library_id, exc,
            )
            failures.append(f"scan-files lib={library_id} (Folder Watch will pick up): {exc}")
    _unlink_each([r.input_path for r in plan.removes], "remove input", plan.target, failures)
    _unlink_each([d.path for d in plan.deletes], "delete output", plan.target, failures)
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
