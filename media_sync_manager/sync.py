"""Execute reconcile plans and run one full sync cycle across all output groups."""

from __future__ import annotations

from collections import OrderedDict

from . import fsops, log, reconcile
from .errors import PermanentError, TransientError
from .jellyfin import JellyfinClient
from .models import Config, GroupPlan
from .tdarr import TdarrClient

_log = log.get("sync")


def describe(plan: GroupPlan) -> list[str]:
    """Human-readable lines describing the planned actions (used by --dry-run and status)."""
    lines: list[str] = []
    if plan.error:
        lines.append(f"[{plan.output_dir}] INCOMPLETE: {plan.error} (deletes suppressed)")
    for s in plan.submits:
        lines.append(f"[{plan.output_dir}] submit ({s.segment} <- {s.playlist}): {s.relkey}")
    for d in plan.deletes:
        lines.append(f"[{plan.output_dir}] delete orphan: {d.path}")
    for reason in plan.skipped:
        lines.append(f"[{plan.output_dir}] skip: {reason}")
    if not plan.touched and not plan.skipped and not plan.error:
        lines.append(f"[{plan.output_dir}] in sync")
    return lines


def execute(plan: GroupPlan, tdarr: TdarrClient) -> None:
    """Apply a plan's side effects: hardlink + scan new items, delete orphan outputs.

    Submits are grouped by library_id (a shared output_dir may span more than one library) so each
    library scans in a single call.
    """
    by_library: "OrderedDict[str, list[str]]" = OrderedDict()
    for s in plan.submits:
        fsops.hardlink(s.source, s.input_path)
        by_library.setdefault(s.library_id, []).append(s.tdarr_path)
    for library_id, tdarr_paths in by_library.items():
        tdarr.scan_files(library_id, tdarr_paths)
    for d in plan.deletes:
        fsops.delete_output(d.path)


def run_cycle(
    config: Config,
    jellyfin: JellyfinClient,
    tdarr: TdarrClient,
    *,
    dry_run: bool = False,
) -> list[GroupPlan]:
    """Reconcile and (unless dry_run) apply every output group. Returns the plans for reporting.

    Per-group errors are logged and do not abort the cycle; the daemon stays up.
    """
    plans = reconcile.plan_all(config, jellyfin)
    if dry_run:
        return plans
    for plan in plans:
        try:
            execute(plan, tdarr)
        except TransientError as exc:
            _log.warning("%s: %s (will retry next cycle)", plan.output_dir, exc)
        except PermanentError as exc:
            _log.error("%s: %s", plan.output_dir, exc)
    return plans
