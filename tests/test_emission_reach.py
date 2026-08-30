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

from revl.compiler import compile_source


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
