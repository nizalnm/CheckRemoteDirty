@echo off
REM ==============================================================================================
REM PROJECT DEPLOYMENT
REM ==============================================================================================
REM This script checks if remote files are clean (match Git HEAD).
REM If clean, it prompts to DEPLOY your local dirty files to the server.
REM Includes automatic backup and verification.
REM
REM CONFIGURATION:
REM 1. Replace "C:\path\to\your\project" with the absolute path to your project's git root.
REM 2. Replace "project.json" with a filename to store the hash snapshot.
REM 3. Replace "ftp_config.json" with the path to your FTP config file.
REM 4. --deployOnClean: This flag enables the deployment prompt.
REM ==============================================================================================

python CheckRemoteDirty.py --workingDir "C:\path\to\your\project" --vsGit "project.json" --ftpConfig "ftp_config.json" --deployOnClean

pause
