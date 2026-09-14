#!/usr/bin/env python3
"""Walk an entire remote FTP tree and reconcile it against local git, surfacing
two different things CRD's own dirty-file-scoped diffing can't see on its own:

  1. ORPHANS   — files that exist on the remote but that git has never heard of
                 at all (a teammate created them straight on the server). These
                 get pulled straight into the working dir; there's no local copy
                 to conflict with.
  2. MODIFIED  — files git DOES track, where the remote's content is a genuine
                 edit, not just a re-save with different line endings or
                 trailing whitespace. These are NEVER written straight to the
                 working file — that would risk silently clobbering local work,
                 exactly the failure mode merge-from-crd-backup exists to
                 prevent. Instead this writes a real CRD-format conflict backup
                 (`<file>.<remote_mtime>.conflict_bk` + `.meta.json` sidecar) in
                 the exact layout/schema CRD itself produces, so
                 merge-from-crd-backup's tooling (resolve_backup.py,
                 three_way_merge.py) picks them up identically to a conflict
                 CRD found during an actual deploy attempt — nothing downstream
                 needs to know these were found proactively instead.

"Genuine edit" uses CRD's own whitespace-agnostic comparison (strip \r, \n,
space, tab, then compare) — the same normalization CRD's own DIFF HASH /
MATCH GOAL logic uses. A file re-saved by an editor with different line
endings or re-indented but semantically identical hashes equal and is skipped,
same as it would be in CRD's own preflight. This mirrors what a Beyond Compare
-style content-aware diff gives you, without depending on that specific tool —
any FTP access works.

On top of that, an optional persistent exclude list filters out server-only
housekeeping paths (logs, cache, Thumbs.db, ...) that were never meant to be
version-controlled, on top of whatever .gitignore already covers — handy when
.gitignore doesn't happen to list something the remote host generates.

CONNECTION RESILIENCE: unlike CRD's own preflight (which only ever touches
specific, already-known file paths — it never lists a directory at all), this
script does a full recursive directory listing, which is a much chattier
conversation. Some hosts reset the control connection partway through,
especially over the old NLST-plus-per-entry-cwd-probe path this script used to
fall back to when a server doesn't support MLSD (two extra round trips per
single file/directory, on one long-lived connection). To cope: directory
listing prefers MLSD, then a single-round-trip Unix `LIST` parse, and only
falls back to the NLST+cwd-probe path as a last resort; the walk itself is
iterative (not recursive) so a reconnect can resume from wherever the stack
left off instead of losing all progress; and every FTP operation is retried
against a fresh reconnect a couple of times before giving up.

Usage:
    scan_remote_vs_local.py --ftp-config acmeapp_config.json --working-dir C:\\www\\acmeapp --ref staging
    scan_remote_vs_local.py --ftp-config acmeapp_config.json --working-dir C:\\www\\acmeapp --ref staging --pull-orphans
    scan_remote_vs_local.py --ftp-config acmeapp_config.json --working-dir C:\\www\\acmeapp --ref staging --baseline-ref latest-staging-deployed
    scan_remote_vs_local.py --ftp-config acmeapp_config.json --working-dir C:\\www\\acmeapp --ref staging --exclude "cache/*" --exclude "*.log" --exclude "Thumbs.db"

Options:
    --ftp-config PATH   CRD-style FTP config JSON (host/user/password/port/remote_root)
    --working-dir PATH  local git repo root to compare against
    --ref REF            git ref "tracked" is measured against (default: HEAD —
                          check out the mirror branch first, e.g. staging)
    --crd-root PATH      CRD install dir, for writing conflict backups in CRD's own
                          layout (default: $CRD_ROOT or C:\\www\\CheckRemoteDirty)
    --repo-name NAME      backups/<repo-name>/... folder name (default: basename of
                          --working-dir, matching CRD's and resolve_backup.py's convention)
    --baseline-ref REF    commit to record as the conflict sidecar's merge_base_ref —
                          pass the project's deployed tag (e.g. latest-staging-deployed)
                          so merge-from-crd-backup's three_way_merge.py has a real base
                          immediately instead of falling back to two-way archaeology
    --exclude PATTERN     glob pattern to skip entirely (repeatable); matched against
                          both the full relative path and the basename
    --pull-orphans        download orphan files into --working-dir (default: report only)
    --mtime-cutoff VALUE  raw YYYYMMDDHHMMSS (UTC) watermark from stage1_gate.py's
                          mtimeWatermark; tracked files whose remote mtime predates it
                          skip the RETR+compare entirely (assumed unchanged since the
                          last clean run) — see walk_remote()'s docstring for the mtime
                          source and main()'s watermark comment for the safety argument
    --retries N           reconnect-and-retry attempts per FTP operation before giving
                          up (default: 2)
    --json
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from ftplib import FTP_TLS, error_perm, error_temp
from pathlib import Path

RETRYABLE_EXC = (ConnectionResetError, EOFError, OSError, error_temp)

# Unix `ls -l`-style LIST line, e.g.:
#   drwxr-xr-x   2 user group     4096 Jan 01 00:00 dirname
#   -rw-r--r--   1 user group    12345 Jan 01 00:00 filename
_LIST_RE = re.compile(
    r"^(?P<type>[dl\-])\S*\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+\S+\s+\S+\s+\S+\s+(?P<name>.+?)\s*$"
)


def load_ftp_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def connect_ftp(config: dict, timeout: float = 30.0) -> FTP_TLS:
    # Without an explicit timeout, a socket read on a stalled/idle connection
    # blocks forever -- no exception, no reconnect, no progress. That's a real
    # failure mode on shared hosting under load, and it's indistinguishable
    # from the scan still working. A finite timeout turns that hang into a
    # socket.timeout (an OSError subclass, already in RETRYABLE_EXC), so
    # FtpSession._retry's existing reconnect-and-retry logic actually gets a
    # chance to run instead of the whole process just sitting there.
    ftp = FTP_TLS(timeout=timeout)
    ftp.connect(config["host"], config.get("port", 21))
    ftp.login(config["user"], config["password"])
    ftp.prot_p()
    return ftp


class FtpSession:
    """FTP connection wrapper with automatic reconnect-and-retry.

    A full-tree walk is far chattier than CRD's own targeted file checks, and
    some hosts reset the control connection partway through. Every accessor
    here retries its operation against a freshly reconnected session (up to
    `retries` times, with a short backoff) rather than letting one reset kill
    the whole scan."""

    def __init__(self, config: dict, retries: int = 2, backoff: float = 1.5, timeout: float = 30.0):
        self.config = config
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.ftp: FTP_TLS | None = None
        self.reconnects = 0
        self.reconnect()

    def reconnect(self):
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:
                pass
            self.reconnects += 1
        self.ftp = connect_ftp(self.config, timeout=self.timeout)

    def _retry(self, op):
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                return op()
            except RETRYABLE_EXC as exc:
                last_exc = exc
                if attempt >= self.retries:
                    raise
                time.sleep(self.backoff * (attempt + 1))
                self.reconnect()
        raise last_exc  # pragma: no cover

    def mlsd(self, path: str):
        return self._retry(lambda: list(self.ftp.mlsd(path)))

    def list_dir(self, path: str):
        """Unix-style LIST, one round trip per directory: [(name, is_dir, size), ...]."""
        def op():
            lines: list[str] = []
            self.ftp.dir(path, lines.append)
            return lines
        return [e for e in (_parse_list_line(l) for l in self._retry(op)) if e]

    def nlst(self, path: str):
        return self._retry(lambda: self.ftp.nlst(path))

    def cwd(self, path: str):
        return self._retry(lambda: self.ftp.cwd(path))

    def size(self, path: str):
        return self._retry(lambda: self.ftp.size(path))

    def voidcmd(self, cmd: str):
        return self._retry(lambda: self.ftp.voidcmd(cmd))

    def retrbinary_to_bytes(self, remote_path: str) -> bytes:
        def op():
            buf = bytearray()
            self.ftp.retrbinary(f"RETR {remote_path}", buf.extend)
            return bytes(buf)
        return self._retry(op)

    def quit(self):
        try:
            self.ftp.quit()
        except Exception:
            pass


def _parse_list_line(line: str):
    line = line.rstrip("\r\n")
    if not line or line.lower().startswith("total "):
        return None
    m = _LIST_RE.match(line)
    if not m:
        return None
    name = m.group("name")
    if name in (".", ".."):
        return None
    if m.group("type") == "l" and " -> " in name:
        name = name.split(" -> ", 1)[0]  # symlink: keep the link's own name, treat as a file
    is_dir = m.group("type") == "d"
    return name, is_dir, int(m.group("size"))


def _normalize_mtime(raw: str | None) -> str | None:
    """Normalize an MLSD "modify" fact to a plain 14-digit YYYYMMDDHHMMSS
    string (UTC, no separators, fractional seconds dropped) so it can be
    compared lexicographically — directly against itself, against
    get_remote_mtime()'s MDTM-derived value once dashes/colons are stripped,
    and against --mtime-cutoff / the stored watermark, all of which use this
    same plain format. None in, None out: a missing/malformed fact just means
    this file never qualifies for the --mtime-cutoff skip in main()."""
    if not raw:
        return None
    digits = raw.split(".", 1)[0]
    return digits if len(digits) == 14 and digits.isdigit() else None


def crd_normalize(data: bytes) -> bytes:
    """CRD's own whitespace-agnostic normalization: strip CR, LF, space, tab
    entirely before hashing/comparing. Kept identical to
    CheckRemoteDirty.py's calculate_file_hash_and_size() so a file this script
    calls a genuine conflict is exactly the set CRD's own preflight would also
    flag as DIFF HASH — no separate notion of "meaningfully different"."""
    return data.replace(b"\r", b"").replace(b"\n", b"").replace(b" ", b"").replace(b"\t", b"")


def crd_md5(data: bytes) -> str:
    return hashlib.md5(crd_normalize(data)).hexdigest()


def should_prune_dir(rel: str, patterns: list[str]) -> bool:
    """Should the walk skip descending into this directory entirely?

    True either because the directory's own rel path matches an exclude
    pattern directly (a literal name like "vendor", or a glob that happens to
    match the bare directory path), or because a "dir/*"-style pattern would
    exclude every conceivable child anyway (probed with a synthetic child
    name) — in which case there is no point paying for the listing at all.
    Pure prefix-of-the-final-filter logic, not a new exclusion rule: anything
    this prunes was always going to end up filtered out of the result by
    is_excluded() later, just after the (expensive) full recursive walk.
    """
    if is_excluded(rel, patterns):
        return True
    return is_excluded(f"{rel}/__prune_probe__", patterns)


def on_path_to_any_leaf(rel: str, touched: set[str]) -> bool:
    """True if `rel` is worth descending into when walk_remote() is given
    include_roots — plain membership in `touched`, nothing more clever than
    that. `touched` here is meant to be the FULL touched-path set from
    action=dump (every directory whose bubbled_max_mtime exceeds the
    watermark - by the bubbling invariant that's every ancestor of every
    real change, all the way up to root, AND every intermediate directory
    down to the exact changed leaf - not just the deepest touched paths).

    That distinction matters and was the source of a real bug: reducing to
    "deepest touched leaves only" and checking is-this-an-ancestor-or-
    descendant-of-a-leaf seems equivalent but isn't - once inside a leaf's
    own subtree, EVERY path trivially satisfies "descendant of", which
    silently walks that leaf's own untouched children too (e.g. a touched
    xs/plugins/noty's own stale lib/docs/demo/test/.github/src
    subdirectories all got walked, even though action=dump's response
    already had rows for each of them proving they were NOT touched).
    Plain set membership against the full touched set doesn't have this
    problem: xs/plugins/noty/lib simply never being IN that set is enough to
    prune it, at any depth, with the exact same precision --exclude gets
    from walking the local git tree — no distinction between "on the way
    down" and "already arrived" needs to be made at all."""
    return rel in touched


def walk_remote(session: FtpSession, remote_root: str,
                 exclude: list[str] | None = None,
                 include_roots: list[str] | None = None) -> tuple[list[tuple[str, int, str | None]], dict]:
    """Return ([(rel_path, size, mtime), ...], stats) for every file under remote_root.

    `mtime` is the MLSD "modify" fact (raw YYYYMMDDHHMMSS, UTC, no separators)
    when the listing came from MLSD — the common case — or None when it came
    from the LIST/NLST fallback paths, which don't carry a reliable mtime
    without an extra per-file MDTM round trip this walk doesn't spend. A file
    with mtime None just never qualifies for the --mtime-cutoff skip in
    main() and gets fully checked, same as before this field existed.

    Iterative (stack-based), not recursive: if a directory listing needs a
    reconnect partway through, the walk resumes from wherever the stack left
    off instead of losing everything gathered so far.

    Directories matching `exclude` are never pushed onto the stack in the
    first place — no listing, no reconnect exposure, no files collected only
    to be discarded a moment later by the post-walk is_excluded() filter.
    This matters: on a host with a large vendored/composer/node_modules tree,
    that subtree can be the majority of the whole remote file count, and
    every directory inside it is one more round trip (and one more chance to
    need a reconnect) for content nobody expects to find hand-edited on a
    live server anyway.

    `include_roots`, when given, still starts the walk at remote_root and
    still lists every ancestor directory along the way — that matters, it's
    what catches an orphan/stray file sitting loose in remote_root itself or
    in any other ancestor, the one thing a naive "jump straight to the known
    paths" version would miss. What changes is the descend decision: a
    child directory is only pushed onto the stack if it's itself a member of
    include_roots (see on_path_to_any_leaf() - plain set membership, not a
    prefix/ancestor check). This MUST be the full touched-path set from
    action=dump (every ancestor down to each real change, not just the
    deepest "leaf" paths) — reducing to leaves and substituting an ancestor-
    or-descendant-of check was tried and is wrong: it can't tell a touched
    leaf's own untouched children apart from a real change, since every
    path below a leaf trivially "descends from" it. Passing the full set
    costs nothing extra (it's still small - proportional to how deep the
    actual changes are, not to how many stale siblings exist) and gets the
    exact same per-level precision --exclude has. Every other sibling is
    skipped exactly like an `exclude`-matched one is, just without needing
    to name it. `exclude` still applies on top when both are given, e.g. for
    permanent never-relevant paths."""
    files: list[tuple[str, int, str | None]] = []
    stats = {"mlsd_dirs": 0, "list_dirs": 0, "nlst_dirs": 0, "pruned_dirs": 0}
    pruned_paths: list[str] = []
    patterns = exclude or []
    leaves = set(include_roots) if include_roots else set()
    root = remote_root.rstrip("/") or "/"
    stack: list[tuple[str, str]] = [(root, "")]
    dirs_visited = 0
    progress_start = time.monotonic()
    last_progress = progress_start

    def maybe_descend(full: str, rel: str) -> None:
        if patterns and should_prune_dir(rel, patterns):
            stats["pruned_dirs"] += 1
            if len(pruned_paths) < 20:
                pruned_paths.append(rel)
            return
        if leaves and not on_path_to_any_leaf(rel, leaves):
            stats["pruned_dirs"] += 1
            if len(pruned_paths) < 20:
                pruned_paths.append(rel)
            return
        stack.append((full, rel))

    while stack:
        remote_dir, rel_prefix = stack.pop()
        dirs_visited += 1

        # Silence during the walk is exactly what makes a stalled connection
        # indistinguishable from one still working -- surface a heartbeat
        # every few seconds so a background run's output file shows real
        # progress instead of staying empty until the very end.
        now = time.monotonic()
        if now - last_progress >= 5.0:
            print(f"[{now - progress_start:6.0f}s] walked {dirs_visited} dir(s), "
                  f"found {len(files)} file(s), {stats['pruned_dirs']} dir(s) pruned, "
                  f"{len(stack)} dir(s) queued, "
                  f"{session.reconnects} reconnect(s) so far -- at {rel_prefix or '/'}",
                  file=sys.stderr, flush=True)
            last_progress = now

        try:
            entries = session.mlsd(remote_dir)
        except Exception:
            entries = None

        if entries is not None:
            stats["mlsd_dirs"] += 1
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                rel = f"{rel_prefix}{name}" if not rel_prefix else f"{rel_prefix}/{name}"
                full = f"{remote_dir}/{name}"
                kind = facts.get("type")
                if kind == "dir":
                    maybe_descend(full, rel)
                elif kind == "file":
                    files.append((rel, int(facts.get("size", 0) or 0), _normalize_mtime(facts.get("modify"))))
            continue

        try:
            listed = session.list_dir(remote_dir)
        except Exception:
            listed = None

        if listed is not None:
            stats["list_dirs"] += 1
            for name, is_dir, size in listed:
                rel = f"{rel_prefix}{name}" if not rel_prefix else f"{rel_prefix}/{name}"
                full = f"{remote_dir}/{name}"
                if is_dir:
                    maybe_descend(full, rel)
                else:
                    files.append((rel, size, None))
            continue

        # Last-resort fallback: NLST + a cwd probe per entry to tell files from
        # directories. Two extra round trips per entry on top of the NLST
        # itself — only reached when a host supports neither MLSD nor a
        # parseable LIST, since it's the most likely path to exhaust a host
        # that's already fragile enough to need this fallback.
        stats["nlst_dirs"] += 1
        names = session.nlst(remote_dir)
        for full in names:
            name = full.rsplit("/", 1)[-1]
            if name in (".", ".."):
                continue
            rel = f"{rel_prefix}{name}" if not rel_prefix else f"{rel_prefix}/{name}"
            if patterns and should_prune_dir(rel, patterns):
                stats["pruned_dirs"] += 1
                if len(pruned_paths) < 20:
                    pruned_paths.append(rel)
                continue
            if leaves and not on_path_to_any_leaf(rel, leaves):
                stats["pruned_dirs"] += 1
                if len(pruned_paths) < 20:
                    pruned_paths.append(rel)
                continue
            try:
                session.cwd(full)
                session.cwd("..")
                stack.append((full, rel))
            except error_perm:
                try:
                    size = session.size(full) or 0
                except Exception:
                    size = 0
                files.append((rel, size, None))

    stats["pruned_dir_samples"] = pruned_paths
    return files, stats


def get_remote_mtime(session: FtpSession, remote_path: str) -> str | None:
    """Mirrors CheckRemoteDirty.py's own MDTM parsing exactly: MDTM's raw
    YYYYMMDDHHMMSS response, reformatted with dashes/colons, no timezone
    conversion (CRD and the server are assumed to share a clock, same as the
    rest of this workflow already assumes)."""
    try:
        resp = session.voidcmd(f"MDTM {remote_path}")
    except Exception:
        return None
    if not resp.startswith("213"):
        return None
    raw = resp.split()[1].split(".")[0]
    if len(raw) >= 14:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    return raw


def git_tracked_set(working_dir: str, ref: str) -> set[str]:
    r = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return {line.replace("\\", "/") for line in r.stdout.decode("utf-8", errors="replace").splitlines()}


def git_ignored_set(working_dir: str, rel_paths: list[str]) -> set[str]:
    if not rel_paths:
        return set()
    r = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=working_dir,
        input="\n".join(rel_paths).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return {line.replace("\\", "/") for line in r.stdout.decode("utf-8", errors="replace").splitlines()}


def git_show(working_dir: str, ref: str, rel: str) -> bytes | None:
    r = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return r.stdout if r.returncode == 0 else None


def git_range_paths(working_dir: str, range_spec: str) -> set[str]:
    """Paths changed across a range (git diff --name-only A..B) — the pending
    deploy diff. Modified files on these paths are deploy-collision territory
    (CRD preflight / Stage 5's 3-way merge own them) and must never be
    pull-replaced on the mirror, or the post-deploy mirror catch-up merge
    manufactures a conflict over an edit Stage 5 already reconciled."""
    r = subprocess.run(
        ["git", "diff", "--name-only", range_spec],
        cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return {line.replace("\\", "/") for line in r.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()}


def guard_pull_checkout(working_dir: str, ref: str) -> str | None:
    """Refuse any pull mode unless the working tree is safe to write into:
    clean (no dirty/untracked files that a pull could clobber or entangle),
    and actually on the mirror branch — either `ref` itself (the persistent
    mirror-worktree case) or a crd-pull/* collector branch cut from it. This
    is what stops a pull from silently landing teammate content on the deploy
    branch when someone runs the script from the wrong checkout.

    Returns an error string to refuse with, or None when safe."""
    r = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=working_dir,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    dirty = [line for line in r.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if dirty:
        return (f"working tree at {working_dir} has {len(dirty)} dirty/untracked file(s) - "
                "refusing to pull on top of them (commit or stash first)")
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=working_dir,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    branch = r.stdout.decode().strip()
    ref_leaf = ref.rsplit("/", 1)[-1]
    if branch != ref and branch != ref_leaf and not branch.startswith("crd-pull/"):
        return (f"checked-out branch is '{branch}', not '{ref}' or a crd-pull/* collector "
                "branch - refusing to pull remote content onto a non-mirror checkout "
                "(run from the mirror worktree, or check out the mirror branch first)")
    return None


def is_excluded(rel: str, patterns: list[str]) -> bool:
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        pat_norm = pat.rstrip("/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat_norm):
            return True
        if rel == pat_norm or rel.startswith(pat_norm + "/"):
            return True
        if fnmatch.fnmatch(base, pat):
            return True
    return False


def write_conflict_backup(crd_root: Path, repo_name: str, rel: str, remote_bytes: bytes,
                           remote_mtime: str | None, local_hash: str, git_hash: str,
                           remote_hash: str, baseline_ref: str | None) -> dict:
    backup_dir = crd_root / "backups" / repo_name / os.path.dirname(rel)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts_suffix = (
        remote_mtime.replace(":", "").replace(" ", "_").replace("-", "")
        if remote_mtime else datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    )
    basename = os.path.basename(rel)
    backup_path = backup_dir / f"{basename}.{ts_suffix}.conflict_bk"
    meta_path = Path(str(backup_path) + ".meta.json")

    if not backup_path.exists():
        backup_path.write_bytes(remote_bytes)

    meta = {
        "schema": "crd-conflict-meta/1",
        "rel_path": rel,
        "conflict_backup": backup_path.name,
        "detected_at": datetime.datetime.now().isoformat(),
        "remote_mtime": remote_mtime,
        "hashes": {"local": local_hash, "git": git_hash, "remote": remote_hash, "goal": local_hash},
        "baseline": {
            "merge_base_ref": baseline_ref,
            "git_baseline_ref": baseline_ref,
            "last_deploy_commit": None,
            "last_deploy_hash": None,
        },
        "merge_hint": (
            f"3-way: git show {baseline_ref or '<baseline>'}:{rel} > base; "
            "git merge-file <local> base <conflict_bk>. Falls back to 2-way if merge_base_ref is null."
        ),
    }
    if not meta_path.exists():
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {"backup": str(backup_path), "sidecar": str(meta_path)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ftp-config", required=True)
    p.add_argument("--working-dir", required=True)
    p.add_argument("--ref", default="HEAD")
    p.add_argument("--crd-root", default=os.environ.get("CRD_ROOT", r"C:\www\CheckRemoteDirty"))
    p.add_argument("--repo-name", default=None)
    p.add_argument("--baseline-ref", default=None)
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--include-root", action="append", default=[],
                   help="restrict descent to these git-relative paths only (repeatable) - MUST "
                        "be the FULL touched-path set (every ancestor down to each real change, "
                        "e.g. remote_manifest_precheck.py's action=dump 'touched' paths), not "
                        "just the deepest ones - see walk_remote()'s docstring for why leaves-only "
                        "is wrong. remote_root and every ancestor on the way down are still "
                        "listed (so loose files/orphans at any level are still found); a "
                        "directory is only descended into if it's itself in this set. Compact "
                        "alternative to enumerating --exclude patterns for the same effect")
    p.add_argument("--pull-orphans", action="store_true")
    p.add_argument("--pull-modified", action="store_true",
                   help="mirror-worktree mode: overwrite the working copy of modified tracked "
                        "files with the remote's bytes (remote wins - a mirror has no local work "
                        "to protect), instead of writing conflict backups. Paths inside "
                        "--deploy-range still get backups, never overwrites.")
    p.add_argument("--deploy-range", default=None,
                   help="git range (e.g. latest-staging-deployed..claude) whose changed paths "
                        "are deploy-collision territory: exempt from --pull-modified, always "
                        "handled via conflict backup + the merge-from-crd-backup skill instead")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--timeout", type=float, default=30.0,
                   help="per-socket-operation timeout in seconds before a stalled read/write "
                        "counts as retryable (default: 30)")
    p.add_argument("--mtime-cutoff", default=None,
                   help="raw YYYYMMDDHHMMSS (UTC) watermark from a prior clean run (see "
                        "stage1_gate.py's mtimeWatermark). A tracked file whose MLSD 'modify' "
                        "fact is present and older than this is assumed unchanged since it was "
                        "last verified and skipped entirely - no RETR, no compare, counted "
                        "straight into matched_count. Files with no mtime fact (LIST/NLST "
                        "fallback, or the fact simply wasn't returned) are always fully checked, "
                        "same as before this flag existed - it only ever narrows the set of "
                        "content actually fetched, never the set of files listed.")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    scan_started_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    config = load_ftp_config(args.ftp_config)
    remote_root = config.get("remote_root", "/")
    working_dir = os.path.abspath(args.working_dir)
    crd_root = Path(args.crd_root)
    repo_name = args.repo_name or Path(working_dir).name

    if args.pull_orphans or args.pull_modified:
        refusal = guard_pull_checkout(working_dir, args.ref)
        if refusal:
            print(f"REFUSED: {refusal}", file=sys.stderr)
            return 2

    deploy_range_paths: set[str] = set()
    if args.deploy_range:
        deploy_range_paths = git_range_paths(working_dir, args.deploy_range)

    baseline_ref_resolved = None
    if args.baseline_ref:
        r = subprocess.run(["git", "rev-parse", args.baseline_ref], cwd=working_dir,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        baseline_ref_resolved = r.stdout.decode().strip() if r.returncode == 0 else args.baseline_ref

    session = FtpSession(config, retries=args.retries, timeout=args.timeout)
    try:
        remote_files, walk_stats = walk_remote(session, remote_root, exclude=args.exclude,
                                                include_roots=args.include_root)
        print(f"walk complete: {len(remote_files)} remote file(s), "
              f"{session.reconnects} reconnect(s)", file=sys.stderr, flush=True)
        remote_files = [(rel, size, mtime) for rel, size, mtime in remote_files
                        if not is_excluded(rel, args.exclude)]
        tracked = git_tracked_set(working_dir, args.ref)

        untracked = [rel for rel, _size, _mtime in remote_files if rel not in tracked]
        ignored = git_ignored_set(working_dir, untracked)
        orphans = [rel for rel in untracked if rel not in ignored]

        pulled = []
        if args.pull_orphans:
            for i, rel in enumerate(orphans, 1):
                if i == 1 or i % 20 == 0 or i == len(orphans):
                    print(f"pulling orphan {i}/{len(orphans)}", file=sys.stderr, flush=True)
                remote_path = f"{remote_root.rstrip('/')}/{rel}"
                local_path = Path(working_dir) / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(session.retrbinary_to_bytes(remote_path))
                pulled.append(rel)

        modified = []
        pulled_modified = []
        missing_locally = []
        matched = 0
        skipped_via_mtime = 0
        tracked_remote = [(rel, size, mtime) for rel, size, mtime in remote_files if rel in tracked]
        for i, (rel, _size, mtime) in enumerate(tracked_remote, 1):
            if i == 1 or i % 20 == 0 or i == len(tracked_remote):
                print(f"comparing tracked file {i}/{len(tracked_remote)}", file=sys.stderr, flush=True)
            local_path = Path(working_dir) / rel
            if not local_path.is_file():
                missing_locally.append(rel)
                continue

            if args.mtime_cutoff and mtime and mtime < args.mtime_cutoff:
                # Unchanged on the remote since the last time a clean run
                # verified it (see stage1_gate.py's mtimeWatermark) - assume
                # it still matches without spending a RETR to re-prove it.
                matched += 1
                skipped_via_mtime += 1
                continue

            remote_path = f"{remote_root.rstrip('/')}/{rel}"
            remote_bytes = session.retrbinary_to_bytes(remote_path)
            local_bytes = local_path.read_bytes()

            if crd_normalize(remote_bytes) == crd_normalize(local_bytes):
                matched += 1
                continue

            remote_mtime = get_remote_mtime(session, remote_path)

            # Mirror semantics: on the mirror checkout (guarded above), the
            # remote is the truth for any path the pending deploy doesn't
            # touch — replace and let the caller's commit record it. Git
            # history is the safety net here; no backup sidecar needed.
            if args.pull_modified and rel not in deploy_range_paths:
                local_path.write_bytes(remote_bytes)
                pulled_modified.append({"rel_path": rel, "remote_mtime": remote_mtime})
                continue

            git_bytes = git_show(working_dir, args.ref, rel)
            local_hash = crd_md5(local_bytes)
            remote_hash = crd_md5(remote_bytes)
            git_hash = crd_md5(git_bytes) if git_bytes is not None else "N/A"

            written = write_conflict_backup(
                crd_root, repo_name, rel, remote_bytes, remote_mtime,
                local_hash, git_hash, remote_hash, baseline_ref_resolved,
            )
            modified.append({
                "rel_path": rel, "remote_mtime": remote_mtime,
                "in_deploy_range": rel in deploy_range_paths, **written,
            })
    finally:
        session.quit()

    # Safe to advance the watermark only when this run leaves nothing
    # unresolved: any still-divergent file (a conflict backup was written,
    # or a tracked path was missing locally) means the tree isn't fully
    # accounted for, so next run must still check it rather than trust a new
    # cutoff that would skip right past it. scan_started_at (captured before
    # the walk) is used rather than "now" so a file touched partway through
    # this run - which may or may not have been caught depending on when the
    # walk reached it - is never treated as covered by this run's watermark.
    safe_to_advance_watermark = not modified and not missing_locally
    suggested_new_watermark = scan_started_at if safe_to_advance_watermark else None

    result = {
        "remote_root": remote_root,
        "ref": args.ref,
        "baseline_ref": baseline_ref_resolved,
        "remote_file_count": len(remote_files),
        "tracked_count": len(tracked),
        "matched_count": matched,
        "skipped_via_mtime_count": skipped_via_mtime,
        "mtime_cutoff_used": args.mtime_cutoff,
        "scan_started_at": scan_started_at,
        "suggested_new_watermark": suggested_new_watermark,
        "orphans": orphans,
        "pulled": pulled,
        "pulled_modified": pulled_modified,
        "modified": modified,
        "missing_locally": missing_locally,
        "walk_stats": walk_stats,
        "reconnects": session.reconnects,
    }

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print(f"Scanned {len(remote_files)} remote file(s) under {remote_root} vs '{args.ref}':")
        if walk_stats.get("pruned_dirs"):
            print(f"  skipped {walk_stats['pruned_dirs']} director{'y' if walk_stats['pruned_dirs'] == 1 else 'ies'} "
                  f"entirely via --exclude (not listed, not walked) — e.g. "
                  + ", ".join(walk_stats.get("pruned_dir_samples", [])[:5])
                  + (" ..." if walk_stats["pruned_dirs"] > 5 else ""))
        if walk_stats["nlst_dirs"]:
            print(f"  note: {walk_stats['nlst_dirs']} of "
                  f"{sum(walk_stats.values())} directories needed the slow NLST+cwd-probe "
                  "fallback (server doesn't support MLSD or a parseable LIST) — consider "
                  "narrowing --exclude if this host is unreliable for full-tree scans")
        if session.reconnects:
            print(f"  note: reconnected {session.reconnects} time(s) after the server reset the connection")
        if args.mtime_cutoff:
            print(f"  {skipped_via_mtime} of those matches skipped the RETR+compare entirely "
                  f"(remote mtime older than --mtime-cutoff {args.mtime_cutoff})")
        print(f"  {matched} match local (identical, or differ only by whitespace/line endings)")
        print(f"  {len(orphans)} orphan(s) — on remote, not tracked, not gitignored")
        for rel in orphans:
            print(f"    {rel}")
        if args.pull_orphans:
            print(f"  downloaded {len(pulled)} orphan(s) into {working_dir}")
        elif orphans:
            print("  (re-run with --pull-orphans to download these)")
        if pulled_modified:
            print(f"  {len(pulled_modified)} modified tracked file(s) pull-replaced with the "
                  f"remote's version (mirror semantics - commit these)")
            for m in pulled_modified:
                print(f"    {m['rel_path']}  (remote mtime {m['remote_mtime']})")
        print(f"  {len(modified)} tracked file(s) genuinely modified on the server"
              + (" (deploy-collision paths - Stage 5 territory)" if args.pull_modified and modified else ""))
        for m in modified:
            range_note = "  [in deploy range]" if m.get("in_deploy_range") else ""
            print(f"    {m['rel_path']}  (remote mtime {m['remote_mtime']}) -> {m['backup']}{range_note}")
        if modified:
            print("  These were NOT written to your working files — conflict backups were "
                  "written in CRD's own format. Resolve each with the merge-from-crd-backup skill.")
        if missing_locally:
            print(f"  {len(missing_locally)} file(s) tracked in git and present on remote, "
                  f"but missing from the local working dir — check these by hand:")
            for rel in missing_locally:
                print(f"    {rel}")
        if suggested_new_watermark:
            print(f"  clean run — safe to advance the watermark to {suggested_new_watermark} "
                  f"(stage1_gate.py --record-run --outcome clean --new-watermark {suggested_new_watermark})")
        else:
            print("  not advancing the watermark — unresolved modified/missing-locally file(s) "
                  "above still need checking next run too")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
