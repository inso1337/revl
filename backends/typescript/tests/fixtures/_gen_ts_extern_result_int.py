"""One-off generator for ts_extern_result_int.ir.json, NOT part of the build.

Issue #2147: a verbatim `@ts` body declared to return `Int` was not coerced on
the way out. This tier's `Int` is a `bigint` (emit.py `TYPE_MAP`) while a JS
body naturally yields a `number`, so the emitted function carried a `bigint`
annotation over a `number` return and the value threw at its FIRST USE, inside
the language's own helpers, as `Cannot mix BigInt and other types`.

The source reproduces the issue's own report. `day_number_now` is the shape
from the issue (a `Math.floor(...)` over a `Date`, i.e. a JS `number`), read
through the UTC getters rather than the local ones so the value is 0 on every
machine and the test can assert exact numbers. `civil_date` is the issue's
`iso_date`/`civil_date` pair, whose `days + 719468` is where the reported
`TypeError` surfaced; `plus_one` and `halved` are the `+` and `div_trunc`
uses. `big_day_number` returns 9007199254740994, a `number` above 2^53 (the
`bigint`-vs-`number` precision seam) that is exactly representable as a
double, so `big_plus_one` == 9007199254740995n is an ODD bigint past 2^53 —
a value no double can hold, which is what makes the assertion prove the value
is a `bigint` and not a rounded `number`.

Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_ts_extern_result_int.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SOURCE = (
    "extern pure fn day_number_now() -> Int\n"
    "  = @ts {\n"
    "    const now = new globalThis.Date(0)\n"
    "    return Math.floor(globalThis.Date.UTC(\n"
    "      now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) / 86400000)\n"
    "  }\n"
    "\n"
    "extern pure fn big_day_number() -> Int\n"
    "  = @ts {\n"
    "    return 9007199254740994\n"
    "  }\n"
    "\n"
    "fn civil_date(days: Int) -> Int {\n"
    "  let z = days + 719468\n"
    "  return z\n"
    "}\n"
    "\n"
    "pub fn iso_date(n: Int) -> Int = civil_date(n)\n"
    "\n"
    "pub fn plus_one() -> Int = day_number_now() + 1\n"
    "\n"
    "pub fn halved() -> Int = day_number_now().div_trunc(2)\n"
    "\n"
    "pub fn iso_date_now() -> Int = iso_date(day_number_now())\n"
    "\n"
    "pub fn big_plus_one() -> Int = big_day_number() + 1\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "ts_extern_result_int.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "ts_extern_result_int.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
