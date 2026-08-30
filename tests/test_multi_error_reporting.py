"""Roadmap 386, Stage 1: report ALL refusals per compile, not just the first.

The frontend used to abort on the FIRST `RevlError` raised anywhere in the
pipeline, so an author fixing N independent refusals paid N full recompile
round-trips. Stage 1 collects every recoverable refusal (per-component G/A
refusals and the whole-composition post-passes) and reports them together,
while keeping `diagnostics[0]` byte-identical to what today's single-error
compile reports for the same input.

These tests pin the Stage-1 contract AND the five Fable-review corrections
(header-stub topology completeness, partial-component robustness, plan()
multi-diagnostic, diagnostics[0] stability, and post-pass crash guarding).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.diagnostics import report  # noqa: E402
from revl.errors import RevlError  # noqa: E402


_LEDGER = """\
service Ledger {
  fn record(k: Str, v: Str)
}
"""

# Three components, each with ONE distinct undeclared-requirement refusal
# (G1). Today only the first would be reported; Stage 1 reports all three.
_THREE_REFUSALS = _LEDGER + """\
component A requires ledger: Ledger {
  effect foo.record("a", "1") undo foo.record("a", "")
}
component B requires ledger: Ledger {
  effect bar.record("b", "1") undo bar.record("b", "")
}
component C requires ledger: Ledger {
  effect baz.record("c", "1") undo baz.record("c", "")
}
"""


def _compile_expecting_refusal(tmp_path, monkeypatch, text, name="prog.rvl"):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(path)])
    return excinfo.value


def test_three_components_three_distinct_refusals_one_pass(tmp_path, monkeypatch):
    """The headline H38-in-miniature regression: three components, three
    distinct refusals, compiled once, must yield three diagnostics with three
    distinct locations."""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, _THREE_REFUSALS)

    diags = report(error)["diagnostics"]
    messages = sorted(d["message"] for d in diags)
    assert len(diags) == 3, messages
    assert any("foo" in m for m in messages), messages
    assert any("bar" in m for m in messages), messages
    assert any("baz" in m for m in messages), messages
    # three distinct locations (distinct lines in one file)
    assert len({d["line"] for d in diags}) == 3, diags


def test_exit_code_is_one_for_one_and_for_many(tmp_path, monkeypatch):
    """The exit-code contract is unchanged: 1 on ANY refusal, whether one or
    many. The count lives in the payload, not the exit code."""
    from revl.__main__ import main

    monkeypatch.chdir(tmp_path)
    many = tmp_path / "many.rvl"
    many.write_text(_THREE_REFUSALS)
    one = tmp_path / "one.rvl"
    one.write_text(_LEDGER + """\
component A requires ledger: Ledger {
  effect foo.record("a", "1") undo foo.record("a", "")
}
""")
    assert main(["compile", str(many)]) == 1
    assert main(["compile", str(one)]) == 1


def test_json_mode_emits_all_diagnostics_as_well_formed_records(tmp_path, monkeypatch, capsys):
    """`--json` already calls `report(...)`; it now receives the full list for
    free. Every entry is a well-formed `classify` record."""
    import json

    from revl.__main__ import main

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "prog.rvl"
    path.write_text(_THREE_REFUSALS)

    assert main(["compile", "--json", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    diags = payload["diagnostics"]
    assert len(diags) == 3, diags
    for d in diags:
        assert d["severity"] == "error"
        assert d["code"] and d["file"] and d["line"] and d["message"]


def test_clean_compile_is_unchanged(tmp_path, monkeypatch):
    """Byte-identity floor: a program with no refusal compiles to a normal IR,
    with no `errors`/`poisoned` residue leaking into the output."""
    clean = _LEDGER + """\
component Keeper provides ledger: Ledger {
  provide ledger { fn record(k, v) { } }
}
"""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "clean.rvl"
    path.write_text(clean)
    ir = compile_files([str(path)])
    assert "components" in ir
    names = [c["name"] for c in ir["components"]]
    assert names == ["Keeper"]
    assert not any(c.get("poisoned") for c in ir["components"])


def test_multifile_both_refusals_attributed_to_own_source(tmp_path, monkeypatch):
    """Two files, each with a refusing component: BOTH are reported, each
    attributed to its own `comp.source` filename (roadmap 312 + item 386)."""
    monkeypatch.chdir(tmp_path)
    svc = tmp_path / "svc.rvl"
    a = tmp_path / "a.rvl"
    b = tmp_path / "b.rvl"
    svc.write_text(_LEDGER)
    a.write_text("""\
component A requires ledger: Ledger {
  effect aa.record("a", "1") undo aa.record("a", "")
}
""")
    b.write_text("""\
component B requires ledger: Ledger {
  effect bb.record("b", "1") undo bb.record("b", "")
}
""")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(svc), str(a), str(b)])
    diags = report(excinfo.value)["diagnostics"]
    assert len(diags) == 2, diags
    files = {Path(d["file"]).name for d in diags}
    assert files == {"a.rvl", "b.rvl"}, files


def test_multifile_first_diagnostic_equals_todays_single_error(tmp_path, monkeypatch):
    """Fable exit test 4: the FIRST diagnostic of a multi-file compile is the
    refusal today's single-error compile would have reported for the same
    input — the first file's component, by compile-order sort. This is what
    keeps `diagnostics[0]` stable for every legacy single-error consumer."""
    monkeypatch.chdir(tmp_path)
    svc = tmp_path / "svc.rvl"
    a = tmp_path / "a.rvl"
    b = tmp_path / "b.rvl"
    svc.write_text(_LEDGER)
    a.write_text("""\
component A requires ledger: Ledger {
  effect aa.record("a", "1") undo aa.record("a", "")
}
""")
    b.write_text("""\
component B requires ledger: Ledger {
  effect bb.record("b", "1") undo bb.record("b", "")
}
""")
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(svc), str(a), str(b)])
    first = report(excinfo.value)["diagnostics"][0]
    # `svc.rvl` (paths[0]) holds no component, so the first *refusal* in
    # compile order is A in a.rvl — exactly what an abort-on-first compile
    # would have surfaced.
    assert Path(first["file"]).name == "a.rvl", first
    assert "aa" in first["message"], first


def test_type_table_error_truncates_without_cascade(tmp_path, monkeypatch):
    """Design exit test 3: a phase-2 table failure (an unknown type in a
    signature) still ABORTS — the table it poisons feeds everything downstream,
    so continuing would fabricate a cascade. The report carries that one error
    and NONE of the downstream component refusals it truncates."""
    truncating = """\
fn broken() -> Nope { return 1 }

service Ledger { fn record(k: Str, v: Str) }
component A requires ledger: Ledger {
  effect ghost.record("a", "1") undo ghost.record("a", "")
}
"""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, truncating)
    diags = report(error)["diagnostics"]
    assert len(diags) == 1, diags
    # the downstream component refusal is NOT fabricated past the fatal table
    # error (truncation, not cascade)
    assert not any("ghost" in d["message"] for d in diags), diags


def test_header_stub_no_fabrication_and_real_g2_still_reported(tmp_path, monkeypatch):
    """Fable exit test 1 (the soundness fix): a component whose BODY refuses
    still contributes its HEADER provisions to the link topology. So a real G2
    provision conflict on a key it declares is STILL reported, and NO fabricated
    "no provider" error appears for a consumer of that key."""
    src = """\
service Ledger { fn record(k: Str, v: Str) }

component P provides led: Ledger {
  effect ghost.record("a", "1") undo ghost.record("a", "")
  provide led { fn record(k, v) { } }
}
component Q provides led: Ledger {
  provide led { fn record(k, v) { } }
}
"""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    messages = [d["message"] for d in diags]
    # P's body refusal is reported...
    assert any("ghost" in m for m in messages), messages
    # ...and the real G2 conflict on `led` (P's HEADER still provides it) is
    # STILL reported despite P's body having aborted.
    assert any("provision conflict" in m and "G2" in m for m in messages), messages
    # no fabricated "no provider"/route error for the shared key
    assert not any("no component provides" in m for m in messages), messages


def test_partial_spawner_leaves_spawn_reg_but_no_crash_or_fabrication(tmp_path, monkeypatch):
    """Fable exit test 2: a component that registers a spawn site then refuses
    leaves entries in the shared `spawn_reg`. The spawn post-passes must not
    crash on that partial state, nor fabricate a spawn/attenuation diagnostic
    for the poisoned spawner (it is excluded as `poisoned`)."""
    src = """\
service Ping { fn go() -> Int }

component Worker provides ping: Ping {
  provide ping { fn go() = 1 }
}
component Boss {
  let w = effect spawn Worker with { } undo w.dispose()
  effect ghost.go() undo ghost.go()
}
"""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, src)
    diags = report(error)["diagnostics"]
    messages = [d["message"] for d in diags]
    # Boss's real body refusal is reported
    assert any("ghost" in m for m in messages), messages
    # no fabricated spawn/attenuation diagnostic from the poisoned spawner
    assert not any("spawn" in m and "capabilit" in m for m in messages), messages
    assert not any("attenuat" in m.lower() for m in messages), messages


def test_plan_on_multi_refusal_candidate_returns_all_diagnostics(tmp_path, monkeypatch):
    """Fable exit test 3: `plan()` on a candidate with several independent
    refusals surfaces ALL of them (it iterates the carrier's `.errors`), not
    just the first."""
    from revl.plan import plan

    result = plan(source=_THREE_REFUSALS)
    assert result["admissible"] is False
    codes_msgs = [d["message"] for d in result["diagnostics"]]
    assert len(result["diagnostics"]) == 3, codes_msgs
    assert any("foo" in m for m in codes_msgs), codes_msgs
    assert any("bar" in m for m in codes_msgs), codes_msgs
    assert any("baz" in m for m in codes_msgs), codes_msgs


def test_post_pass_typeerror_on_poisoned_state_does_not_mask_diagnostics(tmp_path, monkeypatch):
    """Fable exit test 5: a post-pass tripping over poisoned/partial state with
    an UNEXPECTED (non-RevlError) exception must not replace N good diagnostics
    with a traceback — while the compile is failing it is dropped; on a CLEAN
    compile the same crash propagates as the bug it is (Change 4)."""
    import revl.lower as lower_mod

    def _boom(*args, **kwargs):
        raise TypeError("simulated crash on poisoned state")

    # failing compile: the collected refusal survives the post-pass crash
    monkeypatch.setattr(lower_mod, "check_taint", _boom)
    one_refusal = _LEDGER + """\
component A requires ledger: Ledger {
  effect foo.record("a", "1") undo foo.record("a", "")
}
"""
    error = _compile_expecting_refusal(tmp_path, monkeypatch, one_refusal, name="fail.rvl")
    diags = report(error)["diagnostics"]
    assert len(diags) == 1, diags
    assert "foo" in diags[0]["message"], diags

    # clean compile: the crash is a real bug, so it propagates (not swallowed)
    clean = _LEDGER + """\
component Keeper provides ledger: Ledger {
  provide ledger { fn record(k, v) { } }
}
"""
    clean_path = tmp_path / "clean.rvl"
    clean_path.write_text(clean)
    with pytest.raises(TypeError):
        compile_files([str(clean_path)])
