import argparse
import subprocess
import hashlib
import os
import json
import ftplib
import datetime
import sys
from ftplib import FTP_TLS

def calculate_file_hash_and_size(filepath):
    """Calculates MD5 hash and size of a file."""
    hash_md5 = hashlib.md5()
    size = 0
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
                size += len(chunk)
        return hash_md5.hexdigest(), size
    except FileNotFoundError:
        return None, None

def get_file_timestamp(filepath):
    """Returns the last modified timestamp of a file in ISO 8601 format."""
    try:
        timestamp = os.path.getmtime(filepath)
        return datetime.datetime.fromtimestamp(timestamp).isoformat()
    except FileNotFoundError:
        return None

def get_git_dirty_files(working_dir):
    """
    Returns a list of relative file paths that are dirty (modified/added) 
    in the git repository at working_dir.
    """
    try:
        # Check for both modified and untracked files
        # -uall shows untracked files
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        dirty_files = []
        for line in result.stdout.splitlines():
            # git status --porcelain format: XY PATH
            # X = index status, Y = work tree status
            # We care about the path, which starts at index 3
            if len(line) > 3:
                # Handle potential quoting in paths
                path = line[3:].strip('"')
                dirty_files.append(path)
                
        return dirty_files
    except subprocess.CalledProcessError as e:
        print(f"Error running git status: {e.stderr}")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: 'git' command not found. Please ensure git is installed.")
        sys.exit(1)

def get_git_file_content(repo_path, rel_path):
    """
    Returns the content of the file at rel_path from HEAD in the repo at repo_path.
    Returns None if file is not in HEAD.
    """
    try:
        # Use simple forward slashes for git pathspec
        git_path = rel_path.replace('\\', '/')
        result = subprocess.run(
            ["git", "show", f"HEAD:{git_path}"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None

def get_git_file_timestamp(repo_path, rel_path):
    """
    Returns the commit timestamp of the file at rel_path from HEAD in ISO 8601 format.
    Returns None if file is not in HEAD.
    """
    try:
        git_path = rel_path.replace('\\', '/')
        # -1 means last commit, %aI is ISO 8601 author date
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "HEAD", "--", git_path],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def load_json(filepath):
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r') as f:
        return json.load(f)

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

def compare_with_ftp(ftp_config_path, file_data_list, check_size_only=False):
    """
    Compares local files (from hash file or git status) with remote FTP files.
    """
    config = load_json(ftp_config_path)
    if not config:
        print(f"Error: Could not load FTP config from {ftp_config_path}")
        return

    try:
        ftp = connect_ftp(config)
        remote_root = config.get('remote_root', '/')
        
        print("\n--- FTP Comparison Results ---")
        print(f"{'File':<40} | {'Status':<15} | {'Remote Info':<30}")
        print("-" * 90)

        for item in file_data_list:
            rel_path = item['path'].replace('\\', '/')
            remote_path = f"{remote_root}/{rel_path}".replace('//', '/')
            
            local_hash = item.get('hash', 'N/A')
            local_ts = item.get('timestamp', 'N/A')
            local_size = item.get('size', None)
            
            # Check if file exists remotely and get basic info
            remote_size = None
            remote_mtime = None
            remote_hash = None # Requires download
            
            try:
                # Try to get size
                remote_size = ftp.size(remote_path)
                
                # Check Size Only logic
                if check_size_only:
                    if local_size is not None and remote_size is not None:
                        if local_size != remote_size:
                            print(f"{rel_path:<40} | {'DIFF SIZE':<15} | Local: {local_size} vs Remote: {remote_size}")
                        else:
                            print(f"{rel_path:<40} | {'MATCH (Size)':<15} | Size: {local_size}")
                    else:
                         print(f"{rel_path:<40} | {'MISSING/UNK':<15} | Cannot compare size")
                    
                    # Skip hash check
                    continue

                # Try to get modification time (MDTM yyyyMMddHHmmss)
                mdtm_resp = ftp.voidcmd(f"MDTM {remote_path}")
                if mdtm_resp.startswith('213'):
                     remote_mtime = mdtm_resp.split()[1]
                
                # Check hash (Standard mode)
                h_md5 = hashlib.md5()
                def handle_binary(more_data):
                    h_md5.update(more_data)

                ftp.retrbinary(f"RETR {remote_path}", handle_binary)
                remote_hash = h_md5.hexdigest()

                status = "MATCH"
                details = f"Hash: {remote_hash[:8]}..."
                
                if local_hash != remote_hash:
                    status = "DIFF HASH"
                
                # Check timestamp logic if needed, but hash is authoritative for content
                
            except ftplib.error_perm as e:
                status = "MISSING"
                details = f"Remote error: {str(e)[:20]}..."
                remote_hash = "N/A"

            print(f"{rel_path:<40} | {status:<15} | {details}")

        ftp.quit()

    except Exception as e:
        print(f"FTP Error: {e}")

def main():
    parser = argparse.ArgumentParser(description="CheckStagingDirty: Check local git dirty state vs remote FTP.")
    
    parser.add_argument("--workingDir", required=True, help="Local project directory with git repo.")
    parser.add_argument("--vsGit", help="Path to create/overwrite hashfile based on current git dirty files.")
    parser.add_argument("--vsHashFile", help="Path to existing hashfile to compare against.")
    parser.add_argument("--updateHashFile", help="Update existing hashfile with current git dirty files' hash/timestamp.")
    parser.add_argument("--ftpConfig", help="Path to FTP config JSON file.")
    parser.add_argument("--checkSizeOnly", action="store_true", help="If set, only compares file sizes. Faster but less accurate regarding content equality (ignores line endings issues).")

    args = parser.parse_args()
    working_dir = os.path.abspath(args.workingDir)

    if not os.path.isdir(working_dir):
        print(f"Error: Working directory {working_dir} does not exist.")
        sys.exit(1)

    affected_files_data = []

    # MODE: vsGit
    if args.vsGit:
        print(f"Scanning for dirty files in {working_dir}...")
        dirty_files = get_git_dirty_files(working_dir)
        
        for rel_path in dirty_files:
            # For vsGit mode, we now fetch content/timestamp from HEAD
            file_content = get_git_file_content(working_dir, rel_path)
            
            if file_content is not None:
                file_hash = hashlib.md5(file_content).hexdigest()
                file_size = len(file_content)
                timestamp = get_git_file_timestamp(working_dir, rel_path)
                
                affected_files_data.append({
                    "path": rel_path,
                    "hash": file_hash,
                    "size": file_size,
                    "timestamp": timestamp
                })
            else:
                # File present in dirty list (e.g. added/staged) but not in HEAD.
                # We skip it because we are strict about comparing HEAD vs Remote.
                # (Or user might expect it to show as 'missing in HEAD' but specs imply HEAD content)
                print(f"Skipping {rel_path} (not found in HEAD)")
        
        save_json(args.vsGit, affected_files_data)
        print(f"Saved {len(affected_files_data)} dirty file records to {args.vsGit}")

    # MODE: vsHashFile
    elif args.vsHashFile:
        print(f"Loading hash file from {args.vsHashFile}...")
        affected_files_data = load_json(args.vsHashFile)
        if affected_files_data is None:
            print("Error: Hash file not found.")
            sys.exit(1)
        print(f"Loaded {len(affected_files_data)} records.")

    # MODE: updateHashFile
    elif args.updateHashFile:
        print(f"Updating hash file {args.updateHashFile}...")
        
        # Load existing if available
        existing_data = load_json(args.updateHashFile) or []
        existing_map = {item['path']: item for item in existing_data}
        
        # Get current dirty files
        dirty_files = get_git_dirty_files(working_dir)
        
        # Update map
        for rel_path in dirty_files:
            abs_path = os.path.join(working_dir, rel_path)
            file_hash, file_size = calculate_file_hash_and_size(abs_path)
            timestamp = get_file_timestamp(abs_path)
            
            if file_hash:
                existing_map[rel_path] = {
                    "path": rel_path,
                    "hash": file_hash,
                    "size": file_size,
                    "timestamp": timestamp
                }
        
        affected_files_data = list(existing_map.values())
        save_json(args.updateHashFile, affected_files_data)
        print(f"Updated hash file. Total records: {len(affected_files_data)}")

    else:
        print("Error: Must specify one of --vsGit, --vsHashFile, or --updateHashFile")
        parser.print_help()
        sys.exit(1)

    # FTP Comparison Step
    if args.ftpConfig: 
        if affected_files_data:
            compare_with_ftp(args.ftpConfig, affected_files_data, check_size_only=args.checkSizeOnly)
        else:
            print("No file data to compare with FTP.")

if __name__ == "__main__":
    main()
