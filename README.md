# CheckRemoteDirty

A Python utility to verify if locally modified (dirty) files in a Git repository differ from those deployed on a remote FTP server. This helps determine whether your local changes are in sync with or different from what is currently live. This is especially useful when your project does not have proper CI/CD set up and your teammates have the annoying habit of not committing their changes before deploying to the remote server. By comparing your own local hash snapshots with the remote server before deployment, you can avoid accidentally wiping out someone else's hard work, their grave sin of git-slacking notwithstanding. 

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
| `--vsGit` | Path to a JSON file. Creates or overwrites this file with hashes from the current git **HEAD** commit for dirty files. Ignores local uncommitted changes. |
| `--vsHashFile` | Path to an existing JSON file. Loads the list of files to check from this file. |
| `--updateHashFile` | Path to an existing JSON file. specific uses existing file list but updates hashes/timestamps based on current local files. |
| `--ftpConfig` | Path to the FTP configuration JSON file. If provided, performs the comparison against the remote server. |
| `--checkSizeOnly` | **Optional**. If set, skip downloading/hashing files. Only compares file sizes. **Warning**: This mode is useless for cross-platform comparisons (e.g. Windows vs Linux) because line-endings (CRLF vs LF) cause size differences even if content matches. Use only if you are sure platforms match. |
| `--deployOnClean` | **Optional**. If set, and if all remote files are found to be "clean" (matching the source of truth, e.g., git HEAD), prompts to deploy the local dirty files. |

### Common Workflows

#### 1. "Pre-Flight" Safety Check (Recommended)
**Goal**: You have local dirty changes you want to manually deploy, yet you feel it is too insignificant to git-commit first *(Hold the pitchforks o CI/CD crusaders! I am but a wee bit of a web developer)*. You want to ensure the specific files you are about to overwrite on the server have not been modified by someone else (i.e., ensure Remote == Git HEAD).

*   **Command**:
    ```bash
    python CheckRemoteDirty.py --workingDir "." --vsGit "dirty.json" --ftpConfig "ftp.json"
    ```
*   **Logic**:
    1.  Identifies your local dirty files.
    2.  Fetches the *original* content of these files from `HEAD` (ignoring your local edits).
    3.  Compares `HEAD` content vs `Remote`.
    4.  **Result**:
        *   **MATCH**: Remote is clean (synced with HEAD). Safe to deploy your changes.
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
*   `--vsGit <file>`: Use Git HEAD as the source of truth (Safety Check).
*   `--updateHashFile <file>`: Use Local Working Directory as source of truth (Verification).
*   `--vsHashFile <file>`: Use a previously saved snapshot (Audit).

**Options**:
*   `--ftpConfig <file>`: required to perform the remote comparison.

## Configuration

### FTP Config File
Create a JSON file (e.g., `ftp_config.json`) with your connection details:

```json
{
    "host": "ftp.example.com",
    "port": 21,
    "user": "ftp_user",
    "password": "your_password",
    "remote_root": "/public_html/myapp"
}
```

*   `remote_root` (Optional): The directory on the remote server where the project files reside. Defaults to `/` if omitted.

## Output
 
 The script outputs a table showing the status of each file:
*   **MATCH**: Local hash matches remote hash. (Hashes are calculated after stripping `\r` and `\n` to ignore line-ending differences).
*   **DIFF HASH**: Local content differs from remote content.
*   **MISSING**: File does not exist on the remote server.
