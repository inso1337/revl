"""Issue #2692: one derived inverse-provenance class on every audit crossing.

The premise is that `revl` already records "how can this be undone" facts in
several unrelated places, and an audit reader had to consult a different
document per crossing, with the "nothing is known" case existing only as a
crash-recovery OUTCOME after the fact. This file pins the single field that
replaces that scatter: `inverse`, a closed-vocabulary value carried on every
extern crossing in `revl audit --json`, derived from facts already in the IR,
with no new syntax and no behaviour change.

Three things are load-bearing here:

  * the vocabulary is closed (`declared`/`generated`/`compensation`/`forward`/
    `irreversible`/`unknown`), and `unknown` is a VALUE, never a missing field;
  * the class is derived, never inferred from a name or a shape: an `open`/
    `close` pair with no declared inverse stays `unknown`;
  * a class that WEAKENS between two generations is authority drift the
    `--diff` gate reports and fails unless it is acknowledged.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report, evaluate, render  # noqa: E402
from revl.interchange import validate  # noqa: E402
from revl.inverse import INVERSE_CLASSES, inverse_class, inverse_rank  # noqa: E402

# One corpus reaching every class the slice can derive today. The two `pure`
# reads and the `local` write are the no-provenance case; the acquire and the
# witnessed mutation declare exact inverses; the compensated emission carries an
# offset; the two owed emissions carry a forward claim; the bare emission has
# nothing.
CORPUS = """\
type Handle = { fd: Int }
type W = { path: Str }
type E = { msg: Str }

extern pure fn digest(x: Str) -> Str = @py { return x }
extern pure fn release_h(h: Handle) -> Unit = @py { return None }
extern pure fn restore(w: W) -> Unit = @py { return None }
extern pure fn rollback() -> Unit = @py { return None }
extern local fn record(p: Str) -> Unit = @py { return None }
extern acquire fn open_h() -> Handle undo release_h(result) = @py { return {"fd": 1} }
extern witnessed fn touch(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }
extern emission fn send(x: Str) -> Str = @py { return x }
extern emission fn commit(x: Str) -> Str compensate rollback() = @py { return x }
extern emission deferred idempotent(key: k) fn post(k: Str) -> Unit
  = @py { return None }
extern emission deferred idempotent fn ping(k: Str) -> Unit = @py { return None }
extern emission deferred fn drop_off(k: Str) -> Unit = @py { return None }

service Journal { fn append(n: Str) -> Unit }

component Worker provides j: Journal {
  let h = effect open_h() undo release_h(h)
  emit send(digest("m"))
  emit commit("v")
  emit post("k")
  emit ping("k")
  emit drop_off("k")
  effect touch("t")
  provide j {
    fn append(n) = record("r")
  }
}
"""

# The same crossing name, declared first with an exact inverse and then as a
# compensated emission: the crossing token (`host:C:deposit`) is unchanged, so
# only the inverse class can carry the drift.
_DECLARED = """\
type W = { path: Str }
type E = { msg: Str }
extern pure fn restore(w: W) -> Unit = @py { return None }
extern witnessed fn deposit(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }
component C {
  effect deposit("t")
}
"""

_COMPENSATION = """\
extern pure fn rollback() -> Unit = @py { return None }
extern emission fn deposit(p: Str) -> Str compensate rollback() = @py { return p }
component C {
  emit deposit("t")
}
"""

# A read that turns into an effect under the same crossing name. `unknown` is
# the weakest inverse class, so the rank alone would call this a strengthening;
# an effect appearing where there was none is the dangerous direction.
_READ = """\
extern pure fn deposit(p: Str) -> Str = @py { return p }
service J { fn run() -> Str }
component C provides j: J {
  provide j { fn run() = deposit("t") }
}
"""

_READ_TO_EMISSION = """\
extern emission fn deposit(p: Str) -> Str = @py { return p }
service J { emission fn run() -> Str }
component C provides j: J {
  provide j { fn run() { emit deposit("t"); return "x" } }
}
"""

_READ_TO_WITNESSED = """\
type W = { path: Str }
type E = { msg: Str }
extern pure fn restore(w: W) -> Unit = @py { return None }
extern witnessed fn deposit(p: Str) -> Result[W, E] undo restore(result) = @py { return {"path": p} }
component C {
  effect deposit("t")
}
"""


def _audit(source: str) -> dict:
    return audit_report(compile_source(source, "corpus.rvl"))


def _extern(audit: dict, name: str) -> dict:
    return next(ext for ext in audit["externs"] if ext["name"] == name)


def _boundary_entry(audit: dict, component: str, name: str) -> dict:
    return next(ext for ext in audit["boundary"][component]["externs"]
                if ext["name"] == name)


# --------------------------------------------------------------- vocabulary

def test_the_vocabulary_is_the_closed_set():
    assert INVERSE_CLASSES == (
        "declared", "generated", "compensation", "forward", "irreversible",
        "unknown")


def test_an_unknown_value_ranks_below_every_known_class():
    for cls in INVERSE_CLASSES:
        if cls != "unknown":
            assert inverse_rank(cls) > inverse_rank("unknown")
    assert inverse_rank("not-a-class") == 0


def test_the_trust_order_is_the_documented_one():
    # docs/audit-diff.md's weakening table and docs/interchange-format.md's
    # vocabulary both present this order; the diff direction is derived from it.
    ordered = ["declared", "generated", "compensation", "forward",
               "irreversible", "unknown"]
    ranks = [inverse_rank(cls) for cls in ordered]
    assert ranks == sorted(ranks, reverse=True)
    assert len(set(ranks)) == len(ordered)


# ------------------------------------------------- qualified derivations

def test_declared_exact_inverses_read_declared():
    audit = _audit(CORPUS)
    assert _extern(audit, "open_h")["inverse"] == "declared"      # acquire
    assert _extern(audit, "touch")["inverse"] == "declared"       # witnessed


def test_an_extern_with_no_undo_and_no_register_reads_unknown():
    # The acceptance case: a `pure` read and a `local` write are forbidden an
    # inverse slot, so no provenance fact exists. The field is present and says
    # so, rather than being omitted.
    audit = _audit(CORPUS)
    for name in ("digest", "release_h", "restore", "rollback", "record"):
        entry = _extern(audit, name)
        assert entry["inverse"] == "unknown", (name, entry)
    local = _extern(audit, "record")
    assert local["class"] == "local"
    # the fact comes from the lowered extern: a local write is forbidden an
    # `undo` slot, so there is nothing for the class to read.
    record_ir = next(ext for ext in compile_source(CORPUS, "corpus.rvl")["externs"]
                     if ext["name"] == "record")
    assert "undo" not in record_ir


def test_an_emission_with_no_offset_and_no_forward_claim_is_irreversible():
    audit = _audit(CORPUS)
    assert _extern(audit, "send")["inverse"] == "irreversible"


def test_a_compensated_emission_reads_compensation():
    audit = _audit(CORPUS)
    entry = _extern(audit, "commit")
    assert entry["inverse"] == "compensation"
    assert entry["class"] == "emission"


@pytest.mark.parametrize("name", ["post", "ping"])
def test_a_deferred_emission_with_a_register_reads_forward(name):
    audit = _audit(CORPUS)
    entry = _extern(audit, name)
    assert entry["deferred"] is True
    assert entry.get("register") in ("keyed", "declared")
    assert entry["inverse"] == "forward"


def test_a_deferred_emission_without_a_register_has_no_forward_claim():
    audit = _audit(CORPUS)
    entry = _extern(audit, "drop_off")
    assert entry["deferred"] is True
    assert "register" not in entry
    # the emission rule still applies: the effect is irreversible; a forward
    # re-issue the declaration makes safe is what is missing.
    assert entry["inverse"] == "irreversible"


def test_the_class_is_on_the_per_component_crossing_too():
    audit = _audit(CORPUS)
    assert _boundary_entry(audit, "Worker", "open_h")["inverse"] == "declared"
    assert _boundary_entry(audit, "Worker", "commit")["inverse"] == "compensation"
    assert _boundary_entry(audit, "Worker", "send")["inverse"] == "irreversible"
    assert _boundary_entry(audit, "Worker", "digest")["inverse"] == "unknown"


def test_every_crossing_carries_a_class_and_none_is_inferred_from_a_name():
    # `open_file`/`close_file` is exactly the shape a name-based inference would
    # pair; `delete` is a verb that reads "reversible" to a human. Both have no
    # declared provenance, so they stay unknown/irreversible. Nothing is
    # inferred, and `generated` is never produced from a name.
    audit = _audit(
        "extern pure fn open_file(p: Str) -> Str = @py { return p }\n"
        "extern pure fn close_file(p: Str) -> Unit = @py { return None }\n"
        "extern emission fn delete(p: Str) -> Unit = @py { return None }\n"
        "component C { emit delete(open_file(\"p\")) }\n")
    classes = {ext["name"]: ext["inverse"] for ext in audit["externs"]}
    assert classes["open_file"] == "unknown"
    assert classes["close_file"] == "unknown"
    assert classes["delete"] == "irreversible"
    assert "generated" not in classes.values()


def test_every_boundary_crossing_entry_carries_the_field():
    audit = _audit(CORPUS)
    for component in audit["boundary"].values():
        for entry in component["externs"]:
            assert "inverse" in entry
            assert entry["inverse"] in INVERSE_CLASSES


def test_unknown_is_a_value_never_a_missing_key():
    audit = _audit(CORPUS)
    for ext in audit["externs"]:
        assert "inverse" in ext
    assert _extern(audit, "digest")["inverse"] == "unknown"


def test_literal_and_empty_inputs_fall_back_to_unknown():
    assert inverse_class(None) == "unknown"
    assert inverse_class({}) == "unknown"
    assert inverse_class({"class": None}) == "unknown"
    assert inverse_class({"class": "witnessed"}) == "unknown"


# ---------------------------------------------------------- audit --json

def test_the_cli_json_carries_the_class_on_every_crossing(tmp_path, capsys):
    src = tmp_path / "corpus.rvl"
    src.write_text(CORPUS)
    assert main(["audit", str(src), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert validate(doc) == []
    assert {ext["name"]: ext["inverse"] for ext in doc["externs"]} == {
        "digest": "unknown",
        "release_h": "unknown",
        "restore": "unknown",
        "rollback": "unknown",
        "record": "unknown",
        "open_h": "declared",
        "touch": "declared",
        "send": "irreversible",
        "commit": "compensation",
        "post": "forward",
        "ping": "forward",
        "drop_off": "irreversible",
    }
    for entry in doc["boundary"]["Worker"]["externs"]:
        assert entry["inverse"] in INVERSE_CLASSES


def test_the_cli_and_the_library_report_the_same_class(tmp_path, capsys):
    # `revl audit --json` (the CLI) and `audit_report` (the library twin the
    # diff and policy gates read) are two builders of the same surface. The two
    # entry dicts have unrelated historical drift, so this pins the contract
    # this slice owns: the derived class is identical on both.
    src = tmp_path / "corpus.rvl"
    src.write_text(CORPUS)
    assert main(["audit", str(src), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    cli = {ext["name"]: ext["inverse"] for ext in doc["externs"]}
    library = {ext["name"]: ext["inverse"]
               for ext in audit_report(compile_files([str(src)]))["externs"]}
    assert cli == library
    assert cli["commit"] == "compensation"
    assert cli["drop_off"] == "irreversible"
    assert cli["digest"] == "unknown"


# ------------------------------------------------------------ the drift gate

def test_declared_to_compensation_is_a_weakened_inverse():
    before = _audit(_DECLARED)
    after = _audit(_COMPENSATION)
    # the crossing token is unchanged; only the inverse class moved.
    result = evaluate(before, after)
    assert result["added"] == []
    assert result["inverse_weakened"] == ["inverse:deposit"]
    assert result["inverse_strengthened"] == []
    assert result["widened"] is True
    assert "inverse:deposit" in result["unacknowledged"]


def test_a_weakened_inverse_is_acknowledged_like_any_other_drift():
    before = _audit(_DECLARED)
    after = _audit(_COMPENSATION)
    result = evaluate(before, after, accepted={"inverse:deposit"})
    assert result["widened"] is False
    assert result["acknowledged"] == ["inverse:deposit"]


def test_compensation_to_declared_is_a_strengthening_and_passes():
    before = _audit(_COMPENSATION)
    after = _audit(_DECLARED)
    result = evaluate(before, after)
    assert result["inverse_weakened"] == []
    assert result["inverse_strengthened"] == ["inverse:deposit"]
    assert result["widened"] is False


def test_a_read_that_becomes_an_emission_fails_the_gate():
    # `unknown` is the weakest inverse class, so the rank alone would call this
    # a strengthening. An effect appearing where there was none is the
    # dangerous direction, and the unchanged `host:C:deposit` token means this
    # diff is the only place it shows.
    before = _audit(_READ)
    after = _audit(_READ_TO_EMISSION)
    assert _extern(before, "deposit")["inverse"] == "unknown"
    assert _extern(after, "deposit")["inverse"] == "irreversible"
    result = evaluate(before, after)
    assert result["inverse_weakened"] == ["inverse:deposit"]
    assert result["inverse_strengthened"] == []
    assert result["widened"] is True
    assert "inverse:deposit" in result["unacknowledged"]
    assert "inverse provenance WEAKENED" in render(result, "before.json")


def test_a_read_that_becomes_a_witnessed_mutation_fails_the_gate():
    # Same shape, but the new class outranks `unknown` in the inverse
    # vocabulary (`declared`), so only the effect-class rule catches it.
    before = _audit(_READ)
    after = _audit(_READ_TO_WITNESSED)
    assert _extern(before, "deposit")["inverse"] == "unknown"
    assert _extern(after, "deposit")["inverse"] == "declared"
    result = evaluate(before, after)
    assert result["inverse_weakened"] == ["inverse:deposit"]
    assert result["inverse_strengthened"] == []
    assert result["widened"] is True


def test_an_acknowledged_read_to_emission_passes():
    result = evaluate(_audit(_READ), _audit(_READ_TO_EMISSION),
                      accepted={"inverse:deposit"})
    assert result["widened"] is False


def test_a_baseline_captured_before_the_field_is_not_comparable():
    # An audit captured before this slice carries no `inverse` on any extern.
    # That absence is NOT `unknown`; the two sides are not comparable, so the
    # diff must invent neither a weakening nor a strengthening.
    current = _audit(_DECLARED)
    legacy = {k: ([{kk: vv for kk, vv in ext.items() if kk != "inverse"}
                   for ext in v] if k == "externs" else v)
              for k, v in current.items()}
    assert all("inverse" not in ext for ext in legacy["externs"])
    result = evaluate(legacy, current)
    assert result["inverse_weakened"] == []
    assert result["inverse_strengthened"] == []
    assert result["widened"] is False
    assert "clean" in render(result, "legacy.json")


def test_the_mcp_widening_report_names_inverse_drift():
    # The MCP release report is a derived widening surface: every axis
    # `evaluate` gates must be named there, or a release could be blocked with
    # no bucket saying why.
    from revl.mcp.update_tools import _widening_from_audits  # noqa: PLC0415

    report = _widening_from_audits(_audit(_DECLARED), _audit(_COMPENSATION))
    assert report["widened"] is True
    assert report["inverseWeakened"] == ["inverse:deposit"]


def test_the_human_diff_names_the_weakened_inverse():
    result = evaluate(_audit(_DECLARED), _audit(_COMPENSATION))
    text = render(result, "previous.json")
    assert "inverse provenance WEAKENED" in text
    assert "inverse:deposit" in text


def test_the_cli_diff_fails_and_accepts_the_inverse_token(tmp_path, capsys):
    previous = tmp_path / "before.json"
    current = tmp_path / "after.rvl"
    previous.write_text(json.dumps(_audit(_DECLARED), indent=2))
    current.write_text(_COMPENSATION)

    assert main(["audit", str(current), "--diff", str(previous)]) == 1
    out = capsys.readouterr().out
    assert "inverse:deposit" in out

    assert main(["audit", str(current), "--diff", str(previous),
                 "--accept", "inverse:deposit"]) == 0
