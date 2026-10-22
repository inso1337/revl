# The escape demo: refused, explained, repaired

Three components try to exceed what they declared. Each is refused with a
diagnostic that names the guarantee and suggests the fix, then takes the
smallest edit that makes it admissible, and the repaired source is audited so
the crossing the compiler now permits is the one you can see.

```sh
python demo/escape/run_demo.py
python demo/escape/run_demo.py --verbose   # every artifact in full
make demo-escape
```

It needs the compiler and nothing else: no runtime, no network, no host
process. Everything is written to a throwaway temp dir, so the checkout is
untouched and a second run passes as cleanly as the first. It exits nonzero on
the first failed check (issue #2691).

## The three scenarios

| # | what the component does | guarantee | the rejection example it pins | the repair |
|---|---|---|---|---|
| 1 | declares `emission[fs.read]`, then emits `fs.write` | G4, capability-scoped emission | [`g4_emission_widens_declared_capability.rvl`](../../examples/rejections/g4_emission_widens_declared_capability.rvl) | widen the declaration to `emission[fs.read, fs.write]` |
| 2 | reads screen text as `Untrusted[Str]` and passes it to a `Trusted[Str]` shell sink | G9, no authority from untrusted data | [`g9_service_return_launders_taint.rvl`](../../examples/rejections/g9_service_return_launders_taint.rvl) | keep the text as data, make the command a literal |
| 3 | reads a `Secret[Str]` and passes it to a log | G-SECRET-FLOW, confidentiality at the boundary | [`gsecret_service_return_discloses.rvl`](../../examples/rejections/gsecret_service_return_discloses.rvl) | log a redacted marker instead of the value |

The three repairs are different kinds of answer, and that is the point. A
declaration is widened when the body is right and the promise was narrow
(scenario 1). A flow is cut when the authority was never in the data
(scenario 2). A value is redacted when the boundary genuinely needs the secret
to stay on this side (scenario 3).

## What each step shows

For every scenario the demo compiles the refused source, asserts the refusal
code, and prints the compiler's own diagnostic, including the fix line it
produces. It then compiles the repaired source, asserts the result is IR, and
runs `revl audit`, asserting the permitted crossing by name (`read_file
[fs.read]`, `write_file [fs.write]`, and so on).

The refusal is the artifact, not an error being worked around: `revl compile`
exits 1, and the demo fails if it ever starts admitting the source.

## What this demo does not show

It never runs a repaired component, never asks an endorsement policy to approve
a declassification, and never proves that a command literal is a good command.
The repairs are admissible programs, not safe ones. Declassification is itself
a declared, audited, policy-forbiddable slot (`endorse[<origin>]`), which this
demo does not exercise; it takes the repairs that need no new declaration.

`docs/rejections.md` is the full catalogue of what the compiler refuses and
how to fix it. `docs/guarantees.md` is the decode table for the G-codes.
