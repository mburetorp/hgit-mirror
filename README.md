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

Adding an exclude to an existing mirror commits the removal of the matching files (`Stop mirroring: ...`). Earlier Git history still contains them; re-create the mirror with `--init` if they must be gone from history. Un-excluding a subrepo seeds it with a snapshot of its latest revision.

## How it works

1. Discovers subrepos by recursively reading `.hgsub` files.
2. Collects new Mercurial commits per repo using `hg log -T json`.
3. Applies commits to the Git mirror in chronological order.
4. Rewrites subrepo patch paths so each subrepo lands under the correct monorepo directory.
5. Converts `.hgeol` to `.gitattributes` and `.hgignore` to `.gitignore` during initial setup.
6. Stores the last synced Mercurial revision per repo in `.git/hg-mirror-state.json`.

Initial setup uses full snapshots for each imported commit because a partial history window may not contain the parent commits required for patch application. Incremental sync uses `hg export --git` and `git am`.

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
