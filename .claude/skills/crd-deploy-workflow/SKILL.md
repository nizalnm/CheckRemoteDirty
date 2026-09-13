---
name: crd-deploy-workflow
description: Drives the full CheckRemoteDirty (CRD)-based deploy pipeline for a git+FTP project — proactively reconciling the WHOLE remote tree against a mirror branch (e.g. "staging": both files a teammate created directly on the server AND tracked files someone edited on the server, filtered for real content changes vs. mere line-ending/whitespace noise), running a CRD preflight safety check from the project's actual deploy branch (which may be a separate dev branch, not the mirror), resolving every conflict found (proactively or by CRD) via the merge-from-crd-backup skill, deploying, and force-moving the project's singleton "last deployed" git tag to the new commit. Use this whenever the user asks to deploy a CRD-managed project (e.g. "deploy acmeapp", "deploy widgetco to staging", "push widgetco live"), asks to pull/sync/reconcile files that exist on the remote server but not in git or that were edited directly on the server ("pull orphaned files from X", "someone edited files directly on the server, get them into git", "check the remote for anything newer than local before I start", "I usually diff this with Beyond Compare first, do that"), asks what's pending or safe to deploy on a project ("what's pending deploy on acmeapp", "is widgetco safe to push"), or wants the "latest deployed" tag moved/checked. Also trigger when the user describes any piece of this pipeline without naming it — "run the safety check before I push", "merge in whatever the server has that we don't", "move the deployed tag to this commit" — as long as the project is one with a CRD workflow config. Do NOT use this for projects with no CRD config/no FTP deploy target, for plain git merges/deploys unrelated to CRD, or for resolving a single already-known conflict backup in isolation (that narrower job belongs to the merge-from-crd-backup skill directly).
---

# CRD deploy workflow

This skill orchestrates an eight-stage pipeline around CheckRemoteDirty (CRD), the
FTP-vs-git safety tool at the core of this whole workflow. CRD itself only
answers "does the local/git state match the live server?" — the stages here
are the human process built around that answer: pull in what the server has
that git doesn't, converge both branches with it *before* shipping anything,
check safety, merge conflicts, deploy, and record what's now live. Expose the
stages as simple actions ("deploy X", "what's pending on X") — the user
shouldn't need to know the eight steps by name to use this.

**Read `merge-from-crd-backup`'s SKILL.md before Stage 5.** This skill calls
into it rather than re-implementing 3-way merge logic — that skill owns the
merge contract (modes, exit codes, report format) and is the authority on it.

## Setup: one config file per project

Every project this skill drives needs a small JSON file, `<project>_workflow.json`,
sitting next to that project's other CRD files (its `*_config.json` FTP creds
and `*_dirty_check.json` hash manifest). See `assets/sample_workflow_config.json`
for the template:

```json
{
  "project": "acmeapp",
  "workingDir": "C:\\www\\acmeapp",
  "mirrorBranch": "staging",
  "deployBranch": "staging",
  "deployedTag": "latest-staging-deployed",
  "dirtyCheckFile": "acmeapp_dirty_check.json",
  "ftpConfig": "acmeapp_config.json",
  "stage1StateFile": "acmeapp_stage1_state.json",
  "gitRemote": "origin",
  "excludePatterns": ["cache/*", "*.log", "Thumbs.db"]
}
```

`stage1StateFile` names the small JSON sidecar `scripts/stage1_gate.py` uses to
track Stage 1's daily-scan gate and drift-triggered enforcement window (see
Stage 1 below). It lives next to `ftpConfig`/`dirtyCheckFile` under
`CRD_ROOT`, same as those. If omitted, defaults to `<project>_stage1_state.json`.
Missing file == fresh state, so it doesn't need to be created up front.

**`mirrorBranch` and `deployBranch` are two different jobs that happen to be
the same branch in some projects, but not all.**

- `mirrorBranch` is a passive record of "what does the server actually have
  right now" — the branch Stage 1's remote-vs-local scan lands its findings
  on. In some projects (acmeapp) this is also the branch that gets deployed.
  In others (widgetco) it's kept in sync by CRD's own conflict-backup flow
  and by Stage 1, but nothing is ever deployed *from* it — it always already
  mirrors live, so running preflight/deploy against it finds nothing to push.
- `deployBranch` is what Stage 4/6 actually run CRD's preflight and deploy
  against — the branch new work actually lands on (a dev branch, e.g.
  widgetco's `claude`, or a worktree branch feeding into it).

If a project doesn't distinguish the two, set both fields to the same branch
name (as in the example above) — `deployBranch` defaults to `mirrorBranch`
when omitted, so simple setups don't need to think about this at all.
**Check which case you're in before assuming** — `git log --oneline -1
<mirrorBranch>` vs the branch actually being worked on; if they're
consistently identical in history, they're the same branch for this purpose.
If unsure, ask the user rather than guessing: deploying from the wrong branch
either silently deploys nothing (mirror already matches live) or deploys the
wrong commit.

`excludePatterns` is a persistent, project-level list of glob patterns for Stage 1's remote scan to skip outright — server-only housekeeping (logs, cache, thumbnails) that was never meant to be version-controlled. This is on top of whatever `.gitignore` already covers, for the case where `.gitignore` doesn't happen to list something the host generates.

**CRD's actual deploy tool (`CheckRemoteDirty.py`) has its own separate, hardcoded exclusion** baked into `get_files_changed_in_commit()`: any path under `.agent/`, `.agents/`, `.beads/`, `.ralph-tui/`, or `tasks/` is silently dropped from the file list for *both* `--vsGit` and the range-scoped `--vsGitListHash` form — these paths are permanently deploy-exempt (agent-workspace/session bookkeeping, not app content), independent of `excludePatterns`. That's a feature, not a bug: files like `.agents/AGENTS.md` are meant to stay git-versioned forever without ever going out over FTP. It also means Stage 1's proactive scan (a separate script, its own `excludePatterns`) will otherwise flag these paths as "modified" every single run once the live copy inevitably drifts — because it doesn't know about CRD's hardcoded list. Mirror CRD's five prefixes into every project's `excludePatterns` (as globs: `.agents/*`, `.agent/*`, `.beads/*`, `.ralph-tui/*`, `tasks/*`) so Stage 1 stops reporting deliberately-never-deployed drift as if it were a real conflict.

**`CheckRemoteDirty.py` also takes its own `--exclude <pattern>` flag** (repeatable, fnmatch-style glob against the git-relative path), applied on top of the five hardcoded prefixes above. Unlike the hardcoded list, this one *is* driven by `excludePatterns` — pass every pattern from the project's `excludePatterns` as a repeated `--exclude` on **every** Stage 4/6 invocation (both the plain and range-scoped forms below already show this). This is for paths that must stay fully committed to git but should never be FTP-checked or deployed — the canonical case is a `dev/db/migrations/*tracker*.sql`-style pattern for tracker-DB audit/replay migration records (see "Finishing comments" below): they're real, meaningful history that belongs in git forever, just never on the live server. If a project's `excludePatterns` has entries that only make sense for Stage 1's server-scan (housekeeping paths like `cache/*`, `*.log` that would never appear in a git diff range anyway), passing them here too is harmless — a pattern that never matches anything in the file list is a no-op.

`mirrorWorktree` (split-branch projects only): path to a **persistent git
worktree that permanently holds `mirrorBranch` checked out** (create once
with `git worktree add <path> <mirrorBranch>`). Two jobs at once. It's where
Stage 1's pulls and Stage 2's commits actually happen, so the main working
dir never has to leave `deployBranch` at any stage. And it's a checkout
lock: git refuses to check out a branch that's already checked out in
another worktree, so no concurrent session can accidentally land the main
dir on the mirror branch and clobber (or get clobbered by) remote pulls.
Keep it persistent rather than temporary — the lock only protects while the
worktree exists, and re-creating it per run costs a full checkout each time
for no benefit. Note the flip side: while it exists, mirror-branch git
operations (Stage 2 commits, Stage 8's catch-up merge) must run **inside
the worktree** — `git checkout <mirrorBranch>` in the main dir will fail by
design.

If a project's `dirtyCheckFile` doesn't exist yet, or you're correcting an
inconsistently-named leftover from before this skill existed (a typo'd
project name, a one-off dated filename from a manual run, etc.), don't just
point the config at a fresh empty filename. CRD's `--vsGit` mode loads
whatever JSON already exists at that path (`load_json(args.vsGit) or []`) and
carries its per-file records forward rather than starting from a blank slate
— copy the old file's content into the correctly-named one first, point
`dirtyCheckFile` at the new name, and re-run whatever check you were doing so
it picks up the carried-forward state instead of losing it.

If a project the user names has no `<project>_workflow.json` yet, that's not
an error — help them create one. Ask for (or infer from the repo) the working
directory, and the mirror/deploy branch and deployed-tag names — check what
already exists (`git branch -a`, `git tag`, `git log`) before assuming
`staging` / `latest-staging-deployed`; those are just this workflow's common
defaults, not guaranteed, and `mirrorBranch`/`deployBranch` being the same
branch is a per-project fact, not a default to assume either way. The FTP
config and dirty-check filenames should match whatever that project's existing
CRD wrapper scripts (e.g. `<project>_preflight.ps1`) already use — don't
invent new ones if the project already has CRD set up.

**Where CRD itself lives is separately configurable**, so this is portable to
a teammate's machine: resolve it the same way `merge-from-crd-backup` already
does — the `CRD_ROOT` environment variable, falling back to
`C:\www\CheckRemoteDirty` if unset. Repos live under `REPOS_ROOT`
(env var, falling back to `C:\www`) unless a workflow config's `workingDir` is
absolute (it always should be). Don't hardcode either path in anything you
write or run.

## The eight stages

Run these in order for a full deploy ("deploy acmeapp"), gated by a Stage 0
preflight that must resolve clean before Stage 1 starts. A user can also ask
for just one stage — map their words to the stage, run only that, and say so
(Stage 0 still applies whenever the request would touch `workingDir`, i.e.
anything from "pull orphaned files" through "deploy X"; the read-only
"what's pending" check does not need it).

### Stage 0 — Preflight: is the main working dir clean?

Before Stage 1 touches anything, check the actual deploy-branch working tree
(`workingDir` — the main checkout, not `mirrorWorktree`):

```bash
cd <workingDir>
git status --short
```

If clean, proceed to Stage 1.

If dirty, determine provenance before deciding what to do — don't assume the
files are safe to commit, defer, or ignore just because a deploy was
requested:

1. **Check for another Claude Code session already working in this
   directory.** Call the session-management `list_sessions` tool and look
   for an entry whose `cwd` matches `workingDir` (or, for worktree-based
   projects, a worktree under this same repo) with recent activity. A past
   session having once used this `cwd` is normal and not a signal by
   itself — this project's main dir is not worktree-isolated, so plenty of
   unrelated past sessions will list it. What matters is whether one looks
   like the actual author of the *current* uncommitted changes: recent
   `lastActivityAt`, and/or the changed file paths plausibly matching what
   that session's title says it was doing.
2. **If another live session plausibly owns the dirty files:** send it a
   message (`send_message`) asking it to commit its work if done, and to
   signal back — then wait for that signal before proceeding to Stage 1.
   Don't commit someone else's in-flight work on their behalf, and don't
   silently deploy around it: their commit might be exactly what's meant to
   ship this cycle, or might conflict with what Stage 3 is about to fold in.
3. **If the dirty files are this session's own already-completed work**
   (edited earlier in this same conversation, and you consider that work
   done): say so plainly and ask the user directly whether to commit it now
   as part of this deploy. Don't commit unasked — this codebase's standing
   rule is to only commit when the user requests it — and don't silently
   proceed as though it were already committed either.
4. **If the dirty files have no identifiable Claude-session owner at all**
   (no matching session, or the owning session is long stale) — or once
   steps 2/3 resolve without a clear "yes, commit and proceed" answer — stop
   and ask the user to choose:
   - **Deploy committed files only** — proceed with Stages 1-8 as if the
     dirty files don't exist. They won't appear in any commit-range diff, so
     they're simply excluded from this deploy; the working tree stays dirty
     afterward, and say so plainly (it's easy to mistake "deploy succeeded"
     for "everything I see in the tree went out").
   - **Commit the dirty files and deploy them too** — commit on
     `workingDir`'s current branch, then continue; the newly-committed files
     become part of the same deploy range.
   - **Abort** — stop the whole run here, no further stages.

Never silently start Stage 1 against a dirty `workingDir` without one of the
above resolutions — a stray uncommitted edit that quietly rides along into
(or quietly gets excluded from) a live deploy is exactly the kind of
surprise this stage exists to prevent.

### Stage 1 — Reconcile the whole remote tree against the mirror branch

**Gate this stage before running it — it does not run on every deploy by
default.** Stage 1's full recursive directory listing is expensive relative
to CRD's own targeted preflight (see below), so the default is once per
calendar day: the first deploy of the day runs it, later deploys that same
day skip straight to Stage 4. That default gate is *overridden* by a
drift-triggered enforcement window: if a later stage (Stage 4's preflight,
typically) ever finds a real, unexpected conflict — drift Stage 1 didn't
catch, whether because it was gated off this cycle or missed it — Stage 1
starts running on **every** deploy again until it comes back clean three
times in a row, at which point the daily gate resumes.

Check the gate first, from any checkout:

```bash
python <this skill>/scripts/stage1_gate.py --state-file <stage1StateFile> --check --json
```

Read `should_run` from the output:

- **`true`** — run the scan below as usual, then record what it found:
  `python <this skill>/scripts/stage1_gate.py --state-file <stage1StateFile> --record-run --outcome clean` if it found no orphans/modified files, or `--outcome drift` if it did. Recording `drift` (re)activates the enforcement window and resets the clean-streak to 0 — say so to the user briefly ("drift found — Stage 1 will run every deploy until 3 clean scans in a row").  Recording `clean` while enforcement is active advances the streak, and lifts enforcement once it hits 3 (the tool reports which happened — surface it: e.g. "2/3 clean scans, still enforcing" or "3 clean scans reached — back to once-daily").
- **`false`** — skip straight to Stage 4 for this deploy. Say so plainly
  ("already scanned today, skipping Stage 1") so the user isn't left assuming
  it ran.

**If Stage 1 was skipped this cycle and Stage 4 then finds an unexpected
`DIFF HASH`** (see Stage 4), don't just note it for *next* time — treat it as
a live signal that the server is being touched directly right now:

```bash
python <this skill>/scripts/stage1_gate.py --state-file <stage1StateFile> --record-external-drift
```

This activates the enforcement window immediately. Then, as a precaution for
**this same deployment**, go back and run Stage 1's full scan right now
(before proceeding to Stage 5), and record its outcome with `--record-run`
exactly as above — don't wait for the next deploy to actually run the scan
that enforcement is meant to trigger.

The server can diverge from git in two different ways, and CRD's own diffing
only ever catches files git already knows to be dirty — it can't see either of
these on its own:

- **Orphans** — a file exists on the remote that git has never heard of at
  all (a teammate created it straight on the server).
- **Modified** — a file git *does* track, but the remote's copy is a real
  edit — not just a re-save with different line endings or trailing
  whitespace, the kind of noise a plain byte diff would wrongly flag.

```bash
python <this skill>/scripts/scan_remote_vs_local.py --ftp-config <ftpConfig> --working-dir <workingDir> --ref <mirrorBranch> --baseline-ref <deployedTag> --exclude "<pattern>" ...
```

Pass every pattern from the config's `excludePatterns` as a repeated
`--exclude`. Pass `--baseline-ref <deployedTag>` (the singleton tag from Stage
6, e.g. `latest-staging-deployed`) so any conflict this finds already carries
a real, valid 3-way merge base — that's a big improvement over CRD's own
reactive flow, which usually starts with no base at all.

The "modified" comparison reuses **CRD's own normalization** (strip CR, LF,
space, tab, then compare) — the exact rule CRD's own `MATCH GOAL`/`DIFF HASH`
logic uses. A file that only differs by line endings or whitespace hashes
equal and is silently skipped, the same as it would be in CRD's own preflight;
this is what makes the "later modified timestamp" case tractable via plain
FTP instead of needing a tool like Beyond Compare specifically — any FTP
access is enough, because the noise-filtering is CRD's normalization, not a
feature of any particular diff client.

**This stage does a full recursive directory listing of the remote host,**
which is a much chattier conversation than CRD's own preflight (which only
ever touches specific, already-known file paths — it never lists directories
at all). Some hosts handle that fine; others reset the connection partway
through. If this stage fails or times out on a host, don't block the rest of
the pipeline on it — skip straight to Stage 4 (CRD's own preflight has a track
record against hosts this stage might not), and say so; a failed proactive
scan is a missed optimization, not a blocker, since CRD's own reactive
conflict detection in Stage 4/5 still catches anything Stage 1 would have.

Read the report before acting on it. An unexpectedly large orphan list often
means `--ref` is wrong (you're comparing against a stale branch, not
`mirrorBranch`) rather than a genuine pile of new files.

**Pulling (projects with a `mirrorWorktree` — the preferred setup):** run the
pull inside the mirror worktree, with both pull modes on and the pending
deploy range declared:

```bash
cd <mirrorWorktree>
python <this skill>/scripts/scan_remote_vs_local.py --ftp-config <ftpConfig> --working-dir <mirrorWorktree> --ref <mirrorBranch> --baseline-ref <deployedTag> --pull-orphans --pull-modified --deploy-range "<deployedTag>..<deployBranch>" [--exclude ...]
```

- **Orphans** are downloaded into the worktree (no local copy exists to
  conflict with).
- **Modified tracked files** are pull-replaced with the remote's bytes —
  mirror semantics, the remote wins. A mirror checkout has no local work to
  protect by construction (all local development enters through
  `deployBranch`), so no merge and no backup sidecar is needed: the commit
  in Stage 2 is the versioned record, git history is the safety net.
- **Exception, enforced by `--deploy-range`:** any modified path that also
  appears in the pending deploy diff is *not* pull-replaced — it gets a
  CRD-format conflict backup instead, because that file needs Stage 5's
  3-way merge on `deployBranch` (reconciling the teammate's edit with the
  pending local work), and the merged result reaches `mirrorBranch` via
  Stage 8 after deployment. Pull-replacing it here would put both branches'
  versions of the same path in play at once, manufacturing a git conflict
  at Stage 3's fold over an edit Stage 5 is designed to reconcile properly.

The script hard-refuses any pull mode (exit 2, before touching the network)
unless the target working tree is clean AND checked out on `mirrorBranch`
(or a `crd-pull/*` collector branch) — so running it against the wrong
checkout, or on top of someone's in-flight edits, fails loudly instead of
clobbering anything.

**Fallback (no worktree yet):** the old collector-branch flow in the main
working dir still works — `git checkout <mirrorBranch> && git pull && git
checkout -b crd-pull/<project>-<date>`, then the same scan command with
`--working-dir <workingDir>` — but it requires the main dir's tree to be
clean and leaves it off `deployBranch` until Stage 2 finishes, which is
exactly the concurrent-session hazard the worktree exists to remove. Offer
to create the worktree (`git worktree add <path> <mirrorBranch>`, then
record it as `mirrorWorktree` in the config) instead of using the fallback
twice.

**Scanning without pulling** (report-only, from any checkout) also still
works — without `--pull-*` flags the script never writes to the working
dir; modified files just produce conflict backups under
`<CRD_ROOT>/backups/<repo>/...` in CRD's own format, which Stage 5 can act
on regardless of what's checked out. Two caveats when scanning from the
`deployBranch` checkout: the "modified" list will include deploy-ahead noise
(local simply newer than the mirror — classify against the baseline tag
before reading entries as teammate edits), and the backups are inert until
something applies them — don't treat a backup as "handled".

If Stage 1 pulled nothing, there's nothing to commit in Stage 2. If it found
no modified files either, Stage 4's preflight should come back clean.

### Stage 2 — Commit the pulls onto the mirror branch

With a `mirrorWorktree`, Stage 1's pulls (orphans + pull-replaced modified
files) already sit in the worktree as ordinary dirty files on
`mirrorBranch`. Read the diff before committing — an unexpected entry here
is the last cheap moment to catch a bad pull — then commit directly:

```bash
cd <mirrorWorktree>
git status --short
git add -A
git commit -m "chore(crd-pull): record server state - orphans + teammate edits from <today's date>"
```

In the collector-branch fallback, commit on the collector branch the same
way, then fold it in:

```bash
git checkout <mirrorBranch>
git merge --no-ff crd-pull/<project>-<date> -m "merge(crd-pull): fold in server-only files"
git checkout <deployBranch>
```

Either way this updates `mirrorBranch`'s record of "what the server actually
has" — it is **not** where you run the deploy from next (the main working
dir should be on `deployBranch` for Stages 3-6; with a worktree it never
left). Stage 3 now brings this content over to `deployBranch` — before the
deploy, deliberately.

### Stage 3 — Fold the mirror into the deploy branch (split-branch projects only)

Skip when `mirrorBranch == deployBranch`, or when Stages 1/2 pulled nothing
this cycle (nothing to fold).

Everything Stage 2 committed — teammate edits on paths `deployBranch` never
touches, files created straight on the server — exists only on
`mirrorBranch` so far. Fold it into `deployBranch` **now, before the
preflight and deploy**, from the main working dir (already on
`deployBranch`; require a clean tree — if concurrent-session work is in
flight, resolve that first):

```bash
git status --short
git diff --name-only <deployBranch>...<mirrorBranch>   # review what's about to come in
git merge <mirrorBranch> -m "merge(crd-sync): fold server-side changes into <deployBranch>"
```

Mechanically this merge is clean by construction: Stage 2's commits are
restricted to paths outside the pending deploy diff, so only one side ever
changed any given path. **That conflict-freedom is precisely why it needs
eyes.** Git cannot flag *semantic* overlap — a teammate may have built the
same feature this deploy is about to ship, under different filenames (a
parallel take on the same tracker task, possibly a better one). After the
merge, compare what came in against the pending deploy diff (`git diff
--name-only <deployedTag>..HEAD`): same module or directory, similar
basenames, the same tracker keys or feature names in file headers and
commit messages. On any topical overlap, **stop and surface it to the user
before deploying** — folding before the deploy exists exactly so a
superior or duplicate implementation is discovered while there's still time
to adopt or reconcile it, instead of shipping the redundant version and
walking it back afterward.

Two mechanical notes:

- **This fold cannot contaminate the deploy.** CRD decides per file by
  hash, and everything pulled from the server is byte-identical to the
  server, so at deploy time those files classify `MATCH GOAL` and are
  skipped — never uploaded back.
- **Junk stays out of dev history at the review step, not after.** A
  server-only stray (log, scratch file) that slipped past `excludePatterns`
  into the orphan pull should be cleaned up on `mirrorBranch` and added to
  `excludePatterns` before this merge, not imported and reverted later.

If the user explicitly wants to defer the fold for a cycle, that's allowed —
but say plainly that it re-introduces both problems this stage exists to
prevent: `deployBranch` drifting from real server state, and
parallel-work discovery happening only after the deploy.

### Stage 4 — CRD preflight against the remote

Run this from `deployBranch`'s checked-out working tree (confirm you're on it
— `git branch --show-current` — before running).

**`--vsGit` alone only catches *uncommitted* working-tree changes** (it's
literally `git status --porcelain -uall` under the hood) — it does NOT look at
commits already made. For a same-branch project where you edit-then-check
before committing, that's exactly right:

```bash
python <CRD_ROOT>/CheckRemoteDirty.py --workingDir <workingDir> --vsGit <dirtyCheckFile> --ftpConfig <ftpConfig> --exclude "<pattern>" ...
```

Pass every pattern from the project's `excludePatterns` as a repeated `--exclude` here too (same list Stage 1 uses) — this is the flag that actually keeps deploy-exempt paths (tracker-migration audit records, etc.) off the live server; `excludePatterns` alone only reaches Stage 1's separate scan script, not this tool.

But for a **split mirrorBranch/deployBranch project** (work lands as commits
on `deployBranch` well before it's deployed — the normal case for e.g.
widgetco' `claude`), a clean working tree makes this command report nothing
at all even when there's a pile of undeployed commits. Use the range-scoped
form instead, deriving the file list from everything committed since the last
deploy and classifying each against that baseline:

```bash
python <CRD_ROOT>/CheckRemoteDirty.py --workingDir <workingDir> --vsGit <dirtyCheckFile> --ftpConfig <ftpConfig> --gitBaselineHash <deployedTag> --vsGitListHash "<deployedTag>..HEAD" --exclude "<pattern>" ...
```

`--vsGitListHash "A..B"` derives the file list from `git diff --name-only
A..B` (a real range diff, not a single commit's own change-set); reference
content for the FTP comparison still comes from `--gitCommitHash` (defaults to
`HEAD`). `--gitBaselineHash <deployedTag>` classifies any remote file that
still matches the last-deployed commit as `MATCH BASELINE` (safe to overwrite,
no real conflict) instead of a false-positive dirty flag — every
already-committed file the deploy branch changed will otherwise look "dirty"
with no baseline to compare against. **Which form applies is a fact about the
project (whether `mirrorBranch == deployBranch`), not a per-run choice** — use
the range-scoped form whenever they differ.

(If the project has its own `<project>_preflight.ps1`/`.sh`, check what it
actually invokes before assuming it's equivalent — it may only wrap the plain
form.) This is a **read-only safety check**: it reports `MATCH GOAL` / `MATCH
BASELINE` / `DIFF HASH` per file without deploying anything. `DIFF HASH` means
the server has an edit git doesn't know about — a real conflict, and the thing
Stage 5 resolves. If everything is clean, you can skip straight to Stage 6.

**Any `DIFF HASH` here is exactly the "unexpected drift" Stage 1's gate cares
about.** If Stage 1 ran this cycle, its own `--record-run --outcome drift`
call already covers this (do that instead of a second call here). If Stage 1
was *skipped* this cycle (the daily gate said not to run it) and this stage
still turned up a `DIFF HASH`, that's the drift catching you off guard — go
back to Stage 1's gate section above, run `--record-external-drift`, then run
Stage 1's full scan right now for this same deployment before moving on to
Stage 5.

If `mirrorBranch` and `deployBranch` are different branches, expect this to
report the actual pending work on `deployBranch` — not "clean" — since
`mirrorBranch` already mirrors live and `deployBranch` is where new commits
land. A "nothing to deploy" result here when you know there's unpushed work
is the tell that you're accidentally still on `mirrorBranch` (or forgot the
range-scoped flags above).

**The file list can grow between one run and the next** if another session
commits to `deployBranch` in the meantime (no worktree isolation means this
genuinely happens) — re-run this preflight immediately before Stage 6 rather
than trusting an earlier run's count, and re-confirm with the user if the
set of files changed (see Stage 6).

This is also the whole answer to "what's pending deploy on X" — run this stage
alone and report the table back; don't proceed further unless the user asked
for the full deploy.

### Stage 5 — Resolve every conflict via merge-from-crd-backup

For each `DIFF HASH` file, CRD needs a decision (`[k]eep`+download, or
`[l]ist` to download all pending conflicts as backups without touching the
deploy). Once conflict backups exist under `<CRD_ROOT>/backups/<project>/...`,
enumerate and resolve them:

```bash
python <path to merge-from-crd-backup skill>/scripts/resolve_backup.py --scan <project>
```

Then invoke the **merge-from-crd-backup skill** for each pending conflict it
lists — that skill owns the 3-way merge, the safety-net commit, and the report
format; don't reimplement any of it here. Its default is to apply the merge
directly to the working file, which is what you want before deploying (Stage 6
needs the merged file, not the pre-merge one). Stay on `deployBranch` for this
— the merge applies to whatever's currently checked out.

Only move to Stage 6 once every conflict from this scan is resolved (or the
user has explicitly told you to defer specific ones).

**Split-branch projects: commit the resolutions before Stage 6.**
`merge-from-crd-backup` applies its merge to the working file but does not
commit the result (only a pre-merge safety-net commit, if the file was
dirty). Stage 6's split-branch form derives its file list from `git diff
--name-only <deployedTag>..HEAD` — a commit-based range. A merged-but-
uncommitted file that isn't otherwise part of today's committed diff will be
silently absent from that range and never reach the deploy. Commit each
resolved file (`git commit -m "chore(crd-merge): fold in server edits to
<path>" -- <path>`, one file or one logical group at a time, same as the
merge-from-crd-backup skill's own safety-net commits) before moving on. This
doesn't apply to same-branch projects, where the plain `--vsGit` form derives
its file list from `git status` and picks up the uncommitted merge directly.

### Stage 6 — Deploy

Still on `deployBranch`, using the **same mode** (plain or range-scoped) that
Stage 4 needed for this project:

```bash
python <CRD_ROOT>/CheckRemoteDirty.py --workingDir <workingDir> --vsGit <dirtyCheckFile> --ftpConfig <ftpConfig> --exclude "<pattern>" ... --deployOnClean
# split-branch form:
python <CRD_ROOT>/CheckRemoteDirty.py --workingDir <workingDir> --vsGit <dirtyCheckFile> --ftpConfig <ftpConfig> --gitBaselineHash <deployedTag> --vsGitListHash "<deployedTag>..HEAD" --exclude "<pattern>" ... --deployOnClean
```

Use the **same `--exclude` list** here as Stage 4 — the file list must match between the read-only preflight and the real deploy, or the deploy could act on files the preflight never showed the user.

**This is a real, visible, hard-to-reverse action — it overwrites files on a
live server.** Before running it, tell the user exactly what's about to go
out: the branch, the commit, and the file list from Stage 4/5 that changed.
Wait for explicit confirmation. Don't fold this confirmation into a general
"deploy X" go-ahead the user gave at the start of the conversation — the set
of files can change between when they asked and when the deploy actually runs
(that's the whole point of Stages 1-5); if it changed since Stage 4 (e.g.
another session committed in between), show the updated list and confirm
again before proceeding — the earlier confirmation covered the old file set,
not this one.

`--deployOnClean` still prompts an interactive `Proceed with deployment?
(Y/n)` even when everything is clean — with no attached terminal that prompt
hangs / errors on EOF, so pipe the confirmation in (`echo Y | python
CheckRemoteDirty.py ...`). Piping "Y" is answering CRD's own internal
prompt, not a substitute for the user's explicit go-ahead you already
obtained in chat — get that first, same as always.

**CRD can surface up to three separate interactive prompts sharing the same
stdin**, not just the one above: a per-file `[r]eplace/[k]eep/[l]ist` choice
if it hits an unexpected `DIFF HASH`, and a `Proceed anyway? (y/N)` warning if
any file has uncommitted local-vs-git differences, both of which happen
*before* the final deploy confirmation. A single piped `Y` only ever answers
whichever prompt comes first; if a second one is needed, it hits `EOFError`
and the whole run aborts — but this is safe, not a partial deploy: the actual
upload loop only starts *after* the final confirmation succeeds, so any
crash-on-EOF here means zero files were touched. If you hit this, it means
Stage 4's report was stale (something changed between it and the deploy) —
don't retry with more piped input, re-run Stage 4 fresh and re-confirm with
the user instead.

**CRD now supports deletions, but only as an explicit opt-in, separate from
the normal upload flow above** (`CheckRemoteDirty.py`'s deletion-support
design, `docs/deletion-support-proposal.md` in the CRD repo — added
2026-08-02). Both Stage 4 command forms already print any deleted-in-git
paths that are still live on the remote, with no flag needed — that
visibility is unconditional. Actually removing them from the server is a
**separate, explicit action, never bundled into Stage 6's regular deploy**:

```bash
python <CRD_ROOT>/CheckRemoteDirty.py --workingDir <workingDir> --vsGit <dirtyCheckFile> --ftpConfig <ftpConfig> --gitBaselineHash <deployedTag> --vsGitListHash "<deployedTag>..HEAD" --exclude "<pattern>" ... --pruneDeleted
```

This soft-deletes (atomic rename into `.crd-trash/<timestamp>/...` on the
remote, never a hard delete) after its own separate, stricter confirmation
(type back the file count). Treat this the same as Stage 6 itself — tell the
user exactly which paths would be quarantined and get explicit go-ahead
before running it, even though quarantine is designed to be cheap to reverse
(`--restoreFromTrash <path>`) unlike the old all-or-nothing behavior. Don't
run `--pruneDeleted` automatically as part of a routine "deploy X" — it's
opt-in for a reason; only reach for it when the user has actually asked
about the deleted files Stage 4 surfaced, or when doing a dedicated cleanup
pass. The genuinely permanent step, `--purgeTrash --olderThanDays N --yes`,
is further still — a separate command entirely, dry-run by default, and not
something this workflow should ever suggest running as part of a normal
deploy cycle.

### Stage 7 — Move the deployed tag

The commit that just went live is `deployBranch`'s current `HEAD`:

```bash
python <this skill>/scripts/move_deployed_tag.py --repo <workingDir> --commit HEAD --tag <deployedTag>
```

This is deliberately a **singleton, force-moved** tag — exactly one commit is
ever "what's live right now" for this project, which is what lets
`merge-from-crd-backup` resolve a 3-way merge base unambiguously later. The
script refuses to move the tag somewhere that isn't a descendant of where it
already points (guarding against an out-of-order run silently corrupting that
invariant) — if it refuses, stop and ask the user rather than passing
`--force` reflexively.

**Never push this tag to `origin` — not with `--push`, not by any other
route (`git push origin <deployedTag>`, `git push --tags`, etc.).** The
deployed tag is a **local-machine fact, not a shared one**: it records "what
this machine last pushed live via CRD," and `origin` is a passive point
where every contributor's commits land regardless of who deployed what from
where. A dev machine that has never run a CRD deploy has no business owning
an opinion on what's "live," and two machines deploying independently would
fight over the same shared ref for two genuinely different facts. Keep the
move purely local:

```bash
python <this skill>/scripts/move_deployed_tag.py --repo <workingDir> --commit HEAD --tag <deployedTag>
```

If a `<deployedTag>` is ever found already sitting on `origin` (e.g. a
leftover from before this rule was understood), delete it there —
`git push origin --delete <deployedTag>` — and say so; do not treat its
presence on `origin` as sanctioning pushing it again.

### Stage 8 — Fast-forward the mirror branch (split mirrorBranch/deployBranch projects only)

Skip this stage entirely when `mirrorBranch == deployBranch` — the deploy
already happened on that branch and there's nothing to catch up.

When they differ, Stage 6 just deployed `deployBranch`'s HEAD to the live
server, and Stage 7 moved the tag to record that — but neither step touches
`mirrorBranch`'s git ref. Left alone, this means `mirrorBranch` falls one
deploy further behind reality every cycle, and the **next** Stage 1 scan
mistakes everything just deployed for orphans "a teammate created directly on
the server" (they didn't — it's our own history that `mirrorBranch` never
recorded). Close the gap immediately after Stage 7. With a `mirrorWorktree`,
run it **inside the worktree** — the main dir can't (and shouldn't) check
out `mirrorBranch` while the worktree holds it:

```bash
cd <mirrorWorktree>
git merge --ff-only <deployedTag>
```

(No worktree: the old `git checkout <mirrorBranch> && git merge --ff-only
<deployedTag> && git checkout <deployBranch>` in the main dir.)

When the pipeline ran in full, this fast-forward **always succeeds**:
Stage 3 merged `mirrorBranch` into `deployBranch` before the deploy, so
every `mirrorBranch` commit is an ancestor of the deployed commit and the
mirror is strictly behind. A fast-forward failure here therefore means one
of two things:

- **Stage 3's fold was skipped or deferred this cycle** (mirror has this
  run's Stage 2 commits, deployed commit doesn't). Do a real merge instead —
  clean by the disjoint-paths argument, since Stage 2's commits are
  restricted to paths outside the deploy diff:

  ```bash
  git merge <deployedTag> -m "merge(crd-deploy): fold in the just-deployed commit"
  ```

- **Anything else** — commits on `mirrorBranch` that don't trace back to
  this run's Stage 2 (someone committed to it directly, or a previous
  cycle's pulls were never reconciled): a genuine, unexpected divergence.
  Stop and ask the user how to reconcile rather than merging blind. A
  *content conflict* in the fallback merge is the same signal — it means
  the `--deploy-range` exclusion was skipped or wrong somewhere; don't
  force a side, check whether the deployed version already incorporates
  the teammate's edit (via Stage 5) before resolving.

This step is *why* the old "no blanket merge" caution doesn't apply here:
that caution is about not merging `deployBranch`'s unreviewed, not-yet-live
work into `mirrorBranch` preemptively. This is the opposite direction and
timing — advancing `mirrorBranch` to a commit that Stage 6 just finished
proving live, and no further. If a project's convention is to never advance
`mirrorBranch` via git at all (FTP-sync-back only, by deliberate choice),
skip this stage for that project and say so — but flag to the user that
Stage 1's orphan counts will then keep including the project's own past
deploys every time, which is the tradeoff of that choice.

## Finishing comments: flag any pending live DB migrations

CRD only ever moves files over FTP — it never runs SQL against the live
database. If the deploy range (`<deployedTag>..HEAD` on `deployBranch`, the
same range Stage 4/6 already compute) added new files under the project's
migrations directory (commonly `dev/db/migrations/`, but confirm from the
project's own migration runner — e.g. `dev/scripts/migrate.php status` for a
project that has one), those schema changes are now sitting in the deployed
code but have **not** been applied to the remote database. Silently leaving
this unsaid is how a deploy "succeeds" while the live app immediately breaks
against a schema it now expects but doesn't have.

As the last step of every full deploy (Stage 6 onward), before the finishing
summary:

1. List migration files added in the deploy range:
   `git diff --name-status <deployedTag_before_move>..<deployedTag_after_move> -- <migrations dir>`
   (diff against the *pre-deploy* tag position — capture it before Stage 7
   moves the tag — since that's what "already applied" means).
2. **Exclude tracker-only migrations** — files that are audit/replay records
   of writes already made directly to a separate shared tracker DB (not this
   project's own live app DB), the same distinction `tracker-populate` and
   `tracker-readiness-analysis` write out (their header comments say so
   explicitly, e.g. "Audit/replay record of what the write script executed
   ... NOT consumed by dev/scripts/migrate.php" or "targets the REMOTE shared
   tracker DB, NOT this project's own app DB"). These need no action here —
   the tracker DB was already written to directly when the skill ran, and
   `migrate.php`-style runners deliberately skip them.
3. Whatever's left is a real, unapplied app-DB migration. **State this
   plainly and explicitly in the finishing comments** — don't fold it
   silently into "deploy complete" — naming each file and the command to run
   it against the remote (the project's own migration runner if it has one,
   e.g. `php dev/scripts/migrate.php up`, run *against the remote DB's
   credentials*, not local). This tool does not run remote SQL on its own
   (running arbitrary migrations against a live production database without
   an explicit, separate go-ahead is exactly the kind of hard-to-reverse
   action this skill's own deploy-confirmation gate exists for) — surface it
   as an action item for the user, or offer to run it only after they
   explicitly confirm the target database and accept the risk.
4. If none of the deploy range's migrations are real app-DB migrations (empty
   after step 2, or step 1 found nothing), say so briefly and positively
   ("no pending DB migrations") rather than staying silent — silence is
   indistinguishable from "didn't check."

## Mapping user requests to stages

| User says | Run |
| --- | --- |
| "deploy X" / "push X live" | Stage 0 preflight, then Stages 1 → 8 in order, with confirmation gates at 6 and 7 (Stages 3 and 8 auto-skip when mirrorBranch == deployBranch; Stage 1 itself only actually scans when its gate says to — see Stage 1) |
| "pull orphaned files from X" / "check the remote for anything newer before I start" | Stage 0, then Stage 1 only (plus Stage 2's commit if pulls landed) |
| "fold the server's / teammates' changes into claude" | Stage 0, then Stages 1 → 3 (scan, commit on mirror, fold into deploy branch — with the overlap review) |
| "what's pending deploy on X" / "is X safe to push" | Stage 4 only (on `deployBranch`) — report the table, stop; read-only, Stage 0 not required |
| "merge in the server's conflicts for X" | Stage 5 only (assumes Stage 4 already ran / conflicts already exist) |
| "move the deployed tag for X" | Stage 7 only |
| "sync staging up to what's deployed" | Stage 8 only |

If a user asks for the full deploy but a config file doesn't exist yet, walk
them through creating one (see Setup) before Stage 1 — don't guess at FTP
credentials or branch names.

## Packaging this for teammates

CheckRemoteDirty is public and MIT-licensed at
**https://github.com/nizalnm/CheckRemoteDirty** — a teammate's first move
should generally be pulling from there directly (or opening a PR there if
they've improved something), not treating a copied bundle as the source of
truth going forward.

For a one-time drop-in bundle (this skill + `merge-from-crd-backup` + the
non-secret CRD installables), run:

```bash
python scripts/package_teammate_bundle.py --crd-root <CRD_ROOT> --output crd-teammate-bundle.zip
```

It deliberately leaves out every `*_config.json` (FTP credentials),
`*_dirty_check.json`/`*_workflow.json` (this machine's deploy history and repo
paths), and the `backups/`/`remotes/` directories — see the script's own
docstring for the full exclusion list and reasoning. The zip's bundled README
walks a teammate through setting `CRD_ROOT`/`REPOS_ROOT` and filling in their
own config from the `sample_*` templates.
