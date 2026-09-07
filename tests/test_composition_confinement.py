"""Composition confinement: the per-root admission-profile split (roadmap item
426, slice S4).

The design is `docs/design/426-composition-layers.md`; S4 is §11 "the
confinement split" and its mechanism is §9.3 Part 2: `compile_files` grows a
per-root `profiles` map so a first-party root and a non-first-party (stack
layer) root are admitted in ONE call, each against its OWN profile, with the
`_link` phase running once and unprofiled over the merged program. Before S4
`compile_files` took exactly one `profile` for the whole merged program, which
§9.3's "new critical" showed could express neither decision 3 (one call for the
whole delta) nor decision 6 (mixed trust per row) without reintroducing the
intermediate-state nondeterminism the pure fold exists to kill.

Which of 426 §12's exit tests this file covers:

 13  CRITICAL A is closed         COMPILER HALF: a non-first-party root whose
                                  source declares/reaches a host body is refused
                                  by the untrusted-author profile while the
                                  first-party root's host body admits, in one
                                  call. The `--trust-host-code` shape change and
                                  the panel's `clean` forfeiture are S5 (the
                                  authority panel), not built here.
 15  the new critical is closed   COVERED: one first-party row declaring an
                                  extern and one non-first-party row declaring an
                                  extern are admitted in ONE `compile_files`
                                  call — the first admits, the second is refused
                                  naming the row, and the merged manifest is
                                  byte-identical to a first-party-only compile of
                                  the same rows. The `from`-path-inside-the-truc
                                  jail is S6 (distribution) and is noted there.

Exit test 14 (CRITICAL B, the config token) and 16 (the fail-closed panel) are
the authority panel, S5, and are BUILT below (they wait on this S4 split plus
the 428 F3 mandatory pin, both now landed). The S5 half of exit test 13 (the
`--trust-host-code` shape change and the panel's `clean` forfeiture) is built
below too. 17-18 are distribution, S6, in tests_truc_*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from revl import AdmissionProfile
from revl import authority_panel
from revl.composition import admit_composition, resolve, resolve_file, sole_composition
from revl.compiler import compile_files
from revl.errors import RevlError
from revl.parser import parse_file

# A first-party row: it legitimately declares and reaches its own host body.
# The base composition and the operator's site layer are first-party (§4.1), so
# a `pure`/host extern in first-party source is a normal, admitted declaration.
_FIRST_PARTY = """
service Fp { fn run() -> Str }
extern pure fn fp_host(t: Str) -> Str = @py { return t }
component FpComp provides fp: Fp {
  provide fp { fn run() = fp_host("ok") }
}
"""

# A non-first-party row (a stack layer, §4.1): whatever its `from` path names it
# is untrusted, so the untrusted-author profile forbids it declaring host code —
# 425 F1's declared-`pure` exfiltrating body has no reachable spelling here.
_NON_FIRST_PARTY = """
service Lay { fn run() -> Str }
extern pure fn lay_host(t: Str) -> Str
  = @py { import os; return os.environ.get("HOME", "") }
component LayComp provides lay: Lay {
  provide lay { fn run() = lay_host("x") }
}
"""

# A non-first-party row that declares NO host code and only composes: it is a
# clean untrusted-author turn and must admit even under the profile.
_NON_FIRST_PARTY_CLEAN = """
service Lay { fn run() -> Str }
component LayComp provides lay: Lay {
  provide lay { fn run() = "pure" }
}
"""

_FP = "/composition/base.rvl"
_LAYER = "/composition/layer.rvl"


def _compile(profiles, sources):
    return compile_files(list(sources), sources=sources, profiles=profiles)


# --------------------------------------------------------------------------- #
# Exit test 15 — the new critical: mixed trust in ONE call.
# --------------------------------------------------------------------------- #

def test_mixed_delta_first_party_extern_admits_layer_extern_refused():
    """One first-party row declaring an extern and one non-first-party row
    declaring an extern are admitted in ONE `compile_files` call: the first-party
    extern admits, the non-first-party one is refused, and the refusal names the
    row (its file)."""
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    with pytest.raises(RevlError) as exc:
        _compile(
            {_FP: None, _LAYER: prof},
            {_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY})
    msg = str(exc.value)
    # the non-first-party row is refused, naming its extern and the G8 boundary.
    assert "forbids new" in msg and "extern" in msg
    assert "lay_host" in msg
    assert getattr(exc.value, "code", None) == "G8"
    # and it is the LAYER row that is named, not the first-party base.
    assert "layer.rvl" in msg
    assert "base.rvl" not in msg


def test_mixed_delta_refusal_names_the_row_whichever_side_is_untrusted():
    """The refusal follows the profile, not the file order: making the OTHER root
    non-first-party refuses that one instead."""
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    with pytest.raises(RevlError) as exc:
        _compile(
            {_FP: prof, _LAYER: None},
            {_FP: _NON_FIRST_PARTY, _LAYER: _FIRST_PARTY})
    msg = str(exc.value)
    assert "lay_host" in msg          # the non-first-party body, now under _FP
    assert "base.rvl" in msg          # named at the root that carries the profile
    assert "layer.rvl" not in msg


def test_both_first_party_admits_every_extern():
    """With both roots first-party (profile None), the split is inert: the base's
    host body admits and the second clean row admits too, exactly as an
    unprofiled compile."""
    doc = _compile(
        {_FP: None, _LAYER: None},
        {_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY_CLEAN})
    names = {c["name"] for c in doc["components"]}
    assert names == {"FpComp", "LayComp"}
    externs = {e["name"] for e in doc.get("externs") or []}
    assert "fp_host" in externs


def test_clean_non_first_party_row_admits_alongside_first_party():
    """A non-first-party row that only composes (declares no host code) admits
    under its profile, in the same call as a first-party host-body row."""
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    doc = _compile(
        {_FP: None, _LAYER: prof},
        {_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY_CLEAN})
    names = {c["name"] for c in doc["components"]}
    assert names == {"FpComp", "LayComp"}


def test_manifest_byte_identical_to_first_party_only_compile():
    """The manifest a mixed admit produces for the admitted rows is byte-identical
    to compiling the SAME rows first-party — the per-root split changes which
    rows are refused, never the shape of the ones that admit (exit test 15's
    byte-identity clause; the `_link` runs once, unprofiled, over the merged
    program either way)."""
    both_clean = {_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY_CLEAN}
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    split = _compile({_FP: None, _LAYER: prof}, both_clean)
    first_party = _compile({_FP: None, _LAYER: None}, both_clean)
    assert split["manifest"] == first_party["manifest"]


def test_single_profile_path_unchanged_by_the_split():
    """Passing a bare `profile=` (no `profiles` map) is byte-identical to the
    pre-split compiler: every root shares the one profile, so a first-party host
    body is refused right alongside the layer's."""
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    with pytest.raises(RevlError) as exc:
        compile_files(
            [_FP, _LAYER],
            sources={_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY},
            profile=prof)
    # under ONE profile the FIRST root's extern is what trips first.
    assert "forbids new" in str(exc.value)


# --------------------------------------------------------------------------- #
# Exit test 13 (compiler half) — CRITICAL A: a layer host body is refused.
# --------------------------------------------------------------------------- #

def test_layer_host_body_refused_by_default_first_party_body_admits():
    """CRITICAL A, the half S4 owns: a layer shipping a host body is refused by
    the untrusted-author profile (425 F1's declared-`pure` exfiltrating body has
    no reachable spelling on the layer path), while the first-party base's host
    body admits — both decided in one call, each against its own profile. The
    `--trust-host-code` override and the panel's `clean` forfeiture are S5."""
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    with pytest.raises(RevlError) as exc:
        _compile(
            {_FP: None, _LAYER: prof},
            {_FP: _FIRST_PARTY, _LAYER: _NON_FIRST_PARTY})
    assert getattr(exc.value, "code", None) == "G8"
    assert "layer.rvl" in str(exc.value)


def test_layer_reaching_a_first_party_host_extern_is_refused():
    """The import/reach bypass, per root: a non-first-party layer that reaches a
    host-body extern declared in a co-compiled first-party module is refused by
    that layer's profile, even though the layer declares no extern of its own —
    the reach sweep starts from the layer's bodies under the layer's profile."""
    base = """
service Fp { fn run() -> Str }
pub extern pure fn shared_host(t: Str) -> Str = @py { return t }
component FpComp provides fp: Fp {
  provide fp { fn run() = shared_host("ok") }
}
"""
    layer = """
use "base.rvl" { shared_host }
service Lay { fn run() -> Str }
component LayComp provides lay: Lay {
  provide lay { fn run() = shared_host("reach") }
}
"""
    base_path = "/composition/base.rvl"
    layer_path = "/composition/layer.rvl"
    prof = AdmissionProfile.untrusted_author({"Fp", "Lay"})
    with pytest.raises(RevlError) as exc:
        compile_files(
            [base_path, layer_path],
            sources={base_path: base, layer_path: layer},
            profiles={base_path: None, layer_path: prof})
    # the reach sweep refuses the host-body reach from the untrusted layer.
    assert "shared_host" in str(exc.value)


# --------------------------------------------------------------------------- #
# The granted allowlist, per root — a non-first-party row's reach is bounded by
# ITS OWN granted set (§9.3 Part 2: `granted` is a property of the row).
# --------------------------------------------------------------------------- #

_BASE_TWO_SERVICES = """
service Kv { fn get(k: Str) -> Str }
service FsSvc { fn read(p: Str) -> Str }
component KvProvider provides kv: Kv { provide kv { fn get(k) = "v" } }
component FsProvider provides fs: FsSvc { provide fs { fn read(p) = "d" } }
"""

# a layer that reaches the ambient `fs` — a service the base provides but that
# the layer's own granted set does not name.
_LAYER_REACHES_FS = """
service Lay { fn run() -> Str }
component LayComp requires fs: FsSvc provides lay: Lay {
  provide lay { fn run() = fs.read("x") }
}
"""


def test_non_first_party_reach_bounded_by_its_own_granted_set():
    """A non-first-party layer reaching an ambient service outside its granted
    set is refused (R2), naming the row; the first-party base is unaffected."""
    b = "/composition/base.rvl"
    lay = "/composition/layer.rvl"
    prof = AdmissionProfile.untrusted_author({"Kv"})   # grants Kv, NOT FsSvc
    with pytest.raises(RevlError) as exc:
        compile_files(
            [b, lay],
            sources={b: _BASE_TWO_SERVICES, lay: _LAYER_REACHES_FS},
            profiles={b: None, lay: prof})
    msg = str(exc.value)
    assert "not in the granted set" in msg and "FsSvc" in msg
    assert "LayComp" in msg
    assert getattr(exc.value, "code", None) == "R2"


def test_first_party_root_reach_is_not_allowlisted():
    """The same reach from a first-party root (profile None) admits: the
    allowlist is a property of the row's own profile, so a first-party row is
    unrestricted even in a composition that also carries a bounded layer."""
    b = "/composition/base.rvl"
    lay = "/composition/layer.rvl"
    doc = compile_files(
        [b, lay],
        sources={b: _BASE_TWO_SERVICES, lay: _LAYER_REACHES_FS},
        profiles={b: None, lay: None})
    assert "LayComp" in {c["name"] for c in doc["components"]}


# =========================================================================== #
# S5 — the authority panel (§8). A composition on disk, folded with layers,
# read through the panel API. `_panel_project` writes the four kinds of source
# an S5 test needs: services, a first-party base-row component, an optional
# host-body layer component, and the base composition + named layers.
# =========================================================================== #

_SERVICES = """
service Db      { fn query(q: Str) -> Str }
service Metrics { fn tick() -> Int }
"""

_PGDB = """
use "services.rvl" { }
component PgDb provides db: Db {
  config { url: Str, pool: Int = 8 }
  provide db { fn query(q) = q }
}
"""

# A non-first-party layer component that DECLARES a host body: 425 F1's shape.
_HOST_LOGGER = """
use "services.rvl" { }
extern pure fn log_host(t: Str) -> Str = @py { import os; return os.environ.get("HOME", "") }
component Logger provides metrics: Metrics {
  provide metrics { fn tick() = 1 }
}
"""

# A clean non-first-party layer component: no host body, only composition.
_CLEAN_LOGGER = """
use "services.rvl" { }
component Logger provides metrics: Metrics {
  provide metrics { fn tick() = 1 }
}
"""

_BASE_TMPL = """
composition Demo {
  use "services.rvl"
  row @db from "pgdb.rvl" provides db
    config { url: "postgres://primary:5432/app", pool: 8 }
%(clauses)s%(stack)s%(site)s}
"""


def _panel_project(tmp_path: Path, *, db_clauses="    open { url, pool }\n",
                   stack=(), site=None, components=None, **layers):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services.rvl").write_text(_SERVICES)
    (tmp_path / "pgdb.rvl").write_text(_PGDB)
    for name, text in (components or {}).items():
        (tmp_path / f"{name}.rvl").write_text(text)
    (tmp_path / "layers").mkdir(exist_ok=True)
    for name, text in layers.items():
        (tmp_path / "layers" / f"{name}.rvl").write_text(text)
    clauses = "".join(f'  stack "layers/{n}.rvl"\n' for n in stack)
    site_clause = f'  site "layers/{site}.rvl"\n' if site else ""
    doc = tmp_path / "base.rvl"
    doc.write_text(_BASE_TMPL % {"clauses": db_clauses, "stack": clauses,
                                 "site": site_clause})
    return doc


def _tables(doc: Path, tmp_path: Path):
    """(base, candidate) resolved row tables — base with no layers, candidate
    folded."""
    decl = sole_composition(parse_file(str(doc)), str(doc))
    base = resolve(decl, str(doc), str(tmp_path))
    candidate = resolve_file(str(doc), str(tmp_path))
    return base, candidate


# --------------------------------------------------------------------------- #
# Exit test 14 — CRITICAL B: the config token, its digest, and the bounded
# refusal.
# --------------------------------------------------------------------------- #

def test_config_only_layer_is_not_invisible_and_the_token_carries_a_digest(tmp_path):
    """426 exit test 14. A layer whose only operation is `configure @db { url }`
    on an UNBOUNDED field produces a `config:@db:url:<digest>` widening token and
    the panel does not print `clean`. Changing the value again produces a
    DIFFERENT token, so a prior `--accept` does not cover it."""
    doc = _panel_project(
        tmp_path, site="tune", tune="""
layer Tune for Demo {
  configure @db with { url: "postgres://replica:5432/app" }
}
""")
    base, cand = _tables(doc, tmp_path)
    res = authority_panel.panel(base, cand)
    toks = [t["token"] for t in res["tokens"]]
    assert any(t.startswith("config:@db:url:") for t in toks), toks
    assert not res["clean"]
    digest_one = [t for t in toks if t.startswith("config:@db:url:")][0]

    # a DIFFERENT value -> a DIFFERENT token (the digest pins the ack, §8.4).
    doc2 = _panel_project(
        tmp_path / "two", site="tune", tune="""
layer Tune for Demo {
  configure @db with { url: "postgres://elsewhere:5432/app" }
}
""")
    _b2, c2 = _tables(doc2, tmp_path / "two")
    digest_two = [t["token"] for t in authority_panel.panel(_b2, c2)["tokens"]
                  if t["token"].startswith("config:@db:url:")][0]
    assert digest_one != digest_two


def test_a_pure_numeric_config_change_gets_no_token(tmp_path):
    """426 §8.7: `pool: 8 -> 16` feeds pure computation, so it is NOT
    authority-bearing and produces no `config:` token — the panel stays clean on
    a numeric-only change."""
    doc = _panel_project(
        tmp_path, site="tune", tune="""
layer Tune for Demo {
  configure @db with { pool: 16 }
}
""")
    base, cand = _tables(doc, tmp_path)
    res = authority_panel.panel(base, cand)
    assert res["tokens"] == []
    assert res["clean"]


def test_a_bounded_field_configured_outside_its_reach_is_refused(tmp_path):
    """426 exit test 14, first half. A `configure` moving a field with a
    declared composition `reach` bound to a value whose host is outside the
    bound is a REFUSAL at resolution (§8.3), naming the field — never a silent
    redirect, and it never reaches the panel."""
    doc = _panel_project(
        tmp_path,
        db_clauses='    open { url }\n    reach { url: host("primary:5432") }\n',
        site="evil", evil="""
layer Evil for Demo {
  configure @db with { url: "postgres://attacker.example:5432/app" }
}
""")
    with pytest.raises(RevlError) as exc:
        resolve_file(str(doc), str(tmp_path))
    msg = str(exc.value)
    assert "outside the reach" in msg and "attacker.example" in msg


def test_a_bounded_field_configured_in_bound_admits_and_still_tokens(tmp_path):
    """426 §8.4: a bounded field changed but kept IN bound admits, and still
    renders as a `config:` widening in the panel (a bounded change is a change),
    so the panel does not silently green a redirect that merely stayed legal."""
    doc = _panel_project(
        tmp_path,
        db_clauses='    open { url }\n    reach { url: host("primary:5432") }\n',
        site="ok", ok="""
layer Ok for Demo {
  configure @db with { url: "postgres://primary:5432/other" }
}
""")
    base, cand = _tables(doc, tmp_path)
    res = authority_panel.panel(base, cand)
    toks = [t for t in res["tokens"] if t["field"] == "url"]
    assert toks and toks[0]["bounded"] is True
    assert not res["clean"]


# --------------------------------------------------------------------------- #
# Exit test 16 — the panel is fail-closed.
# --------------------------------------------------------------------------- #

def test_an_unclassifiable_config_field_prevents_clean(tmp_path):
    """426 exit test 16, first half. An unclassifiable (string, no declared
    reach) config field is treated as AUTHORITY-BEARING: it produces a `config:`
    token, is listed under BLIND SPOTS, and prevents `clean` (§8.5 conjunct 5,
    incompleteness costs noise not silence)."""
    doc = _panel_project(
        tmp_path, site="tune", tune="""
layer Tune for Demo {
  configure @db with { url: "postgres://replica:5432/app" }
}
""")
    base, cand = _tables(doc, tmp_path)
    res = authority_panel.panel(base, cand)
    assert "@db.url" in res["unclassifiable"]
    assert not res["clean"]
    assert "BLIND SPOTS" in authority_panel.render(res)


def test_a_trust_host_code_row_prevents_clean_with_an_unchanged_token_set(tmp_path):
    """426 exit test 16, second half. A row admitted under `--trust-host-code`
    forfeits `clean` however quiet the tokens are (§8.5 conjunct 3): trust basis
    is CLAIMED and the token set is UNCHANGED (the layer adds a clean row and
    changes no config), yet the panel is not clean."""
    doc = _panel_project(
        tmp_path, stack=("obs",), components={},
        obs="""
layer Obs for Demo {
  add row @metrics from "../metrics.rvl" provides metrics
}
""")
    (tmp_path / "metrics.rvl").write_text(_CLEAN_LOGGER)
    base, cand = _tables(doc, tmp_path)
    nfp = [r.qualified for r in cand.rows
           if authority_panel._trust_of(cand)[r.label] == "non-first-party"]
    assert nfp, "the added row is non-first-party"

    # with no trust flag the added clean row leaves the panel clean (no tokens).
    plain = authority_panel.panel(base, cand)
    assert plain["tokens"] == [] and plain["trust_basis"].startswith("MEASURED")
    assert plain["clean"]

    # trusting it flips the basis to CLAIMED and forfeits clean, tokens unchanged.
    trusted = authority_panel.panel(base, cand, trust_host_code=set(nfp))
    assert trusted["trust_basis"] == "CLAIMED"
    assert trusted["tokens"] == []
    assert not trusted["clean"]
    assert nfp[0] in trusted["claimed"]


# --------------------------------------------------------------------------- #
# Exit test 13 (S5 half) — CRITICAL A: --trust-host-code shape + clean forfeit.
# --------------------------------------------------------------------------- #

def test_layer_host_body_refused_by_default_through_admit_composition(tmp_path):
    """426 exit test 13, the S5 half. A stack layer shipping a host body is
    refused BY DEFAULT when the composition is admitted with confinement on —
    the untrusted-author profile the row's trust class selects refuses the
    declared extern."""
    doc = _panel_project(
        tmp_path, stack=("otel",), otel="""
layer Otel for Demo {
  add row @logger from "../logger.rvl" provides metrics
}
""")
    (tmp_path / "logger.rvl").write_text(_HOST_LOGGER)
    with pytest.raises(RevlError) as exc:
        admit_composition(str(doc), str(tmp_path), confine=True)
    assert getattr(exc.value, "code", None) == "G8"


def test_trust_host_code_admits_the_body_and_the_panel_forfeits_clean(tmp_path):
    """426 exit test 13, the S5 half. `--trust-host-code` admits the same layer
    as reviewed first-party code, and the panel changes SHAPE: trust basis
    CLAIMED, the row named under UNCHECKED HOST CODE, and `clean` forfeited."""
    doc = _panel_project(
        tmp_path, stack=("otel",), otel="""
layer Otel for Demo {
  add row @logger from "../logger.rvl" provides metrics
}
""")
    (tmp_path / "logger.rvl").write_text(_HOST_LOGGER)
    base, cand = _tables(doc, tmp_path)
    nfp = [r.qualified for r in cand.rows
           if authority_panel._trust_of(cand)[r.label] == "non-first-party"]

    # admitting with the row trusted no longer refuses.
    admit_composition(str(doc), str(tmp_path), confine=True,
                      trust_host_code=set(nfp))

    res = authority_panel.panel(base, cand, trust_host_code=set(nfp))
    assert res["trust_basis"] == "CLAIMED"
    assert nfp[0] in res["claimed"]
    assert not res["clean"]
    text = authority_panel.render(res)
    assert "UNCHECKED HOST CODE" in text and nfp[0] in text
