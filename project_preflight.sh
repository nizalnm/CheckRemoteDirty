#!/bin/bash
# ==============================================================================================
# PROJECT PREFLIGHT CHECK (Safety Check)
# ==============================================================================================
# This script checks if your local dirty files match the remote server's state (via Git HEAD).
# Use this BEFORE deploying to ensure you are not overwriting someone else's changes.
#
# CONFIGURATION:
# 1. Replace "/path/to/your/project" with the absolute path to your project's git root.
# 2. Replace "project.json" with a filename to store the hash snapshot (e.g. "myapp.json").
# 3. Replace "ftp_config.json" with the path to your FTP config file.
# ==============================================================================================

python3 CheckRemoteDirty.py --workingDir "/path/to/your/project" --vsGit "project.json" --ftpConfig "ftp_config.json"
