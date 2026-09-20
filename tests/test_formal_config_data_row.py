"""The config-is-data row of the formal differential harness (issue 1161).

The G4 guarantee has three rules, and only two of them are about a CROSSING:

  * `Oracle.g4OK` — the emission MARKER rule, over a classified statement's
    marker against the interface's declared `emission[...]`;
  * `Oracle.hostAcquireOK` — the host ACQUIRE rule, over the position a host
    acquire verb is written at;
  * `Oracle.configDataOK` — CONFIG-IS-DATA (item 378), over a config field's
    declared TYPE. A config value is injected as static data at plug/spawn/load
    time, so its type must be built, transitively, out of data: an arrow field
    is a live callable the body invokes past every authority fold, and a
    `service` field is a capability handed over with no wiring at all.

The third judges a DECLARATION rather than a body, so no crossing fact could
express it. Until the export carried type-shape facts, a refusal of that class
was invisible to the model and the harness reported it as `missed-G4` — fatal —
wherever the fixture was put, `tests/formal_corpus/` included
(`diff_corpus.CORPUS_DIRS` is `("examples", "tck", "tests")`).

What this module is FOR is the exporter, not the Lean side. `configDataOK` is
three tokens; the risk lives in `diff_corpus.config_shape`, which decomposes a
declared type the way `typecheck._walk_config_type` descends it — and does so
over the PARSED program, where `lower._resolve_type_aliases` has not erased a
transparent alias and `taint.extract_and_normalize` has not stripped a
qualifier. So the sweep below runs the SHIPPED `check_config_field_is_data`, at
its own point in the shipped pipeline, with the arguments `lower._check_config`
hands it, and holds the exporter to its verdict field by field.

This module runs in the plain `pytest tests/` job. `make formal` needs a Lean
toolchain, and the python half of the harness had no test without one — the
same collection gap `tests/test_formal_attenuation_namespace.py` and
`tests/test_formal_derived_namespace.py` were written to close.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: The reproducer, and the whole point of the issue: a config-is-data rejection
#: fixture that lives as a FILE. PR #1156 had to carry its shapes as inline
#: strings in `REJECTED_PROGRAMS` instead, which the gate/reference census
#: measures and the formal harness never walks, because the missing row made a
#: refusal of this class fatal wherever the file was put.
FIXTURE = "examples/rejections/g4_config_record_arrow.rvl"
FIXTURE_FIELD = (FIXTURE, "component", "Loader", "hooks")

#: Every form `config_shape` can label a node with. The first group is data;
#: the second is what the walk refuses on. `Oracle.configDataForms` is the
#: first group, and `test_both_sides_spell_the_same_allowlist` pins that.
DATA_FORMS = frozenset(
    {"scalar", "container", "record", "variant", "struct", "tparam"})
REFUSING_FORMS = frozenset({"arrow", "service", "erased", "opaque"})


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus", ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows]


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(["\t".join(r) for r in tsv])


def _rows(tsv, kind, rel=None):
    return [r for r in tsv
            if r and r[0] == kind and (rel is None or r[1] == rel)]


# --------------------------------------------- the shipped checker as oracle

#: `lower._check_config` spells the owner for the diagnostic; the export spells
#: it for a key. One regex reads the first back into the second.
_OWNER = re.compile(r"^(component|extern) `([^`]+)`$")


def shipped_config_verdicts(source: str, rel: str) -> dict:
    """`{(kind, owner, field): "ok"|"fail"}` from the SHIPPED checker.

    `check_config_field_is_data` is replaced for the duration of one compile by
    a recorder that CALLS it and swallows the refusal, so every field of the
    file is judged rather than only the ones before the first offender. Nothing
    about the walk, its arguments or where it runs is reproduced here: the
    point is to have an oracle that cannot drift from the compiler, since
    `config_shape` is a transcription of the same descent and a transcription
    checked against a copy of itself checks nothing.
    """
    from revl import lower as lower_mod
    from revl.compiler import compile_source
    from revl.errors import RevlError
    from revl.typecheck import check_config_field_is_data

    seen: dict = {}

    def recorder(filename, line, field_name, owner, type_name, **kw):
        m = _OWNER.match(owner)
        assert m, f"unexpected config owner spelling {owner!r}"
        try:
            check_config_field_is_data(filename, line, field_name, owner,
                                       type_name, **kw)
        except RevlError:
            seen[(m.group(1), m.group(2), field_name)] = "fail"
        else:
            seen[(m.group(1), m.group(2), field_name)] = "ok"

    original = lower_mod.check_config_field_is_data
    lower_mod.check_config_field_is_data = recorder
    try:
        try:
            compile_source(source, rel)
        except RevlError:
            # The file may be refused for something else, before or after the
            # config phase. Whatever the recorder reached is still the shipped
            # verdict for those fields.
            pass
    finally:
        lower_mod.check_config_field_is_data = original
    return seen


# ------------------------------------------------------------- the fixture

def test_the_fixture_is_a_file_in_a_corpus_directory(harness):
    """The exit criterion of the issue, stated as the thing it is about. A
    shape that exists only as a string in `REJECTED_PROGRAMS` is invisible to
    anything that walks the corpus as files, the formal harness included."""
    path = ROOT / FIXTURE
    assert path.is_file()
    assert FIXTURE.split("/")[0] in harness.CORPUS_DIRS


def test_the_shipped_checker_refuses_the_fixture():
    """The premise. If this ever stops refusing, the rest of the module is
    measuring the wrong thing and should be read again, not deleted."""
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        compile_source((ROOT / FIXTURE).read_text(encoding="utf-8"), FIXTURE)
    info = classify(excinfo.value)
    assert (info["code"], info["category"]) == ("G4", "config-data")
    assert "has type `Hooks`, which reaches an arrow (function) type" \
        in str(excinfo.value)


def test_the_fixture_carries_no_other_refusal():
    """Non-vacuity for the fixture itself: the config field is the ONLY thing
    wrong with it. Drop the arrow field from `Hooks` and the file compiles, so
    the refusal above is attributable to that field and not to whichever other
    rule happened to fire first."""
    from revl.compiler import compile_source

    text = (ROOT / FIXTURE).read_text(encoding="utf-8")
    assert ", tail: (Str) -> Str" in text
    compile_source(text.replace(", tail: (Str) -> Str", ""), FIXTURE)


def test_the_model_derives_the_fixture_refusal(verdicts):
    """The exit criterion on the model side: the refusal is now DERIVED, from
    facts, rather than absent. `missed-G4` on the pre-change model."""
    assert verdicts.configs[FIXTURE_FIELD] == "fail"


def test_the_fixture_offends_through_an_indirection(tsv):
    """...and it is derived by DESCENDING, not by reading the spelling. The
    field is written `Hooks`, which is a data form; the arrow is one level
    down, behind a record field, next to a scalar sibling that is fine. A row
    that judged the written head would admit this file."""
    nodes = [(r[6], r[7]) for r in _rows(tsv, "CN", FIXTURE)
             if r[4] == "hooks"]
    assert nodes == [("record", "Hooks"), ("scalar", "Str"),
                     ("arrow", "(Str) -> Str")]


def test_the_fixture_has_no_crossing_to_blame(verdicts):
    """Why it is a ROW and not a case in `g4OK`. Every crossing rule the model
    already had admits this file: the marker row is clean, the provide-method
    bound is inside its declaration, and there is no spawn edge at all. The
    only thing wrong with the program is a declared type."""
    assert verdicts.comps[(FIXTURE, "Loader")] == "ok"
    assert verdicts.providers[
        (FIXTURE, "Loader", "out", "Sink", "write")] == "ok"
    assert not [k for k in verdicts.spawns if k[0] == FIXTURE]


# ------------------------------------------ the exporter against the checker

def test_the_export_agrees_with_the_shipped_checker_field_by_field(
        harness, tsv, verdicts):
    """The load-bearing test. For every file in the corpus that declares a
    config field, run the SHIPPED `check_config_field_is_data` where the
    compiler runs it and compare, field by field, with the `CD` verdict the
    export derives from its own walk.

    This is the one place `config_shape` can be wrong without the gate
    noticing. A spurious `fail` over an accepted file moves it into
    `formal-strict`, which was informational until issue #1169 and is now a
    gate failure; before that promotion the corpus gate stayed green while
    the model quietly refused a program revl ships.

    The two walks are NOT the same walk. The checker sees a program
    `taint.extract_and_normalize` has stripped and `lower._resolve_type_aliases`
    has erased; the export sees the program as parsed, and reaches the same
    verdict by resolving the alias through the type table instead. That they
    agree on every field of the corpus is the claim.
    """
    files = sorted({r[1] for r in _rows(tsv, "CF")})
    assert files, "the corpus declares no config field at all"
    reached, disagreed, unreached = 0, [], []
    for rel in files:
        shipped = shipped_config_verdicts(
            (ROOT / rel).read_text(encoding="utf-8"), rel)
        for r in _rows(tsv, "CF", rel):
            key = (r[2], r[3], r[4])
            want = shipped.get(key)
            if want is None:
                # The file is refused before the config phase runs, so the
                # shipped checker never judged this field and there is nothing
                # to compare against.
                unreached.append(f"{rel} {key}")
                continue
            reached += 1
            got = verdicts.configs[(rel, r[2], r[3], r[4])]
            if got != want:
                disagreed.append(f"{rel} {key}: shipped={want} export={got}")
    assert disagreed == [], "\n".join(disagreed)
    assert reached >= 60, (
        f"only {reached} config fields reached the shipped checker "
        f"({len(unreached)} unreached: {unreached[:5]})")


@pytest.mark.parametrize("label,expected,source", [
    ("an arrow config field", "fail", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { cb: (Str) -> Str } }
"""),
    ("a config field aliasing an arrow", "fail", """
type Cb = (Str) -> Str
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { cb: Cb } }
"""),
    ("a config field aliasing a LIST of arrows", "fail", """
type Row = List[(Str) -> Str]
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { cb: Row } }
"""),
    ("a config field aliasing an alias of an arrow", "fail", """
type Inner = (Str) -> Str
type Outer = Inner
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { cb: Outer } }
"""),
    ("a config field of a service type", "fail", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { s: S } }
"""),
    ("a config field aliasing the erased type", "fail", """
type Loose = Any
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { x: Loose } }
"""),
    ("a config field of an undeclared head", "fail", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { x: Opaque } }
"""),
    ("a record config field with a smuggled arrow", "fail", """
type Hooks = { head: Str, tail: (Str) -> Str }
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { h: Hooks } }
"""),
    ("a LIST of records with a smuggled arrow", "fail", """
type Row = { head: Str, tail: (Str) -> Str }
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { h: List[Row] } }
"""),
    ("a scalar config field", "ok", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { n: Str } }
"""),
    ("a config field aliasing a scalar", "ok", """
type Name = Str
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { n: Name = "x" } }
"""),
    ("a config field of a nullary-tag variant", "ok", """
type Colour = Red | Green
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { c: Colour } }
"""),
    ("a config field of a record of data", "ok", """
type Limits = { soft: Int, hard: Int }
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { l: Limits } }
"""),
    ("a config field of a generic record instantiated with data", "ok", """
type Box[T] = { v: T }
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { b: Box[Int] } }
"""),
    ("a config field of a recursive data type", "ok", """
type Tree = Leaf | Node(Tree)
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { t: Tree } }
"""),
    ("a config field written as a secret", "ok", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { t: Secret[Str] } }
"""),
    ("a container of data", "ok", """
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { m: Map[Str, List[Int]] } }
"""),
])
def test_the_export_agrees_on_a_shape_the_corpus_does_not_carry(
        harness, label, expected, source):
    """The corpus only exercises four of the ten forms `config_shape` can
    label — `arrow`, `container`, `scalar`, `variant` — so the sweep above
    cannot see a `record`, `tparam`, `service`, `erased` or `opaque` node go
    wrong. These do, and each is checked against the shipped checker the same
    way: the exporter never gets to be its own oracle.

    (`struct` is the tenth, and nothing here reaches it: the parser accepts a
    structural record literal as a declared type at no site a config field's
    type can be written or resolved through. The branch is kept because the
    checker has one — `_walk_config_type` opens with it, item 71 — and the two
    walks stay arm for arm.)

    The alias rows are the reason the walks can differ at all: the checker
    judges `(Str) -> Str` because `_resolve_type_aliases` erased `Cb` first,
    and the export judges `Cb` and resolves it through the type table.
    """
    from revl.parser import Parser

    rel = "shape.rvl"
    shipped = shipped_config_verdicts(source, rel)
    assert shipped, f"{label}: the shipped config phase was never reached"
    assert set(shipped.values()) == {expected}, f"{label}: {shipped}"

    prog = Parser(source, rel).parse()
    rows = [r.split("\t") for r in harness.config_rows(prog, rel)]
    fields = [r for r in rows if r[0] == "CF"]
    assert fields, f"{label}: the export emitted no config field"
    for cf in fields:
        forms = [r[6] for r in rows
                 if r[0] == "CN" and (r[2], r[3], r[4]) == (cf[2], cf[3], cf[4])]
        got = "ok" if all(f in DATA_FORMS for f in forms) else "fail"
        assert got == expected, f"{label}: forms={forms}"


def test_an_extern_config_field_is_held_to_the_same_bar(harness):
    """`lower._check_config` is called for a component's config AND an
    extern's (item 379), so the export ships rows for both owners. An extern's
    `config` block is not one of the sites `_resolve_type_aliases` substitutes
    at, so the checker meets the alias name UNRESOLVED there and refuses it as
    opaque; the export resolves it and refuses the arrow behind it. Different
    reasons, one verdict, which is all the `CD` row carries."""
    from revl.parser import Parser

    source = """
type Handler = (Str) -> Str
extern pure fn load_rows(path: Str) -> Int
  config { on_row: Handler }
  = @py { return 0 }
service S { emission[log] fn go(r: Str) -> Int }
component C requires log: S { config { n: Str } }
"""
    rel = "extern_config.rvl"
    shipped = shipped_config_verdicts(source, rel)
    assert shipped[("extern", "load_rows", "on_row")] == "fail"
    assert shipped[("component", "C", "n")] == "ok"

    rows = [r.split("\t")
            for r in harness.config_rows(Parser(source, rel).parse(), rel)]
    owners = {(r[2], r[3], r[4]) for r in rows if r[0] == "CF"}
    assert owners == {("extern", "load_rows", "on_row"), ("component", "C", "n")}
    forms = [r[6] for r in rows
             if r[0] == "CN" and r[2] == "extern" and r[4] == "on_row"]
    assert any(f not in DATA_FORMS for f in forms), forms


# ------------------------------------------------------------- the allowlist

def test_both_sides_spell_the_same_allowlist(harness):
    """The classification is carried in the fact row and the JUDGMENT is
    stated twice — once in python, once in `Oracle.configDataForms`. Two
    spellings of one list is exactly the drift a differential oracle cannot
    report, because the two sides would disagree only on a form neither corpus
    file exercises and then agree on everything measured."""
    lean = (ROOT / "formal" / "harness" / "Oracle.lean").read_text(
        encoding="utf-8")
    m = re.search(r"def configDataForms : List String :=\s*\[([^\]]*)\]", lean)
    assert m, "Oracle.lean no longer defines configDataForms"
    assert set(re.findall(r'"([^"]+)"', m.group(1))) == DATA_FORMS
    assert set(harness.CONFIG_DATA_FORMS) == DATA_FORMS


def test_the_allowlist_refuses_a_form_neither_side_has_heard_of(harness):
    """It is an ALLOWLIST, and that is the whole design. The checker's own rule
    is "a head that is not *provably* data is refused" — written that way
    because enumerating the forbidden heads left `Any`, `Value` and every
    opaque nominal passing (item 378). A denylist here would reintroduce that
    one layer out, so an unknown form must REFUSE: the model can then only be
    stricter than the checker, never blind to one of its refusals."""
    assert not (REFUSING_FORMS & DATA_FORMS)
    rel = "synthetic.rvl"
    rows = [
        "\t".join(["CF", rel, "component", "C", "f", "Whatever"]),
        "\t".join(["CN", rel, "component", "C", "f", "0", "quantum", "Whatever"]),
    ]
    v = harness.reference_from_tsv(rows)
    assert v.configs[(rel, "component", "C", "f")] == "fail"


def test_every_exported_form_is_one_of_the_ten(tsv):
    """...and the corpus never smuggles a form past that check either."""
    forms = {r[6] for r in _rows(tsv, "CN")}
    assert forms <= (DATA_FORMS | REFUSING_FORMS), sorted(forms)


# --------------------------------------------------------------- the wiring

def test_the_cd_row_is_compared_and_counted(harness, verdicts):
    """A verdict map nothing reads is a verdict map that cannot disagree.
    `Verdicts.configs` is in the tuple, in `total()`, and comes back out of
    `parse_verdicts` in the shape the Lean side writes it."""
    assert "configs" in harness.Verdicts._fields
    text = "\t".join(["CD", FIXTURE, "component", "Loader", "hooks",
                      "data=fail"]) + "\n"
    assert harness.parse_verdicts(text).configs[FIXTURE_FIELD] == "fail"
    assert verdicts.total() == sum(
        len(getattr(verdicts, f)) for f in harness.Verdicts._fields)
    assert len(verdicts.configs) > 0


def test_a_config_refusal_clears_missed_g4(harness, capsys):
    """The wiring that made the issue fatal. `checker_alignment` buckets a file
    the checker refuses under G4 as `missed-G4` — one of the two
    `FATAL_BUCKETS` — unless some G4 row of the model says `fail`. The `CD` row
    has to join the two crossing rules in that fold, or the fixture stays fatal
    however well the model derives its refusal."""
    empty = {f: {} for f in harness.Verdicts._fields}
    without = harness.Verdicts(**empty)
    assert harness.checker_alignment({FIXTURE: {}}, [], without) == [
        f"missed-G4: {FIXTURE}"]
    capsys.readouterr()

    with_cd = without._replace(configs={FIXTURE_FIELD: "fail"})
    assert harness.checker_alignment({FIXTURE: {}}, [], with_cd) == []
    capsys.readouterr()


def test_the_coverage_ratchet_bites(harness, monkeypatch):
    """The `CD` row would agree vacuously over a corpus of files everyone
    wrote to compile, so `config_coverage` demands both verdicts and a
    non-empty walk behind the admitting one — the guard
    `attenuation_coverage` and `confinement_coverage` already carry."""
    assert harness.config_coverage() == []

    monkeypatch.setattr(harness, "_CONFIG_FIELDS", {})
    assert len(harness.config_coverage()) == 1

    monkeypatch.setattr(harness, "_CONFIG_FIELDS",
                        {("a", "component", "C", "f"): ("ok", ("scalar",))})
    assert any("NO refused" in m for m in harness.config_coverage())

    monkeypatch.setattr(harness, "_CONFIG_FIELDS", {
        ("a", "component", "C", "f"): ("ok", ()),
        ("b", "component", "C", "g"): ("fail", ("arrow",))})
    assert any("reaches a node" in m for m in harness.config_coverage())
