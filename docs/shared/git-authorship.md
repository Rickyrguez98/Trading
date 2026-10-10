# Git authorship policy

Every commit in this repository is attributed to a single human author.

| | Value |
| --- | --- |
| Name | **Ricardo Rodriguez** |
| Email | **if722544@iteso.mx** |
| GitHub username | **Rickyrguez98** |

Set the local identity so new commits are correct:

```bash
git config user.name  "Ricardo Rodriguez"
git config user.email "if722544@iteso.mx"
```

## Rules

- Commit under the identity above — author **and** committer.
- **No co-author trailers.** Commit messages carry no co-authorship lines and
  no tool, agent or machine attribution of any kind, in the subject, body or
  metadata.
- Commit messages describe *what changed and why*, nothing about how the
  change was produced.
- Do not rewrite published history. Corrections land as new commits.

## Historical note: the one-time identity normalization

Early history contained a mix of commit identities (a local placeholder
author, a shortened name variant, and one GitHub web-UI commit) plus
machine-generated co-author trailers in some message bodies. All 56 commits on
`main` were normalized in a single pass to the identity above, and the
trailers were stripped from the message bodies.

What that pass did and did not touch:

- Author and committer identity were rewritten on every commit; author and
  committer **dates were preserved**.
- **No file content changed** — the rewritten tree was byte-for-byte identical
  to the original.
- The commit **count was unchanged** (56), so no history was dropped, and the
  full test suite still passed afterwards.

Because rewriting history changes commit SHAs, the remote was updated with
`git push --force-with-lease origin main` (which refuses to overwrite if the
remote moved unexpectedly). Backup refs were created before the rewrite and
have since been deleted, so the pre-normalization history is no longer
recoverable — that is deliberate: it existed only to carry the old metadata
this policy removes. Any SHA from before the normalization will not resolve;
use the current history.

## Note on GitHub profile attribution

GitHub links a commit to a user's **profile and avatar** only when the commit
email is a **verified email on that GitHub account**. Commits use
`if722544@iteso.mx`; for them to render with the **Rickyrguez98** avatar, that
address must be added and verified under *GitHub → Settings → Emails*. Until
then commits correctly show the name **Ricardo Rodriguez** but may appear
without the profile link. GitHub's no-reply address
`60189930+Rickyrguez98@users.noreply.github.com` is an always-attributed
alternative if a private email is preferred.
