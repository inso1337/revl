# Findings — rust format-string escape collision (finding #35)

Probe: verifying items 112-114 (advance on go/rust, go Map[V], rust `_Env`
clone) against the harness. All three verified green — the full advance-
based timer suite passes on py/ts/go/rust, and the mtier re-append works on
rust. One new rust-emitter bug surfaced along the way.

## The bug

`revl test --backend rust src/timer_tests.rvl` (with the advance lifecycle
tests) failed to compile:

```
error: invalid reference to positional argument 2014 (no arguments were given)
  --> src/lib.rs:27:72
```

The emitted `assert!` message contained `\\u{2014}` — the em dash U+2014 in
a test name, escaped by `_string` (backends/rust/emit.py:382). In a
format-string position (`assert!`, `format!`), Rust's format macro parses
the `{2014}` inside the escape as a positional-argument reference.

## Repro

Any revl document with a non-ASCII character in a string that lowers into
a rust format/assert literal. Minimal: a lifecycle test name containing an
em dash — `revl test --backend rust` fails; py/ts/go accept the same doc.

## Ask (roadmap item 106 on harness-m3)

`_string` should emit literal UTF-8 for non-surrogate scalars (Rust source
is UTF-8; the `\\u{...}` form is only needed for the lone-surrogate case
json.dumps used to produce), or escape braces in format-string positions.
The harness worked around it by removing the one em dash from the timer
test name.
