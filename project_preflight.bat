@echo off
REM ==============================================================================================
REM PROJECT PREFLIGHT CHECK (Safety Check)
REM ==============================================================================================
REM This script checks if your local dirty files match the remote server's state (via Git HEAD).
REM Use this BEFORE deploying to ensure you are not overwriting someone else's changes.
REM
REM CONFIGURATION:
REM 1. Replace "C:\path\to\your\project" with the absolute path to your project's git root.
REM 2. Replace "project.json" with a filename to store the hash snapshot (e.g. "myapp.json").
REM 3. Replace "ftp_config.json" with the path to your FTP config file.
REM ==============================================================================================

python CheckRemoteDirty.py --workingDir "C:\path\to\your\project" --vsGit "project.json" --ftpConfig "ftp_config.json"

pause
