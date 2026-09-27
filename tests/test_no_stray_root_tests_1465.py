"""Guard for issue #1465: no test_*.py file at the repository root.

`test_issue_319_dunder.py` and `test_issue_322.py` sat at the repository root
instead of under `tests/`. Since `pyproject.toml` sets no `testpaths` and
every CI step that runs pytest passes an explicit path (`pytest tests/ -q`,
`pytest backends/python -q`, ...; see `.github/workflows/ci.yml`), a root-level
test file is never collected anywhere: not in CI, not by `pytest tests/ -q`,
not by any bare `pytest` invocation run from the repository root. It only
runs if someone names it directly. Both files were moved into `tests/`
(`test_dunder_names_refused_319.py`, `test_async_provide_method_acquisition_322.py`);
this test keeps a new one from silently reappearing at the root.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_no_test_files_at_repository_root():
    stray = sorted(p.name for p in _ROOT.glob("test_*.py"))
    assert not stray, (
        f"found test_*.py at the repository root: {stray}. "
        "A root-level test file is never collected by CI or by "
        "`pytest tests/ -q` (pyproject.toml sets no testpaths, and every "
        "CI pytest invocation passes an explicit path) -- move it under "
        "tests/ with a name that says what it tests."
    )
