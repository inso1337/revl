<!--
One thing this template exists to prevent, learned twice on 2026-10-07.

GitHub scans the PR body, the PR title and the commit message for a closing
keyword followed by a `#` reference, and closes that issue on merge. The scan is
not markdown-aware and does not read negation: backticks, a fenced block, an
HTML comment and a "does not" in front of it are all invisible to it. A sentence
whose plain meaning is that the change leaves an issue open has closed it.

Write the reference so no keyword can reach it: `refs #1234`,
`#1234 remains open`, `leaves #1234 open`. Putting the reference first also
works — "#1234 is not closed by this PR" — because the keyword is then no longer
the thing nearest the `#`. Rule 5 of CONTRIBUTING.md has the two incidents, the
mechanism, and the residual risk from replay.
-->

## What this changes

## Verification

<!--
The commands you ran and their result. Name the selector, not "the suite".
Read the skips: a skip is not a pass.
-->

## Issue linkage

<!-- References, and whether this lands the issue or only advances it. -->

## What this does NOT do

<!--
Scope left out on purpose, with the instance names and the tiers, and where the
remainder is tracked. "Fixed" with no scope is the sentence that costs the next
person a full re-investigation.
-->
