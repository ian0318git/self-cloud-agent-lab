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

Direct commits to `main` up to **2026-10-01**; issues and pull requests from that date
on. The transition is deliberate, and is itself recorded in [`DECISIONS.md`](DECISIONS.md).

No pull requests were manufactured for the work that predates it. That work is recorded
where it always was — in the decision log — and a review trail that did not happen is not
worth fabricating.

Pull requests here are opened as **drafts** and reviewed before merge.

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

Long-form documents are maintained in English and 繁體中文: `X.md` and `X.zh-TW.md`.
Editing one means editing the other. The list is whatever `git ls-files '*.zh-TW.*'`
returns — a change that adds one without its pair is an incomplete change.

## Commits

`type(scope): description` — the type is English (`feat`, `fix`, `docs`, `test`,
`refactor`, `chore`), the description may be either language. `git log --oneline` shows
the house style; it is consistent.

## License

This repository's own code — compose files, scripts, documentation — is MIT. That does
**not** cover the images it pulls: Open WebUI in particular is BSD-3-style *plus a
branding clause*. See [`README.md`](README.md#license).

## AI-assisted changes

Parts of this repository are developed with an AI assistant, and some issues and pull
requests are opened by it. They are held to every rule above — each claim labelled, each
verification named — and they are reviewed by a human before merge, like any other
change.
