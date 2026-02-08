#!/usr/bin/env python3
import sys
import os
import hashlib
import subprocess
import argparse

def get_git_file_content(rel_path, repo_path=None, commit_ref="HEAD"):
    """Returns the content of the file at rel_path from commit_ref."""
    try:
        # Use forward slashes for git pathspec
        git_path = rel_path.replace('\\', '/')
        result = subprocess.run(
            ["git", "show", f"{commit_ref}:{git_path}"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None

def normalize_and_hash(content):
    """
    Calculates normalized MD5 hash.
    Normalization: Trim leading/trailing whitespace from each line and remove line breaks.
    """
    if content is None:
        return None
    try:
        # Attempt to decode as UTF-8 to handle line-by-line trimming
        text = content.decode('utf-8')
        # Strip each line and join them without any separators
        lines = [line.strip() for line in text.splitlines()]
        # Remove empty lines as well? "Nonfunctional" often includes empty lines.
        # Let's keep it safe but clean: join non-empty lines if we want strictness,
        # but joining all stripped lines is usually what's expected.
        norm_text = "".join(lines)
        return hashlib.md5(norm_text.encode('utf-8')).hexdigest()
    except UnicodeDecodeError:
        # Fallback to basic CR/LF removal if it's binary content
        norm = content.replace(b'\r', b'').replace(b'\n', b'')
        return hashlib.md5(norm).hexdigest()

def get_local_file_content(filepath):
    """Reads binary content of a local file."""
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None

def resolve_rel_path(filepath, working_dir):
    """
    Converts filepath to a path relative to working_dir if it's an absolute path within it.
    Otherwise returns filepath as is.
    """
    if not working_dir:
        return filepath
        
    abs_wd = os.path.abspath(working_dir)
    abs_fp = os.path.abspath(filepath)
    
    if abs_fp.startswith(abs_wd):
        return os.path.relpath(abs_fp, abs_wd)
    return filepath

def main():
    parser = argparse.ArgumentParser(description="Normalized Diff Script")
    parser.add_argument("paths", nargs="+", help="List of file paths or file1::file2 pairs.")
    parser.add_argument("--vsGitHash", "--vsgit", dest="vsgit", default="HEAD", help="Git commit hash to compare against (default: HEAD).")
    parser.add_argument("--workingDir", "--workingdir", dest="workingDir", help="Optional. The git repository root.")
    
    args = parser.parse_args()
    
    working_dir = args.workingDir
    results = []
    
    for entry in args.paths:
        if "::" in entry:
            # Compare two local files
            path1, path2 = entry.split("::", 1)
            content1 = get_local_file_content(path1)
            content2 = get_local_file_content(path2)
            
            hash1 = normalize_and_hash(content1)
            hash2 = normalize_and_hash(content2)
            
            label = f"{path1} vs {path2}"
            if hash1 is None:
                results.append(f"[ERROR] File not found: {path1}")
            elif hash2 is None:
                results.append(f"[ERROR] File not found: {path2}")
            elif hash1 == hash2:
                results.append(f"[MATCH] {label}")
            else:
                results.append(f"[DIFF ] {label} (different hash)")
        else:
            # Compare against Git
            abs_path = entry
            # Resolve relative path for Git
            rel_path = resolve_rel_path(abs_path, working_dir)
            
            local_content = get_local_file_content(abs_path)
            git_content = get_git_file_content(rel_path, repo_path=working_dir, commit_ref=args.vsgit)
            
            local_hash = normalize_and_hash(local_content)
            git_hash = normalize_and_hash(git_content)
            
            label = f"{abs_path} vs Git {args.vsgit}"
            if local_content is None:
                results.append(f"[ERROR] Local file not found: {abs_path}")
            elif git_content is None:
                results.append(f"[ERROR] Git file not found: {rel_path} in {args.vsgit}")
            elif local_hash == git_hash:
                results.append(f"[MATCH] {label}")
            else:
                results.append(f"[DIFF ] {label} (different hash)")

    # Print buffered results
    for res in results:
        print(res)

if __name__ == "__main__":
    main()
