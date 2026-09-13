---
name: merge-from-crd-backup
description: Merge remote-server changes that CheckRemoteDirty (CRD) saved off the live/staging server back into the local working-directory file — a safe, verified semantic merge, not an overwrite. Use this whenever a saved CRD backup exists to reconcile against. Trigger when the user mentions a CRD/CheckRemoteDirty backup, a `.conflict_bk` file, or a path under `C:\www\CheckRemoteDirty\backups`; says "merge from crd backup"; pastes a backup path with a timestamp suffix and says "merge this"; asks to reconcile a local file against the copy CRD pulled off the server; or names a working file and asks what the server has that local doesn't because a crd backup exists for it. Also trigger when the user describes CRD's behavior WITHOUT naming it — e.g. "the deploy preflight/safety check saved the live copy of the file with a timestamp suffix because it differed, fold those server edits into my local" — and for scanning the backups folder to merge every pending conflict, and for report-only/dry-run previews of such a merge. Do NOT use this for plain local-vs-production diffs where no CRD backup was saved, for setting up CheckRemoteDirty, for git or rebase merge-conflict markers, or for restoring a file wholesale from a personal backup — those are different tasks.
---

# Merge from CheckRemoteDirty backup

CheckRemoteDirty (CRD) is a pre-deploy safety tool: before FTP-deploying, it compares local files
against the live server and saves a copy of any remote file that would be clobbered. Those saved
copies are the only record of edits teammates made directly on the server without committing.

Your job here is to fold those remote edits into the local working file **without losing local work**.
Both sides can be ahead of each other, often within the same file. This is a semantic merge, not a
file copy — never just overwrite the working file with the backup.

## Modes: apply by default, report-only on request

The default is to **apply the merge to the working file**. The user reached for this skill to
reconcile a file, not to read an essay about it — leave the file merged, then report what you did.

Only stay hands-off when the user explicitly asks for a preview — "report only", "dry run", "don't
touch the file yet", "just show me what the server changed". In that mode, do all the analysis and
produce the full report, but leave the working file untouched and say so.

The safety net for the applied case is git, and it depends on the working file's state:

- **Tracked and clean** → apply the merge straight to the working file. Git already holds the
  pre-merge version at `HEAD`, so the merge is fully reversible with `git checkout --` and no
  pre-commit is needed.
- **Dirty (uncommitted changes)** and not in report-only mode → **commit the file first**, by itself,
  as the safety net, *then* apply the merge on top. This draws a clean line in history between "what
  I had locally" and "what the merge changed", so a bad merge is one `git checkout --` away from the
  exact pre-merge state instead of tangled up with unrelated working changes.

  ```bash
  git add -- <rel/path>
  git commit -m "chore(crd-merge): snapshot <rel/path> before merging remote edits" -- <rel/path>
  ```

  Committing only that one pathspec keeps any *other* dirty files in the working tree out of the
  snapshot. If committing just this file isn't clean — it's entangled in a staged change set, or the
  user has a commit in flight — don't force it: stop and ask how they want the safety net taken.

This commit-first rule replaces the old `.orig_bk` sidecar: a commit is a better anchor (it's the
baseline for Step 5's Pivot A for free) and it doesn't leave stray files under the CRD backups tree.

## Layout and naming

```
C:\www\CheckRemoteDirty\backups\<repo>\<path mirroring the repo>\<filename>.<timestamp>[.conflict_bk]
```

`<repo>` is the basename of the working dir, so `backups\widgetco\...` ⇄ `C:\www\widgetco\...`.
`<timestamp>` is the **remote file's mtime**, `YYYYMMDD_HHMMSS` (rarely a 14-digit `YYYYMMDDHHMMSS`
fallback when CRD couldn't read the mtime).

The two suffixes mean different things, and the difference matters:

| Suffix | What happened | Meaning |
| --- | --- | --- |
| `.<ts>.conflict_bk` | Deploy was **skipped**; remote file kept | Unresolved conflict, still pending |
| `.<ts>` | Remote file saved, then **overwritten** by the deploy | Deliberately superseded |

**Pick the newest timestamp, regardless of suffix.** The newest copy is what the server most recently
had, which is what you are merging against. If the newest is a plain `.<ts>` and an older
`.conflict_bk` sits beneath it, the conflict was already resolved by a later deploy — say so in your
report rather than reviving stale content. If both suffixes share one timestamp, they are the same
bytes; prefer the `.conflict_bk`.

## Step 0 — Resolve the paths

Run the bundled resolver. It accepts **either** end of the pair — a backup path or a working file —
and prints the counterpart, every candidate ranked newest-first, and whether the two differ at all:

```bash
python <skill>/scripts/resolve_backup.py "C:\www\widgetco\config\config.php"
python <skill>/scripts/resolve_backup.py "C:\www\CheckRemoteDirty\backups\widgetco\config\config.php.20260624_075623.conflict_bk"
python <skill>/scripts/resolve_backup.py --scan widgetco     # every pending conflict in a repo
python <skill>/scripts/resolve_backup.py <path> --json        # machine-readable
```

Use it rather than eyeballing `ls` output. Timestamps are dense, there are often six or seven
candidates, and picking the wrong one silently merges month-old server content.

If it reports the working file is byte-identical to the latest backup, stop — there is nothing to
merge. Report that and leave the file alone.

**Guard against empty/truncated captures.** CRD downloads over FTP, and a dropped transfer can leave a
**0-byte** (or absurdly small) backup that still carries the newest timestamp. That is a failed
capture, not a real server state — merging it would blank the working file. The resolver flags this
(`!! NEWEST BACKUP IS 0 BYTES`) and points at the newest backup that actually has bytes; fall back to
that one, and note in your report that the newest backup was a failed capture (the user may want to
delete it so it stops being picked as newest). If *no* non-empty backup exists, there is nothing to
merge — stop.

## Step 1 — Normalize line endings before you diff anything

This is the single most common way this task goes wrong. Files pulled off the server over FTP
usually arrive **CRLF**. The local working copy's endings depend on the repo: with `git config
core.autocrlf` set to `true` (common on these Windows checkouts) the working tree is **CRLF**; on an
LF-normalized checkout it is **LF**. Don't assume — a plain `diff` between mismatched endings reports
every line as changed, and it is tempting to conclude the file was rewritten wholesale and just take
the remote copy, destroying local work. Check the working file's actual endings (e.g.
`git ls-files --eol <path>`, or the CR count) rather than guessing.

Always diff with line endings normalized:

```bash
diff -u --strip-trailing-cr <working_file> <backup_file>
```

CRD also ships `C:\www\CheckRemoteDirty\diff_normalized.py`, which tokenizes past whitespace and
reformatting; reach for it when a file has been reindented or reflowed and `--strip-trailing-cr`
still leaves noise.

Sanity-check the scale: if the normalized diff is still 100% of the file, that is a real signal
(different file entirely — see the dims2 guardrail). If it collapses from thousands of lines to a
handful, you just avoided the trap.

**When you write the merged result, preserve the working file's existing line endings** — whichever
they are. Making a targeted edit inside the file (rather than rewriting it whole) inherits the
surrounding endings automatically; flipping the whole file to the backup's endings would show up as a
whole-file diff in the user's next commit.

## Step 2 — dims2 only: verify file sourcing fidelity first

Some `dims2` git branches were mistakenly committed with **imd's** files instead of the branch's own.
So in dims2, a mismatch between backup and local may not be a remote edit at all — it may be that
local is carrying the wrong file entirely.

Before merging anything in dims2, compare the **chosen (newest)** backup against the branch's HEAD
version. Old backups legitimately diverge just by being old, so running this gate on anything but the
one you're merging produces false alarms.

```bash
git show HEAD:<rel/path> | diff --strip-trailing-cr - <backup_file> | grep -c '^[<>]'   # changed lines
```

Then weigh two signals: the changed-line count against the file's size, and whether the two share the
same top-level definitions (function/class names). A file that is *the same file, edited* keeps its
definitions; a file that is *somebody else's file* does not.

- **Identical** → the repo copy is in sync with this deployment branch. Proceed.
- **Incremental drift** — recognizably the same file: the definitions overlap almost entirely, and
  nothing substantial appears only in the backup. This is an ordinary remote edit, even when the
  changed-line count is large. Proceed with the merge.
- **Wholesale divergence** — different functions, different feature set, changed lines approaching or
  exceeding the file's own length. This is the imd-commit bug: **stop, change nothing, and ask the
  user to verify the git file sourcing manually.**
- **Cannot tell** → stop and ask. A wrong guess corrupts the branch, and the user has asked to be the
  one who adjudicates it.

**Always report this comparison**, including when the files are identical — changed lines, definition
overlap, and the verdict you reached. The user audits the judgment call after the fact; they can only
do that if the measurement is in the report even when the gate let you through.

For orientation on what these look like in practice: on `candidates_controller.php` (2076 lines), the
newest backup differed by 563 lines while sharing all 23 functions with none unique to the backup —
drift, safe to merge. Two older backups differed by 3185 and 3718 lines, exceeding the file's own
length — the shape of divergence, and a stop if either had been the chosen backup.

## Step 3 — Anchor the baseline

You need a static "original local" reference to diff against in Step 5, because you are about to
modify the working file. The mode logic from the top of this skill already determines the anchor —
this is just naming it explicitly:

- **Report-only mode** → you won't modify the file at all, so the live working file is its own
  baseline. Nothing to anchor.
- **Applying, file was clean** → `HEAD` is the baseline. Nothing to create.
- **Applying, file was dirty** → the safety-net commit you just made *is* the baseline; its `HEAD` now
  holds the exact pre-merge content.

In every applied case the baseline is a git ref, which is what makes a bad merge recoverable: without
that anchor there is no way to prove afterwards that no local feature was dropped.

## Step 4 — Merge

### Fast path: three-way merge when a base is known (prefer this)

Everything below in the "hunk by hunk" section is a *manual reconstruction* of one missing fact: the
**base** — the version the server last held, that local and remote both descend from. With the base,
"is this line a local addition or a remote deletion?" stops being a judgment call and becomes
mechanical: `git merge-file` resolves every one-sided change automatically and only flags the lines
**both** sides touched — which is exactly where human judgment is actually needed, and nowhere else.

So before reasoning hunk by hunk, check whether a base is available:

- **CRD conflict sidecar.** If CRD wrote `<backup>.conflict_bk.meta.json`, it recorded the base commit
  in `baseline.merge_base_ref`. The resolver surfaces it (`3-WAY BASE available (CRD sidecar): …`).
- **`--gitBaselineHash`.** If the CRD run was given one, that commit is the base.
- **`my_remote.deployed_commit`** in CRD's hash manifest — the commit the last deploy corresponds to.
- **You can often name it yourself:** the commit that was live on the server (e.g. the last deploy tag).

> **The base must be an ancestor of *both* sides — and the supplied base often isn't.** A deploy tag /
> `--gitBaselineHash` is guaranteed to be an ancestor of **local**, never of **remote**. When the server
> file predates the base (a stale/deprecated copy — very common), the base is *newer* than remote's true
> ancestor, so `base → remote` reads as "remote deleted X" for everything the base and local added after
> the snapshot. `git merge-file` applies those fake one-sided deletions and returns **0 conflicts** — a
> silent regression that drops local work while looking clean. Timestamp tell: **if the remote's mtime is
> older than the base commit's date, the base is wrong** (too new). The engine now guards this itself — a
> *stale gate* (remote byte-matches an older local blob ⇒ keep local, `exit 5`) and *base re-derivation*
> (re-pick the local commit contemporaneous with the remote snapshot). If you ever merge by hand instead,
> confirm the base is older than both sides first, and treat a "remote deleted my feature" hunk on a
> months-old backup as staleness, not intent.

When you have any of these, run the bundled engine instead of merging by hand:

```bash
python <skill>/scripts/three_way_merge.py <working_file> <backup_file>          # reads the sidecar
python <skill>/scripts/three_way_merge.py <working_file> <backup_file> --base-ref <commit>
```

- **exit 0 (clean):** it auto-resolved everything. Review its proposed output (`<backup>.merged`), then
  re-run with `--apply` to write it, or apply it yourself. Still run Step 5 verification. If it reports
  `base_corrected_to`, it found the supplied base too new (or not local's ancestor) and re-derived a valid
  one — the merge is trustworthy, but glance at which base it chose.
- **exit 2 (conflicts):** only the both-sides hunks remain, marked with `<<<<<<<` in `<backup>.merged`.
  Resolve *those* by hand with the reasoning below — but you've skipped the archaeology on everything else.
- **exit 3 (no/invalid base):** no usable base — none supplied, or the supplied one postdates the remote
  and no earlier local commit matches its snapshot time. Fall back to the two-way method below.
- **exit 5 (stale_remote):** the remote byte-matches an older local blob — a deprecated copy with zero
  teammate edits. **Keep local; nothing to merge.** This is the case that used to masquerade as a clean
  merge and silently revert local work; pass `--local-ref <branch>` if "local" isn't `HEAD`.

Line endings are normalized internally and the working file's own endings are preserved on output.
This path is strictly better than two-way when a base exists — it can't misjudge add-vs-delete, because
the base settles it. Use the hunk-by-hunk method below only for the residual conflicts, or when exit 3.

### Two-way fallback: hunk by hunk

Work hunk by hunk on the normalized diff. For each one, establish which side is ahead:

- Only local has it → local added it after the server copy was taken. **Keep local. Do not delete it
  just because the remote lacks it.** This is the most damaging mistake available in this workflow —
  the remote copy is a *snapshot from a past timestamp*, and absence there is usually just age.
- Only remote has it → a teammate edited the server directly. Adopt it.
- Both changed the same lines → a genuine conflict. Reason about intent: check the schema, the
  surrounding code, git log. If one side is clearly a bugfix and the other is stale, take the fix.
- Pure formatting or reindentation with no semantic change → prefer the working file's existing style
  and mention that you skipped it, so the user isn't surprised by a no-op hunk.

Merge every hunk you are confident about. For the rest, leave the working file in the pre-merge state
for those hunks, and ask — present each undecided hunk with both sides and your read of the tradeoff.
Partial progress plus a precise question beats a confident wrong merge.

Some of these files hold live credentials and secret keys (`config/config.php` most of all). Reason
about them normally, but don't echo secret values into your report — refer to them by key name.

## Step 5 — Verify with dual-pivot diffing

If the merge touched anything load-bearing — database guards, auth logic, infrastructure sync, SQL
joins — or if you wove logic from both sides into the same region, verify against **both** originals.
Confirming against only one pivot cannot detect the failure mode of the other.

- **Pivot A — Local Guard:** diff the result against the **anchored baseline** from Step 3 — `HEAD`,
  which is either the pre-existing clean commit or the safety-net commit you just made (`git diff` for
  an unstaged merge, or `git diff HEAD` if you staged it). Every difference must be a remote change you
  consciously chose to adopt. Anything else is a regression: a local feature you silently dropped.
- **Pivot B — Remote Adoption:** diff the result against the **CRD backup**. Every remaining
  difference must be local work you consciously chose to keep. Anything else is a remote change you
  silently failed to adopt.

Both diffs should contain only lines you can name a reason for. If a hunk shows up in either pivot
that you can't account for, you have a bug in the merge — fix it before reporting.

## Step 6 — Report, and save the report

Leave the CRD backup in place; it's the audit trail, and CRD checks for its existence to avoid
re-downloading. There is no `.orig_bk` to clean up any more — the safety net is a commit.

Write the report to **both** places:

1. **In your reply to the user**, so they see it immediately.
2. **To disk**, so it survives for later review. Save it under the repo at:

   ```
   dev/migrations/crd-merges/<rel_path_with_dirs_as_underscores>-<merge_timestamp>-merge-report.md
   ```

   The filename flattens the file's repo-relative path into a single token, using `_` for every
   directory separator, so one folder holds a browsable history of every merge. `<merge_timestamp>` is
   `YYYYMMDD_HHMMSS` at merge time (the moment you ran the merge, not the backup's remote mtime).

   Example: merging `public/admin/index.php` produces
   `dev/migrations/crd-merges/public_admin_index.php-20260710_231500-merge-report.md`.

   `mkdir -p dev/migrations/crd-merges` first; it won't exist on the first merge. Save the report even
   in report-only mode — a preview is still worth keeping — but note in it that nothing was applied.

Use this shape for the report (identical on disk and in your reply):

```
## Merged: <rel/path>
Mode: applied | report-only
Backup: <chosen backup path>  (remote mtime <pretty ts>, N candidates, chose newest)
Safety net: clean @ HEAD <sha> | pre-merge commit <sha> | none (report-only)
Sourcing gate (dims2 only): <changed lines vs HEAD, definition overlap, verdict>

### Adopted from remote
- <hunk>: <what and why>

### Kept from local
- <hunk>: <what and why>

### Skipped
- <hunk>: <e.g. reindentation only, no semantic change>

### Needs your decision      (omit if none)
- <hunk>: <both sides, the tradeoff, your recommendation>

### Verification
- Pivot A (vs <baseline>): <what the diff showed>
- Pivot B (vs backup): <what the diff showed>
```

State the pivots explicitly. They are the evidence that the merge is safe, and they are the part the
user is actually checking.
