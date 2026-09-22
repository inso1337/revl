"""The two namespaces in the L2 derived layer (`formal/.../CapCeilings.lean`).

`CapCeilings` proves the capability guarantees twice: once over `held`/`reach`
given as lists, and once over sets DERIVED from a component shape, which is
what makes the theorems statements about the program text rather than about an
oracle. That derived layer has the same two namespaces the harness has:

  * the DECLARED boundary — the token an `emission[...]` clause names, or, for
    a method that names none, the SERVICE it is declared on
    (`lower._cap_keyed` / `lower._undeclared_cap`). That is what an attenuation
    edge compares across a component boundary, because two components wire the
    same boundary under whatever local key each likes;
  * the WIRING KEY — the local `requires` spelling, which is what a statement
    actually contains and therefore what G6 confinement reads (`capKeys`).

Tokening the derived sets by the wiring key, which the layer did before, makes
`SpawnsAdmitted` admit an edge the reference refuses under G4: a parent and
child spelling one key over two different boundaries. That is the failure mode
a formal layer cannot report, because it does not fail loudly — it agrees with
the implementation, and agreement looks like success.

`make formal` proves the theorems, but it needs a Lean toolchain, so on a
machine without one nothing collected what the theorems are ABOUT: the
namespace could be moved back with no test failing. This module runs in the
plain `pytest tests/` job and pins the claim itself — which column each
`derived_*` theorem folds, that the reserved key namespace is the reference's
own, and that every theorem the axioms gate names still exists to be checked.
It reads the Lean source as text on purpose: it is the statement, not the
proof, that can drift silently here.

Its sibling `test_formal_attenuation_namespace.py` covers the same split one
layer down, in the differential harness.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LEAN = ROOT / "formal" / "RevL" / "Theorems" / "CapCeilings.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
REGISTRY = ROOT / "formal" / "scripts" / "nonvacuity.tsv"

#: The reference programs the derived layer's split witnesses track. Cited by
#: name inside the Lean file, so a rename there must not leave one dangling.
FIXTURE = "tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl"
UNDECLARED_FIXTURE = (
    "tests/formal_corpus/g4_spawn_widens_undeclared_emission_same_key.rvl")

#: The theorems whose conclusion is an attenuation fact. Every one of them must
#: read the declared-boundary column; a wiring key among them is the defect.
FOLD_THEOREMS = (
    "SpawnsAdmitted",
    "derived_lineage",
    "derived_attenuation_monotone",
    "derived_lineage_ceiling_le",
    "derived_budget_never_exceeds_root_ceiling",
    "derived_no_star_amplification",
)

_DECL = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?(?:def|theorem|abbrev|structure|inductive)"
    r"\s+([A-Za-z_][A-Za-z0-9_']*)",
    re.MULTILINE,
)


def _strip_comments(src: str) -> str:
    """Drop Lean block (`/- -/`, nesting) and line comments, keeping the
    newlines outside them so declarations stay at column 0."""
    out: list[str] = []
    i, depth = 0, 0
    while i < len(src):
        if src.startswith("/-", i):
            depth += 1
            i += 2
            continue
        if depth and src.startswith("-/", i):
            depth -= 1
            i += 2
            continue
        if not depth and src.startswith("--", i):
            nl = src.find("\n", i)
            i = len(src) if nl < 0 else nl
            continue
        if not depth:
            out.append(src[i])
        i += 1
    return "".join(out)


@pytest.fixture(scope="module")
def code() -> str:
    return _strip_comments(LEAN.read_text())


@pytest.fixture(scope="module")
def decls(code: str) -> dict[str, str]:
    """Declaration name -> its source, comments already gone."""
    found = list(_DECL.finditer(code))
    assert found, "no declarations parsed out of CapCeilings.lean"
    out: dict[str, str] = {}
    for n, m in enumerate(found):
        end = found[n + 1].start() if n + 1 < len(found) else len(code)
        out[m.group(1)] = code[m.start():end]
    return out


def _gated_names() -> list[str]:
    return re.findall(r"RevL\.CapCeilings\.([A-Za-z0-9_']+)", GATE.read_text())


# ------------------------------------------------- what an element is named by

def test_the_iface_carries_declared_capabilities(decls):
    """A service resolves to `(T, P)` pairs, not to bare valuations. Without
    the token there is nothing for the fold to compare but the key, so this is
    the shape the whole split rests on. `none` is the reference's
    "emission method with no capability list" arm, kept as its own entry so a
    service mixing capped and uncapped methods contributes both."""
    assert re.search(r"abbrev\s+Decl\s*:=\s*Option\s+Cap", decls["Decl"])
    assert re.search(r"abbrev\s+Iface\s*:=\s*String\s*→\s*List\s+Decl",
                     decls["Iface"])


def test_the_capability_column_names_what_it_reaches(decls):
    """`capsOfDecls` hands back the DECLARED capabilities unchanged. A
    `⟨k, ...⟩` in its non-empty branch would re-token them by the wiring key,
    which is exactly the laundering this layer had."""
    body = decls["capsOfDecls"]
    nonempty = body.split("| m :: ms =>", 1)
    assert len(nonempty) == 2, body
    assert nonempty[1].strip().startswith("(m :: ms).map (declCap sv)")
    assert "⟨k," not in nonempty[1]
    assert "[undeclCap sv]" in nonempty[0]
    # ...and `declCap` hands a declared capability back untouched.
    assert re.search(r"\|\s*some\s+d\s*=>\s*d\b", decls["declCap"])
    assert re.search(r"\|\s*none\s*=>\s*undeclCap\s+sv\b", decls["declCap"])


def test_the_undeclared_fallback_is_the_service_not_the_key(decls):
    """Item 561. A method declaring `emission` with no capability list names no
    token, and the element it falls back to must be the SERVICE it is declared
    on. The wiring key is the consumer's spelling: two different boundaries
    wired alike compare equal under it, which is the same laundering the
    declared column was moved off in issue 1142."""
    assert "undeclCap sv" in decls["declCap"]
    assert "wireCap" not in decls["declCap"]
    assert "undeclCap sv" in decls["capsOfDecls"]
    # the namer receives both names, so the two columns can read different ones
    assert re.search(r"abbrev\s+Namer\s*:=\s*String\s*→\s*String\s*→"
                     r"\s*List\s+Decl\s*→\s*List\s+Cap", decls["Namer"])
    assert re.search(r"def\s+capsOfDecls\s*\(_k\s*:\s*String\)\s*"
                     r"\(sv\s*:\s*String\)", decls["capsOfDecls"])
    assert re.search(r"def\s+boundsOfDecls\s*\(k\s*:\s*String\)\s*"
                     r"\(_sv\s*:\s*String\)", decls["boundsOfDecls"])


def test_a_service_with_nothing_declared_lands_in_the_reserved_namespace(decls):
    """The element for a boundary no declaration names is namespaced, so it
    cannot be read back as a declared token (`lower._undeclared_cap`)."""
    assert re.search(r"def\s+undeclCap\s*\(s\s*:\s*String\)\s*:\s*Cap\s*:="
                     r"\s*⟨undeclNS\s*\+\+\s*s,\s*\[\]⟩", decls["undeclCap"])


def test_the_reserved_namespace_is_the_reference_s(decls):
    """...and it is the SAME namespace the reference reserves. Two spellings
    would mean the model's disjointness claim is about a prefix nothing in
    `src/revl` avoids."""
    from revl import lower

    m = re.search(r'def\s+undeclNS\s*:\s*String\s*:=\s*"([^"]*)"',
                  decls["undeclNS"])
    assert m, decls["undeclNS"]
    assert m.group(1) == lower._UNDECLARED_NS


def test_the_bound_column_still_names_the_wiring_key(decls):
    """The key namespace is kept, not deleted: `capKeys` and G6 confinement
    need it, and no declared token appears in a statement."""
    assert "⟨k, []⟩" in decls["boundsOfDecls"]
    assert "⟨k, d.params⟩" in decls["declBound"]
    assert "⟨k, []⟩" in decls["declBound"]


def test_the_two_columns_run_one_traversal(decls):
    """Both columns are the same derivation under a different `Namer`, so they
    cannot silently disagree about WHICH crossings exist — only about how each
    one is spelled."""
    for base, namer in (("stmtCaps", "capsOfDecls"), ("stmtBounds", "boundsOfDecls"),
                        ("bodyReach", "capsOfDecls"), ("bodyBounds", "boundsOfDecls"),
                        ("heldCaps", "capsOfDecls"), ("heldBounds", "boundsOfDecls")):
        generic = {"stmtCaps": "stmtCapsN", "stmtBounds": "stmtCapsN",
                   "bodyReach": "bodyReachN", "bodyBounds": "bodyReachN",
                   "heldCaps": "heldCapsN", "heldBounds": "heldCapsN"}[base]
        assert re.search(rf"{generic}\s+{namer}\b", decls[base]), base


# ------------------------------------------------- which column each rule reads

@pytest.mark.parametrize("name", FOLD_THEOREMS)
def test_the_fold_reads_the_declared_boundary(decls, name):
    """The exit criterion of issue 1142. Every attenuation rule folds
    `heldCaps`/`reachIn` — the declared-boundary column. A `heldBounds` or
    `bodyBounds` here is a parent and child compared on a local spelling."""
    body = decls[name]
    assert "heldBounds" not in body, name
    assert "bodyBounds" not in body, name
    assert "heldCaps" in body or "reachIn" in body, name


def test_the_spawn_gate_is_the_one_the_reference_runs(decls):
    """`SpawnsAdmitted` is the derived form of `_check_spawn_attenuation`, so
    it is the definition the laundering edge is decided by."""
    body = decls["SpawnsAdmitted"]
    assert re.search(r"Attenuates\s*\(heldCaps\s+I\s+p\)\s*\(reachIn\s+I\s+P\s+f\s+ch\)",
                     body), body


def test_confinement_keeps_the_key_namespace(decls):
    """The half that must NOT move. A statement spells a wiring key, so G6's
    clause reads `heldBounds`; the attenuation clause in the same theorem reads
    `heldCaps`. Both columns, one theorem — which is why this was not a
    mechanical port of the harness fix."""
    body = decls["derived_confinement_within_ceiling"]
    assert "heldBounds I c" in body
    assert "heldCaps I p" in body
    assert "capKeys (heldBounds I c)" in body


def test_the_bridge_lemma_is_restated_for_both_columns(decls):
    """`derived_held_tokens_are_declared_keys` is the `capKeys` bridge. After
    the split it must say two different things: the bound column's tokens ARE
    the declared keys, and the capability column's are not keys at all."""
    body = decls["derived_held_tokens_are_declared_keys"]
    assert "capKeys (heldBounds I c)" in body
    assert "heldCaps I c" in body
    assert "undeclCap kv.2" in body
    assert "wireCap kv.1" not in body


# ------------------------------------------------------------ the split witness

def test_the_split_witness_exists_and_is_gated(decls):
    """A theorem nothing registers is a theorem the axioms gate never reads.
    The witness that separates the columns is named in the Lean file, in
    `CheckAxioms.lean`, in `run_gate.sh`'s argv and in the non-vacuity
    registry."""
    name = "same_key_different_boundary_refused"
    assert name in decls
    assert f"RevL.CapCeilings.{name}" in CHECK.read_text()
    assert f"RevL.CapCeilings.{name}" in GATE.read_text()
    rows = [r for r in REGISTRY.read_text().splitlines()
            if r.startswith(f"RevL.CapCeilings.{name}\t")]
    assert len(rows) == 1
    cited = [r for r in REGISTRY.read_text().splitlines()
             if f"RevL.CapCeilings.{name}" in r.split("\t")[2:3]
             or (len(r.split("\t")) > 2
                 and f"RevL.CapCeilings.{name}" in r.split("\t")[2].split(","))]
    assert cited, "the witness is registered but no theorem cites it"


def test_the_witness_spells_one_key_over_two_boundaries(decls):
    """Non-vacuity for the witness itself, read off the Lean text: parent and
    child must agree on the wiring key and disagree on the service, or the
    theorem is not about the shape a key-namespaced fold is blind to."""
    assert '("kv", "KvA")' in decls["wKvRouter"]
    assert '("kv", "KvB")' in decls["wKvLeak"]
    table = decls["witIface"]
    assert '"KvA" then [some ⟨"kv.read"' in table
    assert '"KvB" then [some ⟨"kv.write"' in table


def test_the_witness_states_both_verdicts(decls):
    """...and it states BOTH halves: the bound column derives one list for the
    two sides (so a fold over it attenuates), and the capability column
    refuses the edge."""
    body = decls["same_key_different_boundary_refused"]
    assert "bodyBounds witIface wKvLeak = heldBounds witIface wKvRouter" in body
    assert "Attenuates (heldBounds witIface wKvRouter)" in body
    assert "¬ SpawnsAdmitted witIface wProgLaunder 1" in body


@pytest.mark.parametrize("name", ["same_key_different_boundary_refused",
                                  "same_key_undeclared_boundary_refused"])
def test_the_undeclared_witness_is_registered_too(decls, name):
    """Item 561's witness rides the same registration as issue 1142's: named in
    the Lean file, in `CheckAxioms.lean`, in `run_gate.sh`'s argv and cited by
    a non-`concrete` row of the non-vacuity registry."""
    assert name in decls
    assert f"RevL.CapCeilings.{name}" in CHECK.read_text()
    assert f"RevL.CapCeilings.{name}" in GATE.read_text()
    rows = REGISTRY.read_text().splitlines()
    assert len([r for r in rows if r.startswith(f"RevL.CapCeilings.{name}\t")]) == 1
    cited = [r for r in rows if len(r.split("\t")) > 2
             and f"RevL.CapCeilings.{name}" in r.split("\t")[2].split(",")]
    assert cited, f"{name} is registered but no theorem cites it"


def test_the_undeclared_witness_spells_one_key_over_two_services(decls):
    """Non-vacuity for it, read off the Lean text: parent and child agree on
    the key, disagree on the service, and NEITHER service declares a token -
    or it is the declared witness again rather than item 561's."""
    assert '("net", "NetBare")' in decls["wBareRouter"]
    assert '("net", "KvBare")' in decls["wBareWork"]
    table = decls["witIface"]
    assert '"NetBare" then [none]' in table
    assert '"KvBare" then [none]' in table


def test_the_undeclared_witness_states_both_verdicts(decls):
    """...and both halves, the same way: the bound column derives one list for
    the two sides, and the capability column refuses the edge."""
    body = decls["same_key_undeclared_boundary_refused"]
    assert "bodyBounds witIface wBareWork = heldBounds witIface wBareRouter" in body
    assert "Attenuates (heldBounds witIface wBareRouter)" in body
    assert "¬ SpawnsAdmitted witIface wProgBare 1" in body
    assert 'undeclCap "NetBare"' in body and 'undeclCap "KvBare"' in body


def test_the_cited_reference_program_is_still_there(code):
    """The Lean file names the `.rvl` program its witness models. A citation
    that no longer resolves is a model claiming a correspondence it cannot
    have."""
    for fixture in (FIXTURE, UNDECLARED_FIXTURE):
        assert fixture in LEAN.read_text()
        assert (ROOT / fixture).is_file()


# --------------------------------------------------- the gate still names them

def test_every_gated_theorem_exists(decls):
    """`axioms_gate.py` fails on a theorem it cannot find, but only with a Lean
    toolchain. A rename that escapes the argv is otherwise invisible here."""
    missing = sorted({n for n in _gated_names() if n not in decls})
    assert missing == [], missing


def test_every_derived_theorem_is_gated(decls):
    """The other direction: a `derived_*` theorem nobody registered is proved
    by `lake build` and axiom-checked by nothing."""
    gated = set(_gated_names())
    ungated = sorted(n for n in decls
                     if n.startswith("derived_") and n not in gated)
    assert ungated == [], ungated
