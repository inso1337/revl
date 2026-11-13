# MCP answer shapes, measured A/B

The same briefs through the current shape (A) and the token-shaped one
(B), so a shape is kept on a number and not on a claim (#2847). Produced
by `tools/shape_ab.py` from the answers a client actually receives, and
reproduced in CI by `tests/test_shape_ab_2847.py`.

**Briefs:** 136, the compiled component of every cell of the
committed runs `bench/tokens.py` scores (the admitted components of
`bench/results/tokens-to-green.md`). "Admitted" there means it compiled
when the run was scored. Replayed today, under the server's default
authoring profile, the admission gate refuses some of them (G8: the
capability ceiling against the admission kernel, and host blocks under
the untrusted profile). Those briefs are their own bucket below, because
they spend differently from the ones that run to the end.

Tokens are `bench/tokens.py`'s `count_tokens`. *Emitted* is the arguments
of the calls (what the model generates), *received* is the answers (what
enters the context and is paid again on every later turn), *turns* is the
number of calls.

## compound: check, admit, plan versus `revl_ship`

A: `revl_check`, then `revl_admit`, then `revl_plan`, stopping at the
first that fails. B: one `revl_ship`. Both send the running manifest
(232 emitted tokens each time it is sent): B once per
brief, A up to twice. A brief refused at check costs A one call with no
manifest and B one call that carries it, so B emits more there. A
session-held manifest default would spare that; it needs a live load and
is not measured here.

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 304 | 136 | -55.3% | 3 | 1 |
| emitted tokens | 85184 | 53730 | -36.9% | 841 | 384.5 |
| received tokens | 280895 | 215794 | -23.2% | 2370.5 | 1584.5 |
| **emitted + received** | **366079** | **269524** | **-26.4%** | | |

Over all 136 briefs: 84 planned, 51 stopped:check, 1 stopped:gate; 0 where the arms disagree on the outcome. B emits or receives more than A on 52 of them.

### ran to the end (planned) (84 briefs)

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 252 | 84 | -66.7% | 3 | 1 |
| emitted tokens | 75861 | 31979 | -57.8% | 904 | 381 |
| received tokens | 208602 | 139697 | -33.0% | 2492 | 1685 |
| **emitted + received** | **284463** | **171676** | **-39.6%** | | |

### refused before planning (52 briefs)

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 52 | 52 | +0.0% | 1 | 1 |
| emitted tokens | 9323 | 21751 | +133.3% | 163.5 | 402.5 |
| received tokens | 72293 | 76097 | +5.3% | 1404 | 1482 |
| **emitted + received** | **81616** | **97848** | **+19.9%** | | |

## handle: `revl_fmt` then `revl_check`

This exchange is constructed: format a candidate, then check the
formatted text. A: `revl_fmt` returns the formatted text and
`revl_check` is sent that text back. B: `revl_fmt {handle: true}` returns
an `artifactId` and `revl_check {artifactId}` checks it. Both are two
turns. A handle has a fixed cost (the field and the extra argument), so
it does not pay on a text of a few tokens.

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 271 | 271 | +0.0% | 2 | 2 |
| emitted tokens | 42131 | 23932 | -43.2% | 289 | 165.5 |
| received tokens | 170081 | 150931 | -11.3% | 1075.5 | 900.5 |
| **emitted + received** | **212212** | **174863** | **-17.6%** | | |

Over all 136 briefs: 84 checked:ok, 51 checked:refused, 1 stopped:fmt; 0 where the arms disagree on the outcome. B emits or receives more than A on 5 of them.

### checked ok (84 briefs)

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 168 | 168 | +0.0% | 2 | 2 |
| emitted tokens | 23733 | 13583 | -42.8% | 283 | 162 |
| received tokens | 84949 | 74211 | -12.6% | 999 | 871 |
| **emitted + received** | **108682** | **87794** | **-19.2%** | | |

### refused or not formattable (52 briefs)

| metric | A total | B total | delta | A median | B median |
|---|---:|---:|---:|---:|---:|
| turns | 103 | 103 | +0.0% | 2 | 2 |
| emitted tokens | 18398 | 10349 | -43.7% | 323 | 183.5 |
| received tokens | 85132 | 76720 | -9.9% | 1642.5 | 1494 |
| **emitted + received** | **103530** | **87069** | **-15.9%** | | |

## Reading it

- In total, B must not spend more than A on turns, emitted tokens or
  received tokens, and on every compound brief that runs to the end, and
  every handle brief whose text is at least 64 characters, B must not
  spend more than A on any of them. Both arms must reach the same
  outcome, with the same first diagnostic, on every brief.
  `tests/test_shape_ab_2847.py` fails otherwise, and fails when this
  file no longer reproduces, so an answer that grows shows up as a diff.
- Not measured: a diff-shaped answer for `revl_swap` and `revl_edit`
  (it needs a live runtime), the session-held manifest default, and a
  real model's tokenizer (the proxy ranks and tracks, it is not a bill).

## Re-run

```
python3 tools/shape_ab.py            # print the table
python3 tools/shape_ab.py --write    # rewrite this file
python3 tools/shape_ab.py --compare  # reproduce it; exit 1 on drift
```
