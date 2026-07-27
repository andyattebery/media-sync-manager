"use strict";

/* Playlist editor.
 *
 * The DOM is never the source of truth: `state` is, and syncCheckboxes() pushes it outward.
 * Selection never triggers a re-render, which is what keeps scroll position and expansion stable
 * while you click through fifty episodes.
 */

// Initial expansion. Tuned against a real playlist: 154 episodes of one show in 3 seasons, where
// opening everything buries the season headers you actually act on under 154 rows.
const SMALL_PLAYLIST = 40;   // fan seasons open only below this
const MANY_GROUPS = 15;      // above this, leave shows collapsed too

const state = {
  playlistId: null,
  playlistName: "",
  selected: new Set(),      // entry_ids
  idsBySeason: new Map(),   // seasonKey -> Set<entry_id>   (removable only)
  idsByShow: new Map(),     // showKey   -> Set<entry_id>   (removable only)
  orderBySeason: new Map(), // seasonKey -> entry_id[] in display order (removable only)
  allIds: new Set(),
  total: 0,                 // entries in the playlist as last fetched — NOT allIds.size, which
                            // excludes unaddressable rows and collapses duplicates
  boxes: new Map(),         // show|season key -> checkbox element
  itemBoxes: new Map(),     // entry_id -> checkbox element[]  (ARRAY: duplicates share an id)
  openKeys: new Set(),      // expanded show/season keys, preserved across a refresh
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const el = {
  playlist: $("#playlist"), reload: $("#reload"), toolbar: $("#toolbar"),
  selectAll: $("#select-all"), counts: $("#counts"), remove: $("#remove"),
  status: $("#status"), warnings: $("#warnings"), tree: $("#tree"),
  serverLink: $("#server-link"),
};

// Bootstrap's documented dark-mode switch, rather than hand-written media queries.
document.documentElement.dataset.bsTheme =
  matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";

/* Bootstrap's display utilities are !important, so they beat the `hidden` attribute. Toggle
 * .d-none/.d-flex instead — this is the one place that knows it. */
function setShown(node, shown, display = "d-flex") {
  node.classList.toggle("d-none", !shown);
  node.classList.toggle(display, shown);
}

function setStatus(text, isError = false) {
  el.status.textContent = text;
  el.status.classList.toggle("error", isError);
}

function showWarnings(errors) {
  el.warnings.textContent = "";
  if (!errors || !errors.length) { setShown(el.warnings, false, "d-block"); return; }
  const ul = document.createElement("ul");
  for (const e of errors) {
    const li = document.createElement("li");
    li.textContent = e;          // server strings; never innerHTML
    ul.appendChild(li);
  }
  el.warnings.appendChild(ul);
  setShown(el.warnings, true, "d-block");
}

async function api(path, options) {
  const resp = await fetch(path, options);
  let body = null;
  try { body = await resp.json(); } catch (_) { /* empty or non-JSON */ }
  if (!resp.ok && resp.status !== 207) {
    throw new Error((body && body.error) || `${resp.status} ${resp.statusText}`);
  }
  return body;
}

// --- rendering -------------------------------------------------------------

function episodeCode(item) {
  if (item.type !== "Episode") return "";
  if (item.episode_number === null || item.episode_number === undefined) return "—";
  const s = item.season_number === null || item.season_number === undefined
    ? "??" : String(item.season_number).padStart(2, "0");
  return `S${s}E${String(item.episode_number).padStart(2, "0")}`;
}

/* The button label states how many items the click will select — one format that is true for
 * episodes, movies and anything else, and that cannot drift from the behaviour because it counts the
 * very array the click acts on. An episode range ("Select E1–E8") was rejected: it is meaningless
 * outside a numbered season, and it *understates* the selection whenever a season holds an item with
 * no IndexNumber, which sorts last. Misstating the size of a destructive selection is worse than
 * being vague. */
function rangeLabel(groupKey, entryId) {
  const order = state.orderBySeason.get(groupKey) || [];
  const idx = order.indexOf(entryId);
  if (idx === -1) return null;
  const prefix = order.slice(0, idx + 1);
  const all = prefix.every((id) => state.selected.has(id));
  return `${all ? "Clear" : "Select"} first ${prefix.length}`;
}

function makeCheckbox(role, key, ariaLabel) {
  const input = document.createElement("input");
  input.className = "form-check-input flex-shrink-0 mt-0";
  input.type = "checkbox";
  input.dataset.role = role;
  if (role === "item") input.dataset.id = key; else input.dataset.key = key;
  if (ariaLabel) input.setAttribute("aria-label", ariaLabel);
  // No stopPropagation needed: an .accordion-button is a <button>, so the checkbox is a sibling
  // rather than a child, and each control does exactly one thing.
  return input;
}

function renderItem(item, groupKey, groupTitle, domId) {
  const li = document.createElement("li");
  // Vertical padding lives on the LABEL, not here. Padding the row inflates it to 56px while the
  // tap target stays 39px — the padding is dead space around the target. On the label (wired to
  // the input via `for`) the same pixels ARE the target: 41px row, 40px target, desktop unchanged.
  li.className = "list-group-item d-flex align-items-center gap-2 py-0 py-md-1";

  const input = makeCheckbox("item", item.entry_id);
  input.id = domId;
  if (!item.removable) {
    input.disabled = true;
    li.classList.add("text-body-secondary");
    li.title = "No playlist entry id — remove this one in Jellyfin";
  } else {
    const list = state.itemBoxes.get(item.entry_id) || [];
    list.push(input);
    state.itemBoxes.set(item.entry_id, list);
  }
  li.appendChild(input);

  const code = episodeCode(item);
  if (code) {
    const el2 = document.createElement("label");
    el2.className = "form-check-label font-monospace small text-body-secondary flex-shrink-0";
    el2.htmlFor = domId;
    el2.textContent = code;
    li.appendChild(el2);
  }

  const name = document.createElement("label");
  name.className = "form-check-label text-truncate flex-grow-1 py-2 py-md-0"
    + " align-self-stretch d-flex align-items-center";
  name.htmlFor = domId;
  name.textContent = item.name;          // titles are arbitrary strings: never innerHTML
  li.appendChild(name);

  if (item.duplicate) {
    const badge = document.createElement("span");
    badge.className = "badge text-bg-warning flex-shrink-0";
    badge.textContent = "×2";
    badge.title = "Appears more than once; Jellyfin removes all copies together";
    li.appendChild(badge);
  }

  if (item.removable) {
    const btn = document.createElement("button");
    // align-self-stretch fills the row's content box, so the button reaches 40px without adding
    // any height of its own.
    btn.className = "btn btn-sm btn-link text-decoration-none flex-shrink-0 p-0 ms-2 align-self-stretch";
    btn.type = "button";
    btn.dataset.role = "upto";
    btn.dataset.id = item.entry_id;
    // The orderBySeason key, stamped on the button. Do NOT re-derive it by walking to the nearest
    // [data-key] ancestor: in the degenerate non-episode bucket that ancestor is the *show* group
    // (type:Movie) while the array is keyed by the season (type:Movie|none), so the lookup silently
    // misses and the label never updates.
    btn.dataset.group = groupKey;
    btn.textContent = rangeLabel(groupKey, item.entry_id) || "";
    btn.title = `Select this item and every one above it in ${groupTitle}`;
    li.appendChild(btn);
  }
  return li;
}

function renderItemList(items, groupKey, groupTitle, idBase) {
  const ul = document.createElement("ul");
  ul.className = "list-group list-group-flush";
  items.forEach((item, i) => ul.appendChild(renderItem(item, groupKey, groupTitle, `${idBase}i${i}`)));
  return ul;
}

/* One accordion item. `domId` is assigned from render-order position rather than slugged from the
 * group key: slugging collides (series:name:foo|1 and series:name:foo-1 both become
 * series-name-foo-1), and the name-fallback keys are arbitrary show titles. The real key stays on
 * data-key, which is what the selection code and the tests use. */
function accordionItem({ cls, key, title, count, role, domId, open, body }) {
  const item = document.createElement("div");
  item.className = `accordion-item ${cls}`;
  item.dataset.key = key;

  // fs-6 matters: .accordion-header is an <h2>, so a .form-check-input sized at 1em would resolve
  // against the heading's font size and render a 32px checkbox. Resetting the header to 1rem sizes
  // the checkbox AND the button correctly, with a Bootstrap utility rather than custom CSS.
  const header = document.createElement("h2");
  header.className = "accordion-header d-flex align-items-center fs-6";
  const input = makeCheckbox(role, key, `Select all of ${title}`);
  input.classList.add("ms-2", "me-2");
  state.boxes.set(key, input);

  const btn = document.createElement("button");
  btn.className = open ? "accordion-button py-2" : "accordion-button collapsed py-2";
  btn.type = "button";
  btn.dataset.bsToggle = "collapse";
  btn.dataset.bsTarget = `#${domId}`;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  btn.setAttribute("aria-controls", domId);
  btn.textContent = title;
  const badge = document.createElement("span");
  badge.className = "badge text-bg-secondary ms-2";
  badge.textContent = String(count);
  btn.appendChild(badge);
  header.append(input, btn);

  const collapse = document.createElement("div");
  collapse.id = domId;
  // No data-bs-parent: opening one season must not close another, since selections span seasons.
  collapse.className = open ? "accordion-collapse collapse show" : "accordion-collapse collapse";
  const inner = document.createElement("div");
  inner.className = "accordion-body p-0";
  inner.appendChild(body);
  collapse.appendChild(inner);

  item.append(header, collapse);
  return item;
}

function renderSeason(season, domId) {
  return accordionItem({
    cls: "season-group",
    key: season.key,
    title: season.title,
    count: season.count,
    role: "season",
    domId,
    open: state.openKeys.has(season.key),
    body: renderItemList(season.items, season.key, season.title, domId),
  });
}

function renderShow(group, domId) {
  // A degenerate season (one bucket, number === null) is the non-episode case: render its items
  // straight into the show body with no season header of their own.
  const degenerate = group.seasons.length === 1 && group.seasons[0].number === null
    && group.kind === "type" && group.key !== "type:Episode";

  let body;
  if (degenerate) {
    const s = group.seasons[0];
    body = renderItemList(s.items, s.key, group.title, domId);
  } else {
    body = document.createElement("div");
    body.className = "accordion accordion-flush ps-3";
    group.seasons.forEach((s, i) => body.appendChild(renderSeason(s, `${domId}s${i}`)));
  }

  return accordionItem({
    cls: "show-group",
    key: group.key,
    title: group.title,
    count: group.count,
    role: "show",
    domId,
    open: state.openKeys.has(group.key),
    body,
  });
}

/* "154 items in 1 group" leaked the JSON key into the UI, and "group" is doing two jobs: a
 * top-level row is a *show* for episodes and a type bucket (Movies, Music) for everything else.
 * Name what is actually there instead. Type buckets already carry user-facing titles, so list them
 * when there are one or two and fall back to counting beyond that. */
function summarise(data) {
  const items = `${data.total} item${data.total === 1 ? "" : "s"}`;
  const shows = data.groups.filter((g) => g.kind === "series").length;
  const others = data.groups.filter((g) => g.kind !== "series");

  const parts = [];
  if (shows) parts.push(`${shows} show${shows === 1 ? "" : "s"}`);
  if (others.length && others.length <= 2) parts.push(...others.map((g) => g.title));
  else if (others.length) parts.push(`${others.length} other sections`);

  if (!parts.length) return `${items}.`;
  const last = parts.pop();
  return `${items} in ${parts.length ? `${parts.join(", ")} and ${last}` : last}.`;
}

function indexEntry(item, showKey, seasonKey) {
  if (!item.removable) return;   // never let an unremovable row skew a parent's tri-state
  state.allIds.add(item.entry_id);
  if (!state.idsByShow.has(showKey)) state.idsByShow.set(showKey, new Set());
  state.idsByShow.get(showKey).add(item.entry_id);
  if (!state.idsBySeason.has(seasonKey)) state.idsBySeason.set(seasonKey, new Set());
  if (!state.orderBySeason.has(seasonKey)) state.orderBySeason.set(seasonKey, []);

  // Distinct ids only. A duplicated entry occupies two rows but is ONE playlist entry: Jellyfin
  // removes both copies with a single entryId. Pushing it twice would make the range button count
  // rows instead of removals and claim "first 6" while selecting 5 — the label lying about the
  // size of a destructive action, which is the whole reason it states a number.
  const seen = state.idsBySeason.get(seasonKey);
  if (!seen.has(item.entry_id)) state.orderBySeason.get(seasonKey).push(item.entry_id);
  seen.add(item.entry_id);
}

/* Rebuilds the tree AND every lookup structure in one walk. `state.selected` is deliberately not
 * cleared here — that belongs to the callers (playlist change, post-removal refresh), so the two
 * paths cannot disagree about who owns it. */
function render(data) {
  // Capture expansion straight from the DOM rather than tracking Bootstrap's collapse events: the
  // minified EventHandler builds `new Event(name, {bubbles: <computed>})`, so whether component
  // events reach a delegated listener on #tree is not something the source guarantees — and a
  // listener that silently never fires would lose expansion on every refresh.
  for (const c of el.tree.querySelectorAll(".accordion-collapse")) {
    const key = c.parentElement && c.parentElement.dataset.key;
    if (!key) continue;
    if (c.classList.contains("show")) state.openKeys.add(key); else state.openKeys.delete(key);
  }

  state.total = data.total;
  state.idsBySeason.clear();
  state.idsByShow.clear();
  state.orderBySeason.clear();
  state.allIds.clear();
  state.boxes.clear();
  state.itemBoxes.clear();

  // Index before rendering: rangeLabel() reads orderBySeason while building the buttons.
  for (const group of data.groups) {
    for (const season of group.seasons) {
      for (const item of season.items) indexEntry(item, group.key, season.key);
    }
  }

  const frag = document.createDocumentFragment();
  data.groups.forEach((group, i) => frag.appendChild(renderShow(group, `g${i}`)));

  el.tree.textContent = "";
  if (!data.total) {
    const p = document.createElement("p");
    p.className = "empty text-body-secondary py-3";
    p.textContent = "This playlist is empty.";
    el.tree.appendChild(p);
  } else {
    el.tree.appendChild(frag);
  }

  setShown(el.toolbar, true);
  setStatus(summarise(data));
  syncCheckboxes();
  syncToolbar();
}

// --- selection -------------------------------------------------------------

function setSelected(ids, on) {
  for (const id of ids) {
    if (on) state.selected.add(id); else state.selected.delete(id);
  }
  syncCheckboxes();
  syncToolbar();
}

function countSelected(ids) {
  let k = 0;
  for (const id of ids) if (state.selected.has(id)) k++;
  return k;
}

function syncCheckboxes() {
  for (const [key, box] of state.boxes) {
    const ids = state.idsBySeason.get(key) || state.idsByShow.get(key) || new Set();
    const n = ids.size;
    const k = countSelected(ids);
    box.checked = n > 0 && k === n;
    // Must be assigned every time: the browser keeps `indeterminate` independent of `checked`,
    // so a stale true survives and paints a permanent dash.
    box.indeterminate = k > 0 && k < n;
    box.disabled = n === 0;
  }
  for (const [id, boxes] of state.itemBoxes) {
    const on = state.selected.has(id);
    for (const box of boxes) box.checked = on;   // both rows of a duplicate stay in step
  }
  // The master has no data-key and no bucket, so it is not in either registry above.
  const total = state.allIds.size;
  const chosen = state.selected.size;
  el.selectAll.checked = total > 0 && chosen === total;
  el.selectAll.indeterminate = chosen > 0 && chosen < total;
  el.selectAll.disabled = total === 0;

  // Range buttons flip between "Select first N" and "Clear first N" exactly when the click's effect
  // inverts, so the label never promises the wrong thing.
  for (const btn of el.tree.querySelectorAll('[data-role="upto"]')) {
    const label = rangeLabel(btn.dataset.group, btn.dataset.id);
    if (label) btn.textContent = label;
  }
}

function syncToolbar() {
  const chosen = state.selected.size;
  el.counts.textContent = `${chosen} of ${state.allIds.size} selected`;
  el.remove.textContent = state.busy ? "Removing…" : `Remove selected (${chosen})`;
  el.remove.disabled = chosen === 0 || state.busy;
  el.playlist.disabled = state.busy;
  el.reload.disabled = state.busy;
}

function toggleBucket(ids) {
  const list = [...ids];
  if (!list.length) return;
  setSelected(list, countSelected(list) !== list.length);
}

function selectUpTo(entryId) {
  for (const [seasonKey, order] of state.orderBySeason) {
    const idx = order.indexOf(entryId);
    if (idx === -1) continue;
    // The array is removable-only by construction, so the slice IS the prefix — no filtering.
    toggleBucket(order.slice(0, idx + 1));
    return;
  }
}

// --- events ----------------------------------------------------------------

el.tree.addEventListener("change", (ev) => {
  const box = ev.target;
  if (box.dataset.role === "item") {
    setSelected([box.dataset.id], box.checked);
  } else if (box.dataset.role === "season" || box.dataset.role === "show") {
    const key = box.dataset.key;
    toggleBucket(state.idsBySeason.get(key) || state.idsByShow.get(key) || []);
  }
});

el.tree.addEventListener("click", (ev) => {
  // Every checkbox and label click bubbles here too; only range-button presses are ours.
  const btn = ev.target.closest('[data-role="upto"]');
  if (!btn) return;
  ev.preventDefault();
  selectUpTo(btn.dataset.id);
});

// No `toggle` listener: that event fires only on <details>, and the tree is Bootstrap accordions.
// render() reads expansion straight from the DOM instead — see the comment there for why the
// collapse events are not trusted either.

el.selectAll.addEventListener("change", () => toggleBucket(state.allIds));
el.playlist.addEventListener("change", () => loadItems(el.playlist.value));
el.reload.addEventListener("click", () => { if (state.playlistId) loadItems(state.playlistId); });
el.remove.addEventListener("click", removeSelected);

// --- data ------------------------------------------------------------------

async function loadPlaylists() {
  try {
    const data = await api("/api/playlists");
    if (data.server_url) {
      // Say which Jellyfin this is and let you get to it. Instance home, not a per-playlist deep
      // link — that would send you into the playlist UI this tool exists to replace.
      const host = new URL(data.server_url).host;
      el.serverLink.href = `${data.server_url}/web/`;
      el.serverLink.textContent = `${host} ↗`;
      el.serverLink.title = `Open ${data.server_url} in a new tab`;
      el.serverLink.hidden = false;
    }
    for (const p of data.playlists) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      el.playlist.appendChild(opt);
    }
    setStatus("Choose a playlist to begin.");
  } catch (err) {
    setStatus(`Could not load playlists: ${err.message}`, true);
  }
}

async function loadItems(playlistId) {
  showWarnings(null);
  if (!playlistId) {
    state.playlistId = null;
    state.selected.clear();
    el.tree.textContent = "";
    setShown(el.toolbar, false);
    document.title = "Playlist editor";
    setStatus("Choose a playlist to begin.");
    return;
  }
  if (playlistId !== state.playlistId) {
    // Switching playlists: the previous selection refers to entries that are no longer on screen.
    state.selected.clear();
    state.openKeys.clear();
  }
  state.playlistId = playlistId;
  state.playlistName = el.playlist.options[el.playlist.selectedIndex].textContent;
  document.title = `${state.playlistName} — Playlist editor`;
  setStatus("Loading…");
  try {
    const data = await api(`/api/playlists/${encodeURIComponent(playlistId)}/items`);
    // What you want to land on is the level you act at. Seasons are the usual bulk unit, so open
    // shows down to their season headers and stop there — expanding 150 episode rows just to find
    // "Season 1" buries the thing you came for. Only fan seasons open when the whole playlist is
    // small enough to take in at once.
    if (!state.openKeys.size) {
      const openSeasons = data.total <= SMALL_PLAYLIST;
      for (const g of data.groups) {
        if (data.groups.length <= MANY_GROUPS) state.openKeys.add(g.key);
        if (openSeasons) for (const s of g.seasons) state.openKeys.add(s.key);
      }
    }
    render(data);
    return data;
  } catch (err) {
    el.tree.textContent = "";
    setStatus(`Could not load this playlist: ${err.message} — press ↻ to retry.`, true);
    return null;
  }
}

async function removeSelected() {
  const ids = [...state.selected];
  if (!ids.length || state.busy) return;
  const noun = ids.length === 1 ? "item" : "items";
  const ok = window.confirm(
    `Remove ${ids.length} ${noun} from “${state.playlistName}”?\n\n` +
    "The next sync cycle will also delete their transcoded copies. This cannot be undone."
  );
  if (!ok) return;

  state.busy = true;
  syncToolbar();
  // Compare like with like: the refetch reports `total` (every entry), so the baseline must be the
  // previous `total`, not allIds.size — which drops unaddressable rows and collapses duplicates,
  // and would make a successful removal look like "the list didn't shrink".
  const before = state.total;
  let result = null;
  let failure = null;
  try {
    result = await api(`/api/playlists/${encodeURIComponent(state.playlistId)}/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry_ids: ids }),
    });
  } catch (err) {
    failure = err;
  }

  state.busy = false;
  state.selected.clear();
  // Refetch even on failure: the server is the truth about what actually happened.
  const data = await loadItems(state.playlistId);
  showWarnings(result && result.errors);

  if (failure) {
    setStatus(`Removal failed: ${failure.message}`, true);
  } else if (data) {
    const after = data.total;
    const accepted = result.removed;
    if (accepted > 0 && after === before) {
      // Jellyfin answers 204 even when no entryId matched, so acceptance is not proof.
      setStatus(
        `Server accepted ${accepted} removals but the list did not shrink — check the playlist in Jellyfin.`,
        true
      );
    } else if (result.failed > 0) {
      setStatus(`Removed ${accepted} of ${result.requested}; ${result.failed} failed. List went ${before} → ${after}.`, true);
    } else {
      setStatus(`Removed ${accepted} — list went ${before} → ${after}.`);
    }
  }
  syncToolbar();
}

loadPlaylists();
