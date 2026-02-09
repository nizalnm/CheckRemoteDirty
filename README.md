# CheckRemoteDirty

A Python utility to verify if locally modified files in a Git repository differ from those deployed on a remote FTP server. This is especially useful when your project doesn't have proper CI/CD set up and your teammates have the all-too-common  habit of not committing their changes before deploying to the remote server. 

By comparing your local hash snapshots with the remote server before deployment, you can avoid accidentally wiping out someone else's hard work — their grave sin of git-slacking notwithstanding.

## Quick Start (30-Second Setup)

### 1. Requirements
- **Python 3.7+**
- **Git** (accessible via command line)
- No external Python libraries needed (uses `argparse`, `hashlib`, `ftplib`, etc.)

### 2. Basic Command
```bash
python CheckRemoteDirty.py --workingDir "C:\my_project" --vsGit "check.json" --ftpConfig "ftp.json" --deployOnClean
```

### 3. Glossary (Jargon Buster)
*   **Dirty Files**: Files in your local folder that have changes not yet committed to Git.
*   **HEAD**: The latest commit in your current Git branch (your "Clean" baseline).
*   **Git Blob**: The actual snapshot of a file stored inside the Git database.
*   **Baseline**: A known "safe" version of a file (e.g., a previous deployment or a specific Git commit).

### 4. Setup Shortcuts (.bat / .sh)
Sample scripts are provided in the repo to save you from typing long commands:
*   `preflight`: Runs a safety check (Remote vs Git).
*   `deploy`: Safety check + backs up remote + uploads local.
*   `postdeploy`: Verified local disk matching remote.

---

## Why This Tool Exists (The Real-World Problem)

You know the scenario: It's 4:45 PM on Friday. You've got a quick CSS fix to push live. But wait, did Sarah deploy her PHP changes this morning without committing? 
If you FTP your local files, will you overwrite her work?

**Without this tool**: You either (a) blindly deploy and hope, (b) spend 20 minutes manually diffing files, or (c) give up and leave it for Monday.

**With this tool**: Run one command, get a clear "SAFE" or "CONFLICT" status in seconds.

*Hold the pitchforks, o CI/CD crusaders! I know proper deployment pipelines solve this. But alas, not every project gets the red-carpet DevOps treatment.*

## Technical Overview: The Two-Phase Safety Check

To prevent accidental deployments of unfinalized work or merge patches, `CheckRemoteDirty` enforces a two-stage verification process:

### Phase 1: Local Integrity Check (Pre-FTP)
Before connecting to the server, the script compares your **Local Disk Files** against the **Git source of truth** (`HEAD`).

**Why?** To ensure you aren't about to deploy a "messy" file that contains half-finished merge conflicts or uncommitted debug code without realizing it.

| Local vs Git | Status | Prompt |
| :--- | :--- | :--- |
| **Match** | `Clean` | Proceed automatically. |
| **Mismatch** | `LOCAL MISMATCH` | **Decision required**: Use Local, Use Git (Temp), or Abort. |

> **Note on [G]it Version (Temp)**: This is the safest option. The script fetches the "clean" file from Git into a temporary folder for deployment. Your local disk file remains completely untouched.

### Phase 2: Remote Comparison (Local/Git vs Remote)
Once the local goal is set, the script connects to FTP and compares your **Goal** against the **Remote File**.

#### Visual Example Output:
```text
File                                     | Status          | Details
-----------------------------------------------------------------------------------------------
index.php                                | MATCH GOAL      | [L: 2026-02-09 = R: 2026-02-09]
config/db.php                            | MATCH BASELINE  | (Safe, matches baseline/commit)
pemohon/ajax_search.php                  | DIFF HASH       | !! CONFLICT: Unknown remote change
```

| Status | Meaning | Action |
| :--- | :--- | :--- |
| **`MATCH GOAL`** | Remote already matches your update. | Skip (already synced). |
| **`MATCH BASELINE`** | Remote matches an old known version. | Safe to overwrite. |
| **`DIFF HASH`** | Remote has unknown changes. | **Conflict Prompt**. |

#### Conflict Resolution Options:
When a **`DIFF HASH`** is encountered during deployment, you can choose:
1.  **`[r]` replace**: Overwrites the remote file with your chosen version.
2.  **`[ra]` replace all**: Automatically applies "replace" to all remaining conflicts.
3.  **`[k]` keep**: Skips deployment for this file and **immediately downloads a backup** of the remote version to the `backups/` folder.
4.  **`[l]` list**: Skips all deployments but downloads backups for **all** remaining conflicting files.
5.  **`[Enter]` / abort**: Cancels the entire process immediately.

---

### Advanced Argument Interactions

You can mix and match these arguments to control *Scope* (What to check), *Standard Reference* (Phase 1 Benchmark), and *Extra Safety* (Phase 2 Baselines).

| Scenario | Scope (List of Files) | Standard Hash (Ref A) | Additional Baselines (Ref B) | Default Payload |
| :--- | :--- | :--- | :--- | :--- |
| **Default** (Local Sync) | `git status` (Local Dirty) | `HEAD` | history (`my_remote`) | Local Disk File |
| **Deploy Commit** | Changed in `CommitHash` | `CommitHash` | `CommitHash` + history | Local Disk File |
| **Sync Check** | `git status` (Local Dirty) | `HEAD` | `Baseline` + history | Local Disk File |
| **Full Safety Audit** | Changed in `ListHash` | `CommitHash` | `Baseline` + history | Local Disk File |

**Terminology Clarification**:
*   **Standard Hash (Ref A)**: The "Source of Truth" (clean version). If local disk differs from this, Phase 1 flags a mismatch.
*   **Safety Baselines (Ref B)**: Known safe states for the remote server. If the remote file matches *any* of these, it's considered unmodified/outdated and safe to overwrite. By default, the `Standard Hash` is always included as a baseline.
*   **Default Payload**: The script always defaults to uploading your **Local Disk File**. You can switch to the **Git Blob** during the Phase 1 decision prompt.

---


## Configuration

### FTP Configuration (`ftp.json`)

Create a JSON file with your FTP credentials. You can use `sample_ftp_config.json` as a base.

```json
{
    "host": "ftp.example.com",
    "user": "your_username",
    "password": "your_password",
    "port": 21,
    "remote_root": "/public_html/"
}
```

> [!WARNING]
> **Security Risk**: Storing your FTP password in plaintext is dangerous.
> 1.  **NEVER** commit your FTP config file to Git.
> 2.  **ALWAYS** add `*config.json` (or your specific filename) to your `.gitignore`.
> 3.  Ensure your config files have restricted local permissions.

---

## Troubleshooting & FAQ

### Common Errors
| Message | Meaning | Fix |
| :--- | :--- | :--- |
| `FTP Error: 530 Login incorrect` | Wrong username or password. | Double check your `ftp.json`. |
| `FTP Error: 550 No such file` | The remote path provided doesn't exist. | Check `remote_root` in your config. |
| `Git not found` | The `git` command isn't in your system PATH. | Install Git or add it to PATH. |
| `UnboundLocalError` | A technical bug in the script. | Report the issue or check if variable initialization is missing. |

### FAQ
**Q: Does this script change my local files?**
A: **No.** Even when using the "Git Version (Temp)" option, the script only writes to a temporary `backups/` folder. Your local working directory is never modified.

**Q: Can I use this for non-Git projects?**
A: No, the core logic relies on Git HEAD as a source of truth.

**Q: Why does the script say DIFF SIZE when the files look identical?**
A: Likely a line-ending mismatch (Windows CRLF vs Linux LF). Use the included `diff_normalized.py` for a more accurate comparison.

---

## Complete Argument Reference

### Scope & Mode Flags
*   `--workingDir <path>`: **Required**. Path to your local Git repo.
*   `--vsGit <file>`: Use Git HEAD/commit as the source of truth (Safety Check).
*   `--updateHashFile <file>`: Use Local Working Directory as source of truth (Verification).
*   `--vsHashFile <file>`: Use a previously saved JSON snapshot (Audit).

### Advanced Options
*   `--ftpConfig <file>`: Path to your FTP JSON config.
*   `--gitCommitHash <hash>`: Use a specific commit version instead of `HEAD`.
*   `--gitBaselineHash <hash>`: Define a specific "expected" remote state.
*   `--deployOnClean`: Prompts to upload local changes if the remote is "safe."
*   **`--checkSizeOnly`**: Faster, size-only comparison (warning: inaccurate across platforms).

---

## Appendix: `diff_normalized.py`

A standalone utility included in this repo to compare two files (or a file vs git) while **ignoring line endings (`\r\n` vs `\n`)**. This is useful for verifying `DIFF SIZE` warnings.

**Usage**:
```bash
python diff_normalized.py file1.php file2.php
python diff_normalized.py file.php --vsGit HEAD
```


