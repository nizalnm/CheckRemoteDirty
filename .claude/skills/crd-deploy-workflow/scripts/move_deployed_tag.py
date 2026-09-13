#!/usr/bin/env python3
"""Force-move the singleton "last deployed" git tag to the commit that was just
deployed, with a guard rail against moving it backwards by mistake.

The convention (already live in the widgetco and acmeapp repos) is: exactly
one tag — e.g. `latest-staging-deployed` — always points at the commit that is
actually live on the server right now. It is force-moved on every deploy, never
duplicated. That singleton-ness is what makes it useful: a conflict backup's
`.meta.json` sidecar, or `merge-from-crd-backup`'s `--gitBaselineHash`, can
always resolve "what did the server last have?" to one unambiguous commit.

The failure mode this guards against: running the deploy step out of order, or
deploying an older commit by accident, and force-moving the tag onto it — which
would make every future conflict-merge compute the wrong base. By default this
refuses to move the tag anywhere that isn't a descendant of (or equal to) where
it already points; pass --force if you genuinely mean to move it backwards
(e.g. correcting a bad tag after the fact).

Usage:
    move_deployed_tag.py --repo C:\\www\\acmeapp --commit HEAD
    move_deployed_tag.py --repo C:\\www\\acmeapp --commit HEAD --tag latest-staging-deployed --push
    move_deployed_tag.py --repo C:\\www\\acmeapp --commit HEAD --force   # allow moving backwards

Options:
    --repo PATH     git repo root
    --commit REF    commit to point the tag at (default: HEAD)
    --tag NAME      tag name (default: latest-staging-deployed)
    --remote NAME   remote to push to (default: origin)
    --push          also push the moved tag (a real, visible action — confirm with
                    the user before passing this; the caller, not this script,
                    owns that confirmation)
    --force         allow moving the tag to a commit that is NOT a descendant of
                    its current position (default: refuse and exit 3)
    --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def rev_parse(repo, ref):
    r = run(["git", "rev-parse", ref], repo, check=False)
    if r.returncode != 0:
        return None
    return r.stdout.decode().strip()


def is_ancestor(repo, ancestor, descendant):
    r = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], repo, check=False)
    return r.returncode == 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True)
    p.add_argument("--commit", default="HEAD")
    p.add_argument("--tag", default="latest-staging-deployed")
    p.add_argument("--remote", default="origin")
    p.add_argument("--push", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    repo = args.repo
    target = rev_parse(repo, args.commit)
    if not target:
        return _emit(args, {"status": "error", "detail": f"'{args.commit}' does not resolve to a commit"}, 4)

    previous = rev_parse(repo, f"refs/tags/{args.tag}")
    result = {"tag": args.tag, "previous_commit": previous, "target_commit": target}

    if previous and previous != target and not args.force:
        if not is_ancestor(repo, previous, target):
            result["status"] = "refused"
            result["detail"] = (
                f"target {target[:12]} is not a descendant of the tag's current commit "
                f"{previous[:12]} — this would move '{args.tag}' backwards. "
                "Pass --force if that's intentional."
            )
            return _emit(args, result, 3)

    run(["git", "tag", "-f", args.tag, target], repo)
    result["moved"] = previous != target
    result["status"] = "ok"

    if args.push:
        push = run(["git", "push", args.remote, f"refs/tags/{args.tag}", "--force"], repo, check=False)
        result["pushed"] = push.returncode == 0
        if push.returncode != 0:
            result["push_error"] = push.stderr.decode(errors="replace").strip()

    return _emit(args, result, 0 if result["status"] == "ok" else 1)


def _emit(args, result, code):
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        for k, v in result.items():
            print(f"{k}: {v}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
