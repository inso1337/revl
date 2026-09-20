# The flagship demo: one legacy-enterprise agent, end to end

A billing desktop whose refund path has no clean typed service. One revl
composition declares all three honest ways to reach it, and ten checks show a
different guarantee holding over each leg.

```sh
python demo/legacy_enterprise/run_demo.py
python demo/legacy_enterprise/run_demo.py --verbose   # every artifact in full
make demo-flagship
```

It needs the compiler and nothing else: no runtime, no desktop, no network.
Everything is written to a throwaway temp dir, so the checkout is untouched and
a second run passes as cleanly as the first. It exits nonzero on the first
failed check.

Roadmap item 525, issue #1200, from the 2026-09-19 external review. The design
and the honest map of what is missing are in
[docs/design/551-flagship-demo.md](../../docs/design/551-flagship-demo.md).

## What each step exercises

| # | what happens | guarantee | the artifact |
|---|---|---|---|
| 1 | the three-rung agent admits, and its whole boundary is enumerated | G8 | `revl audit` names all seven capabilities |
| 2 | descending the ladder widens the reach and the drift gate fails | G8, item 21 | `revl audit --diff` lists six added crossings |
| 3 | `emission[ui]` is refused | G8, item 521 | the root is not an enumerable boundary |
| 4 | `emission[ui.drag]` is refused | G8, item 521 | the refusal enumerates the five real verbs |
| 5 | screen content reaching a shell sink is refused | G9, item 249 | the diagnostic names the origin `screen.observe` |
| 6 | `ui.download` and `ui.click` may not declare `compensate`; `ui.text` must | G4, item 522 | three refusals, each naming the verb's class |
| 7 | `confidential -> drafter` is refused | item 512 | the action, origin, role and residence, by name |
| 8 | the operator's floor refuses the `ui.download` rung | item 33 | a policy violation with a why-trace |
| 9 | two crossings report bare and one compensated | G4, item 546 | `revl erase-report` |
| + | the admitted whole is signed and checkable | item 127 | `revl attest`, then `--verify --against` |

Six of the ten are refusals, which is the intended proportion. The review's own
reading is that honest refusal is already a strength.

## The ladder

| rung | crossing | capability |
|---|---|---|
| 1 | the typed API | `db.invoice` |
| 2 | a peer service | `net.ledger` |
| 3 | computer-use | `screen.observe`, `ui.find`, `ui.text`, `ui.click`, `ui.download` |

The typed rung exists on purpose. An agent that could only click would prove
nothing about ordering.

## What this demo does not show

Read [section 5 of the design doc](../../docs/design/551-flagship-demo.md)
before quoting this demo for anything. The short version: it never drives a
desktop, never invokes a model, does not check the ladder's descent ORDER, has
no pause before the irreversible step, verifies no postcondition, and writes no
write-ahead log, so there is nothing for `revl replay` or `revl canary` to read.
Each of those has a named owner, two of them upstream in the harness, and none
of them is stubbed here. A stub in a demo becomes a claim the project did not
earn.

`ui.text` reports `[compensated]`, which is not the same as restored. Nothing
here may be summarised as "it rolled back cleanly".

## The files

| file | role |
|---|---|
| `run_demo.py` | the runner and its assertions |
| `programs.py` | the revl programs, as strings |
| `../../tests/test_flagship_demo_525.py` | the gate wrapper, so `pytest tests/` carries it |

The programs are Python strings rather than `.rvl` files because `demo/` is a
census corpus root and the self-host gate does not yet parse `route model` or
carry the computer-use registry. Both divergences were measured before the
decision; section 3 of the design doc has the numbers.
