# ADR-0001: Issue-driven delivery with reviewed PRs

- Status: Accepted
- Date: 2026-11-12

## Context
Work must be traceable and reviewable, and the issue list should document the project.

## Decision
Every change starts as a GitHub issue, is built on `engineering/<#>-<slug>`, and lands through a PR that says `Closes #N`. An independent agent reviews each PR and posts findings as a PR comment; all findings are fixed and re-reviewed (max 5 rounds; originally 3, see the amendment below) before an automatic squash-merge once CI is green.

## Consequences
Clean history and audit trail. More PRs means more ceremony per change.

## Amendment (2026-11-12, issue #30)
The repository owner raised the review limit from 3 to **5 rounds** after PR #26 needed a 4th
round. After 5 rounds that are not clean, the PR is escalated to a human.
