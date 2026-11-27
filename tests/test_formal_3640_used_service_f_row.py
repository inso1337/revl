"""Issue #3640: the formal exporter emitted no F row for a crossing into a
service declared in a `use`d module — `svc_objs` was built from the single
file's own `prog.services`, so the bound index never knew the used service and
`_reach_call` recorded nothing. `diff_corpus._used_services` now resolves a
file's `use` declarations the way compile_files does (file-relative first, then
the repo root that makes `stdlib/...` spellings work) and merges their services
into the bound index.

Pinned with a corpus fixture whose only `Wire` declaration comes from
`use "stdlib/wire.rvl" { Wire }`: the crossing `emit wire.take(1)` must produce
an F row naming cap `wire.take` / bound `wire`. With `_used_services` emptied
(the pre-fix behaviour), the same fixture produces no F row.
"""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

harness = load_by_path("formal_diff_corpus_3640",
                       ROOT / "formal" / "harness" / "diff_corpus.py")

FIXTURE = '''\
use "stdlib/wire.rvl" { Wire }

service Eyes { emission[wire.take] fn watch() -> Int }

component Watcher provides eyes: Eyes requires wire: Wire {
  provide eyes {
    fn watch() -> Int {
      emit wire.take(1)
      return 0
    }
  }
}
'''


@pytest.fixture
def fixture_file():
    d = Path(tempfile.mkdtemp(dir=ROOT, prefix=".tmp3640-"))
    f = d / "used_wire.rvl"
    f.write_text(FIXTURE, encoding="utf-8")
    yield f
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _f_rows(monkeypatch, fixture_file):
    monkeypatch.setattr(harness, "corpus_files", lambda: [fixture_file])
    rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows if r.startswith("F\t")]


def test_the_fixture_compiles_through_the_module_path(fixture_file):
    from revl import compile_files
    ir = compile_files([str(fixture_file)])
    assert [c["name"] for c in ir["components"]] == ["Watcher"]


def test_a_crossing_into_a_used_modules_service_produces_an_f_row(
        monkeypatch, fixture_file):
    frows = _f_rows(monkeypatch, fixture_file)
    assert any(r[2] == "Watcher" and r[3] == "eyes" and r[6] == "wire.take"
               and r[7] == "wire" for r in frows), frows


def test_without_used_service_resolution_the_same_fixture_has_no_f_row(
        monkeypatch, fixture_file):
    """The pre-#3640 behaviour: services from `use`d modules never reached the
    bound index, so no F row was written for the crossing."""
    monkeypatch.setattr(harness, "_used_services", lambda path, prog: {})
    assert _f_rows(monkeypatch, fixture_file) == []
