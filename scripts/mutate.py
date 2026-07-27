"""Mutation campaign: break one thing, confirm the named test goes red, restore.

    python scripts/mutate.py            # all mutations
    python scripts/mutate.py 6 14       # only these ids

A mutation that stays GREEN is the finding: the named test does not constrain the code it claims to
cover. Three real defects came out of this — see docs/development.md §6.

Adding one: append a tuple naming the EXACT site. "delete the guard" is unrunnable when there are
two indeterminate assignments and seven state.busy references, and a mutation that merely deletes a
feature is weaker than one that reproduces the plausible wrong version someone would actually write.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "media_sync_manager/static/app.js"
JF = ROOT / "media_sync_manager/jellyfin.py"
WEB = ROOT / "media_sync_manager/web.py"
PL = ROOT / "media_sync_manager/playlists.py"
SY = ROOT / "media_sync_manager/sync.py"
TD = ROOT / "media_sync_manager/tdarr.py"
CLI = ROOT / "media_sync_manager/cli.py"
DOCKERFILE = ROOT / "Dockerfile"

# (id, file, find, replace, test selector, marker)
MUTATIONS = [
    ("1a group indeterminate", JS,
     "box.indeterminate = k > 0 && k < n;", "box.indeterminate = false;",
     "test_master_checkbox_goes_indeterminate", "e2e"),
    ("1b master indeterminate", JS,
     "el.selectAll.indeterminate = chosen > 0 && chosen < total;",
     "el.selectAll.indeterminate = false;",
     "test_master_checkbox_goes_indeterminate or test_select_all_then_untick", "e2e"),
    ("2  itemBoxes 1:1", JS,
     "const list = state.itemBoxes.get(item.entry_id) || [];\n    list.push(input);",
     "const list = [];\n    list.push(input);",
     "test_duplicate_rows_move_together", "e2e"),
    ("3  drop no-store", WEB,
     'resp.headers["Cache-Control"] = "no-store"', 'pass',
     "test_api_responses_are_not_cacheable", "unit"),
    ("4  client stops deduping", JF,
     "wanted = list(dict.fromkeys(i for i in entry_ids if i))",
     "wanted = [i for i in entry_ids if i]",
     "test_remove_dedupes_and_drops_empty_ids", "unit"),
    ("5  ignore chunk_size", JF,
     "for start in range(0, len(wanted), chunk_size):",
     "for start in range(0, len(wanted), 10**9):",
     "test_remove_chunks_at_the_boundary", "unit"),
    ("6  indexEntry keeps dupes", JS,
     "if (!seen.has(item.entry_id)) state.orderBySeason.get(seasonKey).push(item.entry_id);",
     "state.orderBySeason.get(seasonKey).push(item.entry_id);",
     "test_range_label_counts_rows_not_episode_numbers", "e2e"),
    ("7  drop busy guard (both)", JS,
     "if (!ids.length || state.busy) return;",
     "if (!ids.length) return;", "test_double_click_remove_sends_one_request", "e2e",
     ("el.remove.disabled = chosen === 0 || state.busy;",
      "el.remove.disabled = chosen === 0;")),
    ("8  enableUserData=true", JF,
     '"enableUserData": "false",', '"enableUserData": "true",',
     "test_playlist_entries_maps_metadata_and_sends_no_fields", "unit"),
    ("9  season key drops none", PL,
     'return f"{show_key}|{number if number is not None else \'none\'}"',
     'return f"{show_key}|{number}"',
     "test_season_titles_and_keys", "unit"),
    ("10 textContent->innerHTML", JS,
     "name.textContent = item.name;", "name.innerHTML = item.name;",
     "test_titles_render_as_text_not_markup", "e2e"),
    ("11 _guid keeps dashes", JF,
     'return str(value).replace("-", "").strip() if value else ""',
     'return str(value).strip() if value else ""',
     "test_playlist_entries_falls_back_to_id_and_strips_dashes", "unit"),
    ("12 index unaddressable", JS,
     "if (!item.removable) return;   // never let an unremovable row skew a parent's tri-state",
     "",
     "test_case_set_total_is_pinned", "e2e"),
    ("13 drop openKeys DOM read", JS,
     'if (c.classList.contains("show")) state.openKeys.add(key); else state.openKeys.delete(key);',
     "",
     "test_expansion_survives_a_removal_refresh", "e2e"),
    ("14 hidden attr + d-flex", JS,
     'node.classList.toggle("d-none", !shown);', 'node.hidden = !shown;',
     "test_toolbar_hidden_until_a_playlist_is_chosen", "e2e",
     None, (ROOT / "media_sync_manager/static/index.html",
            '<div id="toolbar" class="d-none gap-3',
            '<div id="toolbar" hidden class="d-flex gap-3')),
    ("15 sort key drops ids", PL,
     "        e.playlist_item_id,\n        e.item_id,\n", "",
     "test_identical_titles_sort_deterministically", "unit"),

    # --- the scan-files / sweep abort (plans/tdarr-scan-files-blocks-sweep.md) ---
    #
    # 16 and 17 must be whole-block replacements. Swapping `try:` for `if True:` in place leaves the
    # `except` clause orphaned, which is a SyntaxError — the test then goes red for the wrong reason
    # and the mutation certifies nothing.
    ("16 scan not guarded", SY,
     "    for library_id, tdarr_paths in by_library.items():\n"
     "        # Guarded per library, INSIDE the loop. One unreachable library must not silence the scans\n"
     "        # for every other library behind it.\n"
     "        try:\n"
     "            tdarr.scan_files(library_id, tdarr_paths)\n"
     "        except (MediaSyncError, OSError) as exc:\n"
     "            _log.warning(\n"
     '                "target %s: scan-files lib=%s failed, Folder Watch will pick up: %s",\n'
     "                plan.target, library_id, exc,\n"
     "            )\n"
     '            failures.append(f"scan-files lib={library_id} (Folder Watch will pick up): {exc}")\n',
     "    for library_id, tdarr_paths in by_library.items():\n"
     "        tdarr.scan_files(library_id, tdarr_paths)\n",
     "test_scan_failure_does_not_abort_removes_or_sweep", "unit"),
    # The plausible wrong version, not the absence of the feature: wrapping the WHOLE loop still
    # reports the failure and still protects the removes — it just silently drops every library
    # queued behind the one that failed.
    ("17 guard outside loop", SY,
     "    for library_id, tdarr_paths in by_library.items():\n"
     "        # Guarded per library, INSIDE the loop. One unreachable library must not silence the scans\n"
     "        # for every other library behind it.\n"
     "        try:\n"
     "            tdarr.scan_files(library_id, tdarr_paths)\n"
     "        except (MediaSyncError, OSError) as exc:\n",
     "    try:\n"
     "        for library_id, tdarr_paths in by_library.items():\n"
     "            tdarr.scan_files(library_id, tdarr_paths)\n"
     "    except (MediaSyncError, OSError) as exc:\n",
     "test_one_library_failing_still_scans_the_others", "unit"),
    ("18 unlink not guarded", SY,
     "    for path in to_remove:\n"
     "        try:\n"
     "            fsops.unlink(path)\n"
     "        except OSError as exc:\n"
     '            _log.error("target %s: %s %s: %s", target, kind, path, exc)\n'
     '            failures.append(f"{kind} {path}: {exc}")\n',
     "    for path in to_remove:\n"
     "        fsops.unlink(path)\n",
     "test_a_failed_unlink_does_not_abort_the_rest", "unit"),
    # Restores the pre-fix code exactly: decode unconditionally, let the JSONDecodeError fall
    # through to the RequestException handler, and report a successful scan as a transport failure.
    ("19 non-JSON body raises", TD,
     "                try:\n"
     "                    return resp.json()\n"
     "                except ValueError:\n"
     "                    # A 200 is not a JSON 200. /api/v2/scan-files answers `200 text/plain` with the\n"
     "                    # body \"OK\" regardless of the Accept header set in __init__, while /cruddb on the\n"
     "                    # same server answers JSON — which is why only the scan looked like a failure.\n"
     "                    #\n"
     "                    # Catch ValueError, not requests.exceptions.JSONDecodeError: the latter is also a\n"
     "                    # RequestException, so without this the decode error falls through to the handler\n"
     "                    # below and is reported as a transport failure.\n"
     '                    _log.debug("tdarr POST %s returned non-JSON: %r", path, resp.text[:80])\n'
     "                    return resp.text\n",
     "                return resp.json()\n",
     "test_post_tolerates_a_plain_text_body", "unit"),
    ("20 fail_detail always shown", CLI,
     'shown = "; ".join(p for p in (detail, fail_detail if not passed else "") if p)',
     'shown = "; ".join(p for p in (detail, fail_detail) if p)',
     "test_doctor_does_not_explain_a_failure_that_did_not_happen", "unit"),
    # Restores the shipped-broken argument order in BOTH containers: --config is top-level, so this
    # is an argparse error and neither service starts.
    ("21 --config after subcommand", DOCKERFILE,
     'CMD ["--config", "/etc/media-sync-manager/config.yaml", "run"]',
     'CMD ["run", "--config", "/etc/media-sync-manager/config.yaml"]',
     "test_shipped_container_commands_parse", "unit",
     None, (ROOT / "docker-compose.yml",
            'command: ["--config", "/etc/media-sync-manager/config.yaml", "web", "--port", "8087"]',
            'command: ["web", "--config", "/etc/media-sync-manager/config.yaml", "--port", "8087"]')),
]


def run(selector, kind):
    cmd = [sys.executable, "-m", "pytest", "-q", "-x", "-k", selector]
    if kind == "e2e":
        cmd += ["-m", "e2e"]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).returncode


wanted = {a.rstrip(":") for a in sys.argv[1:]}
selected = [m for m in MUTATIONS if not wanted or m[0].split()[0] in wanted]
if wanted and not selected:
    sys.exit(f"no mutation matches {sorted(wanted)}; ids are "
             f"{[m[0].split()[0] for m in MUTATIONS]}")

# Every file any selected mutation touches, as it was before we started. The restore check at the
# bottom compares against this.
ORIGINALS = {}
for _m in selected:
    for _p in (_m[1], _m[7][0] if len(_m) > 7 and _m[7] else None):
        if _p is not None and _p not in ORIGINALS:
            ORIGINALS[_p] = _p.read_text()

print(f"{'mutation':28} {'expected test':52} result")
print("-" * 96)
survivors, stale = [], []
for mut in selected:
    mid, path, find, repl, selector, kind = mut[:6]
    second = mut[6] if len(mut) > 6 else None
    other = mut[7] if len(mut) > 7 else None
    backup = path.read_text()
    if find not in backup:
        # Distinct from "survived": a moved or reworded site is a stale mutation, not a weak test.
        # Reporting them the same way would hide the difference behind an identical red line.
        print(f"{mid:28} {selector[:50]:52} *** SITE NOT FOUND ***")
        stale.append(mid)
        continue
    mutated = backup.replace(find, repl, 1)
    if second:
        assert second[0] in mutated, f"{mid}: second site not found"
        mutated = mutated.replace(second[0], second[1], 1)
    path.write_text(mutated)
    ob = None
    if other:
        opath, ofind, orepl = other
        ob = opath.read_text()
        assert ofind in ob, f"{mid}: other-file site not found"
        opath.write_text(ob.replace(ofind, orepl, 1))
    try:
        rc = run(selector, kind)
    finally:
        path.write_text(backup)
        if other and ob is not None:
            other[0].write_text(ob)
    ok = rc != 0
    print(f"{mid:28} {selector[:50]:52} {'caught' if ok else '*** SURVIVED ***'}")
    if not ok:
        survivors.append(mid)

print("-" * 96)
print(f"{len(selected) - len(survivors) - len(stale)}/{len(selected)} caught")
if survivors:
    print("\nSURVIVED — these tests do not constrain the code they name:")
    for m in survivors:
        print(f"  - {m}")
if stale:
    print("\nSITE NOT FOUND — the code moved; update the mutation, it is not a test failure:")
    for m in stale:
        print(f"  - {m}")

# Compare content against the snapshot taken before the run, NOT `git status`: a mutation campaign
# is normally run *while* editing the code it mutates, so a dirty tree is the expected state and
# using git here reported "restore failed" on every real run.
corrupted = sorted(str(p.relative_to(ROOT)) for p, before in ORIGINALS.items()
                   if p.read_text() != before)
if corrupted:
    print("\n*** SOURCE LEFT MUTATED — restore failed: ***")
    for name in corrupted:
        print(f"  {name}")
    sys.exit(2)

sys.exit(1 if (survivors or stale) else 0)
