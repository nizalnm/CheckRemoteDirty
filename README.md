# CheckRemoteDirty

A Python utility to verify if locally modified (dirty) files in a Git repository differ from those deployed on a remote FTP server. This helps determine whether your local changes are in sync with or different from what is currently live. This is especially useful when your project does not have proper CI/CD set up and your teammates have the all-too-common habit of not committing their changes before deploying to the remote server. By comparing your own local hash snapshots with the remote server before deployment, you can avoid accidentally wiping out someone else's hard work, their grave sin of git-slacking notwithstanding. 

## Prerequisites

- Python 3.x
- Git installed and accessible from the command line (`git` command).
- Standard Python libraries: `argparse`, `subprocess`, `hashlib`, `os`, `json`, `ftplib`, `datetime`, `sys`.

## Usage

Run the script from the command line.

`python CheckRemoteDirty.py --workingDir <path_to_repo> [mode_options] [ftp_options]`

### Arguments

| Argument | Description |
| :--- | :--- |
| `--workingDir` | **Required**. The local path to the project directory containing the Git repository. |
| `--vsGit` | Path to a JSON file. Creates or overwrites this file with hashes from the current git **HEAD**, checking currently dirty files. If `--gitCommitHash` is provided, it **ALSO** checks all files changed in that specific commit (even if they are clean locally). |
| `--vsHashFile` | Path to an existing JSON file. Loads the list of files to check from this file. |
| `--updateHashFile` | Path to an existing JSON file. Adds new dirty files from Git to the list and updates local hashes/timestamps for all items. Preserves local remote state history (`my_remote`). |
| `--ftpConfig` | Path to the FTP configuration JSON file. If provided, performs the comparison against the remote server. |
| `--checkSizeOnly` | **Optional**. If set, skip downloading/hashing files. Only compares file sizes. **Warning**: This mode is useless for cross-platform comparisons (e.g. Windows vs Linux) because line-endings (CRLF vs LF) cause size differences even if content matches. Use only if you are sure platforms match. |
| `--deployOnClean` | **Optional**. If set, and if all remote files are found to be "clean" (matching the source of truth, e.g., git HEAD/commit version), prompts to deploy the local dirty files. |
| `--gitCommitHash` | **Optional**. Specify a git commit hash (or ref like `HEAD~1`) to use as the "clean" source of truth instead of `HEAD`. In `--vsGit` mode (without `--vsGitListHash`), this **automatically adds** all files changed in that commit to the check list, allowing you to verify a specific historical deployment even on a clean repo. |
| `--vsGitListHash` | **Optional**. Specify a second git commit hash to derive the **list of files** to be checked. If provided, the reference content hashes are still derived from `--gitCommitHash` (or `HEAD`), but the scope is limited to files in this specific commit. |
| `--gitBaselineHash` | **Optional**. Specify a git commit hash to use as the "expected" remote state. If a remote file differs from your local/HEAD version but **matches** this baseline commit, it is considered safe to overwrite (as the remote is simply outdated, not modified). Ignored if `--checkSizeOnly` is used. |

### Advanced Argument Interactions

You can mix and match these arguments to control *Scope* (What to check), *Reference* (What is safe), and *Payload* (What to deploy).

| Scenario | Scope (List of Files) | Reference A (Safe if Matches) | Reference B (Safe if Matches) | Payload (What is Uploaded) |
| :--- | :--- | :--- | :--- | :--- |
| **Default** (No Hashes) | `git status` (Local Dirty) | `HEAD` | None | Local Disk File |
| **`--gitCommitHash` only** | Changed files in `Hash` | `Hash` | None | Local Disk File |
| **`--gitBaselineHash` only** | `git status` (Local Dirty) | `HEAD` | `Baseline` | Local Disk File |
| **All 3 Hashes** | Changed files in `ListHash` | `CommitHash` | `Baseline` | Local Disk File |
| **Git Version** | Any | `CommitHash` | `Baseline` | Git Blob (Temp) |

**Key Takeaway**:
*   The script **DEFAULTED** to deploying the file currently on your local disk.
*   The arguments provide "Safety Checks" (References) to ensure you aren't overwriting unknown server changes.

---

## The Two-Phase Safety Check

To prevent accidental deployments of unfinalized work or merge patches, `CheckRemoteDirty` enforces a two-stage verification process:

### Phase 1: Local Integrity Check (Pre-FTP)
Before connecting to the remote server, the script compares your **Local Disk Files** against the **Git source of truth** (`HEAD` or `--gitCommitHash`).

If mismatches are found (e.g., uncommitted changes or merge patches), the script pauses for a decision.

**Bulk Decisions**:
If multiple files are mismatched, you will first be prompted for a bulk action:
*   **[U]se Local for ALL**: Apply "Use Local" to all mismatched files.
*   **[G]it Version (Temp) for ALL**: Fetch the Git version for all mismatched files.
*   **[I]ndividual**: Resolve each mismatch one-by-one (see below).
*   **[A]bort**: Stop the entire process.

**Individual Options**:
1.  **[U]se Local**: Treat your current local version (likely with uncommitted patches) as the intended goal for this deployment.
2.  **[G]it Version (Temp)**: **SAFE.** The script will fetch the original content from Git into a **temporary file** for deployment. **Your local disk file remains completely untouched.**
3.  **[A]bort**: Stop the process.

The script then displays a summary of your choices:
> `Deployment Goals: 15 files (3 diff local vs git, using local)`

### Phase 2: Remote Comparison (Local/Git vs Remote)
Once the local state is finalized, the script connects to the FTP server and compares your **Chosen Goal** (either Local Disk or Git-Temp) against the **Remote File**.

| Status | Meaning |
| :--- | :--- |
| **`MATCH GOAL`** | Remote matches your chosen target (Local Disk or Git-Temp). Already synced! |
| **`MATCH BASELINE`** | Remote matches a baseline (unmodified Git commit, previous baseline, or last deploy). Safe to overwrite. |
| **`DIFF HASH`** | Remote matches **NEITHER** your goal nor any baseline. Potential unknown change found! |

---

### Common Workflows

#### 1. "Pre-Flight" Safety Check (Recommended)
**Goal**: You have local dirty changes you want to manually deploy, yet you feel it is too insignificant to git-commit first *(Hold the pitchforks o CI/CD crusaders! I am but a wee bit of a web developer)*. You want to ensure the specific files you are about to overwrite on the server have not been modified by someone else (i.e., ensure Remote == Git HEAD/commit version).
*   **Command**:
    ```bash
    python CheckRemoteDirty.py --workingDir "." --vsGit "dirty.json" --ftpConfig "ftp.json"
    ```
*   **Logic**:
    1.  Identifies your local dirty files.
    2.  Fetches the *original* content of these files from `HEAD/commit version` (ignoring your local edits).
    3.  Compares `HEAD/commit version` content vs `Remote`.
    4.  **Result**:
        *   **MATCH**: Remote is clean (synced with HEAD/commit version). Safe to deploy your changes.
        *   **DIFF**: Remote has unknown changes! **Do not deploy as is** or you will overwrite them. Instead perform the tender loving care of merging the remote changes carefully into your local, before deploying (and committing the merged changes like the good chap you are).


#### 2. "Verify Past Deployment" (Specific Commit) with Safety Check
**Goal**: You want to deploy/verify a specific commit, but you also want to be sure your local files actually match that commit before uploading (to avoid accidentally uploading uncommitted local changes).

*   **Command**:
    ```bash
    python CheckRemoteDirty.py --workingDir "." --vsGit "check.json" --ftpConfig "ftp.json" --gitCommitHash "20043ac..."
    ```
*   **Logic**:
    1.  Identifies files changed in `20043ac...`.
    2.  Compares `Local File` vs `Git Commit Version`.
    3.  **Safety Interlock**:
        *   If `Local != Git`: Pauses in **Phase 1** and warns you.
        *   **[U] Use Local**: Ignores mismatch, uploads your local file.
        *   **[G] Git Version (Temp)**: **SAFE.** Extracts the clean file from git commit to a **temporary file** and uploads it, **leaving your local disk file unharmed**.
    4.  Compares `Chosen Source` vs `Remote`.
    5.  **Result**: Proceed execution if clean.

#### 3. "Post-Deploy" Verification
**Goal**: You just deployed your local dirty files. You want to verify they were uploaded correctly and match your local disk.

*   **Command**:
    ```bash
    python CheckRemoteDirty.py --workingDir "." --updateHashFile "dirty.json" --ftpConfig "ftp.json"
    ```
*   **Logic**:
    1.  Calculates hash of your *current local* dirty files.
    2.  Updates `dirty.json` with these hashes.
    3.  Compares `Local` vs `Remote`.
    4.  **Result**:
        *   **MATCH**: Deployment success.
        *   **DIFF**: Upload failed or corruption occurred.

### Command Reference
 
 **Note**: All flag arguments are case-insensitive (e.g. `--ftpConfig`, `--ftpconfig`, and `--FTPCONFIG` are all valid).
 
 **Mode Flags** (Pick exactly one):
*   `--vsGit <file>`: Use Git HEAD/commit version as the source of truth (Safety Check).
*   `--updateHashFile <file>`: Use Local Working Directory as source of truth (Verification).
*   `--vsHashFile <file>`: Use a previously saved snapshot (Audit).

**Options**:
*   `--ftpConfig <file>`: required to perform the remote comparison.

## Configuration

### 2. FTP Configuration

Create a JSON file (e.g., `myconfig.json`) with your FTP credentials.
> **Tip**: Use `sample_ftp_config.json` as a template.

```json
{
    "host": "ftp.example.com",
    "user": "your_username",
    "password": "your_password",
    "port": 21,
    "remote_root": "/public_html/"
}
```
**Security Note**: Add specific config filenames (like `*config.json`) to your `.gitignore` to avoid committing secrets.
*   `remote_root` (Optional): The directory on the remote server where the project files reside. Defaults to `/` if omitted.

## Output
 
 The script outputs a table showing the status of each file:
*   **MATCH GOAL**: Remote content exactly matches your intended goal (Local file or chosen Git version).
*   **MATCH BASELINE**: Remote content matches an earlier baseline (Git commit hash, custom baseline, or the last version deployed by this script). This means the remote is "safe" but outdated.
*   **DIFF HASH**: Remote content differs from **both** your goal and any baseline. Someone else might have changed the file on the remote server.

## Conflict Resolution

When using the `--deployOnClean` flag, the script will halt if it encounters a file with status **`DIFF HASH`**.

The script will produce a **Conflict Prompt** asking you how to proceed for that specific file:

```
!! CONFLICT: relative/path/to/file.ext differs from remote (DIFF HASH).
   Type 'replace' to overwrite remote, 'keep' to skip (backup remote), or Enter to abort:
```

### Options:

1.  **[r] replace**:
    *   **Action**: Overwrites the remote file with your chosen goal version.
    *   **Use when**: You are confident your local version (or chosen Git version) is the intended source of truth.

2.  **[ra] replace all**:
    *   **Action**: Automatically applies `replace` to the current and **all subsequent** remote conflicts in this run. It respects any `keep` decisions made before hitting `ra`.
    *   **Use when**: You want to overwrite all remaining conflicting files without individual prompts.

3.  **[k] keep**:
    *   **Action**: Skips deployment for this file (keeps the remote version).
    *   **Backup**: The script **immediately downloads** the remote file to `backups/<project>/<path>/filename.timestamp.conflict_bk`.
    *   **Use when**: You want to investigate the remote changes. The immediate backup allows you to manually diff and merge the remote content later without losing it.

4.  **[l] list**:
    *   **Action**: Enters **Bulk List Mode**. Skips all remaining deployments for the current run, but automatically downloads every conflicting remote file to a local `.conflict_bk` file.
    *   **Use when**: You realize there are too many conflicts to resolve manually and you want to stop the deployment while ensuring you have local copies of all remote conflicting files for a later merge.

4.  **[Enter] / abort**:
    *   **Action**: Cancels the entire deployment process immediately.
    *   **Use when**: You are unsure and want to stop everything to check the situation manually.


## Quick Start Scripts

Sample batch (`.bat`) and shell (`.sh`) scripts are provided to help you get started quickly.

1.  **Copy** the sample scripts (`project_preflight`, `project_deploy`, `project_postdeploy`) to your preferred location or rename them (e.g., `myapp_deploy.bat`).
2.  **Edit** the scripts and update the arguments as detailed in the comments within each file:
    *   `--workingDir`: Set this to your project's root folder.
    *   `--vsGit` / `--updateHashFile`: Set a unique JSON filename for your project (e.g., `myapp_hashes.json`).
    *   `--ftpConfig`: Point to your specific FTP config file (e.g., `myapp_ftp.json`).

### Script Types
*   **Preflight** (`preflight`): Runs a safety check. Tells you if the remote server matches Git HEAD/commit version. Use this before deciding to deploy.
*   **Deploy** (`deploy`): Runs the safety check, and if safe, backs up remote files and deploys your local changes.
*   **Post-Deploy** (`postdeploy`): Verifies that the files currently on the server match your local files. Use this after deployment to ensure integrity.


### Bonus Helper: `diff_normalized.py`

A standalone utility to compare two files (or a file vs git) while **ignoring line endings (`\r\n` vs `\n`)**. Correctly handles Windows vs Linux line ending differences that often appear as false positives in standard diff tools.

**Usage**:
```bash
python diff_normalized.py file1.php file2.php
python diff_normalized.py file.php --vsGit HEAD
```
