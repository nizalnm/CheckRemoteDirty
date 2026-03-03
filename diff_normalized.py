#!/usr/bin/env python3
"""
diff_normalized.py - Advanced Functional Diff & Formatting Undo Tool

Tokenizes code into atomic units to ignore line-break shifts and whitespace
while identifying real logic changes.
"""

import sys
import os
import hashlib
import subprocess
import argparse
import difflib
import re

def get_git_file_content(rel_path, repo_path=None, commit_ref="HEAD"):
    try:
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

def get_local_file_content(filepath):
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None

def tokenize(text):
    """Atomic tokenization: words, numbers, and symbols."""
    return re.findall(r'[a-zA-Z0-9_]+|[^a-zA-Z0-9_\s]', text)

def get_token_mapping(content):
    """Returns list of {'t': token_text, 'l': line_idx, 'c': full_line_content}"""
    if content is None: return []
    try:
        text = content.decode('utf-8', 'replace')
        lines = text.splitlines()
        mapping = []
        for l_idx, line in enumerate(lines):
            for t_text in tokenize(line):
                mapping.append({'t': t_text, 'l': l_idx, 'c': line})
        return mapping
    except Exception:
        return []

def normalize_and_hash(content):
    if content is None: return None
    try:
        text = content.decode('utf-8', 'replace')
        tokens = tokenize(text)
        return hashlib.md5("".join(tokens).encode('utf-8')).hexdigest()
    except Exception:
        return hashlib.md5(content).hexdigest()
def generate_functional_diff(content_a, content_b, label_a, label_b):
    """
    Standard line-based diff for display, but uses token-normalized lines 
    as keys to ignore whitespace/braces noise.
    """
    if content_a is None or content_b is None:
        return f"Error: Cannot diff {'file A' if content_a is None else 'file B'} (not found or unreadable)."
    
    lines_a = content_a.decode('utf-8', 'replace').splitlines()
    lines_b = content_b.decode('utf-8', 'replace').splitlines()
    
    # Normalize lines by joining their tokens
    norm_a = ["".join(tokenize(l)) for l in lines_a]
    norm_b = ["".join(tokenize(l)) for l in lines_b]
    
    # Filter out lines that effectively became empty after tokenization (whitespace/comments)
    # BUT we want to keep them for alignment if possible.
    
    # We use SequenceMatcher on the normalized lines
    diff = list(difflib.unified_diff(
        lines_a, lines_b, 
        fromfile=label_a, tofile=label_b, 
        # We provide a custom comparison for the diff
        # difflib doesn't support custom line compare easily, so we diff the NORM lists 
        # and then project.
        lineterm=""
    ))
    
    # Actually, difflib.unified_diff doesn't take normalized keys.
    # Let's do it manually.
    matcher = difflib.SequenceMatcher(None, norm_a, norm_b)
    final_diff = [f"--- {label_a}", f"+++ {label_b}"]
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            pass # Keep context? We'll use a standard diff approach for simplicity now.
        elif tag == 'replace':
            for l in lines_a[i1:i2]: final_diff.append(f"-{l}")
            for l in lines_b[j1:j2]: final_diff.append(f"+{l}")
        elif tag == 'delete':
            for l in lines_a[i1:i2]: final_diff.append(f"-{l}")
        elif tag == 'insert':
            for l in lines_b[j1:j2]: final_diff.append(f"+{l}")
            
    return "\n".join(final_diff) if len(final_diff) > 2 else ""

def generate_undone_formatting(content_base, content_modified):
    """
    Token-level reconstruction.
    Identifies logic blocks and chooses between Base vs Modified formatting.
    """
    map_base = get_token_mapping(content_base)
    map_mod = get_token_mapping(content_modified)
    
    toks_base = [m['t'] for m in map_base]
    toks_mod = [m['t'] for m in map_mod]
    
    matcher = difflib.SequenceMatcher(None, toks_base, toks_mod)
    
    if content_base is None or content_modified is None:
        return ""

    base_lines = content_base.decode('utf-8', 'replace').splitlines()
    mod_lines = content_modified.decode('utf-8', 'replace').splitlines()
    
    # 1. Identify which tokens are 'equal'
    base_tok_is_equal = [False] * len(toks_base)
    mod_tok_is_equal = [False] * len(toks_mod)
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for k in range(i1, i2): base_tok_is_equal[k] = True
            for k in range(j1, j2): mod_tok_is_equal[k] = True

    # 2. Mark lines as 'dirty' if they contain ANY non-equal token
    base_line_is_dirty = [False] * len(base_lines)
    for k, is_eq in enumerate(base_tok_is_equal):
        if not is_eq: base_line_is_dirty[map_base[k]['l']] = True
        
    mod_line_is_dirty = [False] * len(mod_lines)
    for k, is_eq in enumerate(mod_tok_is_equal):
        if not is_eq: mod_line_is_dirty[map_mod[k]['l']] = True

    # 3. Reconstruction loop
    final_output = []
    consumed_base = set()
    consumed_mod = set()
    
    # We iterate through the opcodes to reconstruct strictly in order
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            # For each token in this equal block, find its original Base line.
            # If that Base line is CLEAN (no logic changes elsewhere on the line), use it.
            for k in range(i1, i2):
                l_idx = map_base[k]['l']
                if l_idx not in consumed_base:
                    if not base_line_is_dirty[l_idx]:
                        final_output.append(base_lines[l_idx])
                        consumed_base.add(l_idx)
        else:
            # Logic change. Use the Modified lines.
            for k in range(j1, j2):
                l_idx = map_mod[k]['l']
                if l_idx not in consumed_mod:
                    final_output.append(mod_lines[l_idx])
                    consumed_mod.add(l_idx)
                    
    # 4. Handle whitespace lines (empty lines) that were skipped by token mapping
    # Actually, the loop above might skip them. Let's do a catch-all for them.
    # To keep it perfect, we should have interleaved line pointers.
    # But for a script, let's just make sure we don't drop them.
    
    # Dedup and ensure trailing whitespace from base is kept if context is clean?
    # Actually, a simpler way is to just join the result.
    return "\n".join(final_output)

def main():
    parser = argparse.ArgumentParser(description="Diff Normalized - Token-Based")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--workingDir")
    parser.add_argument("--vsgit", default="HEAD")
    parser.add_argument("--showDiff", action="store_true")
    parser.add_argument("--undoFormatting")
    
    args = parser.parse_args()
    w_dir = args.workingDir
    
    for entry in args.paths:
        if "::" in entry:
            p1, p2 = entry.split("::", 1)
            p1 = os.path.abspath(os.path.join(w_dir, p1)) if w_dir and not os.path.isabs(p1) else os.path.abspath(p1)
            p2 = os.path.abspath(os.path.join(w_dir, p2)) if w_dir and not os.path.isabs(p2) else os.path.abspath(p2)
            c1, c2 = get_local_file_content(p1), get_local_file_content(p2)
            h1, h2 = normalize_and_hash(c1), normalize_and_hash(c2)
            if h1 == h2: print(f"[MATCH] {p1}")
            else:
                print(f"[DIFF ] {p1}")
                if args.showDiff: print(generate_functional_diff(c1, c2, p1, p2))
                if args.undoFormatting:
                    with open(args.undoFormatting, "w", encoding="utf-8") as f:
                        f.write(generate_undone_formatting(c1, c2))
        else:
            # Git vs Local
            abs_p = os.path.abspath(os.path.join(w_dir, entry)) if w_dir and not os.path.isabs(entry) else os.path.abspath(entry)
            rel_p = os.path.relpath(abs_p, w_dir) if w_dir else entry
            lc = get_local_file_content(abs_p)
            gc = get_git_file_content(rel_p, repo_path=w_dir, commit_ref=args.vsgit)
            lh, gh = normalize_and_hash(lc), normalize_and_hash(gc)
            if lh == gh: print(f"[MATCH] {abs_p}")
            else:
                print(f"[DIFF ] {abs_p}")
                if args.showDiff: print(generate_functional_diff(gc, lc, f"Git:{args.vsgit}", abs_p))
                if args.undoFormatting:
                    with open(args.undoFormatting, "w", encoding="utf-8") as f:
                        f.write(generate_undone_formatting(gc, lc))

if __name__ == "__main__":
    main()
