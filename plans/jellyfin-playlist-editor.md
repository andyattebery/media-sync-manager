# Playlist editor web UI

> **First step on approval:** copy this file to `plans/jellyfin-playlist-editor.md` in the repo,
> matching the convention set by `plans/mergerfs-hardlink-exdev.md` (added in `7fd51f4`). It is not
> committed unless you ask.

## Context

Jellyfin's own web UI has no fast way to remove many items from a playlist — you delete them one at a
time. This repo already holds a working, authenticated Jellyfin client and knows which playlists
matter, so it's the natural place to hang a small editor: pick a playlist → see it grouped by show →
season → episode → tick a whole season → remove.

Scope is **browse + bulk remove only**. No sync-impact annotations, no "sync now" button, no search.
Removals land in Jellyfin immediately; the existing poller notices on its next cycle and retires the
input hardlink and the transcoded `sync/` output on its own. That coupling is free — don't rebuild it.

Constraints, already decided:
- **Flask**, behind an optional `[web]` extra. The core CLI/daemon keeps its two-dependency footprint
  (`requests`, `pyyaml`); the flask import must be lazy so `run`/`sync`/`status`/`doctor` work without it.
- **Runs in Docker**, so bind `0.0.0.0` — `127.0.0.1` is unreachable through a port mapping. Host
  exposure is the compose `ports:` line. No auth.

---

## Step 0 — probe the Jellyfin API before writing code

The one thing that can sink this feature: **API-key authorization on the removal endpoint is
version-dependent and known-buggy.** On `release-10.10.z`, `PlaylistsController.RemoveFromPlaylist`
resolves `User.GetUserId()` (empty for an API key) and returns `Forbid()`. An API-key branch was added
in PR #14154 (10.11+), but jellyfin#15600 reports 10.11.3 still returning `400 "Guid can't be empty"`,
closed as a duplicate of the still-open #12999.

Run these three against the real server first:

```sh
JF=http://jellyfin.example.com:8096; KEY=…; UID=…
curl -s -H "X-Emby-Token: $KEY" "$JF/Users/$UID/Items?IncludeItemTypes=Playlist&Recursive=true" \
  | jq '.Items[] | {Id, Name}'

PID=…   # a playlist id from above
curl -s -H "X-Emby-Token: $KEY" \
  "$JF/Playlists/$PID/Items?userId=$UID&enableImages=false&enableUserData=false" \
  | jq '.Items[0] | {Id,PlaylistItemId,Type,SeriesName,SeriesId,SeasonName,ParentIndexNumber,IndexNumber}'

ENTRY=…  # a throwaway entry's PlaylistItemId
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H "X-Emby-Token: $KEY" \
  "$JF/Playlists/$PID/Items?entryIds=$ENTRY"
```

- **204** → proceed as written below. No config change needed.
- **400 / 403** → the plan still holds, but add the user-token fallback in *Contingency* at the end.

Then re-GET and confirm `TotalRecordCount` actually dropped. Jellyfin returns **204 even when
`entryIds` matched nothing** (jellyfin#12971), so a 204 is not proof of removal — this is why the UI
re-fetches and reports a count delta rather than trusting the response.

**Two facts to bank from the probe**, both verified against Jellyfin server source:
- `PlaylistItemId` is set unconditionally by the controller — it is *not* an `ItemFields` option, so no
  `fields=` param controls it. `DELETE …?entryIds=` compares against exactly that value.
- `SeriesName`, `SeriesId`, `SeasonName`, `SeasonId`, `IndexNumber`, `ParentIndexNumber` are assigned
  ungated in `DtoService` for Episodes. So the browse call sends **no `fields=` at all** — and must not
  reuse `_ITEM_FIELDS = "Path,MediaSources"` ([jellyfin.py:17](media_sync_manager/jellyfin.py#L17)),
  whose `MediaSources` is the expensive part and useless here.

Caveat to surface in the README: `PlaylistItemId` currently caches the *media item's* Guid, so two
copies of one episode share an entry id and removing it clears both. Server limitation, not fixable
here.

---

## 1. Data model — a separate `PlaylistEntry`, not a wider `MediaItem`

In [models.py](media_sync_manager/models.py), add alongside `MediaItem`; **do not touch `MediaItem`**.

`MediaItem` is reconcile's input contract — `plan_target` reads only `name` and `media_sources`.
Widening it with six optional fields also makes `series_name is None` unfalsifiable ("it's a Movie" vs
"we didn't ask for that field"), since the two callers request different `fields=`.

The decisive argument is the footgun: give `PlaylistEntry` **no attribute named `id`**. Then
`remove_playlist_entries(pid, [e.id for e in entries])` is an `AttributeError` on first run. On a
merged `MediaItem`, `item.id` would be a valid attribute holding exactly the wrong value — and because
`PlaylistItemId == Id` on today's server, it would appear to work and break the day Jellyfin gives
entries real per-entry ids.

```python
@dataclass(frozen=True)
class PlaylistSummary:          # NOTE: `Playlist` is already taken (config domain) — do not shadow it
    id: str
    name: str

@dataclass(frozen=True)
class PlaylistEntry:
    """One playlist row, for browsing/editing only — never consumed by reconcile.

    `playlist_item_id` is the ONLY value valid as an `entryIds` argument. There is deliberately no
    `id` field: passing the media item's id to the delete call is the obvious bug, and it must not
    be spellable.
    """
    playlist_item_id: str            # PlaylistItemId; falls back to Id; "" when unaddressable
    item_id: str                     # display/debug only
    name: str
    type: str
    series_id: str | None = None
    series_name: str | None = None
    season_id: str | None = None
    season_name: str | None = None
    season_number: int | None = None     # ParentIndexNumber
    episode_number: int | None = None    # IndexNumber

    @property
    def removable(self) -> bool: return bool(self.playlist_item_id)

@dataclass(frozen=True)
class RemovalResult:
    requested: int; removed: int; failed: int; errors: tuple[str, ...] = ()

@dataclass(frozen=True)
class SeasonGroup:
    key: str; title: str; number: int | None; entries: tuple[PlaylistEntry, ...] = ()

@dataclass(frozen=True)
class ShowGroup:
    key: str; title: str; kind: str            # "series" | "type"
    seasons: tuple[SeasonGroup, ...] = ()
```

Both groups expose a `count` property that rolls up. Zero regression surface: `make_episode`
([conftest.py:93-103](tests/conftest.py#L93-L103)) and `FakeJellyfinClient`
([fakes.py:9-30](tests/fakes.py#L9-L30)) keep working unchanged.

No name collisions with `7fd51f4`, which added `CycleResult` and `Target.input_mode` to the same
module — checked against every type in [models.py](media_sync_manager/models.py). Note the adjacent
naming though: this plan's `RemovalResult` sits next to that commit's `CycleResult`, and both are
"what happened when we tried" types. That's consistent, not confusing, but keep the docstrings
explicit about which layer each belongs to.

## 2. `JellyfinClient` — [jellyfin.py](media_sync_manager/jellyfin.py)

**Refactor `_get` ([jellyfin.py:51-58](media_sync_manager/jellyfin.py#L51-L58)) into `_request` +
`_get` + `_delete`.** Keep the error string shape (`jellyfin GET /x failed: …`) byte-identical so
existing tests pass untouched. The `resp.json() if resp.content else None` guard is **required**: the
DELETE returns 204 with an empty body, and `resp.json()` on that raises `JSONDecodeError`, which is
*not* a `RequestException` and would escape uncaught instead of becoming a `TransientError`.

**Kill the duplicate playlist GET.** Extract `_all_playlists()` — the request `find_playlist` already
makes at [jellyfin.py:66-69](media_sync_manager/jellyfin.py#L66-L69) — and have both callers use it:

```python
def _all_playlists(self) -> list[dict]: ...              # the existing GET, extracted verbatim
def list_playlists(self) -> list[PlaylistSummary]: ...   # sorted by (name.casefold(), id)
```

`find_playlist` keeps its docstring, its exact-then-casefold two-pass, and its
`TransientError("playlist not found: …")`. That "not found is transient" behaviour is load-bearing for
reconcile — it must not drift.

**Browse** — `playlist_entries(playlist_id) -> list[PlaylistEntry]`, hitting
`/Playlists/{id}/Items` with `userId`, `enableImages=false`, `enableUserData=false`, and **no
`fields`**. `playlist_items()` ([jellyfin.py:79-84](media_sync_manager/jellyfin.py#L79-L84)) stays
exactly as-is for the sync path.

`enableUserData=false` is a **deliberate product decision, not a payload optimization** — leave it
off. `UserData.Played` looks like the perfect signal for "remove what I've finished", and it is a real
field (`enableUserData` is a genuine parameter on `GetPlaylistItems`; `UserItemDataDto` carries
`Played`, `PlayedPercentage`, `LastPlayedDate`). But in *this* project the transcoded files are watched
off-device in a non-Jellyfin player, so playback is never reported back and `Played` stays `false` for
exactly the items you want to remove. A "Select watched" button built on it would look authoritative
while selecting nothing — worse than not having it. If the playback workflow ever changes so Jellyfin
sees plays, this is the one-parameter flip that unlocks it.

A `_parse_entry` mirroring `_parse_item`, plus two helpers:
- `_guid(v)` → `str(v).replace("-", "")`, since the server compares against the dashless "N" form.
- `_opt_int(v)` → `int` or `None`, tolerating junk.

`playlist_item_id` falls back to the normalised `Id` when `PlaylistItemId` is absent. If neither is
present, it stays `""` and the row is flagged non-removable — never substitute a value you can't verify.

**Removal:**

```python
_ENTRY_CHUNK = 50

def remove_playlist_entries(self, playlist_id, entry_ids, *, chunk_size=_ENTRY_CHUNK) -> RemovalResult
```

DELETEs `/Playlists/{id}/Items?entryIds=a,b,c` in chunks. Drops empty ids, collapses duplicates
(order-preserving), and **returns** a result rather than raising — "40 of 60 removed" is information
the caller must surface. A failing chunk increments `failed` and appends the error; **the loop
continues**. Empty input makes zero HTTP calls.

Chunk size 50: ids are 32 hex chars → ~1.7 kB URL, well under both the conservative 2048-char limit and
Kestrel's 8192-byte request-line cap. Comma-joined is what Jellyfin's own web client sends; if a proxy
mangles it, the one-line switch is `{"entryIds": chunk}` (requests then emits repeated params, which
the same model binder accepts) — leave that as a comment.

Chunking in the client slightly stretches its "dumb transport" docstring. Justified: URL length is a
transport concern, and nothing above the client should know about it. No domain logic leaks in.

## 3. Grouping — new `media_sync_manager/playlists.py`

Pure functions. No flask, no requests, no filesystem — so the thing the browser renders is unit-testable
without a browser or a server.

```python
def group_entries(entries) -> list[ShowGroup]
def duplicate_ids(entries) -> set[str]
```

**Bucketing:**
- Episodes with a series identity → `series:<series_id>`, falling back to
  `series:name:<series_name.casefold()>` when `SeriesId` is null but the name isn't.
- Episodes with neither → `type:Episode`, titled "Episodes (no series)" — but **still bucketed into
  real seasons** by `ParentIndexNumber`, exactly like the `series:*` groups. A series-less episode can
  still carry a season number, and the degenerate-season rule below deliberately does not reach it.
  This is the one case where a `type:` group has more than one season; say so in the tests.
- Everything else (**non-episodes only**) → one group per `Type` (`type:Movie`, …) with a single
  degenerate season
  (`number=None`, title "All") that renders without its own header. Bucketing non-episodes into the
  same show→season→items shape is deliberate: the JS tri-state logic then has exactly one shape to
  handle. Titles via `{"Movie": "Movies", "Audio": "Music", "Video": "Videos"}`, defaulting to
  `f"{type} items"`; empty type → "Other items".

**Seasons key on `season_number`, not `SeasonName` or `SeasonId`** — the number is the stable sortable
identity, and `SeasonId` is null for loose files. Title resolution: `season_name` if set → "Specials"
if number is 0 → `f"Season {n}"` → "Unknown Season".

Season **key string** is `f"{show_key}|{n if n is not None else 'none'}"` — so `series:4f2…|1`,
`series:4f2…|0` for Specials, `series:4f2…|none` for both an unknown season *and* the degenerate
non-episode bucket. Spell this out: two maps in §5.2 are keyed on it, and `None` formatting is exactly
the case a rewrite loses. Show keys are already unique, so prefixing guarantees season keys are too.

**Sort order** — every key ends in an id so ordering is fully deterministic for identical titles (the
tests assert exact order):
- shows: `(kind == "type", title.casefold(), key)` → series alphabetically, then Movies etc. last
- seasons: `(number is None, number == 0, number or 0, title.casefold(), key)` → 1, 2, 3, Specials, Unknown
- entries: `(episode_number is None, episode_number or 0, name.casefold(), playlist_item_id, item_id)`

If you'd rather have Specials first, drop the `number == 0` term — leave a comment so the choice is visible.

`group_entries` does **not** dedupe; it renders the playlist faithfully. `duplicate_ids` feeds a
`"duplicate": true` flag so the UI can badge "×2 — removing clears all copies".

## 4. HTTP API — new `media_sync_manager/web.py`

`create_app(jellyfin) -> Flask` — **no `config` parameter.** Every route below reads only the Jellyfin
client; host and port are argparse (§6), and nothing in the web layer consults `Config`. Taking one
would force every route test and every browser test to construct a `Config` — which needs the `env`
fixture's tmp media dirs — to satisfy a parameter that is never read. A factory taking the
already-built client from
`cli._build_clients` ([cli.py:18-21](media_sync_manager/cli.py#L18-L21)), so tests inject
`FakeJellyfinClient` with no HTTP and no config file. The **only** module that imports flask.

Cross-cutting: an `errorhandler(TransientError)` → `502 {"error": …}` (clean JSON, never an HTML
traceback); `after_request` sets `Cache-Control: no-store` on `/api/*`; the POST requires
`Content-Type: application/json`. With no CORS headers emitted, that content-type requirement is
de-facto CSRF protection — a cross-origin form POST can't set it, and a cross-origin `fetch` gets
preflighted and blocked. Worth one line when there's no auth.

| Route | Response |
|---|---|
| `GET /` | `index.html` from the static dir |
| `GET /api/playlists` | `{"playlists": [{"id","name"}]}` |
| `GET /api/playlists/<id>/items` | `{"playlist_id","total","groups":[{key,title,kind,count,seasons:[{key,title,number,count,items:[…]}]}]}` |
| `POST /api/playlists/<id>/remove` | body `{"entry_ids":[…]}` → `{"requested","removed","failed","errors"}` |

Item objects use `entry_id` (not `id`) — named for its purpose, so the JS never sees a field it could
confuse — plus `item_id`, `name`, `type`, `season_number`, `episode_number`, `removable`, `duplicate`.

Removal status codes: **200** when `failed == 0`, **207** when partial, **502** when everything failed.
Malformed body (not an object, `entry_ids` missing/not a list/empty, non-string element) → **400**.

Serialize with explicit `_group_json`/`_entry_json` helpers, not `dataclasses.asdict` — the wire shape
is a contract the JS depends on and carries fields (`count`, `duplicate`, `removable`) that aren't
dataclass fields.

## 5. Frontend — `media_sync_manager/static/{index.html,app.js,app.css,pico.min.css}`

A nested list, checkboxes, a Remove button. Plain `<script src="app.js">` — no modules, no bundler, no
npm, no build step. Pico CSS v2.1.1 **vendored** as a static file (not CDN: this container is LAN-only
and may have no egress, and a failed `<link>` leaves an unstyled page).

The design target is that **every real removal is two clicks plus a confirm** — see the table in §5.4.
Anything that doesn't serve that got cut.

**Why Pico, concretely.** The core interaction here is a tri-state checkbox over a nested `<details>`
list. Pico styles `input[type=checkbox]:indeterminate` with a minus icon, and styles `details >
summary` including removing the native disclosure marker and supplying a rotating chevron — both
verified in its source, both classless. That is most of this UI. (I did not verify whether Water.css
or Simple.css handle `:indeterminate`; this is a reason to pick Pico, not proof it's uniquely best.)

**What Pico gives us here** — verified against `scss/components/_accordion.scss`,
`scss/forms/_checkbox-radio-switch.scss`, `scss/components/_modal.scss`, and the CSS-variables docs:

- `details > summary`: `list-style: none`, `cursor: pointer`, `::marker` and
  `::-webkit-details-marker` suppressed, and a `::after` chevron (`background-image:
  var(--pico-icon-chevron)`) that rotates from `-90deg` to `0` on `[open]`. **No border, no padding.**
  The selector is a direct-child combinator, so nested `<details>` are styled at every depth.
  → **Delete the `<span class="chev">` from the earlier draft. Pico already draws it.**
- Checkboxes at `1.25em`, `appearance: none` + background images, **`:indeterminate` handled**.
  → Do **not** set `accent-color`; with `appearance: none` it does nothing.
- Buttons, `<select>`, typography, focus rings, light/dark via `prefers-color-scheme` + `data-theme`.
- `--pico-font-size` defaults to `100%` and scales responsively — never lower it below 16px, or a
  focused `<select>` triggers mobile Safari's force-zoom.

**Where Pico does not fit: its modal.** It expects `dialog > article` plus JS-managed
`.modal-is-open`/`.modal-is-opening` classes for scroll-lock and animation, and it does **not** style
`::backdrop`. For one destructive confirmation that is a lot of machinery, so use native
`confirm()` — one line, works everywhere including mobile, no markup. Trade-off: it's an OS dialog,
not a styled one. Worth it here.

### 5.1 `index.html`

```html
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="pico.min.css">
<link rel="stylesheet" href="app.css">

<main class="container">
  <header>
    <select id="playlist"><option value="">Choose a playlist…</option></select>
    <button id="reload" class="outline" type="button" title="Re-read from Jellyfin">↻</button>
  </header>

  <div id="toolbar" hidden>
    <label><input type="checkbox" id="select-all"> Select all</label>
    <span id="counts"></span>
    <button id="remove" type="button" disabled>Remove selected (0)</button>
  </div>

  <p id="status" role="status" aria-live="polite"></p>
  <div id="warnings" hidden></div>
  <div id="tree"></div>
</main>
```

The viewport line is the single highest-impact thing in the file — without it mobile browsers lay out
at a 980px virtual viewport and scale down, shrinking every touch target. The link order is the whole
override mechanism (§5.6): Pico first, ours second. `role="status"` + `aria-live="polite"` announces
"Removed 42" without stealing focus.

### 5.2 State — the DOM is never the source of truth

```js
const state = {
  playlistId: null, groups: [],
  selected: new Set(),         // entry_ids
  idsBySeason: new Map(),      // seasonKey -> Set<entry_id>  (removable only)
  idsByShow:   new Map(),
  orderBySeason: new Map(),    // seasonKey -> removable entry_id[] in display order — for ⤒
  allIds: new Set(),
  boxes:     new Map(),        // "show|season key" -> checkbox element
  itemBoxes: new Map(),        // entry_id -> checkbox element[]  (ARRAY — see below)
  busy: false,
};
```

Sets, not arrays, because a duplicated `entry_id` must count once. The bucket maps hold **removable
ids only** — a non-removable row must never make its parent look partially selected, "Select all"
must never try to remove something it can't, and `orderBySeason` must not contain the empty-string id
that every unaddressable entry shares (§1). Keeping it removable-only also means ⤒'s slice *is* the
prefix, with no filtering step.

**`itemBoxes` maps to an array, not an element.** Two rows for a duplicated entry share one
`entry_id`, so a one-to-one map would silently drop the second row's checkbox and leave it stale after
a group select. This is the one place the duplicate case changes a data structure rather than just a
badge.

### 5.3 Rendering

`render()` builds the tree into a `DocumentFragment` and swaps it in once. It runs **only** on load,
playlist change, and post-removal refresh. **Selection never re-renders** — it mutates `state.selected`
and calls `syncCheckboxes()`, which flips `.checked`/`.indeterminate` on registered elements. That's
what keeps scroll position and expansion stable while clicking through 50 episodes.

**The same walk populates every structure in §5.2** — do not treat this as implied. Clear all six
first, then in one pass over `groups → seasons → items`, for each **removable** entry: add its id to
`idsBySeason`, `idsByShow`, `allIds`, and append it to `orderBySeason` (display order). Register each
show and season checkbox in `boxes` by `data-key`, and **push every item checkbox onto
`itemBoxes.get(entry_id)`**. Non-removable entries are rendered but registered nowhere.

`state.selected` is **not** one of the six — `render()` must not touch it, or the post-removal refresh
would clear the selection twice and the two callers would diverge. Clearing it belongs to the callers:
the playlist-change handler clears it (otherwise the toolbar counts ids from the previous playlist),
and §5.5 clears it after a removal.

Two failure modes this prevents, both silent:
- *Empty maps, correct markup* — every group checkbox looks permanently unchecked.
- *Item checkboxes unregistered* — `state.selected` updates and the group boxes move, but the
  episode rows beneath a ticked season never visually change. That one breaks the headline
  interaction, so `syncCheckboxes()` must walk `itemBoxes` as well as `boxes` (§5.4).

```html
<details class="show" data-key="series:4f2…" open>
  <summary class="row">
    <label><input type="checkbox" data-role="show" data-key="series:4f2…"></label>
    <span class="title">Meadowlark</span><span class="badge">51</span>
  </summary>
  <details class="season" data-key="series:4f2…|1" open>
    <summary class="row">
      <label><input type="checkbox" data-role="season" data-key="series:4f2…|1"></label>
      <span class="title">Season 1</span><span class="badge">26</span>
    </summary>
    <ul>
      <li class="row">
        <label>
          <input type="checkbox" data-role="item" data-id="9c0…">
          <span class="code">S01E02</span><span>First Flight</span>
        </label>
        <button class="upto secondary outline" data-role="upto" data-id="9c0…"
                aria-label="Select this and everything above it in this season">⤒</button>
      </li>
    </ul>
  </details>
</details>
```

- Checkbox inside its `<label>` so the row is a click target; `stopPropagation()` on the label so
  ticking a season doesn't also collapse it.
- `S01E02` in monospace with `tabular-nums` so numbers form a column — that alignment is what makes a
  26-row season scannable. Missing `episode_number` → `—`; non-episodes show no code.
- Movies land in a degenerate season (§3) whose `<ul>` hangs directly off the show with no season
  `<summary>` — one level shallower, handled by putting indent on the `<ul>`, not the row.
- `removable === false` → `disabled` checkbox + `.muted`, `title="No playlist entry id"`, **and no ⤒
  button**. Otherwise clicking ⤒ on a row you can't remove drops that row from its own range and
  silently selects everything above it — the button would appear to do nothing to the row you clicked.
- `duplicate === true` → `<span class="badge">×2</span>`, `title="Jellyfin removes all copies
  together"`. Both rows share an `entry_id` so they always show identical state — honest, since
  removal hits both.
- Text set via `textContent`, never `innerHTML` — episode titles are arbitrary strings containing
  `<`, `&`, quotes.
- Shows start open at ≤200 items, closed above that.

### 5.4 Selection

Three delegated listeners on `#tree` (`change`, `click`, and a capturing `toggle` — `toggle` doesn't
bubble) plus the toolbar; no per-row listeners. The `click` handler must **return immediately unless
`event.target.closest('[data-role="upto"]')` matches** — every checkbox and label click bubbles to it
too, so an unscoped handler fires on ordinary ticks. `setSelected(ids, on)` is the single mutation
point; everything funnels through it, then `syncCheckboxes()` + `syncToolbar()`.

- **Item** — toggles one id.
- **Season / Show / Select all** — `target = !(every removable id beneath is selected)`, then
  add-or-delete the whole bucket. Clicking a partially-selected season selects the rest.
- **⤒ "select to here"** — `orderBySeason.get(seasonKey).slice(0, idx + 1)` where
  `idx = indexOf(clickedId)`, then the *same* rule: `target = !(all of them already selected)`. So it
  adds by default and un-selects a range you've already got. No filtering needed — that array is
  removable-only by construction (§5.2) — and no new concept: it's the bucket rule over a prefix
  instead of a whole season. The clicked id is always present, because ⤒ only renders on removable
  rows (§5.3).

`syncCheckboxes()` walks **both** registries:

```js
// groups: one entry per show/season key, n/k from its bucket
cb.checked       = n > 0 && k === n;
cb.indeterminate = k > 0 && k < n;   // assign EVERY time — the browser keeps `indeterminate`
                                     // independent of `checked`, so a stale true paints a stuck dash
cb.disabled      = n === 0;

// items: every checkbox registered under that id, so both rows of a duplicate stay in step
for (const [id, els] of state.itemBoxes) for (const el of els) el.checked = state.selected.has(id);

// master: not in either registry — it has no data-key and no bucket
selectAll.checked       = allIds.size > 0 && selected.size === allIds.size;
selectAll.indeterminate = selected.size > 0 && selected.size < allIds.size;
```

Pico styles all three checkbox states, so this is the entire tri-state implementation. The master box
is spelled out because it's the one control whose `n`/`k` come from `allIds`/`selected` rather than a
bucket — leaving it to "the same loop" is how it ends up never showing indeterminate.

**Why ⤒ exists, and why it's not shift-click.** The dominant TV pattern is sequential: you watch
S02E01–E08 and stop. Season and show checkboxes don't cover it, so without something the most common
case costs eight taps. Shift-click is the familiar answer but **does not exist on touch**, which
collides with the phone requirement. A per-row "everything above this, in this season" button is one
click, is identical with a mouse or a thumb, and needs no gesture to discover. It's ~12 lines.

Scope is the season deliberately: "finished season 1, eight into season 2" is then season-checkbox +
⤒, two clicks. Rendered on item rows only — on a group row it would be ambiguous. In the degenerate
Movies bucket it still works (selects alphabetically above), which is harmless and needs no
special-case.

With ⤒ in place, all four real operations are two clicks plus the confirm:

| Job | Clicks |
|---|---|
| Finished a season | season checkbox → Remove |
| Finished a show | show checkbox → Remove |
| Watched S02E01–E08 | ⤒ on E08 → Remove |
| A few specific items | tick each → Remove |

**Not doing:** shift-click, long-press anchors, expand/collapse-all, per-group "selected" badges,
selected-row tinting, and a "select watched" button (see §2 — Jellyfin never learns about the plays).

### 5.5 Remove and refresh

`Remove selected (N)` → `confirm("Remove N item(s) from '‹playlist›'? The next sync cycle will also
delete their transcoded copies. This cannot be undone.")` → `POST /api/playlists/<id>/remove`.

Then: clear `selected`, re-`GET`, re-render, and report the delta — `Removed 42 — list went 312 → 270.`
**The delta is the only real proof**, since Jellyfin answers 204 even when nothing matched (Step 0). If
`removed > 0` but the count didn't move, say so loudly. On partial failure render `errors` into
`#warnings` and **still refresh**; on network/5xx, show the error and refresh anyway — the server is
the truth.

View states, all via `#status`/`#tree`: nothing chosen ("Choose a playlist"), loading, empty
("This playlist is empty"), loaded ("312 items in 14 shows"), removing (controls disabled), error
(message + the failed action retryable via ↻).

### 5.6 `app.css` — ~45 lines

Everything Pico has no opinion about. Theming re-points Pico's variables rather than out-specifying
its selectors:

```css
:root { --pico-spacing: .5rem; --pico-form-element-spacing-vertical: .35rem;
        --h-row: 32px; --indent: 20px }        /* NOT --pico-font-size: see below */

.row              { display:flex; align-items:center; gap:8px; min-height:var(--h-row) }
.show > ul, .season > ul { padding-left:var(--indent);
                    border-left:var(--pico-border-width) solid var(--pico-muted-border-color) }
ul                { list-style:none; margin:0 }
.code             { font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
                    font-variant-numeric:tabular-nums; color:var(--pico-muted-color) }
.badge, .muted    { color:var(--pico-muted-color); font-size:.85em }
.upto             { margin-left:auto; padding:2px 8px; font-size:.85em; width:auto }
details.show, details.season { margin-bottom:0 }   /* Pico's default margin, unwound for a dense tree */

@media (hover: hover) { .row:hover { background:var(--pico-secondary-background) } }
```

**Indent lives on the `<ul>`, not the row** — so the degenerate Movies bucket indents one level
shallower with no special case. Border widths use `--pico-border-width` rather than a literal `1px`,
for the same reason colors use tokens: one place to change, and it stays consistent with §5.7's
`#toolbar` border.

`.upto` keeps Pico's `.secondary.outline` button styling and only overrides position and scale —
`width:auto` because Pico stretches buttons to full width by default, which would blow out the row.
Do **not** set `border:0`: `.outline` *is* the border, so that would silently cancel the class the
markup asks for.

**Do not lower `--pico-font-size`.** It's tempting for density and it silently breaks mobile: Safari
force-zooms whenever a *focused* form control computes under 16px, so a smaller root makes every
`<select>` tap zoom the page with no way back. Density comes from `--pico-spacing` and `--h-row`.

**Verify at build time, don't assume:** Pico's `summary::after` chevron uses `float: right`, and
floats are ignored on flex items — so on a `display:flex` summary the chevron becomes the last flex
item instead. That is probably what we want (chevron at the right end), but confirm it visually
before adding any positioning rules for it.

### 5.7 Phone

Pico handles base font size (no iOS zoom) and the responsive container. Two things remain,
one breakpoint, ~12 lines:

```css
@media (max-width: 640px) {
  :root { --h-row: 44px; --indent: 12px }           /* Pico's 1.25em checkbox is ~20px — too small */
  label:has(> input[type=checkbox]) { padding:11px 8px; margin:-11px -8px }   /* target grows, layout doesn't */
  #toolbar { position:fixed; inset:auto 0 0 0; flex-wrap:wrap;
             background:var(--pico-background-color);
             border-top:var(--pico-border-width) solid var(--pico-muted-border-color);
             padding-bottom:calc(8px + env(safe-area-inset-bottom)) }
  #remove  { flex:1 1 100% }
  #tree    { padding-bottom:120px }                 /* last row must clear the fixed bar */
}
```

The negative margin extends the tap area past the visible checkbox without changing layout. The
toolbar moves to the bottom because that's where a thumb reaches on a 6" phone — CSS only, no markup
change. `env(safe-area-inset-bottom)` clears the iPhone home indicator; the `#tree` padding is not
optional or the last episode is permanently unreachable.

### 5.8 CSS contract

With a framework the failure mode isn't duplicated declarations — it's specificity war with vendor
rules, then `!important` when that stops working. Six checks, on `app.css` only (the vendored file is
never edited):

| Rule | Check | Pass |
|---|---|---|
| **≤ 60 lines.** Past that we're re-implementing Pico. | `wc -l static/app.css` | `≤ 60` |
| **No `!important`.** With a framework this is the highest-signal check — it's what losing an override fight looks like. | `grep -c '!important' static/app.css` | `0` |
| **Colors come from `--pico-*`**, never literals. | `grep -nE '#[0-9a-fA-F]{3,8}\b\|rgba?\(' static/app.css` | none |
| **`--pico-font-size` is never set at all.** Lowering it silently restores the iOS zoom bug, and there's no reason to raise it — so "absent" is the check, not "≥16px". | `grep -n 'pico-font-size' static/app.css` | absent |
| **Vendored file byte-identical to upstream.** | `shasum static/pico.min.css` | matches the v2.1.1 release, recorded in the commit |
| **`app.css` loads after `pico.min.css`.** Order is the override mechanism. | `grep -n stylesheet static/index.html` | pico first |

All six are greppable and binary — none depends on me being careful. **Paste `app.css`'s selector list
into the change summary**; at ~45 lines it should be scannable at a glance, and an id chain or a
`body`-prefixed selector in it means an override fight happened.

## 6. CLI — [cli.py](media_sync_manager/cli.py)

`cmd_web(config, jellyfin, tdarr, *, host, port, out=print) -> int`, keeping the house signature
([cli.py:24-71](media_sync_manager/cli.py#L24-L71)); `tdarr` is unused but preserved so `main()`'s
dispatch stays uniform. Re-verified after `7fd51f4` restructured this file: all four of `cmd_sync`,
`cmd_status`, `cmd_run`, `cmd_doctor` still take `(config, jellyfin, tdarr, *, …, out: Out = print) ->
int`, so the convention held through that change and `cmd_web` should follow it.

Note the asymmetry, and keep it: `cmd_web` receives `config` because the CLI convention requires it,
but passes only the client — `web.create_app(jellyfin)` (§4). Both `config` and `tdarr` are inert
here; that's the cost of a uniform dispatch, and it's cheaper than a special case in `main()`.

Two-step lazy import — this is the point of the whole extra:

```python
try:
    import flask  # noqa: F401   probe first: clean message, and monkeypatchable in tests
except ImportError:
    out("the 'web' command needs Flask, which is an optional extra.")
    out("  pip install 'media-sync-manager[web]'")
    return 2
from . import web
```

No module-level reference to `web` or `flask` anywhere in `cli.py`, so `run`/`sync`/`status`/`doctor`
import nothing beyond `requests` + `pyyaml`.

Subparser: `--host` default `0.0.0.0` (the container case is primary; localhost is unreachable through
a port mapping), `--port` default `8087` (avoids Jellyfin 8096 and Tdarr 8265). Print the no-auth
warning on every start.

**Host/port live in argparse, not config.yaml.** `config.parse()`
([config.py:104-142](media_sync_manager/config.py#L104-L142)) is a hard-fail validator for the *sync*
domain; a `web:` section means new branches, new `ConfigError` paths, and new `test_config.py` cases
for something that isn't domain state. The effective control surface is already the compose `ports:`
mapping, and `--help` documents defaults for free. Acknowledged inconsistency: every other knob is in
YAML. If it needs to move later, make the argparse `default=None` and fall back to `config.web_port` —
note that as a comment.

## 7. Packaging & Docker

[pyproject.toml](pyproject.toml): `web = ["flask>=3"]`; add `flask>=3` to `test` so CI actually runs
`test_web.py`; a separate `e2e = ["pytest-playwright"]`; core `dependencies` untouched.

```toml
[project.optional-dependencies]
web  = ["flask>=3"]
test = ["pytest", "responses", "flask>=3"]
e2e  = ["pytest-playwright"]          # ~150MB of browser binaries — deliberately NOT in `test`
```

`e2e` stays out of `test` so the default `pytest tests/` run — and the existing CI job — keeps its
current speed and footprint. `.github/workflows/ci.yaml` gets a **second job** that installs
`".[web,test,e2e]"`, runs `playwright install --with-deps chromium`, then **`pytest -m e2e -v`** — not
`pytest tests/e2e`, which the default `addopts = "-m 'not e2e'"` (§8) would collect and then deselect
entirely, giving a green job that ran zero tests. Add `--strict-markers` so a typo'd marker is an
error rather than another silent pass. The existing job is untouched, so a browser-install failure can
never redden the unit-test signal.

```toml
[tool.setuptools.package-data]
media_sync_manager = ["static/*"]
```

**This entry is not optional.** `packages = ["media_sync_manager"]`
([pyproject.toml:24-25](pyproject.toml#L24-L25)) ships `.py` only — without it, `pip install /src` in
[Dockerfile:3](Dockerfile#L3) yields an image where `/api/*` works and `GET /` 404s. A Docker-only,
confusing failure. The `static/*` glob also carries the vendored `pico.min.css`; a partial ship
(HTML present, stylesheet 404) renders an unstyled page rather than an obvious error, so the Docker
check in Verification fetches the stylesheet explicitly, not just `/`.

[Dockerfile](Dockerfile): `RUN pip install --no-cache-dir "/src[web]"` (quotes matter — unquoted
`[web]` is a shell glob), plus `ENV PYTHONUNBUFFERED=1` and `EXPOSE 8087`. `ENTRYPOINT`/`CMD` unchanged
— the default container is still the poller. Flask adds ~5 MB; one image for both services keeps the
CI publish job untouched.

[docker-compose.yml](docker-compose.yml): add a **second service**, don't replace the CMD.

```yaml
  media-sync-manager-web:
    build: .
    image: ghcr.io/andyattebery/media-sync-manager:latest
    restart: unless-stopped
    command: ["web", "--config", "/etc/media-sync-manager/config.yaml", "--port", "8087"]
    ports:
      - "8087:8087"          # LAN only — the UI has NO authentication
    environment:             # REQUIRED — see below. All three, even though web never calls Tdarr.
      - JELLYFIN_API_KEY
      - TDARR_USER
      - TDARR_PASS
    volumes:
      - /etc/media-sync-manager/config.yaml:/etc/media-sync-manager/config.yaml:ro
```

**The `environment:` block is not optional and the Tdarr vars are not a copy-paste mistake.** Commit
`7fd51f4` added it to the poller service for a reason that applies identically here: `config._expand()`
hard-fails on any `${VAR}` that isn't set, compose does not forward host env implicitly, and
`config.load()` expands the **whole document** before any command runs. So the web container dies at
startup with `environment variable 'TDARR_USER' referenced in config is not set` even though it never
opens a Tdarr connection. Omitting these is the most likely way this service fails first-boot.

- **No `/media` mount.** The editor only talks to Jellyfin over HTTP. Re-verified against `7fd51f4`,
  which made filesystem probing more aggressive: `fsops.detect_mode` now *attempts a real link* rather
  than comparing `st_dev`, but it is reached only from `sync.run_cycle`
  ([sync.py:75](media_sync_manager/sync.py#L75)) and from `doctor`'s `fsops.probe` call — never from
  `config.parse()`, which only validates that `input_mode` is one of `fsops.MODES`
  ([config.py:104-142](media_sync_manager/config.py#L104-L142)), and never from the web path. So
  omitting the bind mount means this container physically cannot touch media files — free blast-radius
  cut, and now a stronger guarantee than before.
- **No flock conflict.** `acquire_lock` ([poller.py:31-43](media_sync_manager/poller.py#L31-L43)) is
  called only from `run_forever` ([poller.py:58](media_sync_manager/poller.py#L58)), reached only via
  `cmd_run`. `cmd_web` never touches it. (Pre-existing and unchanged: `/run` is per-container, so the
  flock has never guarded against two containers running `run`.)

## 8. Tests

Existing patterns: `responses` for HTTP ([test_jellyfin.py](tests/test_jellyfin.py)), in-memory fakes
elsewhere.

- **[fakes.py](tests/fakes.py)** — keep `find_playlist`/`playlist_items` byte-identical so every
  reconcile/CLI test is untouched. Add an `entries` ctor arg, `list_playlists()`, `playlist_entries()`,
  and `remove_playlist_entries()`.

  **`remove_playlist_entries()` must actually delete from `self.entries`, not just record.** A
  record-only fake cannot pass the happy path — `playlist_entries()` would return the unchanged list,
  the count delta in §5.5 would always be zero, and every removal test would report "the list didn't
  shrink." Record-only *is* the silent-204 failure mode, so it must be the opt-in, never the default:

  ```python
  FakeJellyfinClient(entries={...},
                     remove_error=None,      # inject TransientError -> exercises 502 / partial 207
                     pretend_only=False)     # True = accept the call, delete nothing (silent-204)
  ```

  All three flags exist to back a specific test: the default proves removal works, `pretend_only`
  proves the "accepted N but the list didn't shrink" warning fires, `remove_error` proves the 207 and
  502 paths. Getting the default backwards would make the whole suite green while the app is broken.
- **test_jellyfin.py** — `list_playlists` sorted + auth header; existing `find_playlist` tests pass
  *unmodified* after the `_all_playlists` extraction; `playlist_entries` maps all six metadata fields,
  sends `userId`, and sends **no `fields`**; `Id` fallback and dash-stripping; both-ids-missing →
  `removable is False`; DELETE sends comma-joined `entryIds` and a **204 empty body does not raise**
  (regression guard for the `resp.json()` trap); 120 ids at `chunk_size=50` → 3 calls, union == input;
  dedupe + empty-string filtering; empty list → zero HTTP calls; chunk 2 fails → `removed=50, failed=50`
  **and chunk 3 is still attempted**.
- **test_playlists.py** (new, pure) — grouping by `SeriesId`; fallback to `SeriesName`; season order
  `1, 2, Specials, Unknown`; missing `IndexNumber` sorts last; movies after all series;
  unknown-series episodes into `type:Episode` **with their real seasons preserved** (the one `type:`
  group that can have more than one season — §3); non-episodes into a single degenerate season keyed
  `…|none`; count rollup; `duplicate_ids`; deterministic order for identical titles; empty input → `[]`.
- **test_web.py** (new) — `pytest.importorskip("flask")` at module top. Route shapes; POST forwards
  exactly the posted ids; partial → 207, total failure → 502; four bad-body cases → 400;
  `TransientError` → 502; wrong Content-Type → 415. Plus a static-asset test that `GET /`,
  `/pico.min.css`, `/app.css` and `/app.js` all return 200 — that's what guards the `package-data`
  bug in §7, and it fails in the source tree too if a file is misnamed.
  **And one that was missing: assert `Cache-Control: no-store` on `/api/*` responses.** Without it a
  browser or an intervening proxy may serve the post-removal refetch from cache — which would make
  the count delta in §5.5 report a stale number and quietly invalidate the *only* evidence we have
  that a removal happened. It's one header and one assertion, backing the plan's central claim.
- **test_cli.py** — `parse_args(["web"])` defaults; `monkeypatch.setitem(sys.modules, "flask", None)`
  → returns 2 with `media-sync-manager[web]` in the output; with flask present, a stubbed
  `web.create_app` records `(host, port)`.

### Browser tests — `tests/e2e/`, Playwright

**Every bug both plan-review rounds found was a DOM-state bug** — checkboxes not updating,
`indeterminate` never set, the wrong elements registered. None of them is visible to a test that
doesn't drive a real DOM, which is exactly why they survived two prose reviews. So the JS is not
left untested; it's tested by the only thing that can see those failures.

`pytest-playwright` driving the real Flask app with `FakeJellyfinClient` injected — no Jellyfin, no
network, no npm.

**Isolation needs a marker, not just `importorskip`.** `testpaths = ["tests"]`
([pyproject.toml:28](pyproject.toml#L28)) collects `tests/e2e/` too, and `importorskip` only skips
when playwright is *absent* — so on any machine where the extra is installed, i.e. every machine
actually working on this feature, a bare `pytest` launches Chromium. Register the marker and exclude
it by default:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["e2e: browser tests; needs the [e2e] extra + `playwright install chromium`"]
addopts = "-m 'not e2e' --strict-markers"   # bare `pytest` stays fast; `pytest -m e2e` opts in
```

`--strict-markers` matters here: without it a mistyped `@pytest.mark.e2ee` is silently an unknown
marker, so the module escapes the exclusion and runs on every bare `pytest` — or worse, `-m e2e`
selects nothing and the CI job goes green having run zero browser tests.

With `pytestmark = pytest.mark.e2e` in each e2e module, `pytest tests/` keeps running the 71 existing
tests at current speed whether or not the extra is present, and `pytest -m e2e` (what CI's second job
runs) is the only way to start a browser.

**Two fixtures.** The main one is built to contain the whole case set — this is the structural bit. A
single module defines a playlist holding: a series with two numbered seasons *and* Specials; a
series-less episode that still has a season number (§3's one multi-season `type:` group); a Movie
(degenerate season); a duplicated entry; and a non-removable entry. Every render case in §3/§5.3
appears on one page, so the assertions *and the screenshots* cover it by construction rather than by
my remembering to.

The second is a **bulk fixture: one series, 120 episodes**, existing solely for the chunk-boundary
test. The case-set fixture is ~15 items and has nothing to select 120 of; without this, that test
silently degrades into "select everything, one chunk" and stops testing chunking at all — which is
the failure mode where a test exists, passes, and proves nothing.

Tests, one per failure class already found or flagged:

| Test | Catches |
|---|---|
| season box → all its episode boxes checked | the `itemBoxes` registry bug (round 2, #1) |
| tick one episode → season, show **and master** go indeterminate | the master-has-no-bucket bug (round 2, #3) |
| untick then re-tick → back to fully checked, no stuck dash | a missing `cb.indeterminate =` assignment |
| ⤒ on E08 selects E01–E08, nothing in another season | prefix bounds + season scoping |
| ⤒ again on E08 clears exactly those eight | the toggle rule |
| ticking one row of a duplicate ticks both | `itemBoxes` mapping to an element, not a list |
| non-removable row: checkbox disabled, no ⤒ button | §5.3's rendering rule |
| toolbar hidden before a playlist is chosen | a `display:` beating `[hidden]` |

Those cover the interaction. The following were **verified by nothing at all** until this pass — each
is a stated guarantee elsewhere in the plan with no instrument behind it, which is the same failure as
a manual checklist nobody runs:

| Test | Guarantee it backs | Why it matters |
|---|---|---|
| stub deletes nothing but returns 204 → UI says **"accepted N but the list didn't shrink"** | §5.5 | the single most important correctness feature in the plan, and it was untested. Jellyfin genuinely does this (jellyfin#12971) |
| Remove → **cancel** the `confirm()` → zero POSTs, selection intact | §5.5 | the cancel path of an irreversible action |
| switch playlist A → B → selection cleared, counts reset | §5.3 | exactly the round-2 bug class: a structure not reset on a second code path |
| select 120 → client issues **3** chunked DELETEs, union == selection | §2 | chunking is unit-tested in isolation; the boundary was never crossed in a real flow |
| stub returns partial failure → `#warnings` visible **and** the list still refetches | §4, §5.5 | the 207 path; "still refresh" is a correctness rule, not politeness |
| show titled `<img src=x onerror=…>` renders as literal text | §5.3 | `textContent` was a claim with no check |
| scroll, collapse a show, tick boxes → scrollY and `[open]` unchanged | §5.3 | "selection never re-renders" is a design guarantee; nothing asserted it |
| double-click Remove → exactly one POST | §5.4 `busy` | double-submit on a destructive action |
| remove every item → empty state renders, no crash | §5.5 | the list-goes-to-zero edge |
| ↻ refetches and repaints | §5.1 | the button existed with no test |

**Also used for verification, not just regression** — all three run against the same fixture:
1. **Screenshots I inspect.** Desktop and 390px-wide renders of the case-set fixture, plus dark mode.
   This replaces "I assert it looks right," which is what produced the wrong Pico claims earlier.
   They are written to the session scratchpad, **not** into the repo — add `tests/e2e/screenshots/`
   to `.gitignore` if Playwright's default output path is used instead. Committed PNGs are the
   default outcome here and nobody wants them in review diffs.
2. **Settle the two Pico unknowns** (§5.6): whether the `float:right` chevron lands sensibly as a flex
   item, and whether nested `<details>` need unwinding beyond `margin-bottom:0`. Both are currently
   marked "verify at build time, don't assume" — Playwright is how that gets discharged.
3. **The real workflow end to end**: pick playlist → ⤒ a prefix → Remove → confirm → refetch shows the
   delta.

## 9. Docs

[README.md](README.md) — add `web` to Commands (line 63) and a short section after Quick start:
`docker compose up -d` now starts two containers, UI at `http://<host>:8087`; local dev is
`pip install -e ".[web]"`; **no authentication, LAN only, never port-forward**; removals hit Jellyfin
immediately and the poller retires hardlink + transcode within `poll_interval_seconds`; no undo;
duplicates share one entry id so removing one clears all copies.

[config.example.yaml](config.example.yaml) — trailing comment: the editor needs no config of its own,
reuses the `jellyfin:` block, host/port are CLI flags.

---

## Verification

Five different things need proving and they need different instruments. Naming them is what stops the
manual list from silently duplicating the automated one — which is what it was doing before Playwright
existed in this plan.

| What needs proving | Step | Why nothing else can prove it |
|---|---|---|
| The Python is correct | 1 | — |
| The JS mutates the DOM correctly | 2 | the only instrument that sees the bug class both plan reviews found |
| Removal actually reaches Jellyfin | 3, 6 | every automated test uses `FakeJellyfinClient`, which *records* calls and deletes nothing |
| The sync side reacts | 7 | needs a real `media_root` and the poller |
| Real deployment and real device | 5, 8, 9 | container packaging and iOS chrome behaviours have no emulator |

**1. Unit** — `pip install -e ".[web,test]" && pytest tests/ -v`. The 71 existing tests stay green.
Proves: chunking/dedupe/empty-input and the 204-empty-body trap in the client, grouping and sort order
across §3's case set, route status codes, the lazy-flask path.

**2. Browser** — `pip install -e ".[web,test,e2e]" && playwright install chromium && pytest -m e2e -v`
(the `[e2e]` extra alone brings neither flask nor pytest; `-m e2e` is required because the default
`addopts` excludes them — see §8). **Check the collected count is non-zero** — a marker typo or a
wrong `-m` makes this pass having run nothing, which is the worst outcome available here.
Proves everything mechanically assertable about the UI: down-propagation into item checkboxes,
tri-state up to and including master, ⤒ bounds and toggle, duplicate rows moving in step, disabled
non-removables, the `[hidden]` toolbar, remove → refetch → delta.

**This is why step 4 is short.** Anything assertable belongs here, not in a checklist a human is
supposed to remember to run. Does **not** prove a real Jellyfin accepts the DELETE (fake client), or
that any of it looks right.

**3. Jellyfin API** — Step 0's curls, if not already run. If they returned 400/403 the Contingency
section applies, and **steps 3 and 6 must both be re-run after implementing it** — that path has no
other coverage anywhere.

**4. Does it look right** — run locally against a *real* playlist and actually look. Only the things
Playwright can't judge:
   - the step-2 screenshots at desktop and 390px, light and dark
   - **the two Pico unknowns** (§5.6): chevron placement once `float:right` becomes a flex item, and
     whether nested `<details>` need unwinding beyond `margin-bottom:0`
   - ordering against real data — real libraries have punctuation, leading articles, missing episode
     numbers and Specials; the e2e fixture contains only what I thought to put in it
   - a genuinely large playlist (500+): the ≤200 collapse rule, and whether render is still instant
   - CSS greps (§5.8), and paste the selector list

**5. Real phone** — `http://<lan-ip>:8087`. Playwright can run WebKit, which covers Safari's *engine*,
but not iOS's chrome — which is exactly where the mobile bugs live:
   - a focused `<select>` must not force-zoom the page (no emulator reproduces this)
   - the Remove bar clears the home indicator, and the last episode scrolls clear of the bar
   - a tap must not leave a row stuck in `:hover`
   - rotate, then run a real removal end to end

**6. Jellyfin's own web UI** — open the same playlist and confirm the items are gone. **The only step
that proves removal actually happened.** Everything upstream stops at "the correct request was sent",
and Jellyfin returns 204 even when `entryIds` matched nothing.

**7. The sync side reacts** — on the host running the poller: `status` should now list `remove input:`
/ `delete output:` lines; wait `poll_interval_seconds` or run `sync --once`; confirm the input and the
`sync/` output are gone and **the original under `media_root` is untouched**. Proves the Context
section's claim that this feature needs no cleanup code of its own. Note the two containers differ:
the web service has no `/media` mount, so cleanup necessarily happens in the poller.

**8. Docker** — `docker compose up -d --build`, then:
```sh
docker compose logs media-sync-manager-web | head   # MUST NOT show a ConfigError
docker compose ps                                    # web service Up — not Restarting
for p in / /pico.min.css /app.css /app.js; do
  printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "localhost:8087$p"
done
```
The log check is the point, not decoration: omit an `environment:` entry and `config.load()` raises
`environment variable 'TDARR_USER' referenced in config is not set`, the container crash-loops, and
`ps` shows `Restarting` — which reads as "still starting up" if you don't look at the logs. The four
200s catch the `package-data` bug, where a missing stylesheet renders an unstyled page rather than an
error.

**9. Two-dep regression** — clean venv, `pip install .` *without* extras:
`python -c "import media_sync_manager.cli"` must not pull in flask; `doctor` works; `web` prints the
extras hint and exits 2; `pip list` shows no flask.

**10. Both services at once** — the plan asserts (§7) that the web container cannot contend for
`/run/media-sync-manager.lock` because `acquire_lock` is reached only from `cmd_run`. Nothing else
exercises that: with both containers up, remove some items from the UI and confirm the poller keeps
cycling through the removal and its cleanup, with no lock error in either log. Cheap, and it's the
only check on a claim the whole two-service deployment rests on.

**Blind spots, named rather than papered over.** Nothing above covers:
- The **Contingency user-token path**, unless Step 0 forces it into existence. If it is built, steps 3
  and 6 are its only coverage and both must be re-run.
- **Jellyfin disappearing mid-session** — only the manual ↻ error state touches it; there is no
  automated fault-injection.
- **Concurrent edits from Jellyfin's own UI** while the page is open. The next refetch shows the
  truth, but nothing warns you your view is stale — and with no auth there is no session in which to
  warn. Worst case is removing an entry someone already removed, which is a harmless 204.
- **Real-world path/name weirdness** beyond what the fixture contains — that is what step 4's
  "run it against a real playlist" exists for, and it is judgment, not assertion.

## Contingency — only if Step 0's DELETE returned 400/403

Add two optional keys to the `jellyfin:` block, `username` / `password` (defaulted `None` on
`JellyfinConfig`, so [conftest.py:67](tests/conftest.py#L67) is unaffected). Give `JellyfinClient` a
lazy `_ensure_user_token()` mirroring `TdarrClient.ensure_auth`
([tdarr.py:41-60](media_sync_manager/tdarr.py#L41-L60)): `POST /Users/AuthenticateByName` →
`AccessToken`, cached, used as `X-Emby-Token` **for `_delete` only** so reads keep using the API key.
Add one `doctor` line reporting which credential the write path uses.

~35 lines. **Do not build this speculatively** — it is dead weight if the probe returns 204.
