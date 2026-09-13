#!/usr/bin/env python3
"""Package the CRD "installables" plus this skill and merge-from-crd-backup into
one zip a teammate can drop in — deliberately leaving out anything transient or
secret, since this bundle is meant to be handed to someone else.

Excluded on purpose:
    - every *_config.json (FTP credentials) except sample_ftp_config.json
    - every *_dirty_check.json / *dirty.json (per-project deployed-hash manifests
      — these are this machine's deploy history, meaningless and stale elsewhere)
    - every *_workflow.json except sample_workflow_config.json (same reasoning,
      one level up — these name a specific person's local repo paths)
    - backups/ and remotes/ (other people's saved server conflicts and configs)
    - project-specific preflight/deploy/postdeploy scripts (acmeapp_deploy.ps1
      and friends) that hardcode one person's path and config filename — only
      the generic project_*.bat/.sh templates travel, as the fill-in-the-blanks
      starting point
    - __pycache__ anywhere

CheckRemoteDirty itself is a public, MIT-licensed tool a teammate can also just
clone fresh and update independently — see the bundle's own README for the
upstream repo URL. This script exists for convenience (one drop-in zip with the
skills already wired to it), not because the source can't be pulled directly.

Usage:
    package_teammate_bundle.py --crd-root C:\\www\\CheckRemoteDirty --output crd-bundle.zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import zipfile
from pathlib import Path

CRD_ROOT_FILES = [
    "CheckRemoteDirty.py",
    "diff_normalized.py",
    "README.md",
    "LICENSE",
    "sample_ftp_config.json",
    "project_preflight.bat",
    "project_preflight.sh",
    "project_deploy.bat",
    "project_deploy.sh",
    "project_postdeploy.bat",
    "project_postdeploy.sh",
]

SKILL_DIR_NAMES = ["crd-deploy-workflow", "merge-from-crd-backup"]

BUNDLE_README = """# CRD deploy-workflow bundle

This bundle packages CheckRemoteDirty (CRD) plus two Claude Code skills that
drive it: `crd-deploy-workflow` (the end-to-end pull/merge/preflight/deploy/tag
pipeline) and `merge-from-crd-backup` (the 3-way merge engine the workflow
calls into for every conflict CRD finds).

## Upstream

CheckRemoteDirty is public and MIT-licensed: https://github.com/nizalnm/CheckRemoteDirty
Pull updates from there, or open a PR there, rather than treating this bundle's
copy as the source of truth — it's a convenience snapshot, not a fork.

## Install

1. Unzip `CheckRemoteDirty/` somewhere on disk (it doesn't need to be
   `C:\\www\\CheckRemoteDirty` — that's just where the original author keeps it).
2. Copy `sample_ftp_config.json` to `<yourproject>_config.json` and fill in your
   real FTP host/user/password. **Never commit this file.**
3. Copy `sample_workflow_config.json` (inside `crd-deploy-workflow/assets/`) to
   `<yourproject>_workflow.json` next to it, and fill in your project's working
   directory, branch names, and deployed-tag name. `mirrorBranch` (the branch
   recording "what's on the server") and `deployBranch` (where your commits
   land and deploys come from) are the same branch in simple setups and
   different ones in split setups — see `crd-deploy-workflow/SKILL.md` § Setup,
   including the optional `mirrorWorktree` field for split setups.
4. Set two environment variables so both skills can find things regardless of
   where you unzipped them:
   - `CRD_ROOT` — the CheckRemoteDirty folder from step 1
   - `REPOS_ROOT` — the parent folder your git repos live under
5. Copy the two `skills/*` folders into your Claude Code skills directory
   (typically `~/.claude/skills/`).

## Use

Talk to Claude naturally — "deploy acmeapp", "pull orphaned files from the
widgetco remote", "what's pending deploy on X" — and the `crd-deploy-workflow`
skill picks it up. See that skill's `SKILL.md` for the full step-by-step.

## What changed in this snapshot (2026-07-26) and why

The deploy pipeline grew from six to eight stages after a full live deploy on
a split-branch project exposed real gaps. Every change below was
driven by something that actually went wrong or almost did:

1. **Range-scoped preflight/deploy for split-branch projects.** CRD's plain
   `--vsGit` mode only sees *uncommitted* files (`git status` under the
   hood). On a project where work lands as commits on a dev branch long
   before deploying, a clean tree made preflight report "nothing to deploy"
   despite dozens of pending commits. Stages 4/6 now use
   `--gitBaselineHash <deployedTag> --vsGitListHash "<deployedTag>..HEAD"`
   on split-branch projects, deriving the file list from everything
   committed since the last deploy and classifying each file against that
   baseline.

2. **Stage 8 — post-deploy mirror catch-up.** Deploys advanced the live
   server and the deployed tag but never the mirror branch's git ref, so
   every subsequent Stage 1 scan re-flagged our own past deploys as
   "orphans a teammate created on the server". Measured before/after on a
   split-branch project: 151 false orphans dropped to 10 genuine leftovers
   once the mirror was fast-forwarded. The catch-up is now a standing stage.

3. **Persistent mirror worktree + hard pull guards.** Stage 1's pulls now
   happen in a dedicated git worktree that permanently holds the mirror
   branch (`mirrorWorktree` in the workflow config). It doubles as a
   checkout lock — git refuses to check the mirror branch out anywhere
   else — so a concurrent session can't accidentally land the main working
   dir on the mirror branch and clobber (or get clobbered by) remote pulls.
   The scan script gained `--pull-modified` (mirror semantics: the remote
   wins, the commit is the record — no inert backup sidecars for
   mirror-side files) and now hard-refuses ANY pull mode (exit 2, before
   touching the network) unless the target tree is clean and actually on
   the mirror/collector branch.

4. **Stage 3 — fold the mirror into the deploy branch BEFORE deploying.**
   Git can never flag a teammate who built the same feature under different
   filenames — disjoint paths merge cleanly by construction — so the only
   defense is seeing their work before shipping yours. The fold was first
   designed post-deploy and deliberately moved pre-deploy for exactly this
   reason: it now includes a semantic-overlap review (compare what the fold
   brought in against the pending deploy diff: same module, similar
   basenames, same tracker keys) with an explicit stop-and-ask on overlap,
   so a superior parallel implementation is discovered while there's still
   time to adopt it instead of deploying a duplicate and walking it back.
   The fold cannot contaminate the deploy: pulled files are byte-identical
   to the server, so CRD classifies them MATCH GOAL and skips them.

5. **`--deploy-range` exclusion.** Teammate-edited paths that the pending
   deploy *also* touches are never pull-replaced — they get CRD conflict
   backups and go through the 3-way merge (Stage 5) instead. This keeps the
   two reconciliation mechanisms from fighting: paths only the server
   changed are folded as-is; paths both sides changed are properly merged.

6. **Documented sharp edges found live:** CRD can surface up to three
   interactive prompts on one stdin, so a single piped "Y" aborts safely on
   EOF if a surprise conflict appears (never a partial deploy — the upload
   loop hasn't started yet); CRD cannot delete remote files (check the range
   diff for `D` entries before deploying, remove them manually); and when
   renaming a dirty-check manifest, copy the old file's content forward —
   CRD carries its per-file deploy history, and pointing at a fresh empty
   file silently discards it.
"""


def copy_filtered_tree(src: Path, dst: Path, skip_names: set[str]):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_names]
        rel = Path(root).relative_to(src)
        for fn in files:
            if fn in skip_names or fn.endswith(".pyc"):
                continue
            out_dir = dst / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / fn, out_dir / fn)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--crd-root", default=os.environ.get("CRD_ROOT", r"C:\www\CheckRemoteDirty"))
    p.add_argument("--skills-dir", default=str(Path.home() / ".claude" / "skills"))
    p.add_argument("--output", default="crd-teammate-bundle.zip")
    args = p.parse_args(argv)

    crd_root = Path(args.crd_root)
    skills_dir = Path(args.skills_dir)
    staging = Path(args.output).resolve().parent / (Path(args.output).stem + "_staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    crd_out = staging / "CheckRemoteDirty"
    crd_out.mkdir()
    missing = []
    for name in CRD_ROOT_FILES:
        src = crd_root / name
        if src.is_file():
            shutil.copy2(src, crd_out / name)
        else:
            missing.append(name)

    skills_out = staging / "skills"
    skills_out.mkdir()
    for name in SKILL_DIR_NAMES:
        src = skills_dir / name
        if not src.is_dir():
            missing.append(f"skills/{name}")
            continue
        copy_filtered_tree(src, skills_out / name, skip_names={"__pycache__"})

    (staging / "README.md").write_text(BUNDLE_README, encoding="utf-8")

    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in staging.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(staging))

    shutil.rmtree(staging)

    print(f"Wrote {args.output}")
    if missing:
        print("Not found, skipped:")
        for m in missing:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
