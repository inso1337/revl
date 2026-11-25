"""A crossing into a service declared in a `use`d module gets its F row (issue #3640).

`export()` built each file's service table from the file's OWN
`prog.services` (`svc_objs = {svc.name: svc for svc in prog.services}`), but
`compile_files` merges the services a file's NAMED `use` imports bring in — a
service is referred to by its interface name, so `use "stdlib/wire.rvl"
{ Wire }` puts `Wire` in the composition's service table. A file whose provide
method crosses into such a service (`emit wire.take(...)` under
`requires wire: Wire`) therefore had no entry in the bound index, no
(cap, bound) pair from `_reach_call`, and NO F row: the oracle never judged
the provide-method bound for it, while the checker (which resolves `use`d
modules through `compile_files`) may refuse the same crossing — a silent
disagreement, because no row means no refusal to compare.

The exporter now resolves each `use` the way the compiler's loader does
(importer-relative first, then `REVL_IMPORT_PATH`, then the stdlib parent) and
merges the named imports' services into `svc_objs` (the file's own
declarations win a name clash), so the crossings produce B/Q bound facts and
F rows like any in-file service.

The pin fixture is
`tests/fixtures/gate_used_module_service/ok_crossing_used_service_3640.rvl`:
the checker ADMITS it (its provide method's `emission[wire.take, wire.say,
wire.close]` clause covers the crossings), and the exported F rows name the
crossings. Runs in the plain `pytest tests/` job.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

FIXTURE = ("tests/fixtures/gate_used_module_service/"
           "ok_crossing_used_service_3640.rvl")


@pytest.fixture(scope="module")
def harness():
    return load_by_path("formal_diff_corpus_used_service",
                        ROOT / "formal" / "harness" / "diff_corpus.py")


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return rows


def _rows(tsv, kind, rel):
    return [r.split("\t") for r in tsv
            if r.split("\t")[0] == kind and r.split("\t")[1] == rel]


def test_the_imported_service_enters_the_bound_index(tsv):
    """B/Q rows for `Wire` — the service the file never declares."""
    bounds = {(r[2], r[3]): r[4] for r in _rows(tsv, "B", FIXTURE)}
    assert bounds[("Wire", "take")] == "scoped"
    assert bounds[("Wire", "say")] == "scoped"
    assert bounds[("Wire", "close")] == "scoped"
    scoped = {(r[2], r[3], r[4]) for r in _rows(tsv, "Q", FIXTURE)}
    assert ("Wire", "take", "wire.take") in scoped
    assert ("Wire", "say", "wire.say") in scoped
    assert ("Wire", "close", "wire.close") in scoped


def test_a_crossing_into_the_used_service_produces_an_f_row(tsv):
    """The issue's missing row: `emit wire.take(512)` / `emit wire.say(...)`
    in the provide method, bound through the required key `wire`, via the
    verb `wire.take` / `wire.say`."""
    frows = {(r[6], r[7], r[8]) for r in _rows(tsv, "F", FIXTURE)
             if r[4] == "Beat"}
    assert ("wire.take", "wire", "wire.take") in frows
    assert ("wire.say", "wire", "wire.say") in frows
    # the row's other columns name the file's own provision surface
    for r in _rows(tsv, "F", FIXTURE):
        assert r[2:6] == ["Game", "b", "Beat", "beat"]


def test_the_fixture_is_admitted_by_the_checker():
    """The pin file is an `ok_` fixture: compile_files (which resolves the
    `use`d module, the reference side of the alignment) accepts it."""
    from revl.compiler import compile_files
    ir = compile_files([str(ROOT / FIXTURE)])
    assert ir.get("ir_version") == 3


def test_an_alias_use_imports_no_service(harness, tmp_path):
    """`use "m.rvl" as m` is a namespace import: compile_files merges no
    service from it, so the exporter must not either — a crossing through it
    keeps producing no F row (the alias arm is a separate, modelled gap)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "m.rvl").write_text(
        "pub service S { emission[t] fn go(n: Int) -> Int }\n", encoding="utf-8")
    (corpus / "main.rvl").write_text(
        'use "./m.rvl" as m\n'
        "service T { emission[t] fn run(n: Int) -> Int }\n"
        "component C requires s: m.S provides t: T {\n"
        "  provide t { fn run(n: Int) -> Int { return n } }\n"
        "}\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", tmp_path)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        rows, _facts, _census = harness.export()
    # `m.S` does not resolve to a service the exporter knows, so no bound and
    # no F row — exactly the pre-fix behavior for an alias import
    assert not [r for r in rows if r.startswith("F\t") and "main.rvl" in r]
    assert not [r for r in rows if r.startswith("B\t") and "\tS\t" in r]


def test_a_named_use_imports_the_service(harness, tmp_path):
    """The same module imported by NAME puts its service in the table: B and
    F rows appear for the crossing."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "m.rvl").write_text(
        "pub service S { emission[t] fn go(n: Int) -> Int }\n", encoding="utf-8")
    (corpus / "main.rvl").write_text(
        'use "./m.rvl" { S }\n'
        "service T { emission[t] fn run(n: Int) -> Int }\n"
        "component C requires s: S provides t: T {\n"
        "  provide t { fn run(n: Int) -> Int { let x = emit s.go(n) return x } }\n"
        "}\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", tmp_path)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        rows, _facts, _census = harness.export()
    bounds = {tuple(r.split("\t")[2:5]) for r in rows
              if r.startswith("B\t") and "main.rvl" in r}
    assert ("S", "go", "scoped") in bounds
    frows = [r.split("\t") for r in rows
             if r.startswith("F\t") and "main.rvl" in r]
    assert any(r[6] == "t" and r[7] == "s" and r[8] == "s.go" for r in frows)
