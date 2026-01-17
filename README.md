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
| `--vsGit` | Path to a JSON file. Creates or overwrites this file with hashes from the current git **HEAD** commit for dirty files (unless `--gitCommitHash` is provided, see below). Ignores local uncommitted changes. |
| `--vsHashFile` | Path to an existing JSON file. Loads the list of files to check from this file. |
| `--updateHashFile` | Path to an existing JSON file. Adds new dirty files from Git to the list and updates local hashes/timestamps for all items. Preserves local remote state history (`my_remote`). |
| `--ftpConfig` | Path to the FTP configuration JSON file. If provided, performs the comparison against the remote server. |
| `--checkSizeOnly` | **Optional**. If set, skip downloading/hashing files. Only compares file sizes. **Warning**: This mode is useless for cross-platform comparisons (e.g. Windows vs Linux) because line-endings (CRLF vs LF) cause size differences even if content matches. Use only if you are sure platforms match. |
| `--deployOnClean` | **Optional**. If set, and if all remote files are found to be "clean" (matching the source of truth, e.g., git HEAD/commit version), prompts to deploy the local dirty files. |
| `--gitCommitHash` | **Optional**. Specify a git commit hash (or ref like `HEAD~1`) to use as the "clean" source of truth instead of `HEAD`. Useful if you want to verify against a specific historical version. |

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

#### 2. "Post-Deploy" Verification
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
*   **MATCH**: Local hash matches remote hash. (Hashes are calculated after stripping `\r` and `\n` to ignore line-ending differences).
*   **DIFF HASH**: Local content differs from remote content, AND remote content does not match the last-known deployed state.
*   **MATCH LAST UPDATE**: Remote content differs from Git HEAD/commit version but matches the last version deployed by this script. This means the remote change was authorized/safe and can be overwritten by a newer local version.

## Conflict Resolution

When using the `--deployOnClean` flag, the script will halt if it encounters a file where the local version differs from the remote version (Status: `DIFF HASH`), and the remote state is unknown (doesn't match HEAD/commit version or last deploy).

The script will produce a **Conflict Prompt** asking you how to proceed for that specific file:

```
!! CONFLICT: relative/path/to/file.ext differs from remote (DIFF HASH).
   Type 'replace' to overwrite remote, 'keep' to skip (backup remote), or Enter to abort:
```

### Options:

1.  **replace**:
    *   **Action**: Overwrites the remote file with your local version.
    *   **Backup**: The remote file is backed up as part of the standard deployment process (stored in `backups/<project>/<path>/filename.timestamp`).
    *   **Use when**: You are confident your local version is the intended source of truth and overrides the unknown remote change.

2.  **keep**:
    *   **Action**: Skips deployment for this file (keeps the remote version).
    *   **Backup**: The script **immediately downloads** the remote file to `backups/<project>/<path>/filename.timestamp.conflict_bk`.
    *   **Use when**: You want to investigate the remote changes. The immediate backup allows you to manually diff and merge the remote content later without losing it.

3.  **[Enter] / abort**:
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

