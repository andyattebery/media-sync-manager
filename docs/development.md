# Development

How to set up, run, test and debug this project. To *use* it rather than work on it, see
[the user guide](user-guide.md); for *why* it is built the way it is, see
[the sync spec](media-sync-manager-spec.md) and [the playlist editor design](playlist-editor.md).

## 1. Setup

Python **3.12+**. No build step, no Node, no npm — the frontend is vendored CSS/JS served as static
files.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web,test]"
pytest                      # 156 tests, no browser
```

Browser tests are opt-in because they pull ~150MB of browser binaries:

```sh
pip install -e ".[e2e]"
playwright install chromium
pytest -m e2e               # 76 tests
```

The three extras are deliberately not nested — `[e2e]` is only `pytest-playwright`, with neither
flask nor pytest, so it is always installed *alongside* `[web,test]` rather than instead of them.
The core CLI itself depends on nothing but `requests` and `pyyaml`; see §9.

**There is no linter or formatter configured.** No ruff, black, mypy, pre-commit or editorconfig
anywhere, despite `# noqa: E402` and `# type: ignore` comments in the source that imply otherwise.
Nothing will catch style for you. Match the surrounding code.

## 2. Layout

| Path | What |
|---|---|
| `media_sync_manager/` | the package: CLI, sync engine, Jellyfin/Tdarr clients, and the editor (`web.py`, `playlists.py`, `static/`) |
| `tests/` | unit + integration; `tests/e2e/` is Playwright |
| `scripts/` | developer tools — not shipped in the wheel |
| `docs/` | **current** design documentation |
| `plans/` | working plans, **some current and some overtaken**. A plan is a record of what was decided when, so overtaken ones are not rewritten — they get a *superseded* banner at the top pointing at the current doc. Check for that banner before treating one as design. |
| `media-sync-manager-requirements.md` | the original handoff brief; superseded, kept for provenance. |

Module roles are in [playlist-editor.md §2](playlist-editor.md).

## 3. Running it

### Against fixture data — no Jellyfin, no credentials

```sh
python scripts/devserver.py         # http://127.0.0.1:8099
```

This is the fastest way to see the UI and the only one that needs nothing. It serves the **e2e
fixture playlists**, deliberately reused rather than reinvented so that what you click through and
what the tests assert on cannot drift. That dataset is richer than production: duplicates, an
unaddressable entry, a series-less episode with a real season, an `S??E05` row, movies, a
markup-laden title, a 120-item playlist that crosses the removal chunk boundary, and a 21-group one —
the only place the >15-group collapse is visible by eye.

Removals mutate the in-memory fixture, so the tree really shrinks; restart to reset.

### Against a real Jellyfin

Write a `config.yaml` (gitignored, along with `.env`) and keep the key **out of it** — `config.py`
expands `${VAR}` at load:

```yaml
jellyfin:
  url: https://jellyfin.example
  api_key: "${JELLYFIN_API_KEY}"
  user_id: <guid>
```

```sh
export JELLYFIN_API_KEY=...          # from your password manager, not from the file
media-sync-manager --config ./config.yaml web --port 8087
```

`web` binds `0.0.0.0` by default because the container case is primary. **It has no authentication** —
keep it on your LAN, never port-forward it.

### Docker

```sh
docker compose up -d --build        # poller + editor, one image
```

Both services need `JELLYFIN_API_KEY`, `TDARR_USER` and `TDARR_PASS` in the environment. The editor
needs the Tdarr ones **even though it never opens a Tdarr connection**: `config.load()` expands the
whole document before any command runs and hard-fails on a missing `${VAR}`, so omitting them makes
the container crash-loop at startup.

Two checks worth running after a packaging change:

```sh
# every static asset must be 200 — a missing one renders an unstyled page, not an error
for p in / /bootstrap.min.css /bootstrap.bundle.min.js /app.css /app.js; do
  printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "localhost:8087$p"
done

# the two-dependency rule, in a clean venv with NO extras
pip install .
python -c "import media_sync_manager.cli"   # must not pull in flask
media-sync-manager --config ./config.yaml doctor   # works
media-sync-manager --config ./config.yaml web      # prints the extras hint, exits 2
```

## 4. The tests

Three layers, each for a different failure class:

| Layer | Where | Catches |
|---|---|---|
| **unit** | `tests/test_*.py` | parsing, chunking, grouping, route status codes, the CSS contract. Fast, no network, no browser. |
| **integration** | `tests/test_integration.py` | the editor→sync seam on a real tmp filesystem: remove via the editor, run a reconcile cycle, assert the input link and transcoded output are gone and the original is not. |
| **e2e** | `tests/e2e/` | a real DOM. The only thing that sees checkboxes not updating or `indeterminate` never being set. |

### Why bare `pytest` never starts a browser

```toml
addopts = "-m 'not e2e' --strict-markers"
```

`pytest.importorskip("playwright")` alone is not enough — it only skips when Playwright is *absent*,
which is never true on a machine working on this feature. The marker exclusion is what keeps the
default run fast.

**CI runs `pytest -m e2e`, never `pytest tests/e2e`.** A path-based invocation collects the directory
and then deselects every test in it under the default `addopts`, running nothing.
`--strict-markers` turns a typo'd marker into an error rather than a silent skip.

It does **not** go silently green, though, and this doc claimed otherwise until someone checked
pytest instead of reasoning about it. `_pytest/main.py::_main` ends with:

```python
elif session.testscollected == 0:
    return ExitCode.NO_TESTS_COLLECTED      # == 5
```

Deselected items are dropped from `testscollected`, so *any* empty selection exits 5 — deselection
included, not just "no files matched". `pytest tests/e2e` and a run whose markers went missing both
redden CI on their own.

So do not add a collected-count guard to the CI step; one was written and reverted as pure
duplication. Prefer `-m e2e` because it says what it means, not because the alternative is silent.

## 5. Test doubles

`FakeJellyfinClient` (`tests/fakes.py`) duck-types the real client. Each flag exists for a specific
trap:

| Flag | Purpose |
|---|---|
| `find_error`, `find_error_for` | fail `find_playlist` — the **sync** path only. `find_error_for` fails one named playlist, which is how the partial-failure guard is tested. |
| `load_error` | fail `list_playlists`/`playlist_entries` — the **editor's** read path. Without it, the page's two "could not load" branches were unreachable from a test, not merely untested. |
| `pretend_only` | accept the removal, return success, delete nothing — Jellyfin's real silent-204 behaviour. **Must stay opt-in:** a record-only fake makes every removal test report "the list didn't shrink", i.e. a green suite over a broken app. |
| `fail_after=N` | first N ids land, the rest fail — drives the partial (207) path. |

`linked_playlist(name, sources)` builds **both** projections of one playlist — the sync-side
`MediaItem`s and the editor-side `PlaylistEntry`s — with **shared ids**. That shared id is the only
thing connecting an editor removal to what the sync path subsequently sees; split them and
`test_integration.py` passes while testing nothing.

Fixtures: `tests/conftest.py` gives `env` (tmp media/transcode tree), `make_config`, `make_target`,
`make_playlist`, `write_source`, `make_episode`. `tests/e2e/conftest.py` gives `make_editor` (a live
server around a configurable fake), `editor`, and `open_playlist`.

## 6. Mutation testing

"Every test has an assertion" is not "every test constrains the code". `scripts/mutate.py` breaks one
thing at a time and checks the named test goes red.

```sh
python scripts/mutate.py            # all 22
python scripts/mutate.py 6 14       # just these
```

A **survivor** is a finding: the test does not constrain the code it claims to cover. A **site not
found** is different — the code moved and the mutation is stale, which the runner reports separately
so the two are never confused. It also fails loudly if a crash left the source mutated.

Adding one: append a tuple naming the **exact site**. Two rules earned the hard way:

- *Be specific.* "Delete the guard" is unrunnable when there are two `indeterminate` assignments and
  seven `state.busy` references.
- *Target the plausible wrong version*, not the absence of the feature. Padding the row instead of
  the label is what someone would actually write; deleting the padding proves less.

This found three real defects: a test asserting the *fake's* behaviour rather than the app's; a test
that clicked a row and so passed whether or not the registry held a list; and a bug with no test at
all. See [playlist-editor.md §9](playlist-editor.md).

## 7. Debugging browser tests

```sh
pytest -m e2e -k season --headed --slowmo 500      # watch it happen
pytest -m e2e --tracing retain-on-failure          # then: playwright show-trace trace.zip
pytest -m e2e --screenshot only-on-failure --output /tmp/pw
pytest -m e2e --device "Pixel 5"                   # or --browser firefox
```

`page.pause()` in a test opens the inspector. Most e2e failures are timing: a `wait_for_function`
whose condition the *previous* state already satisfies returns instantly and asserts nothing — wait
for the value to **change**, not to match a pattern the last action already produced.

## 8. Frontend rules

Components come from Bootstrap; `app.css` is 8 rule lines. `tests/test_css_contract.py` enforces the
rest — it is a test, not a convention, so none of it depends on anyone remembering:

no `!important` · no colour literals (use `var(--bs-*)`) · no root `font-size` · no hand-rolled media
queries · `:hover` only inside `@media (hover: hover)` · Bootstrap loaded before `app.css` · viewport
meta present · ≤15 rule lines · **sha256 digests of both vendored files**.

**Updating Bootstrap means updating those digests** in `test_css_contract.py::test_vendored_files_are_unmodified`.
The check exists so that patching the vendored file — instead of overriding it in `app.css` — fails
visibly.

## 9. Conventions that are enforced, not exhorted

Each is a test, so the list cannot rot into folklore:

| Rule | Enforced by |
|---|---|
| The core CLI stays on `requests` + `pyyaml`; flask is imported lazily | `test_cli.py::test_core_commands_do_not_import_flask` (a subprocess that checks `flask` never entered `sys.modules`) |
| The editor cannot touch the filesystem or trigger a sync | `test_web.py::test_web_layer_cannot_touch_the_filesystem_or_trigger_a_sync` |
| The editor has exactly one mutating route | `test_web.py::test_the_only_mutating_route_is_remove` |
| Vendored assets are byte-identical to upstream | `test_css_contract.py::test_vendored_files_are_unmodified` |

Also, by construction: `PlaylistEntry` has **no attribute named `id`**, and the JSON field is
`entry_id`. Passing the media item's id to the removal endpoint is the obvious bug, so it is not
spellable. See [playlist-editor.md §3](playlist-editor.md).

## 10. CI and releases

Three jobs in `.github/workflows/ci.yaml`:

| Job | Runs | When |
|---|---|---|
| `test` | `pip install ".[test]"` → `pytest tests/ -v` | push to `main`, PRs, tags |
| `e2e` | `pip install ".[web,test,e2e]"` → `playwright install --with-deps chromium` → `pytest -m e2e -v` | same |
| `docker` | multi-arch build → GHCR | tags only, and only if `test` and `e2e` pass |

`e2e` is a separate job so a browser-install failure cannot redden the unit-test signal.

**To release:** push a tag `X.Y.Z` or `vX.Y.Z`. That publishes
`ghcr.io/andyattebery/media-sync-manager:<version>`. A version containing `-` (e.g. `1.2.0-rc1`) is
treated as a pre-release: the version tag is published but `:latest` is **not** moved.

## 11. Gotchas

Each of these cost real time:

- **Verify a framework's behaviour from its source, not memory.** Four assumptions about Bootstrap
  were wrong at once — that it styles `<details>` with borders, that its controls are already 44px,
  that it styles `::backdrop`, that `accent-color` themes its checkbox. Read the vendored CSS.
- **`fsops` memoises the input-mode probe per process.** An autouse fixture resets it; without that,
  whichever test probes first fixes the mode for the whole session and the suite becomes
  order-dependent.
- **Bootstrap's display utilities are `!important`**, so they beat the `hidden` attribute. Toggle
  `.d-none`, not `hidden` — this shipped as a visible bug once.
- **Bootstrap sets `scroll-behavior: smooth` on `:root`**, so a plain `window.scrollTo` animates and
  reading `scrollY` on the next line gives the old value. Use `behavior: 'instant'` in tests.
- **Jellyfin returns 204 for a removal that matched nothing.** Acceptance is not proof; the UI
  re-reads and reports a count delta. Do not "simplify" that away.
- **A 200 is not a JSON 200, and a decode bug arrives disguised as a network failure.** Tdarr's
  `/api/v2/scan-files` answers `200 text/plain "OK"` while `/cruddb` answers JSON, so `resp.json()`
  raised on a call that had actually succeeded. `requests` makes its own `JSONDecodeError` a
  `RequestException` (via `InvalidJSONError`) *and* a `ValueError` — so the existing transport
  handler swallowed it and relabelled a working scan "tdarr POST failed". Catch `ValueError` for a
  decode, and never assume a wrapper only catches what you meant it to.
- **A best-effort call must not sit on the path to a destructive one.** That mislabelled failure
  escaped `execute()` before the removes and the sweep, so a cycle linked 154 files and retired
  none. Anything advisory gets its own guard, inside its own loop.
