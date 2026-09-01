"""Reach clause on EMISSION externs — roadmap item 373 (confinement on the surface).

R-3 (revl-harness) confined the coding-agent spawn (`extern emission fn
engine_run(...)`): the process runs in a canonicalized workspace, refused
inside a checkout, under a deny-all seatbelt. But `revl audit` printed the
identical `engine_run [emission] backends: py, ts` before and after — the review
surface could not see the one property a reviewer needs: what the crossing was
BOUNDED to. Item 252 got the crossing NAMED; item 373 gets its REACH named.

Surface: `extern emission(confined: <param>) fn engine_run(...)` declares that
the crossing is confined to the region carried by parameter <param>. The reach
is recorded onto the IR extern entry and `revl audit` PRINTS it
(`engine_run [emission] reach: confined(cwd) backends: py, ts`). `revl audit
--diff` flags a WEAKENING (confined -> unconfined, or a removed/changed reach)
the way it flags a new crossing today. A bare emission (no reach clause) parses,
lowers, and audits exactly as today — byte-identical IR.

Semantics (partially checker-verified): the confinement TARGET must name a
PARAMETER of the extern, not a literal the host body picks — so a host body that
ignores the parameter and confines to a baked-in fallback is a reviewable lie,
and a reach that names a non-parameter is rejected at compile time. The seatbelt
itself stays a trust-me claim (the checker cannot read the host body), exactly
like the classification.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.audit_diff import audit_report, diff_reach, evaluate, render


_CONFINED = (
    "extern emission(confined: cwd) fn engine_run("
    "argv_json: Str, cwd: Str, timeout_s: Int) -> Str = @py {\n"
    "    return \"\"\n"
    "} = @ts {\n"
    "    return \"\";\n"
    "}\n"
)

_BARE = (
    "extern emission fn engine_run("
    "argv_json: Str, cwd: Str, timeout_s: Int) -> Str = @py {\n"
    "    return \"\"\n"
    "} = @ts {\n"
    "    return \"\";\n"
    "}\n"
)


def test_confined_reach_parses_and_reaches_ir():
    ir = compile_source(_CONFINED, "reach.rvl")
    externs = {e["name"]: e for e in ir.get("externs") or []}
    assert externs["engine_run"]["reach"] == {"kind": "confined", "target": "cwd"}


def test_bare_emission_has_no_reach_key_byte_compat():
    ir = compile_source(_BARE, "reach.rvl")
    externs = {e["name"]: e for e in ir.get("externs") or []}
    # byte-identity: an emission with no reach clause carries NO reach key
    assert "reach" not in externs["engine_run"]


# ---------------------------------------------------------------------------
# the audit surface PRINTS the reach (and stays byte-compatible without one)
# ---------------------------------------------------------------------------

def _audit_text(source: str, tmp_path, capsys) -> str:
    from revl.__main__ import main
    src = tmp_path / "m.rvl"
    src.write_text(source, encoding="utf-8")
    rc = main(["audit", str(src)])
    assert rc == 0
    return capsys.readouterr().out


def test_audit_prints_the_reach(tmp_path, capsys):
    out = _audit_text(_CONFINED, tmp_path, capsys)
    # the reviewer can now SEE what the crossing is bounded to
    assert "engine_run  [emission]  reach: confined(cwd)  backends: py, ts" in out


def test_audit_line_byte_compat_without_reach(tmp_path, capsys):
    out = _audit_text(_BARE, tmp_path, capsys)
    # a bare emission prints exactly as before — no `reach:` segment
    assert "engine_run  [emission]  backends: py, ts" in out
    assert "reach:" not in out


# ---------------------------------------------------------------------------
# audit --diff: a WEAKENING fails the gate like a new crossing
# ---------------------------------------------------------------------------

def test_confined_to_unconfined_is_a_weakening(tmp_path, capsys):
    prev = audit_report(compile_source(_CONFINED, "reach.rvl"))
    new = audit_report(compile_source(_BARE, "reach.rvl"))
    result = evaluate(prev, new)
    assert result["reach_weakened"] == ["reach-weakened:engine_run"]
    assert result["widened"] is True
    assert "reach-weakened:engine_run" in result["unacknowledged"]
    # the human report names it
    assert "reach WEAKENED" in render(result, "prev.json")


def test_changed_confinement_target_is_a_weakening():
    # two params; the reach moves from one to the other — the bound moved, and
    # the checker cannot prove the new region contains the old one
    src_a = ("extern emission(confined: a) fn f(a: Str, b: Str) -> Str "
             "= @py {\n    return \"\"\n}\n")
    src_b = ("extern emission(confined: b) fn f(a: Str, b: Str) -> Str "
             "= @py {\n    return \"\"\n}\n")
    result = evaluate(audit_report(compile_source(src_a, "r.rvl")),
                      audit_report(compile_source(src_b, "r.rvl")))
    assert result["reach_weakened"] == ["reach-weakened:f"]
    assert result["widened"] is True


def test_unconfined_to_confined_is_a_safe_tightening():
    result = evaluate(audit_report(compile_source(_BARE, "reach.rvl")),
                      audit_report(compile_source(_CONFINED, "reach.rvl")))
    assert result["reach_tightened"] == ["reach-tightened:engine_run"]
    assert result["reach_weakened"] == []
    assert result["widened"] is False


def test_stable_confined_reach_diffs_clean():
    prev = audit_report(compile_source(_CONFINED, "reach.rvl"))
    new = audit_report(compile_source(_CONFINED, "reach.rvl"))
    result = evaluate(prev, new)
    assert result["reach_weakened"] == []
    assert result["reach_tightened"] == []
    assert result["widened"] is False


def test_a_weakening_is_acknowledgeable():
    prev = audit_report(compile_source(_CONFINED, "reach.rvl"))
    new = audit_report(compile_source(_BARE, "reach.rvl"))
    result = evaluate(prev, new, accepted={"reach-weakened:engine_run"})
    assert result["widened"] is False
    assert "reach-weakened:engine_run" in result["acknowledged"]


# ---------------------------------------------------------------------------
# semantics: the confinement TARGET is partially checked (rejection fixtures)
# ---------------------------------------------------------------------------

def test_reach_target_must_name_a_parameter():
    # `workspace` is not a parameter of engine_run — the confinement target has
    # to be caller-supplied data, not a literal the host body could swap in
    src = ("extern emission(confined: workspace) fn engine_run(cwd: Str) -> Str "
           "= @py {\n    return \"\"\n}\n")
    with pytest.raises(RevlError) as exc:
        compile_source(src, "reach.rvl")
    assert "is not one of its parameters" in str(exc.value)


def test_reach_clause_is_emission_only():
    src = ("extern witnessed(confined: p) fn mv(p: Str) -> Result[Str, Str] "
           "undo unmove(p) = @py {\n    return p\n}\n")
    with pytest.raises(RevlError) as exc:
        compile_source(src, "reach.rvl")
    assert "cannot declare a" in str(exc.value)


def test_diff_reach_is_pure_over_audit_dicts():
    # diff_reach reads only the flat externs list — a unit check independent of
    # the boundary table
    prev = audit_report(compile_source(_CONFINED, "reach.rvl"))
    new = audit_report(compile_source(_BARE, "reach.rvl"))
    assert diff_reach(prev, new)["reach_weakened"] == ["reach-weakened:engine_run"]
