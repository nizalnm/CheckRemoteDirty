#!/usr/bin/env python3
"""Resolve CheckRemoteDirty (CRD) backup files <-> working-directory files.

CRD stores remote copies under:
    <CRD_ROOT>/backups/<repo>/<path/mirroring/the/repo>/<filename>.<timestamp>[.conflict_bk]

where <timestamp> is the REMOTE file's mtime as YYYYMMDD_HHMMSS (or a 14-digit
YYYYMMDDHHMMSS fallback when CRD could not read the remote mtime).

Two suffix flavours, with different meanings:
    <name>.<ts>              remote copy taken right before deploy OVERWROTE it
    <name>.<ts>.conflict_bk  remote copy that was KEPT (deploy skipped) => unresolved

Usage:
    resolve_backup.py <path>            # path may be either a backup or a working file
    resolve_backup.py --scan <repo>     # list every pending .conflict_bk in that repo
    resolve_backup.py <path> --json     # machine-readable output

Options:
    --crd-root DIR      CRD install dir (default: $CRD_ROOT or C:\\www\\CheckRemoteDirty)
    --repos-root DIR    where repos live (default: $REPOS_ROOT or C:\\www)
    --working-dir DIR   explicit working dir for the repo, overrides --repos-root
    --json              emit JSON instead of the human-readable report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePath

BACKUP_RE = re.compile(
    r"^(?P<base>.+?)\.(?P<ts>\d{8}_\d{6}|\d{14})(?P<flag>\.conflict_bk)?$"
)

DEFAULT_CRD_ROOT = r"C:\www\CheckRemoteDirty"
DEFAULT_REPOS_ROOT = r"C:\www"


def norm_ts(ts: str) -> str:
    """Normalize both timestamp shapes to a sortable 14-digit string."""
    return ts.replace("_", "")


def pretty_ts(ts: str) -> str:
    n = norm_ts(ts)
    return f"{n[0:4]}-{n[4:6]}-{n[6:8]} {n[8:10]}:{n[10:12]}:{n[12:14]}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_backup_name(name: str):
    m = BACKUP_RE.match(name)
    if not m:
        return None
    return {
        "base": m.group("base"),
        "ts": m.group("ts"),
        "ts_sort": norm_ts(m.group("ts")),
        "conflict": bool(m.group("flag")),
    }


def candidates_for(backup_dir: Path, base_name: str):
    """All backups of base_name in backup_dir, newest first.

    Ties (same timestamp, one plain + one .conflict_bk) put .conflict_bk first:
    identical bytes, but the conflict flag is the one that carries the signal.
    """
    out = []
    if not backup_dir.is_dir():
        return out
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        parsed = parse_backup_name(entry.name)
        if not parsed or parsed["base"] != base_name:
            continue
        parsed["path"] = str(entry)
        parsed["size"] = entry.stat().st_size
        out.append(parsed)
    out.sort(key=lambda c: (c["ts_sort"], c["conflict"]), reverse=True)
    return out


def classify(path: Path, crd_root: Path, repos_root: Path, working_dir: Path | None):
    """Work out whether `path` is a backup or a working file, and derive the pair."""
    backups_root = crd_root / "backups"
    resolved = Path(os.path.abspath(str(path)))

    try:
        rel = resolved.relative_to(backups_root)
        is_backup = True
    except ValueError:
        is_backup = False

    if is_backup:
        parts = rel.parts
        if len(parts) < 2:
            raise SystemExit(f"error: {resolved} is not inside backups/<repo>/...")
        repo = parts[0]
        if repo == "local":
            # backups/local/<repo>/... holds .orig_bk baselines, not remote copies
            if len(parts) < 3:
                raise SystemExit(f"error: {resolved} is not inside backups/local/<repo>/...")
            repo = parts[1]
            rel_inside = PurePath(*parts[2:])
        else:
            rel_inside = PurePath(*parts[1:])
        parsed = parse_backup_name(resolved.name)
        if not parsed:
            # e.g. a .orig_bk file, or a plain name with no timestamp
            base_name = resolved.name
            for suffix in (".orig_bk",):
                if base_name.endswith(suffix):
                    base_name = base_name[: -len(suffix)]
        else:
            base_name = parsed["base"]
        rel_file = PurePath(*rel_inside.parts[:-1]) / base_name
    else:
        # working-dir file: find which repo it lives under
        if working_dir:
            repo_dir = Path(os.path.abspath(str(working_dir)))
        else:
            try:
                rel_to_repos = resolved.relative_to(Path(os.path.abspath(str(repos_root))))
            except ValueError:
                raise SystemExit(
                    f"error: {resolved} is not under {repos_root}; pass --working-dir"
                )
            if len(rel_to_repos.parts) < 2:
                raise SystemExit(f"error: {resolved} has no repo component")
            repo_dir = Path(os.path.abspath(str(repos_root))) / rel_to_repos.parts[0]
        repo = repo_dir.name
        rel_file = PurePath(resolved.relative_to(repo_dir))
        base_name = resolved.name

    if working_dir:
        repo_dir = Path(os.path.abspath(str(working_dir)))
    else:
        repo_dir = Path(os.path.abspath(str(repos_root))) / repo

    working_file = repo_dir / rel_file
    backup_dir = backups_root / repo / PurePath(*rel_file.parts[:-1])
    orig_bk = backups_root / "local" / repo / rel_file
    orig_bk = orig_bk.with_name(orig_bk.name + ".orig_bk")

    return {
        "repo": repo,
        "repo_dir": str(repo_dir),
        "rel_path": str(rel_file).replace("\\", "/"),
        "working_file": str(working_file),
        "working_file_exists": working_file.is_file(),
        "backup_dir": str(backup_dir),
        "base_name": base_name,
        "orig_bk": str(orig_bk),
        "orig_bk_exists": orig_bk.is_file(),
        "input_was_backup": is_backup,
    }


def build_report(path, crd_root, repos_root, working_dir):
    info = classify(Path(path), crd_root, repos_root, working_dir)
    cands = candidates_for(Path(info["backup_dir"]), info["base_name"])
    info["candidates"] = cands
    info["latest_backup"] = cands[0]["path"] if cands else None
    info["pending_conflict"] = any(c["conflict"] for c in cands)

    # A 0-byte backup is a failed/truncated FTP capture, not a real remote
    # state. Merging it would blank the working file. Flag it, and point at the
    # newest backup that actually has bytes so the caller can fall back.
    info["latest_empty"] = bool(cands) and cands[0]["size"] == 0
    nonempty = next((c for c in cands if c["size"] > 0), None)
    info["latest_nonempty_backup"] = nonempty["path"] if nonempty else None

    # If CRD wrote a conflict sidecar next to the chosen backup, surface the
    # 3-way merge base it recorded — that's the signal to prefer three_way_merge.py
    # over manual two-way archaeology.
    info["sidecar"] = None
    info["merge_base_ref"] = None
    if nonempty:
        meta = Path(nonempty["path"] + ".meta.json")
        if meta.is_file():
            try:
                d = json.loads(meta.read_text(encoding="utf-8"))
                info["sidecar"] = str(meta)
                info["merge_base_ref"] = (d.get("baseline", {}) or {}).get("merge_base_ref")
            except Exception:
                pass

    # Compare against the newest NON-EMPTY backup, so an empty newest doesn't
    # produce a misleading "differs" (whole file vs nothing).
    compare = nonempty
    if compare and info["working_file_exists"]:
        info["identical_to_latest"] = (
            sha256(Path(compare["path"])) == sha256(Path(info["working_file"]))
        )
    else:
        info["identical_to_latest"] = None
    return info


def scan_repo(repo: str, crd_root: Path, repos_root: Path, working_dir: Path | None):
    backups_root = crd_root / "backups" / repo
    if not backups_root.is_dir():
        raise SystemExit(f"error: no backups for repo '{repo}' under {crd_root / 'backups'}")

    seen: dict[str, dict] = {}
    for dirpath, _dirnames, filenames in os.walk(backups_root):
        for fn in filenames:
            parsed = parse_backup_name(fn)
            if not parsed or not parsed["conflict"]:
                continue
            full = Path(dirpath) / fn
            report = build_report(full, crd_root, repos_root, working_dir)
            seen[report["rel_path"]] = report
    return sorted(seen.values(), key=lambda r: r["rel_path"])


def print_report(info):
    print(f"repo:          {info['repo']}")
    print(f"rel path:      {info['rel_path']}")
    print(f"working file:  {info['working_file']}"
          f"{'' if info['working_file_exists'] else '   [MISSING]'}")
    print(f"backup dir:    {info['backup_dir']}")
    if info["orig_bk_exists"]:
        print(f"orig_bk:       {info['orig_bk']}   [baseline already anchored]")
    cands = info["candidates"]
    if not cands:
        print("candidates:    (none)")
        return
    print(f"candidates ({len(cands)}, newest first):")
    for i, c in enumerate(cands):
        marker = "->" if i == 0 else "  "
        flag = " conflict_bk" if c["conflict"] else "            "
        empty = "  <-- EMPTY (failed capture)" if c["size"] == 0 else ""
        print(f"  {marker} {pretty_ts(c['ts'])} {flag}  {c['size']:>9,} B  {c['path']}{empty}")

    if info["latest_empty"]:
        print("\n!! NEWEST BACKUP IS 0 BYTES — a failed/truncated FTP capture, NOT a real")
        print("   server state. Do NOT merge it (it would blank the working file).")
        if info["latest_nonempty_backup"]:
            print(f"   Newest usable backup instead: {info['latest_nonempty_backup']}")
        else:
            print("   No non-empty backup exists — nothing to merge.")
        print("   Consider deleting the empty backup so it stops being flagged as newest.")
    print(f"\nLATEST: {info['latest_nonempty_backup'] or info['latest_backup']}")
    if info.get("merge_base_ref"):
        print(f"3-WAY BASE available (CRD sidecar): {info['merge_base_ref']}")
        print("  -> prefer: python three_way_merge.py <working> <backup>   (reads the sidecar)")
    elif info.get("sidecar"):
        print(f"CRD sidecar present but no merge_base_ref: {info['sidecar']} (two-way merge)")
    if info["identical_to_latest"] is True:
        print("Working file is byte-identical to the latest backup: nothing to merge.")
    elif info["identical_to_latest"] is False:
        print("Working file DIFFERS from the latest backup: merge needed.")


def print_scan(reports, repo):
    if not reports:
        print(f"No pending .conflict_bk backups for '{repo}'.")
        return
    print(f"Pending conflict backups for '{repo}' ({len(reports)}):\n")
    for r in reports:
        status = (
            "identical" if r["identical_to_latest"]
            else "MISSING locally" if not r["working_file_exists"]
            else "DIFFERS"
        )
        print(f"  [{status:>15}]  {r['rel_path']}")
        print(f"                     {r['latest_backup']}")
    differing = [r for r in reports if r["identical_to_latest"] is False]
    print(f"\n{len(differing)} of {len(reports)} still need merging.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", help="a CRD backup path or a working-dir file path")
    p.add_argument("--scan", metavar="REPO", help="list all pending .conflict_bk files in REPO")
    p.add_argument("--crd-root", default=os.environ.get("CRD_ROOT", DEFAULT_CRD_ROOT))
    p.add_argument("--repos-root", default=os.environ.get("REPOS_ROOT", DEFAULT_REPOS_ROOT))
    p.add_argument("--working-dir", default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    crd_root = Path(os.path.abspath(args.crd_root))
    repos_root = Path(os.path.abspath(args.repos_root))
    working_dir = Path(args.working_dir) if args.working_dir else None

    if args.scan:
        reports = scan_repo(args.scan, crd_root, repos_root, working_dir)
        if args.json:
            json.dump(reports, sys.stdout, indent=2)
            print()
        else:
            print_scan(reports, args.scan)
        return 0

    if not args.path:
        p.error("give a path, or --scan REPO")

    info = build_report(args.path, crd_root, repos_root, working_dir)
    if args.json:
        json.dump(info, sys.stdout, indent=2)
        print()
    else:
        print_report(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
