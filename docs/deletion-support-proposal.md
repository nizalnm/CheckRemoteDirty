# Proposal: safe, rollbackable deletion support for CRD

Written after a real incident (2026-08-02, widgetco): `resources/forms/legacy_signup_form.pdf.b64`
was deliberately deleted in git (superseded by a new form generator) but CRD had
no way to remove it from the live server — it's currently either an unresolvable
`DIFF HASH` needing a manual keep/skip, or silently left stale. Nothing implemented
yet; this is a design for discussion before any code changes.

## 1. Why this is a distinct, harder problem than upload/overwrite

Every other CRD operation is **recoverable by re-running**: if an upload fails or
uploads the wrong bytes, running CRD again fixes it — the source of truth (git)
still has the correct content. A **delete is the one operation whose failure mode
is data loss**, not staleness. Re-running CRD can't undo an accidental delete the
way it can undo an accidental upload. Every design decision below follows from
that asymmetry: uploads default to "just do it, it's reversible"; deletes must
default to "don't, unless explicitly told to, and even then don't destroy
anything irreversibly."

## 2. Industry patterns worth borrowing

- **`rsync --delete`**: deletion is a separate, explicitly opt-in flag from the
  base sync behavior — sync-without-delete is the safe default, and there are
  *three* delete-timing variants (`--delete-before`, `--delete-during`,
  `--delete-after`) precisely because "when" a delete happens relative to the
  rest of the transfer matters for safety.
- **`terraform plan`/`apply`**: destructive changes are shown in a distinct,
  visually-flagged section of the plan (`-` prefix, red) *before* any apply, and
  `apply` requires a typed confirmation separate from any other action. Additions
  and deletions are never confirmed with the same blanket "proceed? y/n."
- **`kubectl apply --prune`**: pruning (deleting resources no longer in the
  manifest) is opt-in per-invocation and additionally scoped by a label selector
  — you can't accidentally prune something outside the intended blast radius
  just because it happened to be missing from this particular apply.
- **S3 versioning + lifecycle rules / most object stores**: "delete" by default
  is a soft delete (a delete marker, old version retained); a *separate*,
  explicit action (or a time-boxed lifecycle policy) is what does a hard,
  unrecoverable purge. Recovery from an accidental soft delete is a config
  change, not a restore-from-backup exercise.
- **OS trash/recycle bin**: move-then-purge-later is a decades-old, well
  understood UX pattern for exactly this class of "the delete itself should be
  instant and cheap to undo."
- **`git rm` + reflog / most VCS**: the delete is itself a versioned, reversible
  operation for a retention window — this is the same shape as CRD's own
  existing overwrite-backup convention, just not yet applied to deletes.

The common thread: **separate detection from confirmation from execution from
purge**, and make every step but the last one cheap to reverse.

## 3. Proposed design

### 3.1 Detection — a new, distinct file-list category

`get_files_changed_in_commit()` currently derives its list from
`git diff --name-only`, which only shows added/modified paths — a deleted path
is invisible to it today (that's the root cause of the incident: the tool
literally never learns a deletion happened). Change it to
`git diff --name-status` and split the result into two lists:

```python
added_or_modified: list[str]   # existing behavior, unchanged
deleted: list[str]             # new — git says this path no longer exists at <B>
```

`deleted` gets its own comparison pass against the remote: does the remote
still have this path? If yes, it's a **delete candidate**. This is new
territory for `compare_with_ftp()` — today every code path assumes "local has
bytes to compare/upload," which is never true for a delete candidate.

### 3.2 Never delete by default

No existing flag or default behavior triggers a delete, ever. A new explicit
flag is required to even *consider* deleting anything:

```
--pruneDeleted            # opt in to processing delete candidates at all
--pruneMode=soft|hard     # default: soft (see 3.3). hard requires soft first (3.5)
```

Omitting `--pruneDeleted` preserves **exactly today's behavior** (delete
candidates get reported, same as any other `DIFF HASH`-adjacent finding, but
never acted on) — this is a strictly additive change, not a breaking one.

### 3.3 Soft delete: quarantine, don't remove

When a delete candidate is confirmed (3.4) and `--pruneMode=soft` (the
default once `--pruneDeleted` is set), CRD does **not** call `ftp.delete()`.
It **renames the remote file** into a dated quarantine path parallel to the
live tree, e.g.:

```
<remote_root>/.crd-trash/<YYYYMMDD_HHMMSS>/<original/relative/path>
```

FTP's `RNFR`/`RNTO` (i.e. `ftp.rename()`) does this as a single atomic
operation on most servers — no download/upload round-trip needed, and no
window where the file is neither at its original path nor safely elsewhere.
This is the operation that actually makes deletion **instant to undo**: the
file never stops existing on the server, it just moves.

`.crd-trash/` should be added to the project's own `.htaccess`/web-server deny
rules (same treatment already recommended for the migration runner in the
`db-tracker-and-migrations-scaffold` skill) so quarantined files aren't
web-reachable at their new path either.

### 3.4 Confirmation — separate from, and stricter than, the upload prompt

Reuse the asymmetry `terraform plan` establishes: deletions get their own
listing and their own confirmation, never folded into the existing
`Proceed with deployment? (Y/n)`:

```
--- The following N file(s) will be moved to quarantine (recoverable) ---
  - resources/forms/legacy_signup_form.pdf.b64   (deleted in a2f91c3, 2026-08-01)
  ...
Type the number of files to confirm (3), or Enter to abort:
```

Requiring the *count* to be typed back (not just `y`) is a deliberate extra
friction step for the one CRD operation whose failure mode is data loss, not
staleness — cheap insurance against a fat-fingered `Y` on a prompt the operator
has seen a hundred times for ordinary uploads.

### 3.5 Hard delete / purge — a separate command, on a retention window

Soft-deleted files are not permanent. A new, separate CLI mode does the actual
permanent removal, and only for quarantined entries older than a retention
window:

```
python CheckRemoteDirty.py --workingDir <dir> --ftpConfig <cfg> --purgeTrash --olderThanDays 30
```

This is intentionally a **separate invocation, never automatic**, and
defaults to a dry-run listing (what *would* be purged) unless `--yes` is also
passed — mirroring the existing `up`/`baseline` dry-run-then-confirm shape
this whole toolchain already uses elsewhere (`migrate.php`, the
`crd-deploy-workflow` skill's own Stage 6 confirmation gate).

### 3.6 Restore — completing the "rollbackable" requirement

A soft delete that can't be trivially undone isn't actually rollbackable, just
delayed. Add a companion mode:

```
python CheckRemoteDirty.py --workingDir <dir> --ftpConfig <cfg> --restoreFromTrash <path-or-glob>
```

Lists matching quarantine entries (newest first, same disambiguation UX the
`merge-from-crd-backup` skill already uses for picking among several
timestamped backups of one path) and moves the chosen one back to its
original location via the same atomic rename.

### 3.7 Safety rails, all mandatory, not configurable away

- **Never delete a path matching `--exclude`.** If a pattern says "this path
  isn't managed by this deploy," a delete candidate for that path shouldn't be
  actionable either — same reasoning as excluding it from uploads.
- **Protected-path denylist**, checked before anything else, hardcoded and not
  overridable by project config: the server's own config files, `.htaccess`/
  `web.config`, anything under the project's own `.crd-trash/` (no deleting the
  trash can itself via a crafted path), and any path CRD resolves to outside
  the configured project root after normalization (traversal guard — reject
  `..` segments and symlink-resolved paths that escape the root).
- **A delete candidate must be a real git deletion, not an inference.** Only
  paths that appear as `D` in `git diff --name-status <baseline>..<HEAD>` are
  ever delete candidates. A path that's merely absent from the working
  directory for some other reason (wrong branch, incomplete checkout, a
  submodule not initialized) must never be treated as "safe to remove
  remotely" — this is exactly the distinction that matters, and it's the same
  git-diff-derived list `get_files_changed_in_commit()` already builds for
  uploads, just filtered on `D` instead of `A`/`M`.
- **Idempotent and resumable**, given today's actual flaky-FTP experience: a
  file already in quarantine should be a no-op on re-run (check remote
  existence at the quarantine path before attempting another rename), not an
  error that blocks the rest of the batch.

### 3.8 Auditability

Every soft-delete and restore appends a line to a per-project log
(`<CRD_ROOT>/backups/<project>/.trash-log.jsonl` — one JSON object per line:
timestamp, original path, quarantine path, triggering git commit, action
`quarantine`/`restore`/`purge`). This is the delete-side equivalent of the
`.conflict_bk` naming convention already carrying this information for
overwrites; it's what lets a human answer "did we actually mean to remove
this, and when" months later without archaeology.

## 4. Proposed CLI surface (summary)

| Flag | Behavior |
| --- | --- |
| *(none, existing behavior)* | Delete candidates reported, never acted on — no change from today |
| `--pruneDeleted` | Opt in to processing delete candidates as quarantine moves |
| `--pruneMode=soft` (default) | Atomic rename into `.crd-trash/<timestamp>/...` |
| `--pruneMode=hard` | Refuses unless the path is already soft-deleted and past retention — use `--purgeTrash` instead |
| `--purgeTrash --olderThanDays N [--yes]` | Separate command: permanently remove quarantine entries older than N days; dry-run by default |
| `--restoreFromTrash <path>` | Separate command: move a quarantined file back to its original location |

## 5. Phased build order

1. **Detection only** (3.1): surface delete candidates in the existing report
   table as a new status, e.g. `PENDING_DELETE (git) / STILL LIVE (remote)` —
   zero behavior change, pure visibility. This alone would have caught
   today's incident at Stage 4 instead of silently misclassifying it.
2. **Soft delete + confirmation + audit log** (3.2–3.4, 3.7, 3.8) — the actual
   opt-in quarantine mechanism.
3. **Restore** (3.6) — needed before hard delete ships, since "rollbackable"
   is the whole point.
4. **Purge** (3.5) — last, and only once 1–3 have real-world mileage; a
   permanent-delete code path deserves the most scrutiny and the most time in
   production as a safety net before it's trusted to actually remove data.

## 6. Open questions for the user, not decided here

- Default `--olderThanDays` retention window for purge (30 is a common
  default across the object-store examples above, but this project's own
  risk tolerance should set it, not a borrowed default).
- Whether `--pruneDeleted` should be a per-invocation flag (as designed above)
  or a per-project `excludePatterns`-style persistent config default once a
  project's operator has used it safely for a while.
- Whether quarantine should live under the same document root (simplest,
  needs the deny-rule treatment in 3.3) or somewhere the FTP account can reach
  but that's structurally outside the docroot entirely (safer, but not always
  possible depending on hosting — many shared-hosting FTP accounts are
  chrooted to the docroot itself).
