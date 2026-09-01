# The stdlib version stamp (roadmap item 389)

## Why this exists

A consumer that vendors the revl stdlib holds byte-copies of `stdlib/*.rvl` in
its own tree. The revl-harness, for example, copies `stdlib/{json,value,str}.rvl`
so it can build without depending on a checkout. Those copies go stale silently.

When item 104 added `value_is_object` to `stdlib/value.rvl` upstream, the
compiler began recommending `value_is_object` as a fix in its diagnostics. A
consumer whose vendored `value.rvl` predated that change did not carry the
symbol, so the compiler was recommending an API the consumer's stdlib did not
have, and nothing noticed. There was no way to ask the question that would have
caught it: "is my copy of the stdlib the one this compiler expects?"

The stamp answers that question.

## The mechanism

There is one version number and it lives in one place: the literal returned by
`stdlib_version()` in `stdlib/version.rvl`.

```
pub fn stdlib_version() -> Str {
  return "1"
}
```

It is a plain, pure-revl, zero-argument function (no `@py`/`@ts` bodies), so it
lowers on every backend and a consumer can call it on any tier. Because the value
is written into the file, it travels with the file: a vendored copy carries the
version it was copied at.

Two sides compare:

- **What a copy IS** — the `stdlib_version()` literal in *that copy* of
  `stdlib/version.rvl`. A consumer reads it either by calling the function from
  the vendored copy, or by reading the file directly.
- **What the compiler EXPECTS** — `EXPECTED_STDLIB_VERSION` in
  `src/revl/stdlib_version.py`. This is not hand-maintained: the compiler reads
  it once from the stdlib it ships (`stdlib_root()`), so the compiler and its own
  bundled stdlib can never silently disagree. The single edit that bumps the
  version is the literal in `stdlib/version.rvl`.

They disagree exactly when a copy has drifted.

## How a consumer reads it

Two equivalent ways, depending on what the consumer has on hand.

Run it (works on any tier the consumer builds for):

```
use "stdlib/version.rvl" { stdlib_version }

test "my vendored stdlib is current" {
  assert stdlib_version() == "1"   // the version this consumer expects
}
```

Or, from Python tooling, read the stamp out of a tree without running anything
and compare it against the compiler's expected version:

```python
from revl.stdlib_version import EXPECTED_STDLIB_VERSION, read_stamp, check_drift

mine = read_stamp("path/to/my/vendored/stdlib")   # what my copy is, or None
drift = check_drift("path/to/my/vendored/stdlib") # None if in sync, else a reason
```

`check_drift` returns `None` when the copy matches, and a one-line explanation
otherwise. It distinguishes three drifting shapes:

- **no stamp** — the copy predates item 389 entirely (the item-104 case): its
  symbol set is unknown and may lack what the compiler recommends;
- **older stamp** — the copy is behind the compiler;
- **newer stamp** — the copy is ahead of the compiler (a stdlib from a newer
  release paired with an older compiler).

## Drift detection in `revl doctor`

`revl doctor` reports a `stdlib version stamp` check. It resolves the stdlib the
import resolver would actually load — an entry on `REVL_IMPORT_PATH` that carries
a `stdlib/version.rvl` wins (this is how a consumer points the compiler at a
vendored stdlib), otherwise the compiler's own bundled stdlib — reads its stamp,
and compares it against `EXPECTED_STDLIB_VERSION`.

- In sync: `[ok] stdlib version stamp  1  loaded stdlib matches the version this compiler expects`
- Drifted: `[warn] stdlib version stamp  1  stdlib drift: loaded stdlib is version '0' but this compiler expects '1' (the copy is older than the compiler expects); re-vendor the stdlib to match ...`

A consumer whose CI points `REVL_IMPORT_PATH` at its vendored stdlib gets the
drift warning from `revl doctor` before the stale copy can cause a recommended
API to be missing.

## Bump discipline

Bump the integer in `stdlib/version.rvl` by one whenever a **public** stdlib
symbol is **added, removed, or changed** in signature or behaviour. That is: any
change a consumer's vendored copy could fall behind.

- Adding a `pub` fn or extern (item 104's `value_is_object` is the canonical
  example) — bump.
- Removing or renaming a `pub` symbol — bump.
- Changing a `pub` symbol's signature or observable behaviour — bump.
- Touching only comments, or a private (non-`pub`) helper — no bump needed.

The stamp is a monotonic counter, not semver: it only has to increase, so
"newer than expected" and "older than expected" are both distinguishable from
"in sync". `tests/test_stdlib_version.py` pins the invariant that the checkout's
own `stdlib/version.rvl` stamp equals `EXPECTED_STDLIB_VERSION`, so a bump that
edits the literal keeps the compiler and its bundled stdlib in agreement by
construction.
