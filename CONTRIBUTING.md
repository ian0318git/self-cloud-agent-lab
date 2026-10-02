# Contributing

This repository measures a private AI stack and keeps the evidence. The same standard
applies to changes made to it.

Issues and pull requests may be written in **English or 繁體中文**.

## The one rule

**Measured, not assumed.** Every claim in this repository is one of five kinds, and says
which:

| Kind | Means |
| --- | --- |
| **Measured** | observed on the running system |
| **Verified** | confirmed through the application path |
| **Projected** | expected, but not yet measured |
| **Proposed** | design only |
| **Known limitation** | an understood constraint or failure |

A number that was not measured is labelled as not measured. This applies to pull request
descriptions, issue comments, and code comments alike — a claim you cannot point at is a
claim to delete, not one to soften.

## How this repository is developed

Direct commits to `main` are the default and stay allowed. From **2026-10-01** issues and
pull requests are available as well, and are used for the changes that should be read as
changes — where the change *is* a rule, or where the review trail is part of what makes it
worth having. The change is deliberate, and it is recorded as D-077 in
[`DECISIONS.md`](DECISIONS.md).

No pull requests were manufactured for the work that predates it. That work is recorded
where it always was — in the commit history and the decision log — and a review trail that
did not happen is not worth fabricating: a pull request opened from a commit already on
`main` has an empty diff — `gh api
repos/ian0318git/self-cloud-agent-lab/compare/main...c61ff4a --jq '{ahead_by}'` prints
`{"ahead_by":0}`.

Pull requests here are opened as **drafts** and reviewed before merge.

They are merged with a **merge commit** — not a squash, not a rebase. All three are permitted
here, so this is a choice rather than a setting:

```bash
gh api repos/ian0318git/self-cloud-agent-lab \
  --jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge}'
```

It prints `{"allow_merge_commit":true,"allow_rebase_merge":true,"allow_squash_merge":true}`.

What the choice protects is what a review leaves behind. The commits on the branch are that
record — the change, and whatever the review changed about it. A merge commit keeps them as
they were reviewed: a squash would put one commit on `main` and take the rest of the commits
with it — the review's own among them; a rebase would rewrite them, so the commits that landed
would not be the ones that were reviewed. The first merge in this repository, `0ec0333`, keeps
both of PR #5's commits — `git log --reverse --oneline c61ff4a..9c5c562` lists `52d68b7` (the
change before the review) and then `9c5c562` (what round 1 of the review changed about it).

A merge commit is titled by hand, in the form the first one set. `0ec0333`'s subject is
`Merge PR #5: .github/ scaffolding and CONTRIBUTING` — `Merge PR #N: what the pull request
did`, in the words of whoever merges. It is not `type(scope):`, and it is not the platform's
to write: `4db2767`'s subject is `Merge pull request #8 from ian0318git/task/7-merge-method`,
and its body is that pull request's title (`git log --format=%B -1 4db2767`) — what the two
settings produce by themselves. Both have to be overridden at the moment of the merge:

```bash
gh api repos/ian0318git/self-cloud-agent-lab \
  --jq '{merge_commit_title, merge_commit_message}'
```

It prints `{"merge_commit_message":"PR_TITLE","merge_commit_title":"MERGE_MESSAGE"}`.

```bash
gh pr merge <n> --merge \
  --subject 'Merge PR #<n>: what the pull request did' \
  --body '<what the review concluded>'
```

The `--body` is where a merge records its review: `0ec0333`'s carries the size of the change,
what the review found, what was fixed, and where the one unresolved finding was carried
(`git log --format=%B -1 0ec0333`). A review that found nothing is worth the same sentence as
one that did.

Merge commits are not written in the `type(scope):` form; `## Commits` below names them as the
exception, along with the one commit older than that rule.

## Before you start

Read the issue's **Constraints from existing decisions** field.
[`DECISIONS.md`](DECISIONS.md) is the decision log, and it only grows; nobody reads all of
it before starting, which is why that field travels with the task. If your change would
contradict an entry, say so in the issue first — overturning a decision is allowed, and
the overturning is itself a decision.

## Verification

Name the command you ran, and what it printed.

* If the change has no automated test, write the words **no test coverage** and describe
  the manual check. There is currently no CI — `gh api
  repos/ian0318git/self-cloud-agent-lab/actions/workflows` returns zero workflows — so
  nothing else will catch it for you.
* The verifiers in `scripts/` are not a conventional test suite: several **fail against
  the pristine tree on purpose**, and that failure is the demonstration. Read a verifier's
  header before assuming a red result is a problem.
* [`docs/HANDBOOK.md`](docs/HANDBOOK.md) has the operational commands and gates.

## Documentation comes in pairs

Long-form documents are added in pairs — `X.md` and `X.zh-TW.md` — and editing one means
editing the other. A change that adds one without its pair is an incomplete change.

`git ls-files '*.zh-TW.*'` lists the pairs that exist. It cannot tell you that a document
is missing its other half — that is what review is for.

## Commits

`type(scope): description` — the type is English (`feat`, `fix`, `docs`, `test`,
`refactor`, `chore`), the description may be either language. `git log --oneline` shows the
house style. Two things in it are not written this way: merge commits, which have their own
rule in `## How this repository is developed` above — the first two predate it — and
`c2f026c` (2026-09-19), which is older than this rule. Neither was rewritten to fit.

## License

This repository's own code — compose files, scripts, documentation — is MIT. That does
**not** cover the images it pulls: Open WebUI in particular is BSD-3-style *plus a
branding clause*. See [`README.md`](README.md#license).

## AI-assisted changes

Parts of this repository are developed with an AI assistant, and some issues and pull
requests are opened by it. They are held to every rule above — each claim labelled, each
verification named — and they are reviewed by a human before merge, like any other
change.
