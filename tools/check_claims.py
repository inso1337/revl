#!/usr/bin/env python3
"""The repository check for `revl:claim` annotations (issue #2846).

A comment or doc that states a behaviour claim is only as good as the artifact
that makes it true. `src/revl/claims.py` parses the annotation and judges the
registry; this tool adds the two things that need the repository: the docgen
block roster, and `tools/affected_tests.py`'s selection for the annotated file.

    python3 tools/check_claims.py --check        the gate
    python3 tools/check_claims.py --self-test    plant each failure first
    python3 tools/check_claims.py --list         every annotation and witness

WHAT IS CHECKED, and the named failure for each:

  claim-annotation-malformed     `revl:claim` in a comment with no id after it.
  claim-registry-missing         an annotation exists and there is no
                                 `claims.toml` at the root.
  claim-registry-invalid         `claims.toml` is not TOML, has no `[claims]`
                                 table, or holds a value that is not
                                 `test:`/`docgen:`/`claim:<target>`.
  claim-unknown-id               the id is not an entry in `claims.toml`.
  claim-witness-missing          `test:` file or function absent, a file
                                 pytest does not collect, a test that is
                                 unconditionally skipped, a `[param]` that is
                                 not one of the function's parameter ids (or
                                 whose ids cannot be read statically), a
                                 `docgen:` block absent, or a `claim:` chain
                                 whose entry is absent or cyclic. A registry
                                 entry no annotation names is judged too,
                                 reported at `claims.toml`.
  claim-witness-not-selected     a `.rvl` annotation whose test witness is not
                                 in the affected selection for its own file, so
                                 editing the code would not run the test.

The selection check is for `.rvl` code, not prose: a Markdown claim points at a
docgen block (whose byte gate the `docs` selection runs) or at a test whose
existence is judged, not at a test the doc edit has to execute. A full
selection (`FULL`) contains every witness, so it is reported as selected; the
selector is conservative by design and this check does not narrow it. For a
claim in `examples/**` (the `examples/rejections` annotations among them) the
selection is always FULL, so the selection half adds nothing there: those
claims rely on the full run.

`claims.toml` is at the repository root:

    [claims]
    json-canonical-bytes = "test:tests/test_json_cross_tier_bytes.py"

A `[claims]` value that no annotation names is allowed: the registry may be
adopted before the sentence that repeats it, and a claim is registered to be
citable. Its witness is still resolved, so a dead one is reported.

EXIT STATUS: 0 clean, 1 findings (including a missing or invalid registry),
2 the check could not run (the affected selector failed, or an alternate
`--root` was asked for the repository selector that does not belong to it).
`--self-test` exits 1 unless every planted case is caught.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "claims.toml"


def _load(name: str, path: Path):
    """Load a module by path, so the lint job needs no `revl` install and no
    import of the whole compiler package to read the parser."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"check_claims: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLAIMS = _load("_revl_claims_by_path", ROOT / "src" / "revl" / "claims.py")


def _docgen_blocks() -> frozenset[str]:
    """The generated-block keys, `tools/docgen.py`'s BLOCKS being the roster."""
    docgen = _load("_docgen_by_path", ROOT / "tools" / "docgen.py")
    return frozenset(block[0] for block in docgen.BLOCKS)


def _selection(root: Path):
    """A per-file affected-selection callback for the repository the selector
    belongs to, or a named failure when `root` is some other tree.

    The first call walks the tree, so results are cached; the gate asks once
    per annotated `.rvl` file. Under a full selection every witness is already
    run, so the callback returns None (the "no narrow selection" marker). An
    alternate root cannot be judged by this checkout's selector, and silently
    dropping the selection half would be the skip this tool exists to refuse.
    """
    if root.resolve() != ROOT.resolve():
        raise RuntimeError(
            f"the affected selector belongs to {ROOT}, not {root}; "
            "pass --no-selection to judge only ids and witnesses there")
    sys.path.insert(0, str(ROOT / "tools"))
    import affected_tests  # noqa: PLC0415 -- only the real tree runs the selector

    cache: dict[str, set[str] | None] = {}

    def selected(relpath: str) -> set[str] | None:
        if relpath not in cache:
            result = affected_tests.select([relpath], str(ROOT))
            cache[relpath] = None if result["full"] else set(result["pytest"])
        return cache[relpath]

    return selected


def _discover(root: Path) -> list:
    return CLAIMS.discover(root)


def _registry(root: Path) -> dict:
    return CLAIMS.load_registry(root / REGISTRY)


def _run_check(root: Path, *, selection: bool, stream=sys.stdout) -> int:
    registry_path = root / REGISTRY
    sites = _discover(root)
    if not registry_path.is_file():
        if not sites:
            print("claims: no annotations and no claims.toml", file=stream)
            return 0
        print(f"claim-registry-missing: {REGISTRY}: "
              f"{len(sites)} annotation(s) exist and the registry is absent",
              file=stream)
        return 1
    try:
        registry = _registry(root)
    except CLAIMS.RegistryError as exc:
        print(f"claim-registry-invalid: {REGISTRY}: {exc}", file=stream)
        return 1
    try:
        selected = _selection(root) if selection else None
    except Exception as exc:  # noqa: BLE001 -- report, never read as a clean pass
        print(f"claim-selection-unavailable: tools/affected_tests.py: {exc}",
              file=stream)
        return 2
    try:
        findings = CLAIMS.check(
            sites, registry, root,
            docgen_blocks=_docgen_blocks(), selected=selected)
    except Exception as exc:  # noqa: BLE001 -- a selector that raised mid-check
        print(f"claim-selection-unavailable: while judging an annotation: {exc}",
              file=stream)
        return 2
    if findings:
        print(CLAIMS.render(findings), file=stream)
        print(f"claims: {len(findings)} finding(s) in {len(sites)} annotation(s)",
              file=stream)
        return 1
    print(f"claims: {len(sites)} annotation(s), {len(registry)} registry "
          "entr(ies), OK", file=stream)
    return 0


def _run_list(root: Path, stream=sys.stdout) -> int:
    try:
        registry = _registry(root)
    except CLAIMS.RegistryError as exc:
        print(f"claim-registry-invalid: {REGISTRY}: {exc}", file=stream)
        return 1
    sites = _discover(root)
    for site in sorted(sites, key=lambda s: (s.path, s.line)):
        witness = registry.get(site.id) if site.id is not None else None
        shown = str(witness) if witness is not None else "<unregistered>"
        print(f"{site.path}:{site.line}  {site.id or '<malformed>'}  {shown}",
              file=stream)
    return 0


# --------------------------------------------------------------------------- #
# Self-test: plant each failure and require its code.                         #
# --------------------------------------------------------------------------- #
_GOOD_TEST = "def test_ok():\n    assert True\n"
_GOOD_RVL = "// revl:claim known\nfn f() -> Int { return 1 }\n"
_GOOD_MD = "<!-- revl:claim block -->\n"
_GOOD_TOML = ('[claims]\nknown = "test:tests/test_comp.py"\n'
              'block = "docgen:guarantees"\n')
_PARAM_TEST = ('import pytest\n\n\n@pytest.mark.parametrize("x", ["a", "b"])\n'
               'def test_p(x):\n    assert x\n')


def _tree(root: Path, *, rvl: str = _GOOD_RVL, md: str = _GOOD_MD,
          toml: str = _GOOD_TOML, test: str | None = _GOOD_TEST) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "comp.rvl").write_text(rvl, encoding="utf-8")
    (root / "docs" / "guide.md").write_text(md, encoding="utf-8")
    if toml is not None:
        (root / REGISTRY).write_text(toml, encoding="utf-8")
    if test is not None:
        (root / "tests" / "test_comp.py").write_text(test, encoding="utf-8")
        (root / "tests" / "helper_comp.py").write_text(_GOOD_TEST, encoding="utf-8")


def _case(root: Path, *, expected: set[str], selected=None, **files) -> set[str]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _tree(root, **files)
    if not (root / REGISTRY).is_file():
        return {"claim-registry-missing"} if "claim-registry-missing" in expected \
            else set()
    try:
        registry = _registry(root)
    except CLAIMS.RegistryError as exc:
        if "claim-registry-invalid" not in expected:
            raise AssertionError(f"unexpected registry error: {exc}")
        return {"claim-registry-invalid"}
    findings = CLAIMS.check(
        _discover(root), registry, root,
        docgen_blocks=frozenset({"guarantees"}), selected=selected)
    return {finding.code for finding in findings}


def _self_test() -> int:
    cases = 0
    with tempfile.TemporaryDirectory(prefix="revl-claims-") as tmp:
        base = Path(tmp)

        def case(name: str, *, expected: set[str], **kwargs) -> None:
            nonlocal cases
            got = _case(base / name, expected=expected, **kwargs)
            if got != expected:
                print(f"claims self-test: {name}: expected {sorted(expected)}, "
                      f"got {sorted(got)}")
                raise AssertionError(name)
            cases += 1

        case("good", expected=set(), selected=lambda _p: {"tests/test_comp.py"})
        case("unknown-id", expected={"claim-unknown-id"},
             selected=lambda _p: {"tests/test_comp.py"},
             rvl="// revl:claim missing\nfn f() -> Int { return 1 }\n")
        case("malformed", expected={"claim-annotation-malformed"},
             selected=lambda _p: {"tests/test_comp.py"},
             rvl="// revl:claim\nfn f() -> Int { return 1 }\n")
        case("missing-test-file", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "test:tests/gone.py"\n'
                  'block = "docgen:guarantees"\n',
             test=None)
        case("renamed-test", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "test:tests/test_comp.py::test_gone"\n'
                  'block = "docgen:guarantees"\n')
        case("missing-docgen-block", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "test:tests/test_comp.py"\n'
                  'block = "docgen:nope"\n')
        case("not-selected", expected={"claim-witness-not-selected"},
             selected=lambda _p: {"tests/test_other.py"})
        case("claim-chain", expected=set(),
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "claim:base"\n'
                  'base = "test:tests/test_comp.py"\n'
                  'block = "docgen:guarantees"\n')
        case("claim-cycle", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "claim:base"\n'
                  'base = "claim:known"\n'
                  'block = "docgen:guarantees"\n')
        case("bad-witness", expected={"claim-registry-invalid"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml='[claims]\nknown = "somewhere"\n'
                  'block = "docgen:guarantees"\n')
        case("no-registry", expected={"claim-registry-missing"},
             selected=lambda _p: {"tests/test_comp.py"}, toml=None)
        # The missing-registry finding is the tool's, not `claims.check`'s (a
        # checker that never finds a registry has nothing to judge), so run the
        # real entry point for it.
        buffer = io.StringIO()
        if _run_check(base / "no-registry", selection=False,
                      stream=buffer) != 1 \
                or "claim-registry-missing" not in buffer.getvalue():
            print("claims self-test: no-registry: the tool did not fail it")
            raise AssertionError("no-registry")
        cases += 1
        case("fenced-syntax-is-not-a-claim", expected=set(),
             selected=lambda _p: {"tests/test_comp.py"},
             md="```md\n<!-- revl:claim ghost -->\n```\n")
        case("markdown-html-comment", expected={"claim-unknown-id"},
             selected=lambda _p: {"tests/test_comp.py"},
             md="<!-- revl:claim ghost -->\n")
        case("markdown-inline-code-is-not-a-claim", expected=set(),
             selected=lambda _p: {"tests/test_comp.py"},
             md="Write `<!-- revl:claim ghost -->` at the sentence.\n")
        case("parameter-id-absent", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             test=_PARAM_TEST,
             toml='[claims]\nknown = "test:tests/test_comp.py::test_p[nope]"\n'
                  'block = "docgen:guarantees"\n')
        case("parameter-id-present", expected=set(),
             selected=lambda _p: {"tests/test_comp.py"},
             test=_PARAM_TEST,
             toml='[claims]\nknown = "test:tests/test_comp.py::test_p[a]"\n'
                  'block = "docgen:guarantees"\n')
        case("skipped-witness", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             test="import pytest\n@pytest.mark.skip\n" + _GOOD_TEST)
        case("not-collected", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/helper_comp.py"},
             toml='[claims]\nknown = "test:tests/helper_comp.py::test_ok"\n'
                  'block = "docgen:guarantees"\n')
        case("unnamed-entry-dead-witness", expected={"claim-witness-missing"},
             selected=lambda _p: {"tests/test_comp.py"},
             toml=_GOOD_TOML + 'dead = "test:tests/gone.py"\n')

    print(f"claims self-test: OK ({cases} cases)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check revl:claim annotations against claims.toml and the "
                    "affected selection")
    parser.add_argument("--check", action="store_true",
                        help="the gate: unknown ids, missing witnesses and "
                             "witnesses the affected selection does not run")
    parser.add_argument("--self-test", action="store_true",
                        help="plant each named failure in a synthetic tree first")
    parser.add_argument("--list", action="store_true",
                        help="print every annotation and the witness it names")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help=f"repository root (default: {ROOT})")
    parser.add_argument("--no-selection", action="store_true",
                        help="skip the affected-selection half (for a root the "
                             "selector does not belong to)")
    args = parser.parse_args(argv)
    if not (args.check or args.self_test or args.list):
        parser.error("choose --check, --self-test or --list")
    status = 0
    if args.self_test:
        status |= _self_test()
    if args.list:
        status |= _run_list(args.root)
    if args.check:
        status |= _run_check(args.root, selection=not args.no_selection)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
