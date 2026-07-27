> **Resolved.** Both bugs fixed as suggested: `_post` tolerates a non-JSON 200, and `execute()` now
> guards the scan **per library** as well as each unlink, so no advisory call can reach the removes
> or the sweep. Scan failures land in `CycleResult.failures` (so `sync --once` still exits non-zero)
> without aborting the cycle, and the `doctor` cosmetic is fixed via a `fail_detail` argument —
> blanket-suppressing detail on success would have broken four of the ten checks. Covered by
> `test_scan_failure_does_not_abort_removes_or_sweep`, `test_one_library_failing_still_scans_the_others`,
> `test_a_failed_unlink_does_not_abort_the_rest`, `test_post_tolerates_a_plain_text_body` and
> `test_doctor_does_not_explain_a_failure_that_did_not_happen`, and pinned by mutations 16-20 in
> `scripts/mutate.py`. Kept as the record of the live diagnosis.

# `scan_files` failure aborts removes + the sweep (and Tdarr returns non-JSON)

Found while deploying `1.2.0` to the poller host. The symlink work is confirmed working — this is the
next thing in the way.

## TL;DR

Two bugs, one of which is functional:

1. **`TdarrClient._post` assumes every non-empty response is JSON.** Tdarr's `/api/v2/scan-files`
   returns `200 text/plain` with the body `OK`, so `resp.json()` raises and the call is reported as a
   failure even though the scan succeeded server-side.
2. **`sync.execute` does not guard the `scan_files` call**, so that spurious failure propagates out
   of `execute()` before `plan.removes` and `plan.deletes` run. Playlist removals never retire the
   input, and the `sync/` sweep never happens.

(2) is the real problem. (1) is what triggers it here, but any transient Tdarr hiccup does the same.

## Evidence

Live, from the poller host against a real Tdarr (through a reverse proxy):

```
POST https://tdarr.example.com/api/v2/scan-files
  -> http=200  type=text/plain; charset=utf-8  size=2
  -> body: OK

POST https://tdarr.example.com/api/v2/cruddb       # for comparison
  -> http=200  type=application/json; charset=utf-8  size=83946
  -> body: [{"_id":"oWYG1e81j","priority":2,...
```

`cruddb` returns real JSON — which is why `doctor`'s library listing works and only `scan-files`
fails.

Resulting log line from `sync --once`:

```
WARNING media_sync_manager.sync: target tablet: tdarr POST /api/v2/scan-files failed:
Expecting value: line 1 column 1 (char 0) (will retry next cycle)
```

That run created **154 symlinks with zero EXDEV** (the 1.2.0 fix works) but enqueued **0** scans and
ran **0** removes/deletes.

## Where it breaks

`media_sync_manager/tdarr.py::_post`:

```python
resp.raise_for_status()
if resp.content:
    return resp.json()      # <- ValueError on the body "OK"
return None
```

`media_sync_manager/sync.py::execute` — note the asymmetry with the adds loop directly above it,
which 1.2.0 deliberately isolated:

```python
for a in plan.adds:
    try:
        fsops.materialize(a.source, a.input_path, mode)
    except MediaSyncError as exc:        # isolated per input ✅
        ...
        continue
    by_library.setdefault(a.library_id, []).append(a.tdarr_path)
for library_id, tdarr_paths in by_library.items():
    tdarr.scan_files(library_id, tdarr_paths)   # ← NOT guarded ❌ raises
for r in plan.removes:
    fsops.unlink(r.input_path)                  # ← never reached
for d in plan.deletes:
    fsops.unlink(d.path)                        # ← never reached
```

The `execute` docstring already names this exact failure mode as the thing 1.2.0 set out to fix:
*"Previously a single failure aborted the remaining adds, every Tdarr scan, every remove and the
entire sweep for that target."* The adds got isolated; the scan call did not, so the same class of
abort still reaches the removes and the sweep.

## Why the scan is the wrong thing to gate on

`doctor` already tells operators the truth:

> NOTE: Enable Folder Watch on each library (Library settings -> Folder Watch). It is what notices an
> input has been deleted and retires the file; the glue's scan-files call only makes pickup of new
> inputs immediate.

Folder Watch (~30s) is the mechanism; `scan-files` is a latency optimisation. A best-effort
optimisation must not be able to block retirement and the sweep.

## Suggested fix

Guard the scan — this is the load-bearing change:

```python
for library_id, tdarr_paths in by_library.items():
    try:
        tdarr.scan_files(library_id, tdarr_paths)
    except MediaSyncError as exc:
        # Best-effort: Folder Watch picks these up within ~30s regardless.
        _log.warning("target %s: scan-files failed, Folder Watch will pick up: %s", plan.target, exc)
```

And make the client tolerate a non-JSON body, so the common case stops looking like an error:

```python
if resp.content:
    try:
        return resp.json()
    except ValueError:
        return resp.text        # Tdarr returns plain "OK" from /api/v2/scan-files
```

Consider surfacing scan failures in the returned `failures` list (as adds do) so `sync --once` still
exits non-zero, without aborting the cycle.

## Deployment context (for reproduction)

- Running on the host where the mergerfs pool is a **local** filesystem — required for symlinks.
- `input_mode: symlink` pinned explicitly; `doctor` reports
  `input mode: symlink (set explicitly; not probed)`.
- Tdarr runs on a different host and reaches the media over CIFS, with `tdarr.url` pointing at it
  through a reverse proxy. The plain-text `OK` comes from Tdarr itself, not the proxy — the same
  request to `cruddb` through the same path returns `application/json`.
- Folder Watch was disabled during this test and is being enabled; that changes pickup, but not this
  bug, since the abort happens before removes/deletes either way.

## Cosmetic, while you're in there

`doctor`'s `check()` prints its `detail` string on success as well as failure, so a passing check
renders as:

```
[OK ] transcode_root is under media_root: '/media/Transcoded Videos' is outside '/media', so a
relative symlink between them resolves outside an SMB share...
```

i.e. an `[OK ]` line explaining the failure that did not happen. Print `detail` only when
`not passed` (or pass a separate success detail).
