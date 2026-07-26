# Playlist editor — Design

## Context

Jellyfin's own web UI removes playlist items one at a time. On the real "Sync - tablet - 2D
Animation" playlist — 154 Meadowlark episodes across 3 seasons — that is unusable for the ordinary case of
"we've watched season 1, take it off the iPad".

This is a small web page served by the same package: pick a playlist, see it grouped show → season →
episode, bulk-select, remove. It edits the **input** to the sync engine, so it sits one step earlier
in the existing pipeline:

```
[playlist editor] -> Jellyfin playlist -> media-sync-manager -> Tdarr -> sync/ folder -> Infuse
```

A removal here changes the playlist in Jellyfin immediately; the poller notices on its next cycle and
retires that item's input link and its transcoded output. **The editor deletes nothing on disk
itself** — that coupling already exists and is not reimplemented. See
[the sync spec](media-sync-manager-spec.md) for everything downstream.

## 1. Responsibilities

**Does:** list playlists; render one grouped and sorted; bulk-select by show, by season, by range, or
individually; remove the selected entries; report what actually changed.

**Does not, deliberately:** add items, reorder, rename, create or delete playlists, annotate which
device a removal affects, trigger a sync, search or filter, or authenticate. It is not a Jellyfin
replacement — anything that is not "remove many things quickly" belongs in Jellyfin.

## 2. Module layout

| Module | Role |
|---|---|
| `jellyfin.py` | Transport only. Gained `_request`/`_delete`, `list_playlists`, `playlist_entries`, `remove_playlist_entries`, `base_url`. `find_playlist`/`playlist_items` are unchanged and still serve the sync path. |
| `playlists.py` | Pure grouping and sort. No HTTP, no flask, no filesystem, so the thing the browser renders is testable without a browser. |
| `web.py` | The only module that imports flask. `create_app(jellyfin)` takes the client, not a `Config` — no route reads config. |
| `static/` | `index.html`, `app.js`, `app.css`, and vendored `bootstrap.min.css` + `bootstrap.bundle.min.js`. No build step, no npm, no CDN. |

`cli.py`'s `cmd_web` imports flask **lazily**, so `run`/`sync`/`status`/`doctor` still run on the
core two dependencies (`requests`, `pyyaml`). `tests/test_cli.py` asserts this with a subprocess that
imports `media_sync_manager.cli` and checks `flask` never entered `sys.modules`.

## 3. Jellyfin API facts that constrain the design

Each of these cost real investigation and none is guessable from reading our code. They are the
reason several things below look odd.

**`PlaylistItemId` is the removal key, not `Id`.** `DELETE /Playlists/{id}/Items?entryIds=…` matches
against `PlaylistItemId` from `GET /Playlists/{id}/Items`. It is set unconditionally by the
controller — it is *not* an `ItemFields` option, so no `fields=` parameter turns it on. `PlaylistEntry`
therefore has **no attribute named `id`**: passing the media item's id is the obvious bug, and it must
not be spellable.

**Duplicates are not independently addressable.** `PlaylistItemId` currently caches the *media item's*
Guid, so two copies of one episode share an entry id and removing it clears both. Confirmed on the
live server: all 154 entries have `PlaylistItemId == Id`. The UI badges such rows `×2` rather than
pretending they are separable.

**A 204 does not mean anything was removed.** Jellyfin returns 204 even when no `entryId` matched
(jellyfin#12971). Verified on this server: a DELETE with `entryIds=000…0` returned **204 with the
count unchanged at 154**. Acceptance is not proof, which is why the page re-reads the playlist after
every removal and reports a count delta — and says so loudly when the server accepts a removal that
does not shrink the list.

**API-key authorisation on that endpoint is version-dependent.** `release-10.10.z` resolves
`User.GetUserId()` (empty for an API key) and returns `Forbid()`; an API-key branch landed for 10.11,
but jellyfin#12999 tracks it still failing on some builds. **Probed on 10.11.11 (`the Jellyfin server`): 204.**
Re-run after any Jellyfin upgrade — the probe is non-destructive because an entry id of all zeroes
matches nothing:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H "X-Emby-Token: $KEY" \
  "$JF/Playlists/$PID/Items?entryIds=00000000000000000000000000000000"
```

If it ever returns 400/403, the fallback is a user token from `POST /Users/AuthenticateByName` used
for writes only. It is deliberately **not** implemented — dead weight while the key works.

## 4. Grouping

`playlists.group_entries()` buckets entries into shows and seasons. The rules, each with a test in
`tests/test_playlists.py`:

- **Episodes with a series identity** → `series:<SeriesId>`, falling back to
  `series:name:<name.casefold()>` when the id is absent but the name is not. The two keys do not
  merge, so an odd entry cannot silently absorb a whole show.
- **Episodes with neither** → `type:Episode`, titled "Episodes (no series)" — but they **keep their
  real seasons**. An unmatched file can still carry a `ParentIndexNumber`, and flattening it would
  lose that. This is the one `type:` group that can hold more than one season.
- **Everything else** → one group per `Type` (`type:Movie` → "Movies", `type:Audio` → "Music", empty
  type → "Other items") holding a single degenerate season (`number=None`, title "All") which renders
  without a header of its own. Non-episodes get the same show→season→items shape so the selection
  code has exactly one structure to handle.

Season keys are `<show_key>|<n>`, or `<show_key>|none` for an unknown season and for the degenerate
bucket. Titles resolve `SeasonName` → "Specials" (season 0) → `Season N` → "Unknown Season"; the
`SeasonName` check is truthiness, so an empty string falls through rather than rendering blank.

**Sort order**, deterministic because every key ends in an id:

- shows: series alphabetically, then `type:` buckets last
- seasons: 1, 2, 3 … then Specials (0), then Unknown (None)
- entries: by episode number, un-numbered last, then by name

Grouping **does not dedupe** — the playlist is rendered faithfully. `duplicate_ids()` flags repeats
separately.

## 5. Selection model

State lives in JS, never in the DOM; `syncCheckboxes()` pushes it outward. Selection never triggers a
re-render, which is what keeps scroll position and expansion stable while clicking through 50 rows.

**Tri-state** is derived, not stored: for each group, `n` = removable ids beneath it and `k` = how
many are selected. `checked = k === n`, `indeterminate = 0 < k < n`. The `indeterminate` assignment
must happen on *every* pass — the browser keeps it independent of `checked`, so a stale `true`
survives and paints a permanent dash.

**The range button** on each row selects that item and everything above it *within its group*, and
its label states the count: `Select first 8`, flipping to `Clear first 8` once that range is selected.
The count is `slice(0, idx + 1).length` over the very array the click acts on, so label and behaviour
cannot drift.

**Rows versus entries.** A show badge counts *rows*; selection counts *entries*. On the test fixture
Meadowlark's badge reads 13 while selecting it gives 12, because a duplicated episode occupies two rows and
is one entry. Both numbers are correct and the difference is deliberate: the badge describes the list,
the toolbar describes what will be removed.

**Unaddressable entries** (no `PlaylistItemId` and no `Id`) render with a disabled checkbox and no
range button, and are excluded from every bucket so they cannot make a parent look partially selected.

## 6. HTTP contract

| Route | Response |
|---|---|
| `GET /` | `index.html` |
| `GET /api/playlists` | `{"server_url", "playlists": [{"id","name"}]}` |
| `GET /api/playlists/<id>/items` | `{"playlist_id","total","groups":[…]}` |
| `POST /api/playlists/<id>/remove` | `{"entry_ids":[…]}` → `{"requested","removed","failed","errors"}` |

Item objects use **`entry_id`**, never `id` — named for its purpose so the JS cannot confuse it with
the media item id.

Status codes: `200` all removed, `207` partial, `502` all failed or Jellyfin unreachable, `400`
malformed body, `415` wrong content type. Requiring `application/json` is de-facto CSRF protection
with no auth and no CORS headers. All `/api/*` responses carry `Cache-Control: no-store`, because a
cached refetch would make the count delta report a stale number and that delta is the only proof a
removal happened.

Removals are chunked at `_ENTRY_CHUNK = 50` ids per request (~1.7 kB of query string). A failing chunk
increments `failed` and the loop **continues** — "40 of 60 removed" is information the caller must
surface, not an exception.

## 7. Frontend conventions

Bootstrap 5.3.8, vendored. Components come from the library; `app.css` is **8 rule lines** and capped
at 15 by `tests/test_css_contract.py`, which also enforces: no `!important`, no colour literals, no
root `font-size`, no hand-rolled media queries, stylesheet order, the viewport meta tag, and sha256
digests of both vendored files.

The tree is nested Bootstrap accordions with `data-bs-parent` omitted, so opening one season does not
close another — selections often span seasons. Collapse element ids are assigned from render-order
position (`g0`, `g0s1`), not slugged from the group key: slugging collides, since
`series:name:foo|1` and `series:name:foo-1` both reduce to `series-name-foo-1`.

Initial expansion is tuned to the real playlist: shows open when there are ≤ `MANY_GROUPS` (15) of
them, seasons only when the playlist has ≤ `SMALL_PLAYLIST` (40) items. The 154-episode playlist
therefore opens to one show and three season headers rather than 154 rows.

## 8. Decisions that look wrong and are not

Each of these reads like an oversight and is load-bearing. Changing one without reading its reason
will reintroduce a bug that has already been fixed once.

- **`enableUserData=false`.** `UserData.Played` looks perfect for "select everything I've watched",
  and is useless here: the transcoded copies are watched off-device in a non-Jellyfin player, so
  playback is never reported back and `Played` stays false for exactly the items you want to remove.
  A button built on it would look authoritative and select nothing. One parameter flip if that ever
  changes.
- **No per-playlist deep link into Jellyfin.** It would route you into Jellyfin's playlist page — the
  interface this tool exists because it is unusable — to answer a question the count delta already
  answers. The header links the instance home instead.
- **The range button says `Select first N`, never `E1–E8`.** An episode range is meaningless for
  movies, and *understates* the selection when a season holds an un-numbered item (which sorts last):
  it would claim "E1–E4" while ticking five rows. A label that misstates the size of a destructive
  action is worse than a vague one.
- **`.show-group`, not `.show`.** Bootstrap uses `.show` as collapse state; a class meaning "TV show"
  beside one meaning "expanded" is a trap for any future `#tree .show` query.
- **`app.css` sets no root `font-size`.** The previous framework scaled the root with viewport width
  (125% at 1280px), which made its `1.25em` checkbox render at 25px on a desktop. A guard forbidding
  overrides of that variable is what locked the scaling in.
- **Row padding sits on the label, not the `<li>`.** Padding the row inflates it to 56px while the tap
  target stays 39px, because row padding is dead space *around* the target. On the label — which
  carries `for` — the same pixels *are* the target: 41px row, 40px target, desktop unchanged.
  Achieved with `py-2 py-md-0`, a Bootstrap responsive utility, so no media query returns.
- **`FakeJellyfinClient.remove_playlist_entries` actually deletes; `pretend_only` is opt-in.** A
  record-only fake inverts the entire suite — the happy path could never shrink a list, and the
  silent-204 test would pass for the wrong reason. Green tests over a broken app.

## 9. Tests

`pytest` runs 148 unit tests and starts no browser. Browser tests are marked `e2e` and excluded by
`addopts = "-m 'not e2e' --strict-markers"`, because `importorskip` alone only skips when Playwright
is *absent* — which is never true on a machine working on this feature. CI runs `pytest -m e2e -v` as
a separate job; a path-based invocation would collect the directory, deselect everything and go green
having run nothing.

- **Unit** — client parsing/chunking/error paths (`responses`), grouping across the case set, route
  status codes, the CSS contract.
- **e2e (76)** — selection mechanics, the accordion, removal including the silent-204 and
  partial-failure paths, theme rendering asserted by *luminance* rather than by the theme attribute,
  keyboard operation, and mobile on real device profiles (`Pixel 5`, `iPhone SE`) driven by `tap()`.

One fixture playlist holds the **entire render case set** — two series, Specials, a series-less
episode with a real season, a duplicate, an unaddressable entry, movies, and a title full of markup —
so assertions and screenshots cover it by construction. A second 120-item playlist exists solely to
cross the chunk boundary in a real flow.

**Integration** — `tests/test_integration.py` joins the two: an editor removal, then a reconcile
cycle, asserting the input link and the `sync/` output are gone and the original under `media_root`
is not. Nothing covered that chain before, and nothing could: `FakeJellyfinClient` kept two disjoint
playlist stores, so an editor removal was invisible to `playlist_items()`. The two projections must
therefore share ids — `linked_playlist()` builds them together and says so.

### The suite is mutation-checked

"Every test has an assertion" is not "every test constrains the code". Sixteen deliberate breakages
are paired with the test that must catch each; the script lives in the session scratchpad and the
pairing is the durable part. **Current result: 16/16 caught.**

Getting there found three problems that reading had not:

- `test_remove_dedupes_before_reaching_the_client` asserted the *fake's* dedupe, not the app's — the
  fake deduped before recording, so mutating the real client could never turn it red. Repaired by
  recording ids as received, and renamed to say what it checks.
- `test_duplicate_rows_move_together` survived a one-element `itemBoxes`. It clicked a row, which
  checks that row natively, so both rows ended up checked either way. It now selects via the season
  checkbox, touching neither row directly.
- Nothing caught indexing unaddressable entries into the selection buckets. The visible effect is the
  total, and no test pinned it; `test_case_set_total_is_pinned` now does.

Two mutations also had to be made precise before they meant anything: "remove the busy guard" has two
sites (the early return *and* the `disabled` term), and reproducing the `[hidden]` cascade bug needs
`.d-flex` in the markup, not just a changed toggle. A vague mutation is as useless as a vague test.

Where a test guards something subtle, it is mutation-checked the same way. The tap-target tests were
checked against the *plausible wrong version* (padding the row), not merely against removing the
padding.

## 10. Operations

`docker compose up -d` starts two containers from one image: the poller, and
`media-sync-manager-web` running `web --port 8087`.

- **The editor needs `JELLYFIN_API_KEY`, `TDARR_USER` and `TDARR_PASS`** even though it never opens a
  Tdarr connection. `config.load()` expands the whole document before any command runs and hard-fails
  on a missing `${VAR}`, so omitting them makes the container crash-loop at startup.
- **No `/media` mount.** The editor only talks to Jellyfin over HTTP; `fsops.detect_mode` is reached
  only from `sync.run_cycle` and `doctor`. Omitting the mount means the container physically cannot
  touch media files.
- **No authentication.** Anyone who can reach the port can edit your playlists. Keep it on the LAN;
  never port-forward it.
- Binds `0.0.0.0` because `127.0.0.1` inside a container is unreachable through a port mapping; host
  exposure is the compose `ports:` line.
