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

### Modes
 
 You must specify **exactly one** of the following mutually exclusive modes:
 
 **1. Check Git Dirty Files (HEAD) vs Remote (Common Usage)**
Scans the current git status for modified files. For each modified file, it ignores the local working copy and instead fetches the content and timestamp from the **HEAD commit**. It then compares this committed version against the remote server. Untracked or added (but uncommitted) files are ignored/skipped.
```bash
python CheckRemoteDirty.py --workingDir "C:\MyProject" --vsGit "dirty_snapshot.json" --ftpConfig "ftp_config.json"
```

**2. Check from existing Hash File**
Uses a previously saved list of files (e.g., from a previous run) to check against the remote server.
```bash
python CheckRemoteDirty.py --workingDir "C:\MyProject" --vsHashFile "dirty_snapshot.json" --ftpConfig "ftp_config.json"
```

**3. Update Hash File**
Updates the hashes and timestamps in an existing snapshot file based on the current local state.
```bash
python CheckRemoteDirty.py --workingDir "C:\MyProject" --updateHashFile "dirty_snapshot.json"
```

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
