@echo off
REM ==============================================================================================
REM PROJECT POST-DEPLOY VERIFICATION
REM ==============================================================================================
REM This script verifies that the files you just deployed to the server match your local files.
REM Use this AFTER deployment to confirm everything uploaded correctly.
REM
REM CONFIGURATION:
REM 1. Replace "C:\path\to\your\project" with the absolute path to your project's git root.
REM 2. Replace "project.json" with the filename of your hash snapshot.
REM 3. Replace "ftp_config.json" with the path to your FTP config file.
REM 4. --updateHashFile: Updates the json file with current local hashes for verification.
REM ==============================================================================================

python CheckRemoteDirty.py --workingDir "C:\path\to\your\project" --updateHashFile "project.json" --ftpConfig "ftp_config.json"

pause
