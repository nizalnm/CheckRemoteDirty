#!/usr/bin/env python3
"""Install the remote-side mtime-bubbling scanner (assets/sample_remote_scanner.php)
onto a project's own web root via FTP, if - and only if - it isn't already
there. Idempotent: safe to run on every Stage 1 cycle as a "make sure the
optional scanner is present" check, not just a one-time setup step.

Why this needs FTP, not just an HTTP check against remoteManifestUrl: the
deployed scanner deliberately returns the SAME generic 404 for a wrong/stale
token as for the file genuinely not existing (see sample_remote_scanner.php's
own docstring - it's an intentional anti-probing design, not an oversight).
That means an HTTP 404 from remote_manifest_precheck.py can't safely be read
as "go ahead and install" - it's equally consistent with "the file is there
but the configured secret no longer matches it", and blindly reinstalling on
that signal would silently clobber a real deployment (or, if the secret
still matches but is now regenerated anyway, needlessly invalidate every
outstanding hourly token). Checking file existence over FTP (MLSD/SIZE)
sidesteps the ambiguity entirely - it answers the actual question directly,
without going through the deliberately-uninformative HTTP layer at all.

What "install" does:
  1. Check the target path over FTP. Already there -> report installed=false
     (nothing changed) and stop; --force skips this check and reinstalls
     unconditionally (e.g. deliberately rotating the secret).
  2. Generate a fresh secret (unless --reuse-secret-from points at a
     workflow.json that already has one - e.g. a redeploy after edits should
     keep every hourly token derived from it working, not invalidate them).
  3. Read assets/sample_remote_scanner.php, fill in the secret and (if
     --exclude-patterns-json is given) the project's own excludePatterns -
     the latter closes the "kept in sync by hand" gap the template's own
     comments and SKILL.md flag: this script can seed it directly from the
     same excludePatterns already in the project's workflow.json instead of
     a human copying the list into both places.
  4. Upload the filled-in file via FTP to the target path.
  5. If --workflow-config is given, write remoteManifestUrl and
     remoteManifestSecret directly into that file - the secret is NEVER
     printed to stdout/JSON in this mode, only saved to disk. Without
     --workflow-config, the secret IS included in the JSON result (there's
     no other way to hand it back) - prefer passing --workflow-config
     whenever the caller has file access to it, which in this skill's own
     usage it always does.

Usage:
    # One-time setup for a project that doesn't have the scanner yet:
    install_remote_scanner.py --ftp-config acmeapp_config.json \\
        --remote-path _crd_stage1_scan.php --url https://acmeapp.example.com/_crd_stage1_scan.php \\
        --workflow-config acmeapp_workflow.json --json

    # Routine check (idempotent, safe on every Stage 1 cycle):
    install_remote_scanner.py --ftp-config acmeapp_config.json --remote-path _crd_stage1_scan.php --json

    # Deliberate secret rotation (reinstalls even though already present):
    install_remote_scanner.py --ftp-config acmeapp_config.json --remote-path _crd_stage1_scan.php --force --workflow-config acmeapp_workflow.json --json

Output (--json):
    {
      "already_installed": bool,
      "installed": bool,        # true only if this run actually uploaded something
      "remote_path": str,
      "secret": str | null,     # only present when --workflow-config was NOT given and installed=true
      "workflow_config_updated": bool
    }
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from ftplib import FTP_TLS, error_perm

TEMPLATE_DEFAULT = None  # resolved relative to this script's own location


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def connect_ftp(config: dict, timeout: float = 30.0) -> FTP_TLS:
    ftp = FTP_TLS(timeout=timeout)
    ftp.connect(config["host"], config.get("port", 21))
    ftp.login(config["user"], config["password"])
    ftp.prot_p()
    return ftp


def remote_exists(ftp: FTP_TLS, remote_root: str, remote_path: str) -> bool:
    full = f"{remote_root.rstrip('/')}/{remote_path}"
    try:
        size = ftp.size(full)
        return size is not None
    except error_perm:
        # Some servers refuse SIZE on certain files/paths even when they
        # exist (ASCII-mode quirks, etc.) - MLSD on the parent directory is
        # a more reliable fallback than trusting a SIZE error as "missing".
        parent = full.rsplit("/", 1)[0] or "/"
        name = full.rsplit("/", 1)[-1]
        try:
            for entry_name, facts in ftp.mlsd(parent):
                if entry_name == name and facts.get("type") == "file":
                    return True
            return False
        except Exception:
            # Truly can't tell - fail toward NOT installing rather than
            # risking a clobber on an ambiguous read.
            raise RuntimeError(f"could not determine whether {full} exists (SIZE and MLSD both failed)")


def fill_template(template_text: str, secret: str, exclude_patterns: list[str] | None) -> str:
    pattern = re.compile(r"\$BASE_SECRET = '[^']*';")
    filled, n = pattern.subn(f"$BASE_SECRET = '{secret}';", template_text)
    if n != 1:
        raise ValueError(f"expected exactly one $BASE_SECRET assignment in template, found {n}")

    if exclude_patterns is not None:
        # Replace the $EXCLUDE_PATTERNS = array(...); block wholesale. The
        # template's own array spans multiple lines ending in ");" on its
        # own line - matched non-greedily up to the first such close.
        php_list = ",\n    ".join("'" + p.replace("\\", "\\\\").replace("'", "\\'") + "'" for p in exclude_patterns)
        replacement = f"$EXCLUDE_PATTERNS = array(\n    {php_list},\n    $SELF_BASENAME,\n);"
        ep_pattern = re.compile(r"\$EXCLUDE_PATTERNS = array\(.*?\n\);", re.DOTALL)
        filled, n2 = ep_pattern.subn(replacement, filled)
        if n2 != 1:
            raise ValueError(f"expected exactly one $EXCLUDE_PATTERNS array in template, found {n2}")

    return filled


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ftp-config", required=True)
    p.add_argument("--remote-path", default="_crd_stage1_scan.php",
                   help="path relative to the FTP config's remote_root (default: _crd_stage1_scan.php)")
    p.add_argument("--template", default=None,
                   help="path to the template PHP file (default: assets/sample_remote_scanner.php next to this skill)")
    p.add_argument("--workflow-config", default=None,
                   help="if given, remoteManifestUrl/remoteManifestSecret are written directly into this "
                        "file instead of the secret ever appearing in this script's own output")
    p.add_argument("--url", default=None,
                   help="the scanner's full public URL, only needed to write remoteManifestUrl into "
                        "--workflow-config (ignored otherwise)")
    p.add_argument("--exclude-patterns-json", default=None,
                   help="JSON array string of exclude patterns to seed the template with - typically the "
                        "project's own workflow.json excludePatterns, keeping the two copies in sync "
                        "automatically instead of by hand")
    p.add_argument("--force", action="store_true",
                   help="reinstall even if already present (e.g. deliberate secret rotation)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    template_path = args.template
    if template_path is None:
        import os
        template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "assets", "sample_remote_scanner.php")

    ftp_config = load_json(args.ftp_config)
    remote_root = ftp_config.get("remote_root", "/")

    ftp = connect_ftp(ftp_config)
    try:
        already_there = remote_exists(ftp, remote_root, args.remote_path)

        if already_there and not args.force:
            out = {
                "already_installed": True,
                "installed": False,
                "remote_path": args.remote_path,
                "secret": None,
                "workflow_config_updated": False,
            }
        else:
            existing_secret = None
            if args.workflow_config:
                try:
                    existing_secret = load_json(args.workflow_config).get("remoteManifestSecret")
                except FileNotFoundError:
                    pass
            secret = existing_secret if (existing_secret and not args.force) else secrets.token_hex(32)

            exclude_patterns = json.loads(args.exclude_patterns_json) if args.exclude_patterns_json else None

            with open(template_path, "r", encoding="utf-8") as f:
                template_text = f.read()
            filled = fill_template(template_text, secret, exclude_patterns)

            full_remote = f"{remote_root.rstrip('/')}/{args.remote_path}"
            import io
            ftp.storbinary(f"STOR {full_remote}", io.BytesIO(filled.encode("utf-8")))

            workflow_updated = False
            if args.workflow_config:
                try:
                    wf = load_json(args.workflow_config)
                except FileNotFoundError:
                    wf = {}
                wf["remoteManifestSecret"] = secret
                if args.url:
                    wf["remoteManifestUrl"] = args.url
                with open(args.workflow_config, "w", encoding="utf-8") as f:
                    json.dump(wf, f, indent=4)
                    f.write("\n")
                workflow_updated = True

            out = {
                "already_installed": already_there,
                "installed": True,
                "remote_path": args.remote_path,
                "secret": None if args.workflow_config else secret,
                "workflow_config_updated": workflow_updated,
            }
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    if args.json:
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        for k, v in out.items():
            print(f"{k}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
