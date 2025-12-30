#!/bin/bash
# ==============================================================================================
# PROJECT POST-DEPLOY VERIFICATION
# ==============================================================================================
# This script verifies that the files you just deployed to the server match your local files.
# Use this AFTER deployment to confirm everything uploaded correctly.
#
# CONFIGURATION:
# 1. Replace "/path/to/your/project" with the absolute path to your project's git root.
# 2. Replace "project.json" with the filename of your hash snapshot.
# 3. Replace "ftp_config.json" with the path to your FTP config file.
# 4. --updateHashFile: Updates the json file with current local hashes for verification.
# ==============================================================================================

python3 CheckRemoteDirty.py --workingDir "/path/to/your/project" --updateHashFile "project.json" --ftpConfig "ftp_config.json"
