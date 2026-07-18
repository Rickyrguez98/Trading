# Git Authorship Fix — unifying history under one identity

> Repository hygiene note. This documents a **history rewrite** that changed
> commit metadata only — no file content was altered.

## Goal

Every commit in this repository should be attributed to a single human author:

| | Value |
| --- | --- |
| Name | **Ricardo Rodriguez** |
| Email | **if722544@iteso.mx** |
| GitHub username | **Rickyrguez98** |

## What was wrong (before)

`git log` showed a mix of identities and machine-generated trailers across the
56 commits on `main`:

| Count | Author | Committer |
| --- | --- | --- |
| 41 | `Trading Research <research@local>` | `Trading Research <research@local>` |
| 9 | `Trading Research <research@local>` | `Ricardo <if722544@iteso.mx>` |
| 5 | `Ricardo Rodriguez <if722544@iteso.mx>` | `Ricardo Rodriguez <if722544@iteso.mx>` |
| 1 | `Ricardo Rodriguez <…@users.noreply.github.com>` | `GitHub <noreply@github.com>` |

In addition, **35 commits** carried a `Co-Authored-By: Claude Opus 4.7
<noreply@anthropic.com>` trailer in the message body.

## Safety taken before rewriting

1. Confirmed a **clean working tree** (`git status` showed nothing to commit).
2. Created a **backup branch**: `backup/pre-author-rewrite`.
3. Created a **backup tag**: `backup-before-author-rewrite`.
   Both point at the original pre-rewrite tip `7c7e051`.
4. Confirmed the remote: `https://github.com/Rickyrguez98/Trading.git`.

## The rewrite

`git-filter-repo` was not available in this environment, so the rewrite used
`git filter-branch` over `HEAD` only (so the backup branch/tag were left
pointing at the original commits):

```bash
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch \
  --env-filter '
    export GIT_AUTHOR_NAME="Ricardo Rodriguez"
    export GIT_AUTHOR_EMAIL="if722544@iteso.mx"
    export GIT_COMMITTER_NAME="Ricardo Rodriguez"
    export GIT_COMMITTER_EMAIL="if722544@iteso.mx"
  ' \
  --msg-filter '
    sed -E "/^Co-[Aa]uthored-[Bb]y:.*(Claude|anthropic)/d"
  ' \
  -- HEAD
```

- **`--env-filter`** rewrites *both* author and committer identity on every
  commit. Author/committer **dates are preserved** (they are not overridden).
- **`--msg-filter`** strips the `Co-Authored-By: …Claude…/…anthropic…` trailer
  while leaving the rest of each commit message intact.

The local repo identity was also set so future commits are correct:

```bash
git config user.name  "Ricardo Rodriguez"
git config user.email "if722544@iteso.mx"
```

## Verification (after)

```text
56 commits total
56  Ricardo Rodriguez <if722544@iteso.mx> | Ricardo Rodriguez <if722544@iteso.mx>
 0  commits with Trading Research / research@local / GitHub no-reply in author or committer
 0  commits with Claude / Anthropic / Co-Authored-By in the message
```

- Commit **count unchanged** (56), so no history was dropped.
- `git diff backup/pre-author-rewrite HEAD` is **empty** — file content is
  byte-for-byte identical; only metadata changed.
- The full test suite still passes (**151 passed**).

## Publishing the rewrite

Because a history rewrite changes commit SHAs, the remote must be updated with a
force push. This was done **non-destructively** with `--force-with-lease` (which
refuses to overwrite if the remote moved unexpectedly), and only after the
backup branch + tag existed:

```bash
git push --force-with-lease origin main
git push origin backup/pre-author-rewrite      # publish the safety branch
git push origin backup-before-author-rewrite   # publish the safety tag
```

## Rolling back (if ever needed)

The pre-rewrite history is fully recoverable:

```bash
git reset --hard backup-before-author-rewrite   # restore old tip locally
# or inspect without moving HEAD:
git log backup/pre-author-rewrite
```

## Note on GitHub profile attribution

GitHub links a commit to a user's **profile/avatar** only when the commit email
is a **verified email on that GitHub account**. The commits now use
`if722544@iteso.mx`; for them to show the **Rickyrguez98** avatar, that address
must be added and verified under *GitHub → Settings → Emails*. Until then the
commits correctly show the name **Ricardo Rodriguez** but may render without the
profile link. (GitHub's no-reply address `60189930+Rickyrguez98@users.noreply.github.com`
is an always-attributed alternative if a private email is preferred.)
