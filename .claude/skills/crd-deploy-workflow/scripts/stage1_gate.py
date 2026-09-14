#!/usr/bin/env python3
"""Decide whether Stage 1's full remote-tree scan should run this deploy, and
track the drift-triggered enforcement window that overrides the default gate.

Stage 1 (scan_remote_vs_local.py) is the only stage that lists the ENTIRE
remote directory tree — much chattier than CRD's own preflight, which only
ever touches already-known paths. Running it on every single deploy is
wasteful when the server has been well-behaved, so the default is "once per
calendar day, on the first deploy of the day."

But that default is only safe as long as nothing is actually drifting behind
it. If a later stage's reactive check (CRD's own --vsGit preflight, Stage 4)
finds a real, unexpected conflict (DIFF HASH) — drift Stage 1 either missed
or never got a chance to catch because it was gated off that cycle — that's
a signal the server is being touched directly right now, and the daily gate
should stand down: force Stage 1 to run on every deploy until it comes back
clean three times in a row. Any drift encountered *during* that enforced
window (whether Stage 1's own scan or another Stage 4 hit) resets the
clean-run streak back to zero — enforcement only lifts after three
*consecutive* clean Stage 1 scans.

State persists per project in a small JSON sidecar next to that project's
other CRD files (ftpConfig, dirtyCheckFile) — pass its path with
--state-file. Missing file == fresh state (gate defaults to "run", since
there's no record of a first-of-day run yet).

The same sidecar also carries `mtimeWatermark` — the raw YYYYMMDDHHMMSS (UTC)
cutoff scan_remote_vs_local.py's --mtime-cutoff uses to skip re-fetching
content for tracked files that haven't changed on the remote since the last
clean run (see that script's own docstring for the mechanism). It starts
unset (null), which simply means "no cutoff yet — check everything," the
same as running without --mtime-cutoff at all.

A third, independent field, `remoteManifestLastMtime`, carries the last
known-clean root mtime from remote_manifest_precheck.py — a DIFFERENT
optimization (a remote-side PHP scanner deployed under the project's own web
root, for projects that have one; see that script's own docstring) that can
skip or narrow the FTP walk itself, not just skip re-fetching content within
it. Like mtimeWatermark it starts unset (null); use --set-remote-manifest-mtime
to advance it (see Modes below) after a precheck run that left nothing
unresolved.

Usage:
    stage1_gate.py --state-file acmeapp_stage1_state.json --check
    stage1_gate.py --state-file acmeapp_stage1_state.json --record-run --outcome clean
    stage1_gate.py --state-file acmeapp_stage1_state.json --record-run --outcome clean --new-watermark 20260914120000
    stage1_gate.py --state-file acmeapp_stage1_state.json --record-run --outcome drift
    stage1_gate.py --state-file acmeapp_stage1_state.json --record-external-drift
    stage1_gate.py --state-file acmeapp_stage1_state.json --set-remote-manifest-mtime 1789394497

Modes:
    --check
        Reports whether Stage 1 should run this cycle, and why. Does not
        modify state. Exit 0 either way; read the JSON/text "should_run"
        field, don't rely on exit code for the decision. The current
        mtimeWatermark (possibly null) is always included in the "state"
        sub-object of the output — pass it as scan_remote_vs_local.py's
        --mtime-cutoff when should_run is true.

    --record-run --outcome clean|drift [--new-watermark VALUE]
        Call after Stage 1 actually ran this cycle, with what it found.
        Always stamps lastRunDate = today (the daily gate looks at this).
        outcome=drift: activates enforcement and resets the clean streak to 0;
        --new-watermark is rejected here (a drift run means something out
        there wasn't caught by the old watermark — never advance it on the
        same run that found that).
        outcome=clean: if enforcement is active, increments the clean streak
        and deactivates enforcement once it reaches 3; if enforcement was not
        active, this is just a normal once-a-day clean run (no streak kept).
        Pass --new-watermark (scan_remote_vs_local.py's own suggested value,
        printed at the end of a run that left nothing unresolved) to advance
        mtimeWatermark; omit it — e.g. the scan found modified/missing-locally
        files it couldn't safely call "fully accounted for" — and the stored
        watermark is left exactly as it was, so next run still fully checks
        whatever this run left unresolved.

    --record-external-drift
        Call when a LATER stage (Stage 4's CRD preflight, typically) finds an
        unexpected DIFF HASH on a cycle where Stage 1 did NOT run (it was
        gated off). Activates enforcement and resets the clean streak, but
        does NOT stamp lastRunDate — Stage 1 itself hasn't run yet this
        cycle. The workflow should treat this as "go back and run Stage 1
        right now, for this same deployment, as an immediate precaution" —
        then call --record-run with that scan's outcome afterward.

    --set-remote-manifest-mtime VALUE
        Advance remoteManifestLastMtime to VALUE (an integer unix
        timestamp — remote_manifest_precheck.py's own remote_root_mtime
        field). Call this after a precheck run whose remote_available was
        true and which either skipped the walk entirely or successfully
        pruned it — i.e. one whose result you trust as a clean, current
        read of the remote root. Never call it when remote_available was
        false (the precheck couldn't reach the scanner at all) — that would
        advance the baseline past a cycle nothing was actually verified in.
        Independent of --record-run/--outcome; does not touch lastRunDate,
        enforcementActive, or cleanStreak.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

CLEAN_STREAK_TO_DISABLE = 3

DEFAULT_STATE = {
    "lastRunDate": None,
    "enforcementActive": False,
    "cleanStreak": 0,
    "mtimeWatermark": None,
    "remoteManifestLastMtime": None,
}


def load_state(path):
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    state = dict(DEFAULT_STATE)
    state.update({k: data.get(k, v) for k, v in DEFAULT_STATE.items()})
    return state


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def today_str():
    return datetime.date.today().isoformat()


def do_check(state):
    today = today_str()
    if state["enforcementActive"]:
        return {
            "should_run": True,
            "reason": (
                f"drift enforcement active ({state['cleanStreak']}/{CLEAN_STREAK_TO_DISABLE} "
                "consecutive clean scans so far) - scanning every deploy until 3 in a row come back clean"
            ),
        }
    if state["lastRunDate"] != today:
        return {
            "should_run": True,
            "reason": f"first deploy of the day ({today}) - default daily scan",
        }
    return {
        "should_run": False,
        "reason": f"already ran Stage 1 today ({today}) and no drift enforcement is active",
    }


def do_record_run(state, outcome, new_watermark=None):
    state["lastRunDate"] = today_str()
    if outcome == "drift":
        state["enforcementActive"] = True
        state["cleanStreak"] = 0
        return {"enforcementActive": True, "cleanStreak": 0, "detail": "drift found - enforcement (re)activated"}

    # outcome == "clean"
    if new_watermark:
        state["mtimeWatermark"] = new_watermark

    if state["enforcementActive"]:
        state["cleanStreak"] += 1
        if state["cleanStreak"] >= CLEAN_STREAK_TO_DISABLE:
            state["enforcementActive"] = False
            state["cleanStreak"] = 0
            return {
                "enforcementActive": False,
                "cleanStreak": 0,
                "detail": f"{CLEAN_STREAK_TO_DISABLE} consecutive clean scans reached - enforcement lifted",
            }
        return {
            "enforcementActive": True,
            "cleanStreak": state["cleanStreak"],
            "detail": f"clean scan {state['cleanStreak']}/{CLEAN_STREAK_TO_DISABLE} - still enforcing",
        }

    return {"enforcementActive": False, "cleanStreak": 0, "detail": "clean scan, daily gate resumes tomorrow"}


def do_record_external_drift(state):
    state["enforcementActive"] = True
    state["cleanStreak"] = 0
    return {
        "enforcementActive": True,
        "cleanStreak": 0,
        "detail": (
            "unexpected drift found by a later stage while Stage 1 was gated off - "
            "run Stage 1 now for this deployment too, then record its outcome"
        ),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-file", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--record-run", action="store_true")
    mode.add_argument("--record-external-drift", action="store_true")
    mode.add_argument("--set-remote-manifest-mtime", default=None, metavar="VALUE",
                       help="advance remoteManifestLastMtime to this integer unix timestamp "
                            "(remote_manifest_precheck.py's remote_root_mtime) - independent of "
                            "the gate/enforcement fields")
    p.add_argument("--outcome", choices=["clean", "drift"], help="required with --record-run")
    p.add_argument("--new-watermark", default=None,
                   help="raw YYYYMMDDHHMMSS (UTC) to advance mtimeWatermark to - only valid "
                        "with --record-run --outcome clean (see scan_remote_vs_local.py's "
                        "suggested_new_watermark output)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.record_run and not args.outcome:
        p.error("--record-run requires --outcome clean|drift")
    if args.new_watermark and not (args.record_run and args.outcome == "clean"):
        p.error("--new-watermark is only valid with --record-run --outcome clean")
    if args.set_remote_manifest_mtime is not None and not args.set_remote_manifest_mtime.lstrip("-").isdigit():
        p.error("--set-remote-manifest-mtime must be an integer unix timestamp")

    state = load_state(args.state_file)

    if args.check:
        result = do_check(state)
    elif args.record_run:
        result = do_record_run(state, args.outcome, new_watermark=args.new_watermark)
        save_state(args.state_file, state)
    elif args.record_external_drift:
        result = do_record_external_drift(state)
        save_state(args.state_file, state)
    else:
        state["remoteManifestLastMtime"] = args.set_remote_manifest_mtime
        result = {"detail": f"remoteManifestLastMtime set to {args.set_remote_manifest_mtime}"}
        save_state(args.state_file, state)

    result["state"] = state
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        for k, v in result.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
