<?php
/**
 * CRD Stage 1 remote-side mtime bubble-up scanner - TEMPLATE.
 *
 * Deploy a copy of this file to a project's own web root (same level as its
 * front controller / index file) to let remote_manifest_precheck.py skip or
 * narrow Stage 1's FTP walk instead of always walking the whole remote tree
 * one directory/file at a time over FTP. See crd-deploy-workflow/SKILL.md's
 * "Remote-side manifest scanner" section for the full design rationale and
 * how this wires into the rest of the pipeline - this file only needs the
 * two edits marked BEFORE DEPLOYING below.
 *
 * Walks the project's own directory tree LOCALLY on the server (no
 * per-directory network round trip - that's the whole point) and, for every
 * directory, records the bubbled-up latest mtime among: its own mtime
 * (catches deletions - removing an entry changes the parent dir's own mtime
 * even though no remaining file's mtime changes), its direct child files'
 * mtimes, and its direct child directories' own already-bubbled values
 * (post-order, so children are always done before their parent is
 * finalized).
 *
 * Idempotent / resumable: state lives entirely in one SQLite file (crash-safe
 * by construction - a killed process rolls back its own uncommitted batch,
 * never corrupts the file). Each invocation processes stack frames for up to
 * TIME_BUDGET_SECONDS, commits what it finished, and returns. A later
 * invocation - whether the previous one paused itself on purpose or was
 * killed by the host mid-flight - just keeps going from the persisted
 * walk_stack. See ?action=status for a no-op progress check.
 *
 * NOT linked from anywhere; gated by a secret token, checked with
 * hash_equals() (timing-safe) and hour-salted (see below) so a captured
 * token stops working within at most two hours. Wrong/missing token returns
 * a bare 404 rather than any error detail, so an unauthenticated probe
 * learns nothing.
 *
 * The manifest itself (.crd_stage1/manifest.sqlite) is additionally blocked
 * from direct HTTP access by a sibling .htaccess this script writes on first
 * run - defense in depth on top of the token gate, same pattern CRD's own
 * docs already require for .crd-trash/. This has been verified against a
 * real host to actually return 403 (Apache honoring .htaccess is not a given
 * on every host - AllowOverride can be restricted - so re-verify this on any
 * new host after deploying, e.g. by fetching .crd_stage1/manifest.sqlite
 * directly).
 */

// ---- config -----------------------------------------------------------

// BEFORE DEPLOYING (1/2): generate a fresh, project-specific secret - e.g.
// `python -c "import secrets; print(secrets.token_hex(32))"` - and put it
// here. Never reuse a secret across projects, never commit the real value
// anywhere (this file, once filled in, must not itself be committed - keep
// the deployed copy only on the server and record the secret in that
// project's own workflow.json, e.g. CRD_ROOT/remotes/<project>_workflow.json,
// which is already gitignored). The token actually checked on each request
// is NOT this value directly; see hour-salting below. Rotating this
// invalidates every derived token immediately, past and future.
$BASE_SECRET = 'REPLACE_WITH_A_FRESH_64_CHAR_HEX_SECRET_BEFORE_DEPLOYING';

$TIME_BUDGET_SECONDS = 20.0;   // stay safely under a typical 30s max_execution_time
$HEARTBEAT_STALE_AFTER = 15;   // seconds; 'running' older than this => treat as crashed, take over
$COMMIT_EVERY_SECONDS = 2.0;   // how often to commit + refresh the heartbeat mid-run

// Floor between FRESH starts (action=reset, or a first-ever bootstrap) - not
// a general request-rate cap. Status checks and resuming an already
// in-progress walk are exempt (see the check itself, below). This is a
// last-resort circuit breaker against a valid-but-abused token being fired
// in a loop; it is not the primary rate governance for this tool - that's
// the client-side stage1_gate.py's once-daily gate, which operates on a much
// longer timescale than this.
$MIN_FRESH_START_INTERVAL_SECONDS = 15;

// Assumes this script sits at the project web root (same level as its front
// controller). Adjust if placed elsewhere.
$ROOT_DIR = __DIR__;
$DB_DIR = __DIR__ . '/.crd_stage1';
$DB_PATH = $DB_DIR . '/manifest.sqlite';
$SELF_BASENAME = basename(__FILE__);

// BEFORE DEPLOYING (2/2): keep this in sync BY HAND with the project's own
// excludePatterns in its workflow.json - two copies of the same list, one
// here and one there, not read from a single source of truth (a future
// version could have the client pass its own excludePatterns on every
// trigger instead, removing the duplication - see SKILL.md's Stage 1
// section for the tradeoff, this wasn't built yet as of this template's
// writing). This starting list covers what's cross-project-common
// (never-relevant tooling/VCS paths); add the project's own runtime-
// regenerated files (a QR code image, a cache dir, etc.) alongside them.
//
// Matched the same way the client's is_excluded()/should_prune_dir() do it:
// glob against BOTH the full relative path and the bare basename, '*' free
// to cross '/' (no FNM_PATHNAME) - same semantics as Python's fnmatch there.
$EXCLUDE_PATTERNS = array(
    '.agents', '.agents/*',
    '.agent', '.agent/*',
    '.beads', '.beads/*',
    '.ralph-tui', '.ralph-tui/*',
    'tasks', 'tasks/*',
    'userfiles', 'userfiles/*',
    '.ftpquota',
    '.well-known', '.well-known/*',
    'error_log',
    '*.log',
    'dev', 'dev/*',
    '.git', '.git/*',
    '.svn', '.svn/*',
    $SELF_BASENAME,        // this scanner's own updates aren't "site drift"
);

// ---- auth ---------------------------------------------------------------

header('Content-Type: application/json');

$token = isset($_POST['token']) ? $_POST['token'] : (isset($_GET['token']) ? $_GET['token'] : '');

// Hour-salted: the accepted token is sha256(base secret . GMT hour), not the
// base secret itself. Checked against the current hour AND the previous one
// (a full hour of slack) so a request landing right at the boundary, or a
// caller whose clock drifted slightly, doesn't get spuriously rejected -
// same ±1-step tolerance real TOTP/authenticator schemes use. A token
// captured from an old log/history entry stops working within at most two
// hours instead of being valid forever.
function hour_token($base, $hourString) {
    return hash('sha256', $base . $hourString);
}

$nowGmHour = gmdate('YmdH');
$prevGmHour = gmdate('YmdH', time() - 3600);
$validNow = hour_token($BASE_SECRET, $nowGmHour);
$validPrev = hour_token($BASE_SECRET, $prevGmHour);

$authOk = is_string($token) && (hash_equals($validNow, $token) || hash_equals($validPrev, $token));
if (!$authOk) {
    http_response_code(404);
    echo json_encode(array('error' => 'not found'));
    exit;
}

if (!extension_loaded('sqlite3')) {
    echo json_encode(array('error' => 'sqlite3 extension not available on this host'));
    exit;
}

// ---- storage setup --------------------------------------------------------

if (!is_dir($DB_DIR)) {
    @mkdir($DB_DIR, 0755, true);
}
$htaccess = $DB_DIR . '/.htaccess';
if (!file_exists($htaccess)) {
    file_put_contents(
        $htaccess,
        "# Blocks direct HTTP access to the manifest; the trigger script is the only intended access path.\n" .
        "Require all denied\n" .
        "Order deny,allow\n" .
        "Deny from all\n"
    );
}

$db = new SQLite3($DB_PATH);
$db->busyTimeout(5000);
$db->exec('PRAGMA journal_mode = WAL');
$db->exec('PRAGMA synchronous = NORMAL');

init_schema($db);

// ---- dispatch -------------------------------------------------------------

$action = isset($_GET['action']) ? $_GET['action'] : 'run';

if ($action === 'status') {
    echo json_encode(get_status($db));
    $db->close();
    exit;
}

if ($action === 'dump') {
    // Only directories TOUCHED since ?watermark= - not the whole tree. By
    // the bubbling invariant, a directory's bubbled_max_mtime can only
    // exceed watermark if at least one of its own files/subdirs does, which
    // recursively means every ancestor up to the root is touched too - the
    // touched set is always a complete set of root-to-leaf paths, no gaps.
    //
    // Deliberately does NOT also enumerate each touched node's STALE
    // children by name - the client already independently knows the full
    // local (git-tracked) directory structure, so it can derive "stale" by
    // simple set subtraction (any local child not in this touched set) far
    // more cheaply than this endpoint naming every stale sibling one by one,
    // which could get long for a directory with many children. This
    // response is just the small touched set, nothing else.
    //
    // Read-only, exempt from rate limiting same as 'status' - never triggers
    // any walk work. Only meaningful once status is 'done'; mid-flight this
    // is a partial/stale view - caller should check status first.
    if (!isset($_GET['watermark']) || !ctype_digit((string)$_GET['watermark'])) {
        echo json_encode(array('error' => 'dump requires an integer watermark query param'));
        $db->close();
        exit;
    }
    $watermark = (int)$_GET['watermark'];

    $touched = array();
    $stmt = $db->prepare('SELECT path, bubbled_max_mtime, bubbled_max_path FROM dirs WHERE bubbled_max_mtime > :wm ORDER BY path');
    $stmt->bindValue(':wm', $watermark, SQLITE3_INTEGER);
    $rows = $stmt->execute();
    while ($row = $rows->fetchArray(SQLITE3_ASSOC)) {
        $touched[] = array(
            'path' => $row['path'],
            'bubbled_max_mtime' => (int)$row['bubbled_max_mtime'],
            'bubbled_max_path' => $row['bubbled_max_path'],
        );
    }

    $meta = get_meta($db);
    echo json_encode(array('status' => $meta['status'], 'watermark' => $watermark, 'touched' => $touched));
    $db->close();
    exit;
}

// Rate-limit FRESH starts only (action=reset, or the very first bootstrap on
// an untouched project) - never a status check, never the normal multi-call
// resume of an already in-progress walk (that's the intended flow, not
// abuse). Read meta BEFORE the reset drop below, so a rate-limited reset
// request doesn't destroy the existing manifest for nothing - it's rejected
// with the old state left completely intact.
$preMeta = get_meta($db);
$now = time();
$wouldBeFreshStart = ($action === 'reset') || ($preMeta['status'] === 'idle');

if ($wouldBeFreshStart && $preMeta['started_at'] !== '') {
    $sinceLastStart = $now - (int)$preMeta['started_at'];
    if ($sinceLastStart < $MIN_FRESH_START_INTERVAL_SECONDS) {
        http_response_code(429);
        echo json_encode(array(
            'state' => 'rate_limited',
            'detail' => 'a walk was started ' . $sinceLastStart . 's ago - minimum interval between '
                . 'fresh starts is ' . $MIN_FRESH_START_INTERVAL_SECONDS . 's (status checks and '
                . 'resuming an in-progress walk are never rate-limited, only fresh starts)',
            'retry_after_seconds' => $MIN_FRESH_START_INTERVAL_SECONDS - $sinceLastStart,
        ));
        $db->close();
        exit;
    }
}

if ($action === 'reset') {
    $db->exec('DROP TABLE IF EXISTS dirs');
    $db->exec('DROP TABLE IF EXISTS walk_stack');
    $db->exec('DROP TABLE IF EXISTS run_meta');
    init_schema($db);
}

$meta = get_meta($db);

if ($meta['status'] === 'done' && $action !== 'reset') {
    $result = get_status($db, $meta);
    $result['detail'] = 'already complete - pass action=reset for a fresh walk';
    echo json_encode($result);
    $db->close();
    exit;
}

if ($meta['status'] === 'running' && ($now - (int)$meta['heartbeat']) < $HEARTBEAT_STALE_AFTER) {
    echo json_encode(array(
        'state' => 'busy',
        'detail' => 'another invocation is actively running (heartbeat is fresh) - not starting a second one',
        'meta' => $meta,
    ));
    $db->close();
    exit;
}

// status === 'running' with a STALE heartbeat means the previous invocation
// never reached its own graceful pause/completion - the host almost
// certainly killed it (max_execution_time, memory limit, etc). Take over.
$resumedFromCrash = ($meta['status'] === 'running');

set_meta($db, 'status', 'running');
set_meta($db, 'heartbeat', (string)$now);
if ($meta['started_at'] === '') {
    set_meta($db, 'started_at', (string)$now);
}
if ($resumedFromCrash) {
    set_meta($db, 'crash_recoveries', (string)(((int)$meta['crash_recoveries']) + 1));
}

// Bootstrap a brand-new walk only when nothing has been queued or recorded yet.
if (stack_is_empty($db) && !row_exists($db, '')) {
    push_stack($db, '', 'enter');
}

// ---- do the work, budgeted ------------------------------------------------

$start = microtime(true);
$processed = 0;

$db->exec('BEGIN');
$sinceCommit = microtime(true);

while (!stack_is_empty($db)) {
    // Time check comes AFTER doing at least one frame's work, never before -
    // guarantees every invocation makes forward progress regardless of how
    // small the budget is, instead of possibly breaking before processing
    // anything at all.
    $frame = pop_stack($db);
    if ($frame['phase'] === 'enter') {
        process_enter($db, $frame['path'], $ROOT_DIR, $EXCLUDE_PATTERNS);
    } else {
        process_exit($db, $frame['path']);
    }
    $processed++;

    if (microtime(true) - $sinceCommit > $COMMIT_EVERY_SECONDS) {
        $db->exec('COMMIT');
        set_meta($db, 'heartbeat', (string)time());
        $db->exec('BEGIN');
        $sinceCommit = microtime(true);
    }

    if (microtime(true) - $start > $TIME_BUDGET_SECONDS) {
        break; // graceful pause, not a crash - next invocation just continues
    }
}

$db->exec('COMMIT');

$done = stack_is_empty($db);
set_meta($db, 'status', $done ? 'done' : 'paused');
set_meta($db, 'heartbeat', (string)time());
if ($done) {
    set_meta($db, 'completed_at', (string)time());
}

$result = get_status($db);
$result['this_invocation'] = array(
    'processed_this_call' => $processed,
    // Kept as a STRING, not a rounded float - 4.64 isn't exactly
    // representable in binary, so any float this close to it re-expands to
    // full IEEE-754 precision in json_encode() on hosts whose
    // serialize_precision setting isn't the smart-round-trip default (-1).
    // A string sidesteps that entirely, at the cost of needing a parse on
    // the consuming end (fine for a diagnostic field like this one).
    'elapsed_seconds' => sprintf('%.2f', microtime(true) - $start),
    'resumed_from_crash' => $resumedFromCrash,
);
echo json_encode($result);
$db->close();
exit;

// ---- helpers ----------------------------------------------------------

function init_schema($db) {
    $db->exec(
        'CREATE TABLE IF NOT EXISTS dirs (
            path TEXT PRIMARY KEY,
            parent TEXT,
            own_mtime INTEGER,
            file_max_mtime INTEGER,
            file_max_path TEXT,
            bubbled_max_mtime INTEGER,
            bubbled_max_path TEXT
        )'
    );
    $db->exec('CREATE INDEX IF NOT EXISTS idx_dirs_parent ON dirs(parent)');
    $db->exec(
        'CREATE TABLE IF NOT EXISTS walk_stack (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT,
            phase TEXT
        )'
    );
    $db->exec('CREATE TABLE IF NOT EXISTS run_meta (key TEXT PRIMARY KEY, value TEXT)');
}

function get_meta($db) {
    $out = array(
        'status' => 'idle',
        'heartbeat' => '0',
        'started_at' => '',
        'completed_at' => '',
        'crash_recoveries' => '0',
    );
    $res = $db->query('SELECT key, value FROM run_meta');
    while ($row = $res->fetchArray(SQLITE3_ASSOC)) {
        $out[$row['key']] = $row['value'];
    }
    return $out;
}

function set_meta($db, $key, $value) {
    // Plain SQLite (no ON CONFLICT upsert syntax, which needs SQLite >= 3.24
    // - avoiding it keeps this portable to older bundled sqlite3 libraries
    // some hosts still ship).
    $stmt = $db->prepare('DELETE FROM run_meta WHERE key = :k');
    $stmt->bindValue(':k', $key, SQLITE3_TEXT);
    $stmt->execute();
    $stmt = $db->prepare('INSERT INTO run_meta (key, value) VALUES (:k, :v)');
    $stmt->bindValue(':k', $key, SQLITE3_TEXT);
    $stmt->bindValue(':v', $value, SQLITE3_TEXT);
    $stmt->execute();
}

function stack_is_empty($db) {
    return ((int)$db->querySingle('SELECT COUNT(*) FROM walk_stack')) === 0;
}

function push_stack($db, $path, $phase) {
    $stmt = $db->prepare('INSERT INTO walk_stack (path, phase) VALUES (:p, :ph)');
    $stmt->bindValue(':p', $path, SQLITE3_TEXT);
    $stmt->bindValue(':ph', $phase, SQLITE3_TEXT);
    $stmt->execute();
}

function pop_stack($db) {
    $row = $db->querySingle('SELECT seq, path, phase FROM walk_stack ORDER BY seq DESC LIMIT 1', true);
    $db->exec('DELETE FROM walk_stack WHERE seq = ' . (int)$row['seq']);
    return array('path' => $row['path'], 'phase' => $row['phase']);
}

function row_exists($db, $path) {
    $stmt = $db->prepare('SELECT 1 FROM dirs WHERE path = :p');
    $stmt->bindValue(':p', $path, SQLITE3_TEXT);
    $res = $stmt->execute();
    return $res->fetchArray() !== false;
}

function path_is_excluded($rel, $patterns) {
    $basename = ($rel === '') ? '' : basename($rel);
    foreach ($patterns as $pattern) {
        if (fnmatch($pattern, $rel) || ($basename !== '' && fnmatch($pattern, $basename))) {
            return true;
        }
    }
    return false;
}

function process_enter($db, $relPath, $rootDir, $excludePatterns) {
    $full = ($relPath === '') ? $rootDir : $rootDir . '/' . $relPath;
    $ownMtime = @filemtime($full);
    $fileMax = 0;
    $fileMaxPath = null;
    $childDirs = array();

    $dh = @opendir($full);
    if ($dh !== false) {
        while (($entry = readdir($dh)) !== false) {
            if ($entry === '.' || $entry === '..') {
                continue;
            }
            // .crd_stage1 is hardcoded-excluded regardless of the
            // configurable list below - failing to skip it would be a real
            // correctness bug (walking over the manifest DB while writing
            // it), not just noise reduction.
            if ($entry === '.crd_stage1') {
                continue;
            }
            $entryFull = $full . '/' . $entry;
            $entryRel = ($relPath === '') ? $entry : $relPath . '/' . $entry;
            if (path_is_excluded($entryRel, $excludePatterns)) {
                continue;
            }
            if (is_link($entryFull)) {
                continue; // avoid cycles; not worth resolving for this test
            }
            if (is_dir($entryFull)) {
                $childDirs[] = $entryRel;
            } elseif (is_file($entryFull)) {
                $mt = @filemtime($entryFull);
                if ($mt !== false && $mt > $fileMax) {
                    $fileMax = $mt;
                    $fileMaxPath = $entryRel;
                }
            }
        }
        closedir($dh);
    }

    $parent = ($relPath === '') ? null : dirname($relPath);
    if ($parent === '.') {
        $parent = '';
    }

    $stmt = $db->prepare(
        'INSERT INTO dirs (path, parent, own_mtime, file_max_mtime, file_max_path)
         VALUES (:p, :parent, :om, :fm, :fp)'
    );
    $stmt->bindValue(':p', $relPath, SQLITE3_TEXT);
    $stmt->bindValue(':parent', $parent, SQLITE3_TEXT);
    $stmt->bindValue(':om', $ownMtime === false ? 0 : $ownMtime, SQLITE3_INTEGER);
    $stmt->bindValue(':fm', $fileMax, SQLITE3_INTEGER);
    $stmt->bindValue(':fp', $fileMaxPath, SQLITE3_TEXT);
    $stmt->execute();

    // Post-order: this directory's own "exit" (finalize/bubble) frame goes on
    // first, then its children's "enter" frames on top of that - so every
    // child is fully processed (a committed row) before this dir's exit runs.
    push_stack($db, $relPath, 'exit');
    foreach ($childDirs as $childRel) {
        push_stack($db, $childRel, 'enter');
    }
}

function process_exit($db, $relPath) {
    $stmt = $db->prepare('SELECT own_mtime, file_max_mtime, file_max_path FROM dirs WHERE path = :p');
    $stmt->bindValue(':p', $relPath, SQLITE3_TEXT);
    $row = $stmt->execute()->fetchArray(SQLITE3_ASSOC);

    $bubbledMax = (int)$row['own_mtime'];
    $bubbledPath = ($relPath === '') ? '(root)' : $relPath;
    if ((int)$row['file_max_mtime'] > $bubbledMax) {
        $bubbledMax = (int)$row['file_max_mtime'];
        $bubbledPath = $row['file_max_path'];
    }

    $stmt = $db->prepare('SELECT bubbled_max_mtime, bubbled_max_path FROM dirs WHERE parent = :p');
    $stmt->bindValue(':p', $relPath, SQLITE3_TEXT);
    $rows = $stmt->execute();
    while ($child = $rows->fetchArray(SQLITE3_ASSOC)) {
        if ((int)$child['bubbled_max_mtime'] > $bubbledMax) {
            $bubbledMax = (int)$child['bubbled_max_mtime'];
            $bubbledPath = $child['bubbled_max_path'];
        }
    }

    $stmt = $db->prepare('UPDATE dirs SET bubbled_max_mtime = :bm, bubbled_max_path = :bp WHERE path = :p');
    $stmt->bindValue(':bm', $bubbledMax, SQLITE3_INTEGER);
    $stmt->bindValue(':bp', $bubbledPath, SQLITE3_TEXT);
    $stmt->bindValue(':p', $relPath, SQLITE3_TEXT);
    $stmt->execute();
}

function get_status($db, $meta = null) {
    if ($meta === null) {
        $meta = get_meta($db);
    }
    $dirCount = (int)$db->querySingle('SELECT COUNT(*) FROM dirs');
    $doneCount = (int)$db->querySingle('SELECT COUNT(*) FROM dirs WHERE bubbled_max_mtime IS NOT NULL');
    $pending = (int)$db->querySingle('SELECT COUNT(*) FROM walk_stack');
    $root = $db->querySingle("SELECT bubbled_max_mtime FROM dirs WHERE path = ''");

    return array(
        'status' => $meta['status'],
        'dirs_seen' => $dirCount,
        'dirs_finalized' => $doneCount,
        'pending_stack_depth' => $pending,
        'root_bubbled_max_mtime' => $root,
        'root_bubbled_max_mtime_human' => $root ? date('Y-m-d H:i:s', (int)$root) : null,
        'started_at' => $meta['started_at'] !== '' ? date('Y-m-d H:i:s', (int)$meta['started_at']) : null,
        'completed_at' => $meta['completed_at'] !== '' ? date('Y-m-d H:i:s', (int)$meta['completed_at']) : null,
        'crash_recoveries' => (int)$meta['crash_recoveries'],
        'heartbeat_age_seconds' => $meta['heartbeat'] !== '0' ? (time() - (int)$meta['heartbeat']) : null,
    );
}
