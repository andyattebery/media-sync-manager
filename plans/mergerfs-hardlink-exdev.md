# media-sync-manager on a mergerfs pool: hardlinks fail with EXDEV

## TL;DR
`fsops.hardlink` does `os.link(source, dest)` assuming `media_root` and `transcode_root` are one
filesystem. In this deployment they are one **mergerfs pool** (a FUSE union over many physical disks).
A hardlink only works within a single real disk, and mergerfs places the destination directory on a
*different* disk than the source → `os.link` raises `EXDEV`. `doctor`'s same-filesystem check passes
anyway (it compares `st_dev`, which is identical across the whole pool mount), so this isn't caught
until runtime.

## Environment
- MSM container: `/media` is the mergerfs pool. `media_root=/media`,
  `transcode_root=/media/Transcoded Videos`.
- The pool (on the NAS): `mergerfs` unions 12 btrfs disks `/mnt/disks/disk01..12` → mount
  `/mnt/storage`. Relevant fstab options:
  `category.create=mspmfs, minfreespace=1000G, func.getattr=newest, moveonenospc=mfs,
  inodecalc=path-hash, cache.files=off`. `ignorepponrename` and `link-exdev` are **unset**
  (defaults: path-preserving link, `link-exdev=passthrough`).
- In this deployment the container reaches the pool over **CIFS/SMB** (the pool lives on a separate
  host). This matters for the fix — see Caveats.

## What happened (concrete repro)
Config: one target `tablet`, playlists mapped to segments `2d-animation` and `standard`.
Ran `sync --once` on a playlist of Meadowlark episodes.

First hardlink attempted:
```
os.link(
  "/media/TV Shows/Meadowlark/Season 01/meadowlark.s01e01.1080p...mkv",
  "/media/Transcoded Videos/tablet/2d-animation/TV Shows/Meadowlark/Season 01/meadowlark.s01e01.1080p...mkv"
)
→ OSError(errno=EXDEV)
```
MSM output:
```
ERROR media_sync_manager.sync: target tablet: cannot hardlink across filesystems:
'/media/TV Shows/…s01e01….mkv' -> '/media/Transcoded Videos/tablet/2d-animation/…s01e01….mkv'
```
- The **source** `TV Shows/…` physically lives on branch `/mnt/disks/disk05`.
- The **dest dir chain** `…/2d-animation/TV Shows/Meadowlark/Season 01/` was created by MSM's
  `os.makedirs(parent)` (fsops.py) and mergerfs placed it on a *different* branch (most-free), so
  `os.link` crossed disks.
- `execute()` performs all hardlinks before any Tdarr scan, and the exception aborts the target →
  **0 files queued** (self-limiting; a systemic EXDEV never reaches Tdarr). Relevant code:
  `fsops.hardlink` (fsops.py:25-45), `execute()` (sync.py:34-46), same-fs check `_st_dev`
  (cli.py:53-58 / doctor).

## What is known about mergerfs (verified against official docs)

**1. "Same filesystem" is a FUSE illusion.** The pool has one `st_dev` but spans many real disks.
`os.link`/`os.rename` only succeed within one underlying branch. (docs: `config/rename_and_link.md`.)

**2. Link/rename behavior is gated by the create policy's *path-preservation class*** (mergerfs
source `fuse_link.cpp` / `fuse_rename.cpp`):
```
path_preserving = func.create.policy.path_preserving() && !ignorepponrename
```
- Path-preserving policies: all `ep*` (epff/eplfs/eplus/epmfs/eprand) **and all `msp*`**
  (mspmfs/msplfs/msplus/msppfrd). `mspmfs` (this pool) is path-preserving — docs: msp policies are
  "defined as `path preserving` for the purpose of controlling `link` and `rename`'s behaviors."
  (`config/functions_categories_policies.md`.)
- Non-path-preserving: `mfs, ff, lfs, lus, lup, pfrd, rand, newest`.

**3. Path-preserving link algorithm** (`config/rename_and_link.md`; "link uses the same strategy but
without the removals"):
- Try to link on the **source's** branch.
- If the dest parent dir is missing there → ENOENT → run the create policy for the dest path;
  **only if the create policy returns the source's branch** does it clone the dir path and link;
  otherwise → **EXDEV**.
- `mspmfs` selects by most-free-space, which is rarely the source's branch → EXDEV. MSM's
  `os.makedirs` beforehand actually guarantees the miss by creating the dest dir on the most-free
  branch first.

**4. mergerfs exposes each file's real disk via per-file xattrs** (`config/runtime_interface.md`,
section "file / directory xattrs"):

| xattr | returns |
| --- | --- |
| `user.mergerfs.basepath` | the branch mount, e.g. `/mnt/disks/disk05` |
| `user.mergerfs.fullpath` | the real underlying path, e.g. `/mnt/disks/disk05/TV Shows/…/x.mkv` |
| `user.mergerfs.relpath`  | path relative to the pool mount |
| `user.mergerfs.allpaths` | NUL-separated list of all copies |

`getfattr -n user.mergerfs.basepath "/media/TV Shows/Meadowlark/Season 01/meadowlark…s01e01.mkv"` →
`/mnt/disks/disk05`. (Values are "given the current getattr policy"; this pool uses
`func.getattr=newest` — fine for single-copy originals.)

## Recommended fix (app-level, branch-aware linking)
Make `fsops.hardlink` mergerfs-aware:
1. Detect a mergerfs mount (e.g. presence of the `<mount>/.mergerfs` pseudo-file, or
   `getxattr(mount, "user.mergerfs.version")`). If not a pool → keep current `os.link` (no behavior
   change off-pool).
2. On a pool: `getxattr(source, "user.mergerfs.basepath")` → the branch. Rebuild the dest on that
   **same branch's raw path** (`<branch>/Transcoded Videos/tablet/<segment>/<source_rel>`),
   preserving the same relative structure. `os.makedirs` + `os.link` on the **raw path**. Both ends on
   one real disk → real hardlink, no EXDEV; the link appears through the pool normally.
3. Update `doctor`: the current `st_dev` equality check is a false positive on a pool — replace or
   augment it with an actual link probe or a `basepath`-resolution check.

Keep the input path's `/<segment>/` component intact — the Tdarr flow keys the encode profile off the
`/2d-animation/` path segment (`checkFileNameIncludes`, `includeFileDirectory`), so the raw-path dest
must still be `…/<segment>/<source_rel>`.

## Caveats
- **Locality:** the `user.mergerfs.*` xattrs are computed by the mergerfs FUSE layer and are only
  reliable when the mount is accessed **locally**. Over CIFS/SMB they almost certainly do not pass
  through, and there is no raw-disk write path. → MSM must run on the host where the mergerfs pool is
  local, with the raw branches writable. (Verify CIFS xattr passthrough if a networked deployment is
  required; assume it does not work.)
- Make pool support a detected capability, not a hard dependency — off-pool users keep plain
  `os.link`.

## Do NOT "fix" this with mergerfs settings
Both settings that would make `os.link` succeed conflict with the operator's core mergerfs goal
("keep top-level dirs on chosen disks; only spill to another disk when the existing ones are full" —
which is exactly what `mspmfs` + path-preserving link enforce):
- **Non-path-preserving `category.create`** (e.g. `mfs`) → new media stops grouping, spreads by free
  space.
- **`ignorepponrename=true`** → removes the path-preserving EXDEV, which is the *guardrail* that stops
  directories being created on unintended disks; it is global to the pool.
- **`link-exdev=rel-symlink`** → produces a symlink, not a hardlink, and a resolved symlink realpath
  drops the `/segment/` component the Tdarr flow depends on.

The operator has explicitly rejected all three. The app-level `basepath` approach is the only one
that leaves the mergerfs policy untouched.
