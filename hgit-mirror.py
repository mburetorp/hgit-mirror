#!/usr/bin/env python3
"""
hg-to-git-mirror.py - Mirror a Mercurial repository (with subrepos) to a Git monorepo.

Usage:
  # First-time setup
  python hg-to-git-mirror.py --root /path/to/hg-project --mirror /path/to/git-mirror \
      --init [--recent-commits 100] [--remote https://github.com/user/repo.git]

  # Ongoing incremental sync
  python hg-to-git-mirror.py --root /path/to/hg-project --mirror /path/to/git-mirror

  # Interactive menu TUI
  python hg-to-git-mirror.py --root /path/to/hg-project --mirror /path/to/git-mirror \
      --interactive [--remote https://github.com/user/repo.git]

Each project's state is stored in <mirror>/.git/hg-mirror-state.json, so multiple
projects are fully isolated just by using different --mirror directories.

Works on Windows and FreeBSD/Unix. No pip dependencies - stdlib only.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def run_cmd(args: list, cwd: Path, capture: bool = True, dry_run: bool = False) -> str:
    """Run a command, return stdout. Raises on non-zero exit."""
    if dry_run:
        print(f"  [dry-run] {' '.join(str(a) for a in args)}  (cwd={cwd})")
        return ""
    result = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        cmd_str = " ".join(str(a) for a in args)
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd_str}\n"
            f"  cwd: {cwd}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result.stdout if capture else ""


def run_cmd_bytes(args: list, cwd: Path) -> bytes:
    """Run a command, return raw stdout bytes. Raises on non-zero exit."""
    result = subprocess.run(args, cwd=str(cwd), capture_output=True)
    if result.returncode != 0:
        cmd_str = " ".join(str(a) for a in args)
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {cmd_str}\n"
            f"  cwd: {cwd}\n"
            f"  stderr: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# .hgsub parsing
# ---------------------------------------------------------------------------

def read_hgsub(hgsub_path: Path) -> dict:
    """
    Parse a .hgsub file, returning {local_path: source_url}.
    Ignores the [subpaths] section and comment lines.
    """
    entries = {}
    if not hgsub_path.exists():
        return entries
    in_subpaths = False
    for line in hgsub_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("["):
            in_subpaths = True
            continue
        if in_subpaths:
            continue
        if "=" in line:
            local, _, source = line.partition("=")
            entries[local.strip()] = source.strip()
    return entries


def discover_subrepos(hg_root: Path) -> dict:
    """
    Recursively walk .hgsub files starting from hg_root.
    Returns a flat dict: {git_prefix: abs_hg_repo_path}
    The root repo itself is included with prefix "".

    Example return value for morphball:
      {
        "":              Path("D:/code/projects/morphball"),
        "code":          Path("D:/code/projects/morphball/code"),
        "data":          Path("D:/code/projects/morphball/data"),
        "framework":     Path("D:/code/projects/morphball/framework"),
        "framework/sdk": Path("D:/code/projects/morphball/framework/sdk"),
        "uncreation":    Path("D:/code/projects/morphball/uncreation"),
      }
    """
    result = {"": hg_root}
    _walk_hgsub(hg_root, "", result)
    return result


def _walk_hgsub(repo_path: Path, prefix: str, result: dict) -> None:
    hgsub = repo_path / ".hgsub"
    if not hgsub.exists():
        return
    for local_path, _source in read_hgsub(hgsub).items():
        subrepo_abs = repo_path / local_path
        git_prefix = f"{prefix}/{local_path}".lstrip("/") if prefix else local_path
        if subrepo_abs.exists():
            result[git_prefix] = subrepo_abs
            _walk_hgsub(subrepo_abs, git_prefix, result)


# ---------------------------------------------------------------------------
# HG helpers
# ---------------------------------------------------------------------------

HG_SKIP_FILES = {".hgsub", ".hgsubstate", ".hgeol", ".hgtags", ".hgignore"}


def _strip_graph_decorations(text: str) -> str:
    """
    Strip mercurial --graph decoration from `hg log -T json` output.

    When ui.graph=true is set in .hgrc, hg log intermingles graph characters with
    JSON in two ways:

        [@              <- JSON "[" + graph "@" on same line
        |               <- standalone graph line
        o               <- standalone graph line
        |   {           <- graph "|" prefix before JSON "{"
        ~    "field":   <- graph "~" prefix before JSON field

    Rules applied in order:
    1. "[<graph_char>" -> "[" (normalize the JSON array opener).
       Uses [ \\t]* not \\s* to avoid consuming the following newline.
    2. Strip leading graph-char column from mixed lines: "| \t]+<json>" -> "<json>".
    3. Drop lines that consist only of graph characters and whitespace.
    """
    # HG graph chars - must NOT include JSON-significant chars ([ ] { } , " digits)
    _GC = r'@o|/:+\\*_\-~x<>'  # used inside a character class [...] below

    # Rule 1: normalize "[<graph>" array-opener -> "[" (no newline consumed)
    text = re.sub(rf'^\[[{_GC}][ \t]*', '[', text, flags=re.MULTILINE)

    # Rule 2: strip leading graph-char column from mixed lines e.g. "|   {" -> "{"
    text = re.sub(rf'^[ \t]*[{_GC}][ \t]+', '', text, flags=re.MULTILINE)

    # Rule 3: drop pure-graph lines (only graph chars + whitespace, no JSON content)
    _pure_graph = re.compile(rf'^[\s{_GC}]*$')
    return '\n'.join(
        line for line in text.splitlines()
        if line.strip() and not _pure_graph.match(line)
    )


def get_hg_revisions(repo: Path, since_hash: Optional[str], limit: Optional[int],
                     extra_args: Optional[list] = None) -> list:
    """
    Return a list of dicts with keys: hash, date, author, message.
    Results are ordered oldest-first.

    If since_hash is given, returns changesets AFTER that hash (exclusive).
    If limit is given and since_hash is None, returns only the last N changesets.
    extra_args: additional arguments inserted before -T json (e.g. ["-r", "HASH"]).

    Uses JSON output (-T json) to avoid issues with user-configured display styles
    (e.g. --graph enabled globally in .hgrc).
    """
    if extra_args:
        cmd = ["hg", "log", "--config", "ui.color=never"] + extra_args + ["-T", "json"]
    elif since_hash:
        rev_spec = f"descendants({since_hash}) and not {since_hash}"
        cmd = ["hg", "log", "--config", "ui.color=never", "-r", rev_spec, "-T", "json"]
    elif limit:
        cmd = ["hg", "log", "--config", "ui.color=never", "-l", str(limit), "-T", "json"]
    else:
        cmd = ["hg", "log", "--config", "ui.color=never", "-T", "json"]

    try:
        output = run_cmd(cmd, cwd=repo)
    except RuntimeError as exc:
        msg = str(exc)
        if "abort" in msg.lower() or "error" in msg.lower():
            raise
        return []

    if not output.strip():
        return []

    output = _strip_graph_decorations(output)

    try:
        entries = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse hg log JSON from {repo}: {exc}\nOutput: {output[:500]}")

    revisions = []
    for entry in entries:
        # date is [unix_timestamp, utc_offset_seconds]
        ts, tz_offset = entry["date"]
        # Format as ISO-8601-ish for sorting; offset is in seconds (positive = west of UTC)
        dt = datetime.datetime.fromtimestamp(ts - tz_offset, tz=datetime.timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        revisions.append({
            "rev": entry.get("rev", 0),
            "hash": entry["node"],
            "date": date_str,
            "author": entry["user"],
            "message": entry["desc"].splitlines()[0] if entry["desc"] else "",
        })

    # Mercurial output defaults to newest-first. Apply oldest-first by local rev
    # number so patch prerequisites are already present even when commit dates
    # are skewed.
    revisions.sort(key=lambda rev: rev["rev"])

    return revisions


def merge_commits_preserving_repo_order(commits_by_prefix: dict) -> list:
    """
    Merge per-repo commit queues by date while preserving each repo's rev order.

    A plain global date sort can apply a child before its parent when Mercurial
    commit dates are skewed. This keeps each repo's next unapplied commit as the
    only candidate from that repo, then picks the oldest candidate by date.
    """
    indexes = {prefix: 0 for prefix in commits_by_prefix}
    merged = []

    while True:
        candidates = []
        for prefix, commits in commits_by_prefix.items():
            index = indexes[prefix]
            if index < len(commits):
                candidates.append(commits[index])

        if not candidates:
            return merged

        chosen = min(candidates, key=lambda commit: commit[0])
        merged.append(chosen)
        indexes[chosen[1]] += 1


def export_hg_patch(repo: Path, rev_hash: str) -> bytes:
    """Return raw bytes of the git-format patch for a single HG changeset."""
    return run_cmd_bytes(
        ["hg", "export", "--git", "-r", rev_hash],
        cwd=repo,
    )


# ---------------------------------------------------------------------------
# Patch path rewriting
# ---------------------------------------------------------------------------

def rewrite_patch_paths(patch_bytes: bytes, prefix: str) -> bytes:
    """
    Given a git-format patch and a subdirectory prefix, rewrite all file paths
    so they appear under that prefix inside the monorepo.

    Also strips changes to HG-specific files (.hgsub, .hgsubstate, .hgeol, .hgtags).

    If prefix is "" (root repo), path rewriting is skipped but file stripping still applies.
    """
    patch_text = patch_bytes.decode("utf-8", errors="replace")
    lines = patch_text.splitlines(keepends=True)
    output_lines = []
    skip_until_next_diff = False

    for line in lines:
        # Detect start of a file diff section
        if line.startswith("diff --git "):
            skip_until_next_diff = _should_skip_diff_line(line)
            if skip_until_next_diff:
                continue
            if prefix:
                line = _rewrite_diff_git_line(line, prefix)
        elif skip_until_next_diff:
            continue
        elif prefix:
            line = _rewrite_path_line(line, prefix)

        output_lines.append(line)

    return "".join(output_lines).encode("utf-8")


def _should_skip_diff_line(line: str) -> bool:
    """Return True if this diff --git line references a file we want to exclude."""
    # Extract filenames from: diff --git a/foo b/foo
    match = re.match(r"diff --git a/(.+?) b/(.+)", line)
    if not match:
        return False
    filename = Path(match.group(1)).name
    return filename in HG_SKIP_FILES


def _rewrite_diff_git_line(line: str, prefix: str) -> str:
    """Rewrite the 'diff --git a/X b/X' header line."""
    return re.sub(
        r"(diff --git a/)(.+?)( b/)(.+)",
        lambda m: f"{m.group(1)}{prefix}/{m.group(2)}{m.group(3)}{prefix}/{m.group(4)}",
        line,
    )


_PATH_LINE_PREFIXES = ("--- a/", "+++ b/", "--- /dev/null", "+++ /dev/null",
                       "rename from ", "rename to ", "copy from ", "copy to ")


def _rewrite_path_line(line: str, prefix: str) -> str:
    """Rewrite a/b path lines in a patch to include the subdirectory prefix."""
    for pat in ("--- a/", "+++ b/"):
        if line.startswith(pat):
            return pat + prefix + "/" + line[len(pat):]
    for pat in ("rename from ", "rename to ", "copy from ", "copy to "):
        if line.startswith(pat):
            return pat + prefix + "/" + line[len(pat):]
    return line


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def apply_snapshot_commit(
    mirror: Path,
    repo: Path,
    rev_hash: str,
    prefix: str,
    author: str,
    date_str: str,
    message: str,
    all_prefixes: set,
    dry_run: bool = False,
) -> None:
    """
    Apply the full file tree of a repo at rev_hash as a Git commit.
    Used for the first commit from each subrepo, where we can't apply a diff because
    earlier prerequisite commits weren't included in the seed window.

    Uses `hg archive` to get all tracked files at the revision, copies them
    to the correct subdirectory in the mirror, then commits with HG metadata.

    `all_prefixes` is the full set of subrepo prefixes so that child subrepo
    directories (e.g. "framework/sdk" when prefix is "framework") are preserved
    during cleanup and not accidentally staged as deletions.
    """
    dest = (mirror / prefix) if prefix else mirror

    # Determine direct child subrepo directory names to preserve
    base = f"{prefix}/" if prefix else ""
    child_dir_names = {
        p[len(base):].split("/")[0]
        for p in all_prefixes
        if p.startswith(base) and p != prefix
    }

    if dry_run:
        print(f"  [dry-run] hg archive -r {rev_hash[:12]}  -> {dest}  (preserving: {child_dir_names})")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        snapshot_dir = Path(tmpdir) / "snap"
        run_cmd(
            ["hg", "archive", "-r", rev_hash, "-t", "files", str(snapshot_dir)],
            cwd=repo,
        )
        # Selectively clean the destination, preserving child subrepo directories
        # and the .git directory (present at mirror root when prefix is "")
        if dest.exists():
            for item in list(dest.iterdir()):
                if item.name == ".git":
                    continue  # never touch the git repo itself
                if item.is_dir() and item.name in child_dir_names:
                    continue  # preserve child subrepo contents
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
        else:
            dest.mkdir(parents=True, exist_ok=True)

        # Copy tracked files, skipping HG-specific metadata
        for src in snapshot_dir.rglob("*"):
            if not src.is_file():
                continue
            if src.name in HG_SKIP_FILES or src.name == ".hg_archival.txt":
                continue
            rel = src.relative_to(snapshot_dir)
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Stage everything under the prefix (or root), skipping child subrepo paths
    stage_path = str(prefix) if prefix else "."
    run_cmd(["git", "-c", "core.autocrlf=false", "add", stage_path], cwd=mirror)

    # Commit with original HG author and date
    author_name, author_email = _split_author(author)
    run_cmd(
        [
            "git", "commit",
            "--allow-empty",
            "-m", message,
            "--author", f"{author_name} <{author_email}>",
            "--date", date_str,
        ],
        cwd=mirror,
    )


def _split_author(author: str) -> tuple:
    """Split 'Name <email>' into (name, email). Falls back gracefully."""
    match = re.match(r"^(.*?)\s*<([^>]*)>", author)
    if match:
        return match.group(1).strip() or "Unknown", match.group(2).strip()
    return author.strip() or "Unknown", "unknown@unknown"


def apply_patch(mirror: Path, patch_bytes: bytes, dry_run: bool = False) -> bool:
    """
    Apply a git-format patch to the mirror repo using git am.
    Returns True on success, False if the patch had nothing to apply (empty diff).
    Raises on error.
    """
    if not patch_bytes.strip():
        return False

    with tempfile.NamedTemporaryFile(suffix=".patch", delete=False, mode="wb") as f:
        f.write(patch_bytes)
        patch_path = f.name

    try:
        if dry_run:
            print(f"  [dry-run] git am {patch_path}  (cwd={mirror})")
            return True
        try:
            run_cmd(
                ["git", "am", "--no-gpg-sign", "--keep-cr", "--keep", patch_path],
                cwd=mirror,
            )
            return True
        except RuntimeError as exc:
            # If the patch is empty or already applied, skip it.
            if "does not apply" in str(exc) or "nothing to apply" in str(exc).lower():
                if (mirror / ".git" / "rebase-apply").exists():
                    run_cmd(["git", "am", "--abort"], cwd=mirror, dry_run=False)
                print(f"  Warning: patch did not apply cleanly, skipping.")
                return False
            raise
    finally:
        os.unlink(patch_path)


def init_git_repo(mirror: Path, dry_run: bool = False) -> None:
    """Initialize a new bare-ish Git repo (with a working tree) in mirror."""
    if dry_run:
        print(f"  [dry-run] git init --initial-branch=main {mirror}")
        return
    mirror.mkdir(parents=True, exist_ok=True)
    # Try --initial-branch (git >= 2.28); fall back for older git
    try:
        run_cmd(["git", "init", "--initial-branch=main", str(mirror)], cwd=mirror.parent)
    except RuntimeError:
        run_cmd(["git", "init", str(mirror)], cwd=mirror.parent)
        try:
            run_cmd(["git", "checkout", "-b", "main"], cwd=mirror)
        except RuntimeError:
            pass  # Already on main or branch rename not needed
    run_cmd(["git", "config", "core.autocrlf", "false"], cwd=mirror)


# ---------------------------------------------------------------------------
# HG metadata -> Git metadata conversion (.hgeol -> .gitattributes, .hgignore -> .gitignore)
# ---------------------------------------------------------------------------

def _gitattributes_from_hgeol(hgeol_path: Path) -> Optional[str]:
    """Parse .hgeol and return equivalent .gitattributes content, or None."""
    if not hgeol_path.exists():
        return None

    lines = hgeol_path.read_text(encoding="utf-8").splitlines()
    out = ["# Generated from .hgeol by hg-to-git-mirror.py", ""]
    in_patterns = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        if stripped.lower() == "[patterns]":
            in_patterns = True
            continue
        if stripped.startswith("["):
            in_patterns = False
            continue
        if in_patterns and "=" in stripped:
            pattern, _, eol = stripped.partition("=")
            pattern = pattern.strip()
            eol = eol.strip().lower()
            if eol in ("native", "crlf"):
                out.append(f"{pattern} text eol=crlf")
            elif eol == "lf":
                out.append(f"{pattern} text eol=lf")
            elif eol == "bin":
                out.append(f"{pattern} binary")

    if len(out) <= 2:
        return None
    return "\n".join(out) + "\n"


def _gitignore_from_hgignore(hgignore_path: Path) -> Optional[str]:
    """
    Parse .hgignore and return equivalent .gitignore content, or None.

    HG ignore files support two syntaxes (switchable mid-file):
    - 'glob'   - patterns similar to gitignore; mapped directly
    - 'regexp' - Python regexes; not convertible, emitted as comments

    The HG default syntax (if no 'syntax:' directive) is 'regexp'.
    """
    if not hgignore_path.exists():
        return None

    lines = hgignore_path.read_text(encoding="utf-8").splitlines()
    out = ["# Generated from .hgignore by hg-to-git-mirror.py", ""]
    current_syntax = "regexp"  # HG default
    had_regexp = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("#"):
            out.append(stripped)
            continue
        if stripped.startswith("syntax:"):
            current_syntax = stripped[7:].strip().lower()
            continue
        if current_syntax == "glob":
            out.append(stripped)
        else:
            out.append(f"# [regexp, not converted] {stripped}")
            had_regexp = True

    if had_regexp:
        out.insert(2, "# WARNING: some patterns were in regexp syntax and could not be converted.")
        out.insert(3, "")

    # Only content lines (not just the header)
    if all(l.startswith("#") or l == "" for l in out):
        return None
    return "\n".join(out) + "\n"


def apply_git_metadata(mirror: Path, repos: dict, dry_run: bool = False) -> None:
    """
    Convert HG metadata files to Git equivalents for all repos and commit them.

    For each repo:
      .hgeol  -> <prefix>/.gitattributes
      .hgignore -> <prefix>/.gitignore

    All generated files are staged and committed in a single "Add Git metadata" commit.
    """
    staged = []

    for prefix, repo_path in repos.items():
        dest_dir = (mirror / prefix) if prefix else mirror
        dest_dir.mkdir(parents=True, exist_ok=True)

        # .hgeol -> .gitattributes
        ga_content = _gitattributes_from_hgeol(repo_path / ".hgeol")
        if ga_content:
            ga_path = dest_dir / ".gitattributes"
            if dry_run:
                label = f"{prefix}/" if prefix else ""
                print(f"  [dry-run] write {label}.gitattributes (from .hgeol)")
            else:
                ga_path.write_text(ga_content, encoding="utf-8")
                staged.append(str(ga_path.relative_to(mirror)))

        # .hgignore -> .gitignore
        gi_content = _gitignore_from_hgignore(repo_path / ".hgignore")
        if gi_content:
            gi_path = dest_dir / ".gitignore"
            if dry_run:
                label = f"{prefix}/" if prefix else ""
                print(f"  [dry-run] write {label}.gitignore (from .hgignore)")
            else:
                gi_path.write_text(gi_content, encoding="utf-8")
                staged.append(str(gi_path.relative_to(mirror)))

    if not staged:
        return

    for path in staged:
        run_cmd(["git", "-c", "core.autocrlf=false", "add", path], cwd=mirror)
    run_cmd(
        ["git", "commit", "-m", "Add Git metadata (translated from .hgeol and .hgignore)"],
        cwd=mirror,
    )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

STATE_FILENAME = "hg-mirror-state.json"
REFRESH_BAT_FILENAME = "hg-mirror-refresh.bat"


def state_file_path(mirror: Path, override: Optional[Path]) -> Path:
    if override:
        return override
    return mirror / ".git" / STATE_FILENAME


def load_state(mirror: Path, override: Optional[Path]) -> dict:
    path = state_file_path(mirror, override)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "repos": {}}


def save_state(mirror: Path, override: Optional[Path], state: dict) -> None:
    path = state_file_path(mirror, override)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_refresh_bat(mirror: Path, hg_root: Path, remote: str) -> None:
    """
    Write (or update) <mirror>/.git/hg-mirror-refresh.bat so the user can
    re-run an incremental sync with a double-click, without remembering the
    full command line.  The file lives inside .git/ so it is never committed.
    """
    script = Path(__file__).resolve()
    remote_arg = f' --remote "{remote}"' if remote else ""
    content = (
        "@echo off\n"
        f'python "{script}" --root "{hg_root}" --mirror "{mirror}"{remote_arg}\n'
        "pause\n"
    )
    bat_path = mirror / ".git" / REFRESH_BAT_FILENAME
    bat_path.parent.mkdir(parents=True, exist_ok=True)
    bat_path.write_text(content, encoding="utf-8")
    print(f"Refresh script : {bat_path}")


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync_mirror(args: argparse.Namespace) -> None:
    hg_root = Path(args.root).resolve()
    mirror = Path(args.mirror).resolve()
    state_override = Path(args.state_file).resolve() if args.state_file else None
    dry_run = args.dry_run

    print(f"HG root  : {hg_root}")
    print(f"Mirror   : {mirror}")
    print(f"Mode     : {'initial setup' if args.init else 'incremental sync'}")
    print()

    # --- Initial setup ---
    if args.init:
        if (mirror / ".git").exists():
            print(f"Error: {mirror} already contains a Git repo. "
                  f"Use incremental sync (omit --init) or choose a different --mirror path.")
            sys.exit(1)
        print("Initializing Git mirror...")
        init_git_repo(mirror, dry_run=dry_run)

    # --- Discover subrepos ---
    print("Discovering subrepos...")
    repos = discover_subrepos(hg_root)
    for prefix, path in sorted(repos.items()):
        label = f'"{prefix}"' if prefix else "(root)"
        print(f"  {label:30s} -> {path}")
    print()

    # --- Load state ---
    state = load_state(mirror, state_override)

    # --- Collect all new commits across all repos ---
    print("Collecting new commits...")
    commits_by_prefix = {}  # prefix -> list of (date_str, prefix, repo_path, rev_hash, author, message)

    for prefix, repo_path in repos.items():
        since_hash = state["repos"].get(prefix)
        label = f'"{prefix}"' if prefix else "(root)"

        if args.init:
            revisions = get_hg_revisions(repo_path, since_hash=None, limit=args.recent_commits)
        else:
            revisions = get_hg_revisions(repo_path, since_hash=since_hash, limit=None)

        print(f"  {label:30s}: {len(revisions)} new commit(s)")
        repo_commits = []
        for rev in revisions:
            repo_commits.append((rev["date"], prefix, repo_path, rev["hash"], rev["author"], rev["message"]))
        commits_by_prefix[prefix] = repo_commits

    all_commits = merge_commits_preserving_repo_order(commits_by_prefix)

    if not all_commits:
        print("\nNothing to sync. Mirror is up to date.")
        return

    print(f"\nApplying {len(all_commits)} commit(s) in chronological order...")
    print()

    # --- Apply each commit ---
    applied = 0
    latest_hashes: dict = {}  # prefix -> hash of last successfully applied commit
    blocked_prefixes: set = set()

    for date_str, prefix, repo_path, rev_hash, author, message in all_commits:
        if prefix in blocked_prefixes:
            continue

        label = f'"{prefix}"' if prefix else "(root)"
        short_hash = rev_hash[:12]
        print(f"  [{date_str}] {label} {short_hash}  {message[:60]}")

        if args.init:
            # In init mode always use full snapshots - diffs can't apply cleanly
            # against a partial history (we're only seeding the last N commits).
            apply_snapshot_commit(mirror, repo_path, rev_hash, prefix,
                                  author, date_str, message,
                                  all_prefixes=set(repos.keys()),
                                  dry_run=dry_run)
            applied += 1
        else:
            patch_bytes = export_hg_patch(repo_path, rev_hash)
            rewritten = rewrite_patch_paths(patch_bytes, prefix)
            ok = apply_patch(mirror, rewritten, dry_run=dry_run)
            if ok:
                applied += 1
                latest_hashes[prefix] = rev_hash
            else:
                blocked_prefixes.add(prefix)

        if args.init:
            latest_hashes[prefix] = rev_hash

    # --- Apply Git metadata files last (initial setup only) ---
    # Done after history so snapshot commits don't wipe out the generated files.
    if args.init:
        apply_git_metadata(mirror, repos, dry_run=dry_run)

    # --- Update state ---
    for prefix, rev_hash in latest_hashes.items():
        state["repos"][prefix] = rev_hash

    if not dry_run:
        save_state(mirror, state_override, state)
    else:
        print(f"\n  [dry-run] save state to {state_file_path(mirror, state_override)}")

    print(f"\nApplied {applied} of {len(all_commits)} commit(s).")

    # --- Push to GitHub ---
    if args.remote:
        print(f"\nPushing to {args.remote} ...")
        if args.init:
            if not dry_run:
                try:
                    run_cmd(["git", "remote", "add", "origin", args.remote], cwd=mirror)
                except RuntimeError:
                    run_cmd(["git", "remote", "set-url", "origin", args.remote], cwd=mirror)
            run_cmd(["git", "push", "--set-upstream", "origin", "main"], cwd=mirror, dry_run=dry_run)
        else:
            run_cmd(["git", "push"], cwd=mirror, dry_run=dry_run)
        print("Push complete.")
    else:
        print("\nNo --remote specified; skipping push.")
        print(f"To push manually:  cd \"{mirror}\" && git push")

    # Write/update the convenience refresh bat so paths and remote stay current.
    if not dry_run:
        write_refresh_bat(mirror, hg_root, args.remote)


# ---------------------------------------------------------------------------
# Interactive TUI
# ---------------------------------------------------------------------------

def _get_pending_counts(hg_root: Path, repos: dict, state: dict) -> dict:
    """Return {prefix: count} of commits not yet mirrored."""
    counts = {}
    for prefix, repo_path in repos.items():
        since = state["repos"].get(prefix)
        try:
            revs = get_hg_revisions(repo_path, since_hash=since, limit=None)
            counts[prefix] = len(revs)
        except Exception:
            counts[prefix] = -1  # unknown
    return counts


def _get_remote_url(mirror: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(mirror), capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _short_desc(repos: dict, state: dict) -> list:
    """Return list of (prefix_label, hash_short, message_short, date_str) for display."""
    rows = []
    for prefix, repo_path in repos.items():
        h = state["repos"].get(prefix, "")
        label = f'"{prefix}"' if prefix else "(root)"
        if h:
            try:
                revs = get_hg_revisions(repo_path, since_hash=None, limit=1,
                                        extra_args=["-r", h])
                if revs:
                    rev = revs[0]
                    rows.append((label, h[:12], rev["message"][:55], rev["date"][:10]))
                    continue
            except Exception:
                pass
        rows.append((label, h[:12] if h else "-", "-", "-"))
    return rows


def _print_status(hg_root: Path, mirror: Path, repos: dict, state: dict, remote: str) -> int:
    """Print mirror status table. Returns total pending commit count."""
    os.system("cls" if os.name == "nt" else "clear")
    print("=" * 70)
    print("  hg-to-git-mirror  -  interactive mode")
    print("=" * 70)
    print(f"  HG root : {hg_root}")
    print(f"  Mirror  : {mirror}")
    print(f"  Remote  : {remote or 'not configured'}")
    print()

    # Pending counts
    pending = _get_pending_counts(hg_root, repos, state)
    total_pending = sum(v for v in pending.values() if v > 0)

    # Last-synced info
    rows = _short_desc(repos, state)
    col_w = max(len(r[0]) for r in rows) + 2 if rows else 12
    print(f"  {'Subrepo':<{col_w}} {'Hash':13} {'Date':11} {'Last commit message'}")
    print(f"  {'-'*col_w} {'-'*12} {'-'*10} {'-'*40}")
    for label, h, msg, date in rows:
        p_count = pending.get(label.strip('"') if '"' in label else "", 0)
        pending_str = f"  (+{p_count})" if p_count > 0 else ""
        print(f"  {label:<{col_w}} {h:13} {date:11} {msg}{pending_str}")
    print()
    if total_pending:
        print(f"  ! {total_pending} new commit(s) pending sync.")
    else:
        print("  OK Mirror is up to date.")
    print()
    return total_pending


def run_interactive(args: argparse.Namespace) -> None:
    hg_root = Path(args.root).resolve()
    mirror = Path(args.mirror).resolve()
    state_override = Path(args.state_file).resolve() if args.state_file else None
    remote = args.remote

    while True:
        # Reload state and repos each loop so display reflects latest changes
        repos = discover_subrepos(hg_root)
        state = load_state(mirror, state_override)
        cur_remote = _get_remote_url(mirror) or remote

        _print_status(hg_root, mirror, repos, state, cur_remote)

        initialized = (mirror / ".git").exists()
        print("  Actions:")
        if not initialized:
            print("  [1] Init        - create Git mirror and seed with recent history")
        else:
            print("  [1] Init        - (mirror already exists; re-init requires deleting it first)")
        print("  [2] Sync        - apply new HG commits to the mirror")
        print("  [3] Push        - push mirror to remote")
        print("  [4] Sync + Push - sync then push")
        print("  [5] Set remote  - configure GitHub remote URL")
        print("  [q] Quit")
        print()

        choice = input("  Choice: ").strip().lower()
        print()

        if choice == "q":
            break

        elif choice == "1":
            if initialized:
                print("  Mirror already initialised. Delete the mirror directory first to re-init.")
                input("  Press Enter to continue...")
                continue
            n_str = input(f"  Recent commits per repo (default {args.recent_commits}): ").strip()
            n = int(n_str) if n_str.isdigit() else args.recent_commits
            sub = argparse.Namespace(
                root=str(hg_root), mirror=str(mirror), remote=cur_remote,
                recent_commits=n, init=True, dry_run=False,
                state_file=str(state_override) if state_override else "",
            )
            try:
                sync_mirror(sub)
            except SystemExit:
                pass
            input("\n  Press Enter to continue...")

        elif choice == "2":
            sub = argparse.Namespace(
                root=str(hg_root), mirror=str(mirror), remote="",
                recent_commits=args.recent_commits, init=False, dry_run=False,
                state_file=str(state_override) if state_override else "",
            )
            try:
                sync_mirror(sub)
            except SystemExit:
                pass
            input("\n  Press Enter to continue...")

        elif choice == "3":
            if not cur_remote:
                print("  No remote configured. Use [5] to set one first.")
            else:
                sub = argparse.Namespace(
                    root=str(hg_root), mirror=str(mirror), remote=cur_remote,
                    recent_commits=args.recent_commits, init=False, dry_run=False,
                    state_file=str(state_override) if state_override else "",
                )
                # Push only - fake an already-synced run by setting all counts to 0
                # by directly calling the push logic
                try:
                    _do_push(mirror, cur_remote)
                except Exception as exc:
                    print(f"  Push failed: {exc}")
            input("\n  Press Enter to continue...")

        elif choice == "4":
            if not cur_remote:
                print("  No remote configured. Use [5] to set one first.")
                input("\n  Press Enter to continue...")
                continue
            sub = argparse.Namespace(
                root=str(hg_root), mirror=str(mirror), remote=cur_remote,
                recent_commits=args.recent_commits, init=False, dry_run=False,
                state_file=str(state_override) if state_override else "",
            )
            try:
                sync_mirror(sub)
            except SystemExit:
                pass
            input("\n  Press Enter to continue...")

        elif choice == "5":
            new_url = input(f"  Remote URL [{cur_remote}]: ").strip()
            if new_url:
                remote = new_url
                if initialized:
                    try:
                        run_cmd(["git", "remote", "add", "origin", remote], cwd=mirror)
                    except RuntimeError:
                        run_cmd(["git", "remote", "set-url", "origin", remote], cwd=mirror)
                    print(f"  Remote set to: {remote}")
            else:
                print("  No change.")
            input("  Press Enter to continue...")

        else:
            print("  Unknown choice. Please enter 1-5 or q.")
            input("  Press Enter to continue...")


def _do_push(mirror: Path, remote: str) -> None:
    """Push the mirror to the remote (set remote URL if needed)."""
    try:
        run_cmd(["git", "remote", "add", "origin", remote], cwd=mirror)
    except RuntimeError:
        run_cmd(["git", "remote", "set-url", "origin", remote], cwd=mirror)
    try:
        run_cmd(["git", "push", "--set-upstream", "origin", "main"], cwd=mirror)
    except RuntimeError:
        run_cmd(["git", "push"], cwd=mirror)
    print("Push complete.")



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror a Mercurial repository (with subrepos) to a Git monorepo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--root", required=True, metavar="PATH",
        help="Path to the Mercurial project root.",
    )
    parser.add_argument(
        "--mirror", required=True, metavar="PATH",
        help="Where to create/maintain the local Git monorepo.",
    )
    parser.add_argument(
        "--remote", default="", metavar="URL",
        help="GitHub remote URL. Push is skipped if omitted.",
    )
    parser.add_argument(
        "--recent-commits", type=int, default=100, metavar="N",
        help="Number of recent commits to include per repo on initial setup (default: 100).",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Create the Git repo fresh and seed it with recent history.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without making any changes.",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Launch a looping menu TUI for managing the mirror.",
    )
    parser.add_argument(
        "--state-file", default="", metavar="PATH",
        help="Override location of hg-mirror-state.json "
             "(default: <mirror>/.git/hg-mirror-state.json).",
    )

    args = parser.parse_args()
    if args.interactive:
        run_interactive(args)
    else:
        sync_mirror(args)


if __name__ == "__main__":
    main()
