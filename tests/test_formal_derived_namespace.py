"""The two namespaces in the L2 derived layer (`formal/.../CapCeilings.lean`).

`CapCeilings` proves the capability guarantees twice: once over `held`/`reach`
given as lists, and once over sets DERIVED from a component shape, which is
what makes the theorems statements about the program text rather than about an
oracle. That derived layer has the same two namespaces the harness has:

  * the DECLARED boundary — the token an `emission[...]` clause names, which is
    what an attenuation edge compares across a component boundary, because two
    components wire the same boundary under whatever local key each likes
    (`lower._cap_keyed`);
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

#: The reference program the derived layer's split witness tracks. Cited by
#: name inside the Lean file, so a rename there must not leave it dangling.
FIXTURE = "tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl"

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
    r"^(?:def|theorem|abbrev|structure|inductive)\s+([A-Za-z_][A-Za-z0-9_']*)",
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
    the shape the whole split rests on."""
    assert re.search(r"abbrev\s+Iface\s*:=\s*String\s*→\s*List\s+Cap",
                     decls["Iface"])


def test_the_capability_column_names_what_it_reaches(decls):
    """`capsOfDecls` hands back the DECLARED capabilities unchanged. A
    `⟨k, ...⟩` in its non-empty branch would re-token them by the wiring key,
    which is exactly the laundering this layer had."""
    body = decls["capsOfDecls"]
    nonempty = body.split("| d :: ds =>", 1)
    assert len(nonempty) == 2, body
    assert nonempty[1].strip().startswith("d :: ds")
    assert "⟨k," not in nonempty[1]
    assert "[wireCap k]" in nonempty[0]


def test_a_key_with_nothing_declared_lands_in_the_reserved_namespace(decls):
    """The one case where a key still names the boundary is namespaced, so it
    cannot be read back as a declared token (`lower._wire_cap`)."""
    assert re.search(r"def\s+wireCap\s*\(k\s*:\s*String\)\s*:\s*Cap\s*:="
                     r"\s*⟨wireNS\s*\+\+\s*k,\s*\[\]⟩", decls["wireCap"])


def test_the_reserved_namespace_is_the_reference_s(decls):
    """...and it is the SAME namespace the reference reserves. Two spellings
    would mean the model's disjointness claim is about a prefix nothing in
    `src/revl` avoids."""
    from revl import lower

    m = re.search(r'def\s+wireNS\s*:\s*String\s*:=\s*"([^"]*)"', decls["wireNS"])
    assert m, decls["wireNS"]
    assert m.group(1) == lower._WIRE_NS


def test_the_bound_column_still_names_the_wiring_key(decls):
    """The key namespace is kept, not deleted: `capKeys` and G6 confinement
    need it, and no declared token appears in a statement."""
    body = decls["boundsOfDecls"]
    assert "⟨k, []⟩" in body
    assert "⟨k, q.params⟩" in body


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
    assert "wireCap kv.1" in body


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
    assert '"KvA" then [⟨"kv.read"' in table
    assert '"KvB" then [⟨"kv.write"' in table


def test_the_witness_states_both_verdicts(decls):
    """...and it states BOTH halves: the bound column derives one list for the
    two sides (so a fold over it attenuates), and the capability column
    refuses the edge."""
    body = decls["same_key_different_boundary_refused"]
    assert "bodyBounds witIface wKvLeak = heldBounds witIface wKvRouter" in body
    assert "Attenuates (heldBounds witIface wKvRouter)" in body
    assert "¬ SpawnsAdmitted witIface wProgLaunder 1" in body


def test_the_cited_reference_program_is_still_there(code):
    """The Lean file names the `.rvl` program its witness models. A citation
    that no longer resolves is a model claiming a correspondence it cannot
    have."""
    assert FIXTURE in code.replace("\n", " ") or FIXTURE in LEAN.read_text()
    assert (ROOT / FIXTURE).is_file()


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
