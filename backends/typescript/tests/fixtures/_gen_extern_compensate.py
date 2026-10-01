"""One-off generator for extern_compensate.ir.json, NOT part of the build.

Issue #1592: an emission extern that DECLARES its own `compensate` (item 254)
must register that compensation at every site it is emitted from. On the ts
tier it was registered at none. This fixture emits one such extern from each
site the frontend admits:

  * `ExternOk` / `ExternAbort`: the activation body (`yield
    frame.compensation`); `ExternAbort` fails after the emission, so the
    activation aborts and Phase 2 runs the compensation;
  * `Agent`: a provide-method body (`frame.compensationMethod`);
  * `Beat`: an `every` timer firing (`frame.compensationMethod`, once per
    firing);
  * `SiteAbort`: the control, a site-spelled `compensate` on a plain
    emission, which the ts tier registered before this fix.

`put_row` and `restore_row` record onto the shared `hostLog`, so
extern_compensate.test.ts observes which calls ran and in what order. Run once,
by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_extern_compensate.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SOURCE = (
    "extern emission fn put_row(body: Str) -> Int compensate restore_row() = @ts {\n"
    "  record('put ' + body)\n"
    "  return 1n\n"
    "}\n"
    "extern emission fn restore_row() -> Int = @ts {\n"
    "  record('restore')\n"
    "  return 0n\n"
    "}\n"
    # the site-spelled control: a plain emission compensated at the site,
    # which every tier already registered before issue #1592
    "extern emission fn note(body: Str) -> Int = @ts {\n"
    "  record('note ' + body)\n"
    "  return 1n\n"
    "}\n"
    "service Ops { emission fn run(x: Str) }\n"
    "component SiteAbort {\n"
    "  emit note(\"s\") compensate restore_row()\n"
    "  fail \"boom\"\n"
    "}\n"
    "component ExternOk {\n"
    "  emit put_row(\"y\")\n"
    "}\n"
    "component ExternAbort {\n"
    "  emit put_row(\"y\")\n"
    "  fail \"boom\"\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn run(x) { emit put_row(x) } }\n"
    "}\n"
    "component Beat {\n"
    "  every 10s { emit put_row(\"x\") }\n"
    "}\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "extern_compensate.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "extern_compensate.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
