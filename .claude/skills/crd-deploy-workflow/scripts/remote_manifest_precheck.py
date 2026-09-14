#!/usr/bin/env python3
"""Ask a project's remote-side mtime-bubbling scanner (the PHP script deployed
under the project's own web root, e.g. _crd_stage1_scan.php) whether Stage 1's
FTP walk can be skipped or narrowed, instead of always walking the whole
remote tree over FTP one directory/file at a time.

This is an OPTIONAL pre-step, opt-in per project (only meaningful for a
project that actually has the PHP scanner deployed - most CRD projects don't
and this script simply isn't invoked for them). It fails SAFE in every
direction: any network error, timeout, unexpected response, or missing prior
watermark just means "couldn't determine anything - do the normal full FTP
walk", never "skip the check". A stale/wrong read here would be a silent
correctness hole in a safety tool, which is a far worse outcome than an
occasional wasted-but-safe full walk.

Two levels of narrowing, both driven by the same remote scan:

  1. ROOT-LEVEL: if the remote's root_bubbled_max_mtime exactly matches the
     watermark from the last time this check ran clean, NOTHING under the
     whole tree has changed since - skip the FTP walk for this cycle
     entirely (skip_walk: true).

  2. SUBTREE-LEVEL (when the root has moved, so something changed
     somewhere): fetch the TOUCHED set only (action=dump?watermark=N - the
     server already filters to just directories whose bubbled_max_mtime
     exceeds the watermark; by the bubbling invariant every touched node's
     ancestors up to root are touched too, so this set has no gaps). Rather
     than have the server also enumerate every touched node's stale
     siblings by name (which can get long), this script derives "stale"
     itself: it already knows the full LOCAL git-tracked directory
     structure, so any local directory NOT in the touched set is stale by
     simple set subtraction, and - since a directory not in the touched set
     is guaranteed to have an entirely untouched subtree too - the walk
     stops there without needing to look any deeper. prune_patterns is
     meant to be passed straight through as additional --exclude flags to
     scan_remote_vs_local.py / CheckRemoteDirty.py for THIS run only - not
     persisted into the project's own excludePatterns, which stays reserved
     for permanent, deploy-exempt paths (see this skill's SKILL.md).

     include_roots is the same touched set, unreduced, meant for
     scan_remote_vs_local.py's --include-root instead - a compact
     alternative to prune_patterns for the identical effect (verified
     against each other on real production data - see SKILL.md's Stage 1
     section). It is NOT a "just the deepest touched paths" reduction - an
     earlier version tried that and it was wrong: once a walker reaches a
     touched leaf, "is this a descendant of the leaf" is trivially true for
     that leaf's own untouched children too, so a reduced set silently lets
     a leaf's stale subdirectories get walked anyway. The full set doesn't
     have that problem and costs nothing extra to pass - it's still small,
     proportional to how deep the real changes are, not to how many stale
     siblings exist in the tree.

Usage:
    remote_manifest_precheck.py --url https://acmeapp.example.com/_crd_stage1_scan.php \\
        --secret <base secret> --working-dir C:\\www\\acmeapp --ref staging \\
        --watermark 1789394497 --json

    # first-ever check for a project (no watermark yet - always a full walk,
    # but still primes remote_root_mtime for next time):
    remote_manifest_precheck.py --url ... --secret ... --working-dir ... --ref ... --json

Output (--json):
    {
      "remote_available": bool,       # false => every other field is a no-op default; do the normal walk
      "skip_walk": bool,
      "remote_root_mtime": int | null,
      "prune_patterns": [str, ...],   # --exclude-flavored narrowing
      "include_roots": [str, ...],    # --include-root-flavored narrowing (full touched set) - equivalent, more compact
      "reason": str
    }
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

# Default urllib sends a bare "Python-urllib/3.x" User-Agent, which several
# hosts' WAF/mod_security layers block outright (seen in practice against
# this exact endpoint - a browser request always worked, a default urllib
# request got a flat 403 before this script's own PHP ever ran). Any
# reasonably normal-looking UA avoids that; this one doesn't claim to be a
# specific browser, just a generic HTTP client.
USER_AGENT = "CRD-Stage1-Precheck/1.0 (+https://github.com/nizalnm/CheckRemoteDirty)"


def hour_token(base_secret: str, hour_string: str) -> str:
    return hashlib.sha256((base_secret + hour_string).encode("utf-8")).hexdigest()


def current_hour_token(base_secret: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return hour_token(base_secret, now.strftime("%Y%m%d%H"))


def fetch(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def fail_open(reason: str) -> dict:
    return {
        "remote_available": False,
        "skip_walk": False,
        "remote_root_mtime": None,
        "prune_patterns": [],
        "include_roots": [],
        "reason": reason,
    }


def local_children_by_parent(working_dir: str, ref: str) -> dict[str, list[str]]:
    """{dir_path: [immediate child dir paths]} for every directory implied by
    git's own tracked file list under ref, including '' for the repo root.
    Derived from git ls-tree (fast, purely local, no network) rather than
    the working-tree filesystem, so it matches exactly what Stage 1's FTP
    walk itself considers "tracked"."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True,
    )
    all_dirs: set[str] = {""}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "/" not in line:
            continue
        parts = line.split("/")[:-1]  # drop the filename itself
        for i in range(1, len(parts) + 1):
            all_dirs.add("/".join(parts[:i]))

    children: dict[str, list[str]] = {}
    for d in all_dirs:
        if d == "":
            continue
        parent = d.rsplit("/", 1)[0] if "/" in d else ""
        children.setdefault(parent, []).append(d)
    return children


def compute_prune_patterns(touched_paths: set[str], children_by_parent: dict[str, list[str]]) -> list[str]:
    """Walk the LOCAL git-tracked directory tree top-down. The first
    directory not in touched_paths is guaranteed - by the bubbling
    invariant - to have its entire subtree untouched too (if anything
    inside it had changed, this directory would be touched as well).
    Pruned there; never recursed into further."""
    pruned: list[str] = []

    def visit(path: str) -> None:
        if path != "" and path not in touched_paths:
            pruned.append(path)
            return
        for child in children_by_parent.get(path, []):
            visit(child)

    visit("")
    return pruned


# NOTE: an earlier version of this function reduced touched_paths down to
# just its deepest ("leaf") entries, on the theory that scan_remote_vs_local.py
# could walk fully and unrestricted from each leaf. That was wrong and cost a
# real correctness+performance regression, measured live: a touched leaf's
# own STALE children (e.g. xs/plugins/noty's untouched lib/docs/demo/test/
# .github/src subdirectories - each already a separate row in the dump
# proving they weren't touched) got walked anyway, because "is this a
# descendant of a leaf" is trivially true for everything below a leaf,
# leaf's untouched children included. scan_remote_vs_local.py's
# --include-root now expects the FULL touched set instead (see its own
# docstring / on_path_to_any_leaf()) - passed straight through below, no
# reduction needed. It's still small: proportional to how deep the actual
# changes are, not to how many stale siblings exist anywhere in the tree.


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="base URL of the deployed PHP scanner, e.g. https://host/_crd_stage1_scan.php")
    p.add_argument("--secret", required=True, help="base secret (the hourly token is derived from this, never sent raw)")
    p.add_argument("--working-dir", required=True, help="local git repo root, for deriving the tracked directory tree")
    p.add_argument("--ref", default="HEAD", help="git ref to read the tracked tree from (default HEAD)")
    p.add_argument("--watermark", type=int, default=None, help="last known-clean remote_root_mtime (from stage1_gate.py's remoteManifestLastMtime); omit on first-ever check for a project")
    p.add_argument("--timeout", type=float, default=25.0, help="HTTP timeout in seconds for each request (default 25)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    token = current_hour_token(args.secret)
    trigger_url = f"{args.url}?action=reset&token={token}"

    try:
        result = fetch(trigger_url, args.timeout)
    except urllib.error.HTTPError as e:
        out = fail_open(f"HTTP {e.code} from remote scanner - {e.reason}")
    except urllib.error.URLError as e:
        out = fail_open(f"remote scanner unreachable - {e.reason}")
    except (TimeoutError, OSError) as e:
        out = fail_open(f"remote scanner request failed - {e}")
    except (ValueError, json.JSONDecodeError) as e:
        out = fail_open(f"remote scanner returned unparseable response - {e}")
    else:
        if "error" in result:
            out = fail_open(f"remote scanner rejected the request - {result['error']}")
        elif result.get("state") == "rate_limited":
            out = fail_open(f"remote scanner rate-limited this trigger - {result.get('detail', '')}")
        elif result.get("status") != "done":
            out = fail_open(f"remote walk not complete within this check (status={result.get('status')!r}) - not retrying, falling back this cycle")
        else:
            root_mtime = result.get("root_bubbled_max_mtime")
            if args.watermark is not None and root_mtime == args.watermark:
                out = {
                    "remote_available": True,
                    "skip_walk": True,
                    "remote_root_mtime": root_mtime,
                    "prune_patterns": [],
                    "include_roots": [],
                    "reason": "remote root mtime unchanged since last known-clean watermark - nothing to check",
                }
            elif args.watermark is None:
                out = {
                    "remote_available": True,
                    "skip_walk": False,
                    "remote_root_mtime": root_mtime,
                    "prune_patterns": [],
                    "include_roots": [],
                    "reason": "no prior watermark for this project yet - full walk this time, priming the watermark for next cycle",
                }
            else:
                dump_url = f"{args.url}?action=dump&watermark={args.watermark}&token={token}"
                try:
                    dump = fetch(dump_url, args.timeout)
                    touched_paths = {node["path"] for node in dump.get("touched", [])}
                    children_by_parent = local_children_by_parent(args.working_dir, args.ref)
                    patterns = compute_prune_patterns(touched_paths, children_by_parent)
                    # Full set, not a leaf-reduction - see the note above
                    # compute_prune_patterns and on_path_to_any_leaf()'s own
                    # docstring in scan_remote_vs_local.py for why.
                    roots = sorted(touched_paths)
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                        ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as e:
                    # Root-level comparison already succeeded and told us
                    # something changed - that much is trustworthy even if
                    # the dump fetch or local git read now fails. Degrade to
                    # "walk everything" rather than pretending we know
                    # nothing, or worse, pruning off stale/wrong data.
                    patterns = []
                    roots = []
                    out_reason_suffix = f" (pruning step failed, no pruning this cycle: {e})"
                else:
                    out_reason_suffix = ""
                out = {
                    "remote_available": True,
                    "skip_walk": False,
                    "remote_root_mtime": root_mtime,
                    "prune_patterns": patterns,
                    "include_roots": roots,
                    "reason": (
                        f"remote root mtime moved ({args.watermark} -> {root_mtime}) - "
                        f"{len(patterns)} exclude pattern(s) / {len(roots)} include root(s) computed"
                        + out_reason_suffix
                    ),
                }

    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        print(f"remote_available: {out['remote_available']}")
        print(f"skip_walk: {out['skip_walk']}")
        print(f"remote_root_mtime: {out['remote_root_mtime']}")
        print(f"prune_patterns: {len(out['prune_patterns'])} pattern(s)")
        for pat in out["prune_patterns"][:20]:
            print(f"    {pat}")
        if len(out["prune_patterns"]) > 20:
            print(f"    ... and {len(out['prune_patterns']) - 20} more")
        print(f"include_roots: {len(out['include_roots'])} root(s)")
        for r in out["include_roots"]:
            print(f"    {r}")
        print(f"reason: {out['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
