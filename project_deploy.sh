#!/bin/bash
# ==============================================================================================
# PROJECT DEPLOYMENT
# ==============================================================================================
# This script checks if remote files are clean (match Git HEAD).
# If clean, it prompts to DEPLOY your local dirty files to the server.
# Includes automatic backup and verification.
#
# CONFIGURATION:
# 1. Replace "/path/to/your/project" with the absolute path to your project's git root.
# 2. Replace "project.json" with a filename to store the hash snapshot.
# 3. Replace "ftp_config.json" with the path to your FTP config file.
# 4. --deployOnClean: This flag enables the deployment prompt.
# ==============================================================================================

python3 CheckRemoteDirty.py --workingDir "/path/to/your/project" --vsGit "project.json" --ftpConfig "ftp_config.json" --deployOnClean
