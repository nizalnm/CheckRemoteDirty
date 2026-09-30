#!/usr/bin/env python
r"""
crd_deploy_loop.py - deploy a git commit over a FLAKY FTP link with CheckRemoteDirty (CRD), safely and repeatably.

WHY: on some hosts the FTP server drops the connection after a handful of files (SSL BAD_LENGTH, WinError 10054/10013).
A single `CheckRemoteDirty.py --deployOnClean` run then aborts part-way. Every re-run only uploads files that are not yet
at the goal version, so repeating "preflight -> deploy" until nothing is left converges. This script automates that loop and
adds the safeguards a hand-run loop lacks:

  * PINS the goal to one exact commit hash, resolved once at start (a branch that moves mid-run - another session merging -
    can never change what is uploaded).
  * Uploads from a DEDICATED clean detached worktree of exactly that commit, so uncommitted or later commits in the main
    checkout can never leak onto the server, and other people can keep working in the main checkout.
  * Never overwrites a server file that differs from BOTH the old and the new version, unless it is provably a truncated
    upload (empty, or a strict prefix of the new version) left behind by a dropped connection: it stops and tells you.
  * Excludes non-runtime files (tests) and the project's excludePatterns, answers CRD's interactive prompts, waits between
    passes so a throttling server is not hammered, and stops if it makes no progress.
  * Optionally records the result: moves the LOCAL deployed tag (only forward, never pushed) and fast-forwards the mirror.

CONFIG: reads <CRD_ROOT>/<project>_workflow.json (the same file the crd-deploy-workflow skill uses): workingDir, deployBranch,
deployedTag, dirtyCheckFile, ftpConfig, excludePatterns, mirrorWorktree. Optional key "loopExcludePatterns" (list) overrides the
default extra excludes ["dev/tests/*", "tests/*"] - e.g. add a local-only config file that must never go up.
Optional keys "loopDelaySeconds" / "loopMaxDelaySeconds" (integers) set the wait for that project.

WAIT BETWEEN PASSES (seconds). It is ADAPTIVE: the base wait is used while passes make progress (at least one file uploaded);
after a pass that uploads nothing (the server is refusing or throttling) the wait doubles (min 2s) up to the maximum, and it
snaps back to the base as soon as a pass gets a file through. First one set wins for each value:
  base wait:  1. --delay N   2. env CRD_LOOP_DELAY   3. "loopDelaySeconds" in the workflow config   4. built-in default (1)
  max wait:   1. --max-delay N   2. env CRD_LOOP_MAX_DELAY   3. "loopMaxDelaySeconds"   4. built-in default (60)
0 as the base = no wait while things progress (the back-off still applies when they stop).
CRD_ROOT comes from the CRD_ROOT env var (default C:\www\CheckRemoteDirty). Credentials are read from the project's ftpConfig
file; nothing secret is stored in this script.

USAGE
  python crd_deploy_loop.py --project myproject --goal <commit-ish> --status          # preflight only, uploads nothing
  python crd_deploy_loop.py --project myproject --goal <commit-ish>                   # loop until every file is at goal
  python crd_deploy_loop.py --project myproject --goal <commit-ish> --move-tag --cleanup
Options: --delay N (base seconds between passes, default 1)  --max-delay N (back-off ceiling, default 60)  --max-passes N  --exclude PATTERN (repeatable)
         --stall N (give up after N passes without progress, default 8)  --allow-non-descendant

It does NOT run database migrations. It never pushes anything (the deployed tag is local-only by policy).
"""
import argparse
import collections
import ftplib
import io
import json
import os
import re
import subprocess
import sys
import time

CRD_ROOT = os.environ.get('CRD_ROOT', r'C:\www\CheckRemoteDirty').replace('\\', '/')
DEFAULT_LOOP_EXCLUDES = ['dev/tests/*', 'tests/*']
DEFAULT_DELAY_SECONDS = 1        # base wait between passes: start aggressive, back off only when a pass gets nothing through
DEFAULT_MAX_DELAY_SECONDS = 60   # ceiling for the back-off


class Ctx:
    """Everything resolved once at start."""


def sh(args, cwd, input_text=None):
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd, input=input_text)


def git(ctx, *args, cwd=None):
    return sh(['git'] + list(args), cwd or ctx.repo)


def load_context(args):
    ctx = Ctx()
    wf_path = '%s/%s_workflow.json' % (CRD_ROOT, args.project)
    if not os.path.isfile(wf_path):
        sys.exit('No workflow config: %s' % wf_path)
    ctx.wf = json.load(open(wf_path, encoding='utf-8'))
    ctx.repo = ctx.wf['workingDir'].replace('\\', '/')
    ctx.tag = ctx.wf.get('deployedTag', 'latest-deployed')
    ctx.dirty_file = ctx.wf['dirtyCheckFile']
    ctx.ftp_config = ctx.wf['ftpConfig']
    ctx.ftp = json.load(open('%s/%s' % (CRD_ROOT, ctx.ftp_config), encoding='utf-8'))
    ref = args.goal or ctx.wf.get('deployBranch', 'HEAD')
    r = git(ctx, 'rev-parse', '--verify', ref + '^{commit}')
    if r.returncode != 0:
        sys.exit('Cannot resolve goal %r: %s' % (ref, r.stderr.strip()))
    ctx.goal = r.stdout.strip()
    b = git(ctx, 'rev-parse', '--verify', ctx.tag + '^{commit}')
    ctx.baseline = b.stdout.strip() if b.returncode == 0 else None
    ctx.deploy_dir = '%s-deploy-%s' % (ctx.repo, ctx.goal[:8])
    excludes = list(ctx.wf.get('excludePatterns', []))
    excludes += list(ctx.wf.get('loopExcludePatterns', DEFAULT_LOOP_EXCLUDES))
    excludes += list(args.exclude or [])
    ctx.excludes = excludes
    return ctx


def guard_goal(ctx, args):
    """Refuse to move the deployment backwards or sideways unless told to."""
    if ctx.baseline and ctx.baseline != ctx.goal and not args.allow_non_descendant:
        anc = git(ctx, 'merge-base', '--is-ancestor', ctx.baseline, ctx.goal)
        if anc.returncode != 0:
            sys.exit('Goal %s is NOT a descendant of %s (%s). Refusing. Use --allow-non-descendant only if you mean it.'
                     % (ctx.goal[:8], ctx.tag, ctx.baseline[:8]))


def ensure_deploy_dir(ctx):
    if not os.path.isdir(ctx.deploy_dir):
        r = git(ctx, 'worktree', 'add', '--detach', ctx.deploy_dir, ctx.goal)
        if r.returncode != 0:
            sys.exit('Could not create %s: %s' % (ctx.deploy_dir, (r.stderr or r.stdout).strip()[:300]))
    head = sh(['git', 'rev-parse', 'HEAD'], ctx.deploy_dir).stdout.strip()
    dirty = [l for l in sh(['git', 'status', '--short'], ctx.deploy_dir).stdout.splitlines() if not l.startswith('??')]
    if head != ctx.goal:
        sys.exit('%s is at %s, expected %s' % (ctx.deploy_dir, head[:8], ctx.goal[:8]))
    if dirty:
        sys.exit('Uncommitted tracked changes in %s: %s' % (ctx.deploy_dir, ', '.join(dirty[:5])))


def _resolve_seconds(cli_value, env_name, wf_key, default, ctx, label):
    """First set of: CLI value > environment variable > workflow-config key > default. Returns (seconds, source)."""
    candidates = [
        (cli_value, '--' + label),
        (os.environ.get(env_name), env_name + ' env'),
        (ctx.wf.get(wf_key), wf_key + ' in workflow config'),
    ]
    for raw, source in candidates:
        if raw is None or (isinstance(raw, str) and raw.strip() == ''):
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            sys.exit('Invalid wait time %r from %s: must be a whole number of seconds (0 or more).' % (raw, source))
        if value < 0:
            sys.exit('Invalid wait time %d from %s: must be 0 or more.' % (value, source))
        return value, source
    return default, 'built-in default'


def resolve_delay(args, ctx):
    """Base wait between passes: --delay > CRD_LOOP_DELAY env > workflow loopDelaySeconds > default. Returns (seconds, source)."""
    return _resolve_seconds(args.delay, 'CRD_LOOP_DELAY', 'loopDelaySeconds', DEFAULT_DELAY_SECONDS, ctx, 'delay')


def resolve_max_delay(args, ctx):
    """Back-off ceiling: --max-delay > CRD_LOOP_MAX_DELAY env > workflow loopMaxDelaySeconds > default. Returns (seconds, source)."""
    return _resolve_seconds(args.max_delay, 'CRD_LOOP_MAX_DELAY', 'loopMaxDelaySeconds', DEFAULT_MAX_DELAY_SECONDS, ctx, 'max-delay')


def next_wait(previous_wait, base, ceiling, uploaded_ok):
    """Adaptive wait: back to the base after any progress; otherwise double (at least 2s), never above the ceiling."""
    if uploaded_ok > 0:
        return min(base, ceiling)
    return min(max(previous_wait * 2, 2, base), ceiling)


def count_uploaded_ok(output):
    return len([l for l in output.splitlines() if l.startswith('Uploading') and 'Done' in l])


def build_command(ctx):
    cmd = ['python', '%s/CheckRemoteDirty.py' % CRD_ROOT, '--workingDir', ctx.deploy_dir, '--vsGit', ctx.dirty_file,
           '--ftpConfig', ctx.ftp_config, '--gitCommitHash', ctx.goal]
    if ctx.baseline:
        cmd += ['--gitBaselineHash', ctx.tag, '--vsGitListHash', '%s..%s' % (ctx.tag, ctx.goal)]
    for pattern in ctx.excludes:
        cmd += ['--exclude', pattern]
    return cmd


def classify(output):
    rows = [l for l in output.splitlines() if '|' in l and ('MATCH' in l or 'DIFF' in l or 'MISSING' in l)]
    counts, diff = collections.Counter(), []
    for line in rows:
        if 'DIFF' in line:
            counts['DIFF'] += 1
            diff.append(line.split('|')[0].strip())
        elif 'MATCH GOAL' in line:
            counts['at goal'] += 1
        elif 'MATCH BASELINE' in line:
            counts['old (to upload)'] += 1
        else:
            counts['new (to upload)'] += 1
    return counts, diff


def norm(data):
    return re.sub(rb'[\r\n \t]', b'', data)


def is_truncated_upload(ctx, path):
    """True only if the server copy is empty or a strict prefix of the GOAL version (partial upload from a dropped link)."""
    try:
        ftp = ftplib.FTP(ctx.ftp['host'], timeout=60)
        ftp.login(ctx.ftp['user'], ctx.ftp['password'])
        buf = io.BytesIO()
        ftp.retrbinary('RETR /' + path, buf.write)
        ftp.quit()
    except Exception:
        return False
    goal = subprocess.run(['git', 'show', '%s:%s' % (ctx.goal, path)], capture_output=True, cwd=ctx.repo).stdout
    remote, wanted = norm(buf.getvalue()), norm(goal)
    return len(remote) < len(wanted) and wanted.startswith(remote)


def record_result(ctx, args):
    """Move the LOCAL deployed tag forward to the goal and fast-forward the mirror worktree. Never pushes."""
    if ctx.baseline and ctx.baseline != ctx.goal:
        anc = git(ctx, 'merge-base', '--is-ancestor', ctx.baseline, ctx.goal)
        if anc.returncode != 0 and not args.allow_non_descendant:
            print('NOT moving the tag: goal is not a descendant of the current tag.')
            return
    r = git(ctx, 'tag', '-f', ctx.tag, ctx.goal)
    print('tag %s -> %s (local only): %s' % (ctx.tag, ctx.goal[:8], (r.stdout or r.stderr).strip() or 'ok'))
    mirror = ctx.wf.get('mirrorWorktree')
    if mirror and os.path.isdir(mirror):
        m = sh(['git', 'merge', '--ff-only', ctx.tag], mirror)
        print('mirror %s: %s' % (mirror, (m.stdout.strip().splitlines() or [m.stderr.strip()])[-1]))


def cleanup(ctx):
    if os.path.exists(os.path.join(ctx.deploy_dir, 'vendor')):
        print('Not removing %s: it contains a vendor/ entry (removing could follow a junction). Remove it by hand.' % ctx.deploy_dir)
        return
    r = git(ctx, 'worktree', 'remove', '--force', ctx.deploy_dir)
    print('removed deploy worktree' if r.returncode == 0 else 'could not remove worktree: ' + r.stderr.strip())


def main():
    ap = argparse.ArgumentParser(description='Deploy a pinned commit over a flaky FTP link with CheckRemoteDirty.')
    ap.add_argument('--project', required=True, help='name of <project>_workflow.json in CRD_ROOT')
    ap.add_argument('--goal', help='commit-ish to deploy (resolved ONCE to a full hash). Default: the workflow deployBranch tip')
    ap.add_argument('--status', action='store_true', help='preflight only; upload nothing')
    ap.add_argument('--delay', type=int, default=None, help='base seconds to wait between passes (default: env CRD_LOOP_DELAY, else workflow loopDelaySeconds, else 1)')
    ap.add_argument('--max-delay', dest='max_delay', type=int, default=None, help='ceiling of the back-off after passes that upload nothing (default: env CRD_LOOP_MAX_DELAY, else workflow loopMaxDelaySeconds, else 60)')
    ap.add_argument('--max-passes', type=int, default=80)
    ap.add_argument('--stall', type=int, default=8, help='stop after N passes without progress')
    ap.add_argument('--exclude', action='append', help='extra exclude pattern (repeatable)')
    ap.add_argument('--move-tag', action='store_true', help='when finished, move the LOCAL deployed tag and fast-forward the mirror')
    ap.add_argument('--cleanup', action='store_true', help='when finished, remove the dedicated deploy worktree')
    ap.add_argument('--allow-non-descendant', action='store_true')
    args = ap.parse_args()

    ctx = load_context(args)
    guard_goal(ctx, args)
    print('project %s | goal %s | baseline %s (%s) | uploading from %s' % (
        args.project, ctx.goal, (ctx.baseline or 'none')[:8], ctx.tag, ctx.deploy_dir), flush=True)
    ensure_deploy_dir(ctx)
    cmd = build_command(ctx)
    delay, delay_source = resolve_delay(args, ctx)
    max_delay, max_source = resolve_max_delay(args, ctx)
    print('wait between passes: %ds base (%s); backs off to at most %ds (%s) while passes upload nothing' % (delay, delay_source, max_delay, max_source), flush=True)
    wait = delay

    best, stalls = None, 0
    for n in range(1, args.max_passes + 1):
        pre = sh(cmd, CRD_ROOT, 'n\n')
        counts, diff = classify(pre.stdout)
        todo = counts['DIFF'] + counts['old (to upload)'] + counts['new (to upload)']
        print('pass %d: %s -> %d file(s) still to upload' % (n, dict(counts), todo), flush=True)
        unexpected = [d for d in diff if not is_truncated_upload(ctx, d)]
        if unexpected:
            print('STOP: files that differ from BOTH the old and the new version and are not a truncated upload:')
            for d in unexpected:
                print('   ', d)
            print('Do not overwrite blindly: someone may have edited the server directly. Inspect, then decide.')
            return 2
        if todo == 0:
            print('\nALL FILES AT GOAL (%s).' % ctx.goal[:8])
            if args.move_tag:
                record_result(ctx, args)
            else:
                print('Record it (local only, never push the tag):')
                print('  git -C %s tag -f %s %s' % (ctx.repo, ctx.tag, ctx.goal))
            if args.cleanup:
                cleanup(ctx)
            print('Reminder: migrations are NOT run by this script.')
            return 0
        if args.status:
            return 0
        if best is not None and todo >= best:
            stalls += 1
            if stalls >= args.stall:
                print('STOP: no progress for %d passes. The FTP server is probably throttling; wait 10-15 minutes, re-run.' % stalls)
                return 3
        else:
            best, stalls = todo, 0
        answers = ('ra\n' if counts['DIFF'] else '') + 'Y\nY\n'
        res = sh(cmd + ['--deployOnClean'], CRD_ROOT, answers)
        started = len([l for l in res.stdout.splitlines() if l.startswith('Uploading')])
        dropped = ' - link dropped, expected' if ('Error' in res.stderr or 'Error' in res.stdout) else ''
        ok = count_uploaded_ok(res.stdout)
        wait = next_wait(wait, delay, max_delay, ok)
        print('   deploy pass finished (rc %d, %d attempted, %d uploaded)%s; waiting %ds' % (res.returncode, started, ok, dropped, wait), flush=True)
        time.sleep(wait)
    print('STOP: reached max passes; re-run to continue.')
    return 4


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nStopped by user. Safe to re-run later.')
        sys.exit(130)
