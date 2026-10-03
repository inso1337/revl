"""The bench's compiler loaders leave this process with one `revl` (issue #1800).

`bench/rescore.py::load_compiler` and `bench/run.py::compile_check` re-import
the compiler. They used to leave the fresh copy installed in `sys.modules`.
Anything that had imported `revl` first then held the old `RevlError` while
the compiler raised the new one, so `except RevlError` missed real refusals.
`tests/test_doc_examples.py` and `tests/test_selfhost_emit_ts.py` failed
whenever they ran after a test that reached either loader in-process.

The pairwise test runs a reaching test and then a dependent one, in that order,
in a child pytest, which is exactly the shape that failed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"

BAD = "component C {\n  let = 1\n}\n"   # a syntax refusal
DRAFT = (
    "service Cache { fn get(key: Str) -> Str }\n"
    "component C provides c: Cache {\n"
    '  let pool = effect hole[Db] "a pooled connection" undo pool.close()\n'
    "  provide c {\n"
    '    fn get(key) = hole "look up in the store"\n'
    "  }\n"
    "}\n")


def _refused_by_the_first_revl(error_class, compile_source) -> bool:
    try:
        compile_source(BAD, "bad.rvl")
    except error_class:
        return True
    return False


# Each case is the body of a test that reaches one loader in-process.
REACHERS = {
    "rescore.load_compiler": """
        import rescore
        rescore.load_compiler(ROOT)
    """,
    "run.compile_check": """
        import run
        assert run.compile_check("component C {\\n  let = 1\\n}\\n", "bad.rvl")[0] is False
    """,
}


@pytest.mark.parametrize("reacher", sorted(REACHERS))
def test_a_reaching_test_then_a_dependent_test_in_one_session(tmp_path, reacher):
    (tmp_path / "test_a_reach.py").write_text(textwrap.dedent(f"""
        import sys
        from pathlib import Path
        ROOT = Path({str(ROOT)!r})
        sys.path.insert(0, str(ROOT / "bench"))

        def test_reach():
        """) + textwrap.indent(textwrap.dedent(REACHERS[reacher]), "    "),
        encoding="utf-8")
    # The dependent module imports `revl` at COLLECTION time, before the
    # reacher runs, the way tests/test_doc_examples.py does. Its draft is the
    # docs/holes.md example: the hole's expected type comes from the service,
    # which the split process lost (a T3 refusal of a program that compiles).
    (tmp_path / "test_b_depend.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from revl import RevlError, compile_source\n\n"
        "def test_the_compiler_refuses_with_the_error_this_module_imported():\n"
        "    try:\n"
        f"        compile_source({BAD!r}, 'bad.rvl')\n"
        "    except RevlError:\n"
        "        return\n"
        "    raise AssertionError('no RevlError')\n\n"
        "def test_a_draft_hole_still_takes_its_type_from_the_service():\n"
        f"    compile_source({DRAFT!r}, 'draft.rvl')\n", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", str(tmp_path / "test_a_reach.py"),
         str(tmp_path / "test_b_depend.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    assert "3 passed" in run.stdout, run.stdout[-2000:]


def test_compile_check_puts_the_process_revl_back():
    sys.path.insert(0, str(BENCH))
    import run as bench_run  # noqa: PLC0415
    from revl import RevlError, compile_source  # noqa: PLC0415

    before = sys.modules["revl"]
    ok, _ = bench_run.compile_check(BAD, "bad.rvl")
    assert ok is False
    assert sys.modules["revl"] is before
    assert _refused_by_the_first_revl(RevlError, compile_source)
    assert bench_run.scoring_compiler() == "src/revl"


def test_load_compiler_reuses_this_process_revl_for_the_same_root():
    sys.path.insert(0, str(BENCH))
    import rescore  # noqa: PLC0415
    from revl import RevlError  # noqa: PLC0415

    before = sys.modules["revl"]
    compile_source, error_class, _classify = rescore.load_compiler(ROOT)
    assert sys.modules["revl"] is before
    assert error_class is RevlError
    assert _refused_by_the_first_revl(RevlError, compile_source)
