#!/usr/bin/env python3
"""The checker/reference verdict census: every place `selfhost/checker.rvl`'s
`check_service_src` and the reference compiler disagree, enumerated instead of
stumbled upon.

WHY THIS EXISTS
---------------
`tools/gate_reference_census.py` does this for the OTHER self-host verdict
surface, `selfhost/lower.rvl`'s `admit_src`. The checker is a second surface
with its own oracle (`tests/test_selfhost_checker.py`) and, until this file,
nothing that measured it over the tree — so its disagreements with the
reference were whatever the hand-written corpus happened to spell.

That gap has a cost, and issue #1065 is the record of paying it. `lower.rvl`
and `checker.rvl` each carry a `p_prov_methods`, and both read the method body
straight off the end of the parameter list, so a provide method that RESTATES
its declared return type (`fn go() -> Int { ... }`, which the reference parses
and hands to the type layer) failed the WHOLE component at the parse stage.
On the gate the census bucketed that plainly and PR #1063 closed it. On the
checker nobody could say how many documents it touched, because this file did
not exist.

A parse-stage refusal is the worst place for a defect to hide, because it reads
as agreement. The surface refuses, the corpus says "both refuse", and the
guarantee the surface claims to decide was never reached at all.

THE BUCKETS
-----------
`check_service_src` answers with a STRING: `""` accepts, and anything else is
the refusal spelled exactly as the reference spells it. A refusal that begins
`(bad) ` is a PARSE refusal — the surface never reached a guarantee.

  ``false-reject/parse`` / ``false-reject/semantic``
      The reference ADMITS and the checker refuses. `parse` is the incomplete
      slice showing through (the checker's grammar is a subset); `semantic` is
      worse, because the checker reached a guarantee and decided it wrongly.

  ``msg-mismatch/parse``
      Both refuse, and the checker's refusal is a parse `(bad)`. THE MASKING
      BUCKET: the reference refused for a real reason, the checker answered
      with a parse failure, and a reader comparing verdict-shapes would call
      that agreement. Every document that moves out of here is a document whose
      real verdict was never being measured.

  ``msg-mismatch/semantic``
      Both refuse with a real diagnostic, and the texts differ. The checker's
      contract is that its refusals are the reference's verbatim, so these are
      contract breaks even where the verdict direction agrees. Most are STAGE
      differences: the reference refuses earlier, for something outside the
      checker's slice.

  ``no-objection``
      The reference refuses and the checker returns `""`. The checker issues no
      admissions, so this is not the same thing as a wrongly admitted program;
      it is the honest shape of a slice that decides service boundaries, the
      checkable core of G4, and call-site argument types, and nothing else. It
      is still the direction to watch, which is why the documents whose
      no-objection was previously HIDDEN behind a parse refusal are pinned by
      name in `tests/test_selfhost_checker.py`.

  ``agree-admit`` / ``agree-refuse``
      Both admit, or both refuse with identical text.

USAGE
-----
    python3 tools/checker_reference_census.py            # the census table
    python3 tools/checker_reference_census.py --all      # + bench/docs/site/...
    python3 tools/checker_reference_census.py --json out.json

No baseline is recorded here, deliberately. The gate census pins a snapshot
because the gate is a SHIPPED wire that consumers act on; this surface is an
oracle, and a snapshot over the tree would red on every unrelated `.rvl` a
sibling change adds. What is pinned instead lives in the oracle itself:
`tests/test_selfhost_checker.py` holds the corpus-wide invariant that no
document the reference admits fails at the provide-block parse, plus the named
list of documents whose real verdict that parse refusal used to hide.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The corpus directories, taken from the gate census rather than restated: the
# two surfaces are censused over the same tree, and two copies of this list
# would be free to drift into measuring different trees and reporting numbers
# that look comparable and are not.
from gate_reference_census import (  # noqa: E402
    CORPUS_DIRS, EXTRA_DIRS, _SKIP_DIRS,
)

PARSE_PREFIX = "(bad) "
PROVIDE_BLOCK_REFUSAL = "(bad) bad provide block in component "

# The two refusals `p_top` issues when it cannot read a top-level declaration
# head. Both mean "this document was never checked", and both used to fire on
# shapes the reference's own `_parse_program` accepts: the `pub` visibility
# prefix, a named `test` block, the `lifecycle`/`prop`/`fault` qualifiers on
# `test`, a `boot component` and a typed `event` declaration. 75 documents the
# reference ADMITS were refused this way before those heads were ported.
TOP_LEVEL_REFUSALS = (
    "(bad) unexpected token at top level",
    "(bad) unexpected declaration",
)


def build_check_service_src():
    """`selfhost/checker.rvl`'s `check_service_src`, emitted to python and run.

    The `_exec_emitted` shape `tests/test_selfhost_checker.py` uses: the file
    imports the cordis-py `runtime` adapter through its component, and the pure
    checker never touches it, so a lazy stub does.
    """
    from revl import compile_files

    ir = compile_files([str(ROOT / "selfhost" / "checker.rvl")])
    spec = importlib.util.spec_from_file_location(
        "checker_census_pyemit", ROOT / "backends" / "python" / "emit.py")
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)

    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(emitter.emit(ir), "selfhost_checker.py", "exec"),
             namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["check_service_src"]


def build_reference():
    """`""` when the reference admits, else its diagnostic message. The same
    `_ref_check` the checker's oracle uses, so the two cannot disagree about
    what the reference said."""
    from revl.compiler import compile_source
    from revl.errors import RevlError

    def ref(src: str) -> str:
        try:
            compile_source(src, "census.rvl")
        except RevlError as exc:
            return exc.message
        return ""

    return ref


def load_corpus(*, everything: bool = False):
    """`[(relative_path, source)]` — every `.rvl` in the census directories."""
    cases: list[tuple[str, str]] = []
    for sub in CORPUS_DIRS + (EXTRA_DIRS if everything else ()):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.rvl")):
            if _SKIP_DIRS & set(path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            cases.append((str(path.relative_to(ROOT)), text))
    return cases


def bucket(reference: str, checker: str) -> str:
    """The bucket one (reference, checker) verdict pair lands in."""
    kind = "parse" if checker.startswith(PARSE_PREFIX) else "semantic"
    if reference == "":
        return "agree-admit" if checker == "" else f"false-reject/{kind}"
    if checker == "":
        return "no-objection"
    if checker == reference:
        return "agree-refuse"
    return f"msg-mismatch/{kind}"


def census(cases, check, reference):
    """`{bucket: [detail]}` over the cases, in the corpus's own order.

    A `RecursionError` from either side is its own bucket rather than a crash:
    the checker is written in the recursive-descent style its `.rvl` source is,
    and the tree contains documents (its own siblings, notably) deep enough to
    exhaust the interpreter's stack. That is a slice limit, not a verdict.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for name, src in cases:
        try:
            want = reference(src)
        except RecursionError:
            buckets["reference-recursion"].append(name)
            continue
        try:
            got = check(src)
        except RecursionError:
            buckets["checker-recursion"].append(name)
            continue
        except Exception as exc:  # a checker fault is a finding of its own
            buckets["checker-fault"].append(
                f"{name} :: {type(exc).__name__}: {exc}"[:200])
            continue
        key = bucket(want, got)
        if key in ("agree-admit", "agree-refuse", "no-objection"):
            buckets[key].append(name)
        else:
            buckets[key].append(f"{name} :: ref={want!r} checker={got!r}")
    return dict(buckets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="also walk bench/registry/playground/site/docs/forks")
    parser.add_argument("--json", metavar="PATH",
                        help="write the full bucketed listing here")
    parser.add_argument("--bucket", metavar="NAME",
                        help="print the members of one bucket")
    args = parser.parse_args()

    cases = load_corpus(everything=args.all)
    buckets = census(cases, build_check_service_src(), build_reference())

    print(f"programs: {len(cases)}")
    for name in sorted(buckets):
        print(f"  {name}: {len(buckets[name])}")
    if args.bucket:
        for row in buckets.get(args.bucket, []):
            print(f"    {row}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({k: sorted(v) for k, v in sorted(buckets.items())},
                       indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
