#!/usr/bin/env python3
"""Three-way merge a CRD backup into the local working file, using a git commit
as the base — the mechanical version of the skill's two-way + git-archaeology.

The base is "what the server was last known to hold" (the last deployed commit).
With it, `git merge-file` resolves add-vs-delete automatically and only flags the
lines BOTH sides changed — no manual "is this absence intentional or just age?".

Base ref resolution order:
  1. --base-ref <commit>            (explicit; e.g. CRD's --gitBaselineHash)
  2. <backup>.meta.json             (CRD conflict sidecar -> baseline.merge_base_ref)
  3. none  -> exit 3 ("no base; fall back to two-way + archaeology")

BASE VALIDITY (why a "clean" merge can silently drop local work):
  A 3-way merge is only valid if `base` is a common ANCESTOR of BOTH sides. The
  supplied base (a deploy tag / --gitBaselineHash) is guaranteed to be an ancestor
  of LOCAL, but NOT of REMOTE. If the server file predates the base, the base is a
  DESCENDANT of remote's true ancestor, so `base -> remote` reads as "remote
  deleted X" for every X the base+local added after the snapshot. git merge-file
  applies those fake one-sided deletions and returns 0 conflicts -> a silent
  regression wearing a "clean" badge. This is the deprecated-file trap.

  So before trusting the base we:
   1. STALE GATE — if the remote byte-matches any historical local blob, it is
      merely an older local version with ZERO independent edits -> keep local,
      do not merge (status: stale_remote, exit 5).
   2. VALIDATE — the base must be an ancestor of local (git merge-base) AND no
      newer than the remote snapshot (remote has no git id, so use its mtime from
      the sidecar). remote_mtime < base_commit_time => base is too new => wrong.
   3. RECOVER — when the base is wrong, re-derive it as the newest local commit
      whose commit-time <= remote_mtime (what local held when the snapshot was
      taken). If none exists, fall back to two-way (exit 3) rather than emit a
      bogus clean.

Usage:
  three_way_merge.py <working_file> <backup_file> [--base-ref REF]
                     [--local-ref REF] [--repo DIR] [--apply] [--json]

Exit codes:
  0  clean merge (no conflicts)         2  conflicts remain (needs human)
  3  no/only-invalid base (fall back)   4  error (base not in commit, etc.)
  5  stale_remote: remote is an older local version; keep local, nothing to merge
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Small tolerance (seconds) when comparing the remote's filesystem mtime against a
# git commit epoch — guards against clock rounding. Both are treated as local-tz
# epochs (CRD and the server sit in the same tz in practice; mtime is parsed with
# time.mktime, i.e. local time), so no large tz margin is needed.
TIME_MARGIN = 120


def run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def repo_root(start: Path) -> Path | None:
    try:
        r = run(["git", "rev-parse", "--show-toplevel"], cwd=str(start))
        return Path(r.stdout.decode().strip())
    except subprocess.CalledProcessError:
        return None


def strip_cr(data: bytes) -> bytes:
    # Merge on a common LF footing so CRLF-vs-LF never fabricates a conflict.
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def read_sidecar(backup: Path):
    meta = Path(str(backup) + ".meta.json")
    if not meta.is_file():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None


def base_ref_from_sidecar(sidecar: dict | None):
    if not sidecar:
        return None
    return (sidecar.get("baseline", {}) or {}).get("merge_base_ref")


def remote_epoch_from_sidecar(sidecar: dict | None):
    """Parse the sidecar's remote_mtime ('YYYY-MM-DD HH:MM:SS') to a local-tz epoch."""
    if not sidecar:
        return None
    s = sidecar.get("remote_mtime")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return int(time.mktime(dt.timetuple()))
        except Exception:
            pass
    return None


def git_show(repo: Path, ref: str, rel: str):
    try:
        r = run(["git", "show", f"{ref}:{rel}"], cwd=str(repo))
        return r.stdout
    except subprocess.CalledProcessError:
        return None


def commit_epoch(repo: Path, ref: str):
    r = run(["git", "show", "-s", "--format=%ct", ref], cwd=str(repo), check=False)
    try:
        return int(r.stdout.decode().strip())
    except Exception:
        return None


def commit_date(repo: Path, ref: str):
    r = run(["git", "show", "-s", "--format=%cs", ref], cwd=str(repo), check=False)
    return (r.stdout.decode().strip() or None)


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    r = run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(repo), check=False)
    return r.returncode == 0


def file_commits(repo: Path, ref: str, rel: str):
    """Commits touching rel on `ref`, newest first."""
    r = run(["git", "rev-list", ref, "--", rel], cwd=str(repo), check=False)
    return r.stdout.decode().split()


def find_exact_history(repo: Path, ref: str, rel: str, remote_norm: bytes):
    """Newest commit on `ref` whose (CR-normalized) blob equals the remote. A match
    proves the remote is just an old local version -> zero teammate edits."""
    for c in file_commits(repo, ref, rel):
        b = git_show(repo, c, rel)
        if b is not None and strip_cr(b) == remote_norm:
            return c
    return None


def pick_base_by_time(repo: Path, ref: str, rel: str, remote_epoch: int | None):
    """Newest commit on `ref` (touching rel) whose commit-time <= remote_epoch —
    i.e. what local held when the server snapshot was taken. That is the best
    available ancestor to use as a 3-way base when the supplied base is too new."""
    if remote_epoch is None:
        return None
    for c in file_commits(repo, ref, rel):  # newest first
        e = commit_epoch(repo, c)
        if e is not None and e <= remote_epoch + TIME_MARGIN:
            return c
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("working_file")
    ap.add_argument("backup_file")
    ap.add_argument("--base-ref", default=None, help="git commit to use as the merge base")
    ap.add_argument("--local-ref", default="HEAD",
                    help="git ref representing 'local' history for stale/base checks (default: HEAD)")
    ap.add_argument("--repo", default=None, help="repo root (default: discover from working_file)")
    ap.add_argument("--apply", action="store_true", help="write the merged result to the working file if clean")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    working = Path(os.path.abspath(args.working_file))
    backup = Path(os.path.abspath(args.backup_file))
    result = {"working_file": str(working), "backup_file": str(backup)}

    repo = Path(os.path.abspath(args.repo)) if args.repo else repo_root(working.parent)
    if not repo:
        return _emit(args, {**result, "status": "error",
                            "detail": "not inside a git repo; pass --repo"}, 4)

    rel = os.path.relpath(str(working), str(repo)).replace("\\", "/")
    result["rel_path"] = rel
    local_ref = args.local_ref

    sidecar = read_sidecar(backup)

    # Resolve the base ref.
    base_ref = args.base_ref or base_ref_from_sidecar(sidecar)
    result["base_ref"] = base_ref
    result["sidecar"] = str(Path(str(backup) + ".meta.json")) if sidecar else None
    if not base_ref:
        return _emit(args, {**result, "status": "no_base",
                            "detail": "no base ref (no --base-ref, no sidecar merge_base_ref); "
                                      "fall back to two-way + archaeology"}, 3)

    if not working.is_file():
        return _emit(args, {**result, "status": "error",
                            "detail": f"working file missing: {working}"}, 4)
    local_bytes = working.read_bytes()
    remote_bytes = backup.read_bytes()
    remote_norm = strip_cr(remote_bytes)

    # --- STALE GATE: is the remote merely an older version of local? -----------
    # If the remote backup byte-matches any historical local blob, it carries no
    # independent edits. Merging it (with any base) can only strip local work.
    stale_commit = find_exact_history(repo, local_ref, rel, remote_norm)
    if stale_commit:
        result["stale_match_commit"] = stale_commit
        result["stale_match_date"] = commit_date(repo, stale_commit)
        return _emit(args, {**result, "status": "stale_remote",
                            "detail": (f"remote == local history @ {stale_commit[:12]} "
                                       f"({result['stale_match_date']}); no teammate edits — "
                                       "keep local, nothing to merge")}, 5)

    # --- VALIDATE the supplied base against BOTH sides -------------------------
    base_epoch = commit_epoch(repo, base_ref)
    remote_epoch = remote_epoch_from_sidecar(sidecar)
    result["remote_mtime"] = (sidecar or {}).get("remote_mtime")
    result["base_date"] = commit_date(repo, base_ref)

    base_anc_local = is_ancestor(repo, base_ref, local_ref)
    base_too_new = (base_epoch is not None and remote_epoch is not None
                    and remote_epoch < base_epoch - TIME_MARGIN)

    if (not base_anc_local) or base_too_new:
        # --- RECOVER: pick the local commit contemporaneous with the snapshot. -
        corrected = pick_base_by_time(repo, local_ref, rel, remote_epoch)
        reason = ("provided base is not an ancestor of local" if not base_anc_local
                  else "provided base postdates the remote snapshot "
                       f"(base {result['base_date']} > remote {result['remote_mtime']})")
        if corrected and corrected != base_ref:
            result["base_ref_original"] = base_ref
            result["base_corrected_to"] = corrected
            result["base_correction_reason"] = reason
            base_ref = corrected
        elif not corrected:
            return _emit(args, {**result, "status": "no_base",
                                "detail": (f"{reason}; and no earlier local commit matches the "
                                           "remote snapshot time — fall back to two-way + archaeology")}, 3)

    # Pull the (possibly corrected) base.
    base_bytes = git_show(repo, base_ref, rel)
    if base_bytes is None:
        return _emit(args, {**result, "status": "error",
                            "detail": f"'{rel}' not found in base commit {base_ref} "
                                      "(file added after the base? fall back to two-way)"}, 4)
    result["base_ref"] = base_ref

    # Detect the working file's line ending so we can restore it on output.
    working_crlf = b"\r\n" in local_bytes

    with tempfile.TemporaryDirectory() as td:
        tp = Path(td)
        (tp / "base").write_bytes(strip_cr(base_bytes))
        (tp / "local").write_bytes(strip_cr(local_bytes))
        (tp / "remote").write_bytes(remote_norm)

        # -p prints the merge to stdout without touching our files; the return
        # code is the number of conflict regions (>0), or <0 on error.
        proc = subprocess.run(
            ["git", "merge-file", "-p",
             "-L", "local (yours)", "-L", f"base ({base_ref[:12]})", "-L", "remote (server)",
             str(tp / "local"), str(tp / "base"), str(tp / "remote")],
            cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        merged = proc.stdout
        conflicts = proc.returncode if proc.returncode >= 0 else -1

    if conflicts < 0:
        return _emit(args, {**result, "status": "error",
                            "detail": f"git merge-file failed: {proc.stderr.decode(errors='replace')}"}, 4)

    result["conflicts"] = conflicts
    if working_crlf:
        merged = merged.replace(b"\n", b"\r\n")

    if conflicts == 0:
        result["status"] = "clean"
        if args.apply:
            working.write_bytes(merged)
            result["applied"] = True
        else:
            # Stash the proposed result next to the backup for inspection.
            out = Path(str(backup) + ".merged")
            out.write_bytes(merged)
            result["proposed_output"] = str(out)
        return _emit(args, result, 0)
    else:
        result["status"] = "conflicts"
        out = Path(str(backup) + ".merged")
        out.write_bytes(merged)
        result["conflict_output"] = str(out)
        result["detail"] = (f"{conflicts} region(s) changed on BOTH sides — the only lines that "
                            "need human judgment. See <<<<<<< markers in the output file.")
        return _emit(args, result, 2)


def _emit(args, result, code):
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print(f"status:   {result['status']}")
        for k in ("rel_path", "base_ref", "base_ref_original", "base_corrected_to",
                  "base_correction_reason", "remote_mtime", "base_date",
                  "stale_match_commit", "stale_match_date", "sidecar", "conflicts",
                  "proposed_output", "conflict_output", "applied", "detail"):
            if k in result and result[k] is not None:
                print(f"{k+':':<10} {result[k]}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
