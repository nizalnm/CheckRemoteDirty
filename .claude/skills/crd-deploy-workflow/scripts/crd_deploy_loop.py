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

CHUNKS (the fix for "passes that upload nothing"): CRD compares EVERY file of the range inside one FTP process BEFORE it uploads the first byte, and
one CRD process dies around its 155th transfer (WinError 10013, exit code still 0) - a whole-range pass can therefore never reach the upload stage on
a range of 171 files. So the loop never does a whole-range FTP pass: it takes the file list from CRD's own 'Local Dirty Files' announcement (read, then
the process is killed before it transfers more than a row), and works through it in CHUNKS of --chunk N files (default 20; 0 = old whole-range passes):
each pass is a read-only preflight of just that chunk (DIFF vetting) and a deploy run of just that chunk (every other path of the range is excluded by
exact name), so a process lasts about a minute and a drop costs one chunk. A file counts as settled only when a fresh comparison in THIS run saw it at
the goal; the files uploaded in this run are compared again once nothing is pending, and only then does the deploy count as finished.

SKIP-CONFIRMED (fewer reads; used when --chunk 0): CRD downloads and hashes EVERY file of the range on every comparison, and a loop pass compares twice (the preflight and
the deploy run's own table). So once a file is confirmed at the goal (MATCH GOAL in a preflight, or uploaded and verified by CRD in this run) it is
excluded - by exact path - from later passes of THIS run (the goal is pinned, so nothing can legitimately change it). When nothing is left to upload the
loop runs ONE full, unskipped verification pass over the whole range; only that pass can finish the deploy. If it finds a file no longer at the goal
(something rewrote it meanwhile) the loop carries on with that file. --no-skip-confirmed turns the skipping off (every pass compares everything).

HEARTBEAT (so a long pass never looks stuck): CRD's output is streamed live (the child runs unbuffered) and a heartbeat line is printed
every --heartbeat seconds (default 15; env CRD_LOOP_HEARTBEAT; 0 = off), e.g.
  [hb 12:03:41] pass 2/80 DEPLOY | 37/123 uploaded (30%), 4.1 files/min, ETA ~21m | now: public/x.php (14s) | last ok: y.php 9s ago | drops 1 | alive
while a pass is waiting: "[hb ..] pass 2 WAITING 7s left (back-off) ...". Each uploaded file also prints one line "ok [37/123] path".
The same state is written, atomically, to <CRD_ROOT>/<project>_deploy_progress.json every heartbeat (state running|finished|stopped,
phase, pass, counts, current file, seconds since last output, pid, last_update) - read it from ANOTHER terminal when the caller pipes this
script's stdout through something that buffers (e.g. `| Select-Object -Last 40` or `| tail`, which show nothing until the script exits;
prefer not to pipe, or run with `python -u` and redirect to a file). --progress-file overrides the path.
WATCHDOG: if the CRD child prints nothing for --hang-timeout seconds (default 900; 0 = never) it is killed, the pass counts as a dropped link
and the loop continues (re-runs only upload what is still missing).

It does NOT run database migrations. It never pushes anything (the deployed tag is local-only by policy).
"""
import argparse
import collections
import datetime
import ftplib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import types

CRD_ROOT = os.environ.get('CRD_ROOT', r'C:\www\CheckRemoteDirty').replace('\\', '/')
DEFAULT_LOOP_EXCLUDES = ['dev/tests/*', 'tests/*']
DEFAULT_DELAY_SECONDS = 1        # base wait between passes: start aggressive, back off only when a pass gets nothing through
DEFAULT_MAX_DELAY_SECONDS = 60   # ceiling for the back-off
DEFAULT_HEARTBEAT_SECONDS = 15   # how often a "still working" line is printed (0 = off)
DEFAULT_HANG_TIMEOUT_SECONDS = 900  # kill a CRD child that printed nothing for this long (0 = never)


class Ctx:
    """Everything resolved once at start."""


def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm%02ds' % (seconds // 60, seconds % 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


class Progress:
    """Live state of the run, updated by the CRD output readers and reported by the heartbeat thread."""

    def __init__(self, project, goal, max_passes, progress_file, heartbeat, hang_timeout):
        self.project, self.goal, self.max_passes = project, goal, max_passes
        self.progress_file, self.heartbeat, self.hang_timeout = progress_file, heartbeat, hang_timeout
        self.lock = threading.Lock()
        self.started = time.time()
        self.state = 'running'
        self.phase = 'starting'          # starting | preflight | deploy | waiting | done
        self.pass_no = 0
        self.pass_started = time.time()
        self.rows_seen = 0               # preflight table rows read so far in this pass
        self.todo_total = None           # files to upload, from the FIRST preflight
        self.files_total = None          # files of the range announced by CRD (chunked mode)
        self.settled = 0                 # files of the range no longer pending (chunked mode)
        self.uploaded_total = 0          # files confirmed uploaded over the whole run
        self.attempted_pass = 0
        self.in_flight = None            # (path, since)
        self.last_ok = None              # (path, ts)
        self.first_ok_ts = None
        self.drops = 0
        self.last_output = time.time()
        self.wait_until = None
        self.wait_reason = ''
        self.pid = None
        self.killed_hangs = 0
        self.last_error = ''

    # ---- fed by the output readers -------------------------------------------------------------------------------------
    def touch(self):
        self.last_output = time.time()

    def on_line(self, line):
        s = line.strip()
        if s.startswith('Uploading'):
            m = re.match(r'Uploading (.+?) \.\.\.', s)
            path = m.group(1) if m else s[10:80]
            if 'Done' in s:
                with self.lock:
                    self.uploaded_total += 1
                    self.attempted_pass += 1
                    now = time.time()
                    took = now - (self.in_flight[1] if self.in_flight and self.in_flight[0] == path else now)
                    self.last_ok = (path, now)
                    self.first_ok_ts = self.first_ok_ts or now
                    self.in_flight = None
                    total = self.todo_total
                if total:
                    print('   ok [%d/%s] %s (%s)' % (self.uploaded_total, total, path, fmt_dur(took)), flush=True)
                else:
                    print('   ok [%d uploaded, %d/%s settled] %s (%s)' % (self.uploaded_total, self.settled, self.files_total or '?', path, fmt_dur(took)), flush=True)
            else:
                with self.lock:
                    self.attempted_pass += 1
                    self.drops += 1
                    self.in_flight = None
                    self.last_error = s[:160]
                print('   !! upload did not finish: %s' % s[:160], flush=True)
        elif '|' in s and ('MATCH' in s or 'DIFF' in s or 'MISSING' in s):
            with self.lock:
                self.rows_seen += 1

    def on_partial(self, buf):
        """The unfinished line: CRD prints 'Uploading <path> ... ' and only adds 'Done' when the transfer completes."""
        s = buf.strip()
        if s.startswith('Uploading'):
            m = re.match(r'Uploading (.+?) \.\.\.', s)
            if m:
                with self.lock:
                    if not self.in_flight or self.in_flight[0] != m.group(1):
                        self.in_flight = (m.group(1), time.time())

    # ---- reporting -----------------------------------------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.started
            rate = None
            if self.first_ok_ts and self.uploaded_total >= 2 and now > self.first_ok_ts:
                rate = self.uploaded_total / max(1.0, (now - self.first_ok_ts) / 60.0)
            eta = None
            if rate and self.todo_total and self.todo_total > self.uploaded_total:
                eta = (self.todo_total - self.uploaded_total) / rate * 60.0
            return {
                'project': self.project, 'goal': self.goal, 'state': self.state, 'phase': self.phase, 'pid': self.pid,
                'pass': self.pass_no, 'max_passes': self.max_passes, 'elapsed_s': int(elapsed), 'pass_elapsed_s': int(now - self.pass_started),
                'todo_total': self.todo_total, 'files_total': self.files_total, 'settled': self.settled, 'uploaded_total': self.uploaded_total, 'attempted_this_pass': self.attempted_pass,
                'preflight_rows_read': self.rows_seen, 'files_per_min': round(rate, 1) if rate else None, 'eta_s': int(eta) if eta else None,
                'in_flight': {'path': self.in_flight[0], 'for_s': int(now - self.in_flight[1])} if self.in_flight else None,
                'last_ok': {'path': self.last_ok[0], 'ago_s': int(now - self.last_ok[1])} if self.last_ok else None,
                'drops': self.drops, 'hang_kills': self.killed_hangs, 'last_error': self.last_error,
                'seconds_since_last_output': int(now - self.last_output),
                'wait_left_s': int(max(0, self.wait_until - now)) if self.wait_until and self.phase == 'waiting' else None, 'wait_reason': self.wait_reason,
                'last_update': datetime.datetime.now().isoformat(timespec='seconds'),
            }

    def line(self):
        d = self.snapshot()
        head = '[hb %s] pass %d/%d ' % (time.strftime('%H:%M:%S'), d['pass'], d['max_passes'])
        if d['phase'] == 'waiting':
            return head + 'WAITING %ds left (%s) | %s | total %s' % (d['wait_left_s'] or 0, d['wait_reason'], self._uploaded_text(d), fmt_dur(d['elapsed_s']))
        if d['phase'] == 'preflight':
            return head + 'PREFLIGHT (read-only, compares each file with the server) | %d rows read | %s | CRD last output %ds ago | pass %s' % (
                d['preflight_rows_read'], 'alive' if d['seconds_since_last_output'] < 120 else 'QUIET - watchdog at %ds' % self.hang_timeout,
                d['seconds_since_last_output'], fmt_dur(d['pass_elapsed_s']))
        now_txt = 'now: %s (%s)' % (d['in_flight']['path'], fmt_dur(d['in_flight']['for_s'])) if d['in_flight'] else 'now: connecting / verifying'
        ok_txt = 'last ok: %s %s ago' % (os.path.basename(d['last_ok']['path']), fmt_dur(d['last_ok']['ago_s'])) if d['last_ok'] else 'no file finished yet'
        rate = ', %.1f files/min' % d['files_per_min'] if d['files_per_min'] else ''
        eta = ', ETA ~%s' % fmt_dur(d['eta_s']) if d['eta_s'] else ''
        quiet = '' if d['seconds_since_last_output'] < 120 else ' | QUIET %s (watchdog at %ds)' % (fmt_dur(d['seconds_since_last_output']), self.hang_timeout)
        return head + 'DEPLOY | %s%s%s | %s | %s | drops %d%s | total %s' % (self._uploaded_text(d), rate, eta, now_txt, ok_txt, d['drops'], quiet, fmt_dur(d['elapsed_s']))

    @staticmethod
    def _uploaded_text(d):
        if d.get('files_total'):
            return '%d/%d settled (%d uploaded)' % (d['settled'], d['files_total'], d['uploaded_total'])
        if d['todo_total']:
            return '%d/%d uploaded (%d%%)' % (d['uploaded_total'], d['todo_total'], 100 * d['uploaded_total'] // max(1, d['todo_total']))
        return '%d uploaded' % d['uploaded_total']

    def write_file(self):
        try:
            tmp = self.progress_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.snapshot(), f, indent=1)
            os.replace(tmp, self.progress_file)
        except OSError:
            pass  # the progress file is a convenience; never break the deploy over it


def heartbeat_loop(prog, stop):
    prog.write_file()
    while not stop.wait(prog.heartbeat if prog.heartbeat > 0 else 15):
        if prog.heartbeat > 0:
            print(prog.line(), flush=True)
        prog.write_file()


def run_streaming(args, cwd, input_text, prog, phase):
    """Like sh(), but the child runs UNBUFFERED, its output is parsed live into `prog`, and it is killed if it goes silent for
    prog.hang_timeout seconds. Returns an object with returncode / stdout / stderr like subprocess.run(capture_output=True, text=True)."""
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
    prog.phase = phase
    prog.rows_seen, prog.attempted_pass, prog.in_flight = 0, 0, None
    prog.touch()
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    prog.pid = proc.pid
    out_chunks, err_chunks = [], []

    def pump(stream, sink, is_out):
        buf = ''
        while True:
            try:
                chunk = os.read(stream.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode('utf-8', errors='replace')
            sink.append(text)
            prog.touch()
            if is_out:
                buf += text
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    prog.on_line(line.rstrip('\r'))
                prog.on_partial(buf)
        if is_out and buf.strip():
            prog.on_line(buf.rstrip('\r'))

    threads = [threading.Thread(target=pump, args=(proc.stdout, out_chunks, True), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_chunks, False), daemon=True)]
    for t in threads:
        t.start()
    try:
        if input_text:
            proc.stdin.write(input_text.encode())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    killed = False
    while proc.poll() is None:
        time.sleep(0.5)
        if prog.hang_timeout > 0 and time.time() - prog.last_output > prog.hang_timeout:
            killed = True
            prog.killed_hangs += 1
            prog.drops += 1
            print('   !! CRD printed nothing for %s: killing it (pid %d). The pass counts as a dropped link; the loop re-runs and uploads only what is still missing.'
                  % (fmt_dur(prog.hang_timeout), proc.pid), flush=True)
            proc.kill()
            break
    proc.wait()
    for t in threads:
        t.join(timeout=5)
    prog.pid = None
    stderr = ''.join(err_chunks) + ('\nError: killed by the hang watchdog' if killed else '')
    return types.SimpleNamespace(returncode=proc.returncode, stdout=''.join(out_chunks), stderr=stderr)


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


def classify_full(output):
    """(counts, paths with a DIFF, paths MATCH GOAL) from a CRD table."""
    rows = [l for l in output.splitlines() if '|' in l and ('MATCH' in l or 'DIFF' in l or 'MISSING' in l)]
    counts, diff, at_goal = collections.Counter(), [], []
    for line in rows:
        if 'DIFF' in line:
            counts['DIFF'] += 1
            diff.append(line.split('|')[0].strip())
        elif 'MATCH GOAL' in line:
            counts['at goal'] += 1
            at_goal.append(line.split('|')[0].strip())
        elif 'MATCH BASELINE' in line:
            counts['old (to upload)'] += 1
        else:
            counts['new (to upload)'] += 1
    return counts, diff, at_goal


def classify(output):
    counts, diff, _ = classify_full(output)
    return counts, diff


def classify_rows(output):
    """Ordered [(path, kind)] with kind in goal | diff | old | new, from a CRD table."""
    out = []
    for line in output.splitlines():
        if '|' in line and ('MATCH' in line or 'DIFF' in line or 'MISSING' in line):
            path = line.split('|')[0].strip()
            kind = 'diff' if 'DIFF' in line else ('goal' if 'MATCH GOAL' in line else ('old' if 'MATCH BASELINE' in line else 'new'))
            out.append((path, kind))
    return out


def expected_rows(output):
    """How many files CRD announced in its 'Local Dirty Files' list: the comparison table must have exactly that many rows to be complete."""
    return len([l for l in output.splitlines() if re.search(r'\| Git: .* \| Local: ', l)])


def table_complete(res, rows):
    """A preflight is usable when its table has a row for every announced file (a non-zero exit code or a late FTP error does not matter then).
    Without an announced count, fall back to 'has rows and no link-error text'."""
    exp = expected_rows(res.stdout or '')
    if exp:
        return len(rows) >= exp
    return bool(rows) and not link_error(res, use_rc=False)


def link_error(res, use_rc=True):
    text = (res.stdout or '') + (res.stderr or '')
    return (use_rc and res.returncode != 0) or 'FTP Error' in text or 'Traceback' in text or 'WinError' in text


def uploaded_paths(output):
    """Paths CRD reports as uploaded ('Uploading <path> ... Done'); CRD verifies each after the upload."""
    out = []
    for line in output.splitlines():
        if line.startswith('Uploading') and 'Done' in line:
            m = re.match(r'Uploading (.+?) \.\.\.', line)
            if m:
                out.append(m.group(1))
    return out


def skippable(path):
    """Only plain paths are excluded by exact name (an fnmatch metacharacter would match more than the one file)."""
    return bool(path) and not any(c in path for c in '*?[]')


def with_skips(cmd, confirmed):
    extra = []
    for path in sorted(confirmed):
        extra += ['--exclude', path]
    return cmd + extra


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


def is_known_history_version(ctx, path):
    """True when the server copy is byte-identical (whitespace-normalised) to this file at SOME commit in the goal's own
    history: someone deployed an intermediate version of our code surgically. Overwriting it with the goal (a descendant)
    loses nothing. A server-side edit that never existed in git does not match and still STOPs the loop."""
    try:
        ftp = ftplib.FTP(ctx.ftp['host'], timeout=60)
        ftp.login(ctx.ftp['user'], ctx.ftp['password'])
        buf = io.BytesIO()
        ftp.retrbinary('RETR /' + path, buf.write)
        ftp.quit()
    except Exception:
        return False
    remote = norm(buf.getvalue())
    revs = sh(['git', 'log', '--format=%H', '-n', '200', ctx.goal, '--', path], ctx.repo).stdout.split()
    for rev in revs:
        blob = subprocess.run(['git', 'show', '%s:%s' % (rev, path)], capture_output=True, cwd=ctx.repo).stdout
        if norm(blob) == remote:
            print('   %s: server copy equals the version at %s (own history) - safe to replace' % (path, rev[:8]), flush=True)
            return True
    return False


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
    ap.add_argument('--chunk', type=int, default=20, help='upload at most N files per CRD session (default 20; 0 = whole-range passes, which a dropped link can waste entirely)')
    ap.add_argument('--no-skip-confirmed', dest='no_skip_confirmed', action='store_true', help='compare every file of the range on every pass (default: files confirmed at the goal in this run are skipped until the final full verification pass)')
    ap.add_argument('--heartbeat', type=int, default=None, help='seconds between "still working" lines (default: env CRD_LOOP_HEARTBEAT, else 15; 0 = off)')
    ap.add_argument('--hang-timeout', dest='hang_timeout', type=int, default=None, help='kill a CRD run that printed nothing for N seconds (default: env CRD_LOOP_HANG_TIMEOUT, else 900; 0 = never)')
    ap.add_argument('--progress-file', dest='progress_file', default=None, help='JSON progress file (default: <CRD_ROOT>/<project>_deploy_progress.json)')
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
    hb, _ = _resolve_seconds(args.heartbeat, 'CRD_LOOP_HEARTBEAT', 'loopHeartbeatSeconds', DEFAULT_HEARTBEAT_SECONDS, ctx, 'heartbeat')
    hang, _ = _resolve_seconds(args.hang_timeout, 'CRD_LOOP_HANG_TIMEOUT', 'loopHangTimeoutSeconds', DEFAULT_HANG_TIMEOUT_SECONDS, ctx, 'hang-timeout')
    progress_file = (args.progress_file or '%s/%s_deploy_progress.json' % (CRD_ROOT, args.project)).replace('/', os.sep)
    prog = Progress(args.project, ctx.goal, args.max_passes, progress_file, hb, hang)
    stop_hb = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(prog, stop_hb), daemon=True).start()
    print('heartbeat every %ds | hang watchdog %s | progress file %s' % (hb, ('%ds' % hang) if hang else 'off', progress_file), flush=True)
    try:
        if args.chunk > 0 and not args.status:
            return run_chunked(args, ctx, cmd, prog, delay, max_delay, wait)
        return run_passes(args, ctx, cmd, prog, delay, max_delay, wait)
    finally:
        prog.phase = 'done'
        if prog.state == 'running':
            prog.state = 'stopped'
        stop_hb.set()
        prog.write_file()


def run_passes(args, ctx, cmd, prog, delay, max_delay, wait):
    best, stalls = None, 0
    base_cmd = cmd
    confirmed = set()          # paths confirmed at the goal during THIS run (skipped by later passes)
    full_check = False         # the next preflight is the final, unskipped verification of the whole range
    use_skip = not args.no_skip_confirmed and not args.status
    for n in range(1, args.max_passes + 1):
        prog.pass_no, prog.pass_started = n, time.time()
        skipped_now = use_skip and not full_check and bool(confirmed)
        cmd = with_skips(base_cmd, confirmed) if skipped_now else base_cmd
        if use_skip and confirmed and not full_check:
            print('pass %d: skipping %d file(s) already confirmed at the goal in this run' % (n, len(confirmed)), flush=True)
        if full_check:
            print('pass %d: FULL VERIFICATION of the whole range (no skipping)' % n, flush=True)
        pre = run_streaming(cmd, CRD_ROOT, 'n\n', prog, 'preflight')
        counts, diff, at_goal = classify_full(pre.stdout)
        if use_skip:
            if full_check:
                confirmed = {p for p in at_goal if skippable(p)}   # whatever the full pass sees is the truth
            else:
                confirmed |= {p for p in at_goal if skippable(p)}
        todo = counts['DIFF'] + counts['old (to upload)'] + counts['new (to upload)']
        if prog.todo_total is None:
            prog.todo_total = todo
        print('pass %d: %s -> %d file(s) still to upload' % (n, dict(counts), todo), flush=True)
        unexpected = [d for d in diff if not (is_truncated_upload(ctx, d) or is_known_history_version(ctx, d))]
        if unexpected:
            print('STOP: files that differ from BOTH the old and the new version and are neither a truncated upload nor any version in git history:')
            for d in unexpected:
                print('   ', d)
            print('Do not overwrite blindly: someone may have edited the server directly. Inspect, then decide.')
            return 2
        if todo == 0 and skipped_now:
            print('nothing left outside the %d confirmed file(s): running one full verification pass' % len(confirmed), flush=True)
            full_check = True
            continue
        full_check = False
        if todo == 0:
            prog.state = 'finished'
            print('\nALL FILES AT GOAL (%s), verified by a full unskipped pass.' % ctx.goal[:8])
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
        res = run_streaming(cmd + ['--deployOnClean'], CRD_ROOT, answers, prog, 'deploy')
        if use_skip:
            confirmed |= {p for p in uploaded_paths(res.stdout) if skippable(p)}
        started = len([l for l in res.stdout.splitlines() if l.startswith('Uploading')])
        dropped = ' - link dropped, expected' if ('Error' in res.stderr or 'Error' in res.stdout) else ''
        ok = count_uploaded_ok(res.stdout)
        wait = next_wait(wait, delay, max_delay, ok)
        print('   deploy pass finished (rc %d, %d attempted, %d uploaded)%s; waiting %ds' % (res.returncode, started, ok, dropped, wait), flush=True)
        prog.phase, prog.wait_until = 'waiting', time.time() + wait
        prog.wait_reason = 'progress, base wait' if ok > 0 else 'back-off: nothing got through'
        time.sleep(wait)
    print('STOP: reached max passes; re-run to continue.')
    return 4


def read_range_listing(base_cmd, timeout=120):
    """The files of the range as CRD itself announces them ('Local Dirty Files', printed BEFORE any FTP comparison). The CRD process is killed as soon
    as its first comparison row appears, so this costs (almost) no FTP transfers - a whole-range comparison in one process dies around its 155th file."""
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen(base_cmd, cwd=CRD_ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    try:
        proc.stdin.close()
    except OSError:
        pass
    paths, buf, started, done = [], b'', time.time(), False
    while not done and time.time() - started < timeout:
        try:
            piece = os.read(proc.stdout.fileno(), 4096)
        except OSError:
            break
        if not piece:
            break
        buf += piece
        while b'\n' in buf:
            raw, buf = buf.split(b'\n', 1)
            text = raw.decode('utf-8', 'replace').rstrip('\r')
            m = re.match(r'^(.*?)\s+\| Git: .* \| Local: ', text)
            if m:
                paths.append(m.group(1).strip())
            elif '|' in text and ('MATCH' in text or 'DIFF' in text or 'MISSING' in text):
                done = True
                break
    proc.kill()
    proc.wait()
    return paths


def finish_run(ctx, args, prog):
    prog.state = 'finished'
    print('\nALL FILES AT GOAL (%s), verified by fresh comparisons in this run.' % ctx.goal[:8])
    if args.move_tag:
        record_result(ctx, args)
    else:
        print('Record it (local only, never push the tag):')
        print('  git -C %s tag -f %s %s' % (ctx.repo, ctx.tag, ctx.goal))
    if args.cleanup:
        cleanup(ctx)
    print('Reminder: migrations are NOT run by this script.')
    return 0


def run_chunked(args, ctx, base_cmd, prog, delay, max_delay, wait):
    """Never a whole-range FTP pass: take the file list from CRD's own announcement, then compare + upload CHUNK files at a time (see the CHUNKS note).
    Every file must be seen at the goal by a fresh comparison in THIS run, and every file uploaded here is compared again at the end."""
    size = args.chunk
    all_paths = []
    for _ in range(3):
        all_paths = read_range_listing(base_cmd)
        if all_paths:
            break
        time.sleep(2)
    if not all_paths:
        print('STOP: CRD did not announce any file for this range (nothing to deploy, or CRD failed before listing).')
        return 3
    prog.files_total = len(all_paths)
    print('range: CRD announced %d file(s); working in chunks of %d' % (len(all_paths), size), flush=True)
    pending = list(all_paths)      # state unknown until a chunk comparison says otherwise
    uploaded = []                  # uploaded in this run (re-verified at the end)
    verify_queue, verifying, stalls = [], False, 0

    def back_off(reason):
        nonlocal wait
        wait = next_wait(wait, delay, max_delay, 0)
        prog.phase, prog.wait_until, prog.wait_reason = 'waiting', time.time() + wait, reason
        time.sleep(wait)

    for n in range(1, args.max_passes + 1):
        prog.pass_no, prog.pass_started = n, time.time()
        prog.settled = len(all_paths) - len(pending)
        if verifying and not verify_queue:
            if not pending:
                return finish_run(ctx, args, prog)
            verifying = False
        if not verifying and not pending:
            if uploaded:
                verifying, verify_queue = True, sorted(set(uploaded))
                print('pass %d: nothing pending: re-comparing the %d file(s) uploaded in this run' % (n, len(verify_queue)), flush=True)
            else:
                return finish_run(ctx, args, prog)
        queue = verify_queue if verifying else pending
        chunk = queue[:size]
        keep = set(chunk)
        cmd = base_cmd
        for path in all_paths:
            if path not in keep and skippable(path):
                cmd = cmd + ['--exclude', path]
        print('pass %d: %s of %d file(s) (%d pending, %d settled of %d)' % (n, 'VERIFY' if verifying else 'chunk', len(chunk), len(pending), prog.settled, len(all_paths)), flush=True)
        pre = run_streaming(cmd, CRD_ROOT, 'n\n', prog, 'preflight')
        rows = classify_rows(pre.stdout)
        counts, diff, _ = classify_full(pre.stdout)
        if not table_complete(pre, rows):
            stalls += 1
            print('   chunk preflight incomplete (%d of %s rows read): retrying' % (len(rows), expected_rows(pre.stdout) or '?'), flush=True)
            if stalls >= args.stall:
                print('STOP: no progress for %d passes. The FTP server is probably throttling; wait 10-15 minutes, re-run.' % stalls)
                return 3
            back_off('back-off: preflight died')
            continue
        unexpected = [d for d in diff if not (is_truncated_upload(ctx, d) or is_known_history_version(ctx, d))]
        if unexpected:
            print('STOP: files that differ from BOTH the old and the new version and are neither a truncated upload nor any version in git history:')
            for d in unexpected:
                print('   ', d)
            print('Do not overwrite blindly: someone may have edited the server directly. Inspect, then decide.')
            return 2
        for p, k in rows:
            if k == 'goal':
                if p in pending:
                    pending.remove(p)
                if p in verify_queue:
                    verify_queue.remove(p)
        todo = [p for p, k in rows if k != 'goal']
        if verifying:
            if todo:
                print('   verification found %d file(s) not at the goal: back to uploading them' % len(todo), flush=True)
                for p in todo:
                    if p not in pending:
                        pending.append(p)
                    if p in verify_queue:
                        verify_queue.remove(p)
                verifying = False
            stalls = 0
            continue
        stalls = 0
        if not todo:
            continue
        answers = ('ra\n' if counts['DIFF'] else '') + 'Y\nY\n'
        res = run_streaming(cmd + ['--deployOnClean'], CRD_ROOT, answers, prog, 'deploy')
        done = uploaded_paths(res.stdout)
        for p in done:
            if p in pending:
                pending.remove(p)
            uploaded.append(p)
        ok = len(done)
        started = len([l for l in res.stdout.splitlines() if l.startswith('Uploading')])
        wait = next_wait(wait, delay, max_delay, ok)
        print('   chunk done (rc %d, %d attempted, %d uploaded, %d still pending)%s; waiting %ds' % (
            res.returncode, started, ok, len(pending), ' - link dropped, expected' if link_error(res) else '', wait), flush=True)
        stalls = 0 if ok > 0 else stalls + 1
        if stalls >= args.stall:
            print('STOP: no upload got through for %d passes. The FTP server is probably throttling; wait 10-15 minutes, re-run.' % stalls)
            return 3
        prog.phase, prog.wait_until = 'waiting', time.time() + wait
        prog.wait_reason = 'progress, base wait' if ok > 0 else 'back-off: nothing got through'
        time.sleep(wait)
    print('STOP: reached max passes; re-run to continue.')
    return 4


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nStopped by user. Safe to re-run later.')
        sys.exit(130)
