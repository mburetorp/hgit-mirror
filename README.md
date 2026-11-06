# hgit-mirror

Mirror a Mercurial repository, including nested Mercurial subrepositories, into a Git monorepo.

The tool keeps each Mercurial subrepo under its matching path inside the Git mirror and records sync state in the mirror repository so later runs only apply new commits.

## Requirements

- Python 3
- Git available on `PATH`
- Mercurial (`hg`) available on `PATH`

There are no third-party Python package dependencies. The script uses only the Python standard library.

## Usage

Run the script from any directory and pass both the source Mercurial root and the destination Git mirror path.

### First-time setup

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --init
```

By default, initial setup imports the most recent 100 commits from each discovered repo. To change that:

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --init --recent-commits 250
```

Use `--recent-commits 0` to import the complete history of each included repo.

To configure and push to GitHub during setup:

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --init --remote git@github.com:user/repo.git
```

### Incremental sync

After the mirror has been initialized, run:

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror
```

To sync and push:

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --remote git@github.com:user/repo.git
```

### Interactive mode

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --interactive
```

Interactive mode shows pending commits per subrepo and provides menu actions for init, sync, push, sync plus push, and setting the remote URL.

### Excluding paths

Use `--exclude` to keep subrepos, directories or files out of the mirror:

```powershell
python hgit-mirror.py --root "D:\code\projects\aamp (2)" --mirror D:\path\to\git-mirror --init --exclude regression --exclude AGENTS.md
```

Exclude patterns are saved in the state file, so later syncs (including the refresh script) keep applying them without repeating the flags. Use `--unexclude PATH` to remove a saved pattern. Both flags can be repeated.

Patterns are paths relative to the mirror root:

- `regression` excludes that subrepo or directory and everything below it.
- `AGENTS.md` excludes only the file at the mirror root.
- Glob patterns are matched with `fnmatch`, so `*AGENTS.md` excludes the file at every level.

Adding an exclude to an existing mirror commits the removal of the matching files with the generic message `Update mirror exclusions`. Earlier Git history still contains them; create a fresh mirror with `--init` if they must be gone from history. Un-excluding a subrepo seeds it with a snapshot of its latest revision.

### Redacting commit messages

Use repeatable `--redact TEXT` options to replace sensitive terms in commit messages with `***`:

```powershell
python hgit-mirror.py --root D:\private\aamp --mirror D:\private\aamp-git --init --recent-commits 0 --exclude regression --exclude AGENTS.md --exclude "*/AGENTS.md" --redact "Sensitive Project" --redact "Client Name"
```

Matching is literal and case-insensitive, including matches inside longer words, paths, and URLs. Each match becomes exactly three asterisks. Overlapping terms are matched longest-first in one pass. Blank terms are rejected.

The full message (subject and body) is preserved with these replacements. Generated commit messages and message previews use the same replacements. Redaction terms are saved in the state file inside `.git`, so later syncs, the refresh script, and interactive operations reuse them. Repeating `--redact` adds terms to the saved list, even when there are no pending commits.

Exclusions control imported paths; redaction controls message text. Excluded names do not automatically become redaction terms. Included file contents, paths, and author identities are unchanged. New redaction terms apply to future imports; recreate the mirror to apply them to all history.

### Empty commits

Both initial conversion and later syncs discard revisions whose filtered tree has no changes. This includes revisions that only change excluded files or Mercurial metadata, such as subrepo pointers. Their messages do not become Git commits. Mixed revisions retain their included changes and use the redacted message.

Skipped revisions still advance the sync state and are reported separately, so later runs do not retry them. A first snapshot containing included files creates a seed commit even if that revision originally changed only an excluded file. Its message is also redacted.

A fresh mirror with no included files remains without a Git commit until there are included changes (or generated Git metadata). Application failures stop the run, preserving the last successfully processed revision.

## How it works

1. Discovers subrepos by recursively reading `.hgsub` files.
2. Collects new Mercurial commits per repo using `hg log -T json`.
3. Applies commits to the Git mirror in chronological order.
4. Rewrites subrepo patch paths so each subrepo lands under the correct monorepo directory.
5. Converts `.hgeol` to `.gitattributes` and `.hgignore` to `.gitignore` during initial setup.
6. Stores the last synced Mercurial revision per repo in `.git/hg-mirror-state.json`.

Initial setup uses full snapshots for each imported commit because a partial history window may not contain the parent commits required for patch application. Incremental sync uses `hg diff --git -c` and `git apply --index`, then creates a commit with the sanitized message, original author, and original author date. Messages are sanitized before committing.

## State and generated files

The default state file is:

```text
<mirror>/.git/hg-mirror-state.json
```

A convenience refresh script is also written to:

```text
<mirror>/.git/hg-mirror-refresh.bat
```

Both files live inside `.git`, so they are not committed to the mirror repository.

Use `--state-file PATH` to override the state file location.

## Dry run

Preview what would happen without writing changes:

```powershell
python hgit-mirror.py --root D:\path\to\hg-project --mirror D:\path\to\git-mirror --dry-run
```

## Notes

- The mirror directory must not already contain a Git repository when using `--init`.
- If `--remote` is omitted, the script syncs locally and skips pushing.
- Mercurial metadata files such as `.hgsub`, `.hgsubstate`, `.hgeol`, `.hgtags`, and `.hgignore` are excluded from mirrored content.
- `.hgignore` regular-expression patterns cannot be converted exactly to `.gitignore`; the generated `.gitignore` comments those patterns for manual review.

## Tests

Run the regression suite with Python, Git, and Mercurial installed:

```powershell
python -B -m unittest -v test_hgit_mirror
```

The tests create disposable repositories and do not modify your source repositories or mirrors.
