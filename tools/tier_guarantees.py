"""The guarantee x tier support matrix, derived rather than authored.

    python3 tools/tier_guarantees.py [--json] [--show] [--check]

`tools/conformance.py` answers "does every CONSTRUCT survive every tier?".
This answers the question an adopter actually asks before shipping: **what is
guaranteed on the tier I am going to ship?** revl states G1..G9 and the A and
T rules for the language, not for one emitter, and until this block existed
the per-tier answer could only be reconstructed by reading the marker tooling
(issue #1197, roadmap item 523).

WHERE EVERY CELL COMES FROM
---------------------------
Nothing here is a literal somebody typed. Four sources, all of them the ones
the checker itself uses, so a cell cannot drift away from the truth:

1. ``revl.diagnostics.GUARANTEES`` is the ROW SET. It is the compiler's own
   register of diagnostic codes, the same one `tools/docgen.py` generates the
   guarantee tables in `docs/rejections.md`, `DESIGN.md` and
   `docs/guide-humans.md` from. A guarantee cannot exist for the checker and
   be missing here, because the rows ARE its keys.

2. ``examples/rejections/*.rvl`` is the EVIDENCE. Every fixture is compiled by
   the reference frontend and the refusal is classified by
   ``revl.diagnostics.classify`` — the compiler's own classifier, not a
   filename convention — so the reproducer set for a code is whatever the
   compiler says it is.

3. ``selfhost/lower.rvl``'s ``admit_src`` decides the ``revl`` column. Each
   code's reproducers are run through the self-host gate (the fast engine from
   `tools/gate_reference_census.py`, imported rather than copied) and the tag
   it answers with is compared to the reference's. That is a measurement of
   the self-host frontier, not a description of it.

4. ``tools/check_roadmap_markers.py``'s ``--check-tier-parity`` records and
   ``tests/test_cross_tier_execution.py``'s ``DIVERGENCES`` are the DIVERGENCE
   REGISTERS. A parity finding that claims closure while citing one tier makes
   a recorded divergence cell on every tier it does not name; when the finding
   is answered, the cell turns back to `proved` on the next regeneration with
   nobody editing this file.

THE PART THAT HAD TO BE A GATE
------------------------------
The failure this replaces is a hand-written support table, and the specific
way a generated one still rots is a guarantee that arrives with no row: the
block regenerates, the new code renders as a blank or an optimistic `ok`, and
nothing says so. So a host-tier cell that is not `proved` MUST be listed in
``ACKNOWLEDGED`` with a reason, and an ``ACKNOWLEDGED`` entry that is no longer
needed is equally an error. Generation FAILS, loudly, in both directions:

  * add a guarantee to ``GUARANTEES`` with no reproducer and no acknowledgement
    and `python3 tools/conformance.py --check-readme` exits 1 naming the code
    and the tiers;
  * close the gap and leave the acknowledgement behind and it exits 1 too.

A FOURTH VERDICT, ON PURPOSE
----------------------------
Issue #1197 asks for `proved` / `recorded divergence` / `unimplemented`. There
is a fourth, `no reproducer`, for a rule the reference enforces but that no
in-tree program is refused under. Collapsing it into `proved` would be the
mistake `docs/conformance.md` already refuses to make for toolchains: "nothing
checked it" and "it passed" are different answers, and a validator whose
compiler is absent reports `unavailable`, never `ok`. Same rule here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REJECTIONS = ROOT / "examples" / "rejections"
ROADMAP = ROOT / "docs" / "v2.0-roadmap.md"

SELFHOST_TIER = "revl"

#: Gate verdict tags that stand for REGISTERED guarantee codes under another
#: name. `selfhost/lower.rvl` tags a refusal by the FAMILY it belongs to, and
#: almost every family it decides is code-less in the reference (`PRELUDE`,
#: `ROUTE`, `SPAWN`, `HANDOFF`, `BOOT`), so none of them ever indexes a row in
#: this matrix. Item 512's is the first that is not code-less: the gate decides
#: the model-placement DECLARATION half and spells its refusals byte for byte,
#: under the tag `MODEL` (`docs/design/554-route-model-remaining.md`).
#:
#: Without this row the cell below reads `unimplemented` — whose own definition
#: is "the gate raises no objection at all, or refuses for an unrelated reason"
#: — for a rule the gate demonstrably enforces with the reference's own
#: sentence. That is the matrix reporting the opposite of what it measured, and
#: "nothing checked it" and "it passed" being different answers cuts both ways.
#: Kept as a table rather than a prefix rule so a new tag has to be DECIDED
#: here: a tag that silently matched a code would be the fail-open direction.
#:
#: A tag maps to a SET, because one gate FAMILY is not one guarantee. Item
#: 516's council is the case that forced it. The gate tags every council
#: refusal `COUNCIL`, and that tag is right: it names the CONSTRUCT, which is
#: what a consumer reading the wire needs in order to know which of two
#: reference modules refused (`src/revl/model_route.py` or
#: `src/revl/model_council.py`). Issue #1190 then split that one construct
#: across two codes. The eleven refusals that would let a council report
#: agreement it does not have carry `G-COUNCIL-SPLIT`; the two whose subject
#: is a `model role` carry `G-MODEL-PLACE`
#: (`docs/design/557-council-disagreement.md` section 3). One tag therefore
#: cannot name one code, and naming one is wrong in both directions.
#: `COUNCIL -> G-COUNCIL-SPLIT` alone would deny the two role refusals the
#: evidence they have. `COUNCIL -> G-MODEL-PLACE` alone, which is what the
#: port was written against, leaves `G-COUNCIL-SPLIT` reading `unimplemented`
#: for eleven rules the gate decides with the reference's own sentence, and on
#: a tag-only test it credits item 512's row for ANY refusal the gate tags
#: `COUNCIL`, eleven of the thirteen of which are evidence for the other code.
#: That is the fail-open direction, and the message test below closes it.
#:
#: The set does not make the table guess, and it is not read as "any of
#: these". It records which codes a tag CAN stand for, and being in it is not
#: on its own enough to credit a cell: `selfhost_verdicts` credits a refusal
#: resolved through this table only when the gate's MESSAGE is the reference's
#: message byte for byte. That is the same agreement test
#: `tools/gate_reference_census.py` applies to this same corpus, and it is the
#: only thing in the tree that tells the two halves of `COUNCIL` apart without
#: this file restating the rule that splits them. A restatement would be a
#: second copy of `docs/design/557-council-disagreement.md` section 3, free to
#: drift away from the one the compiler actually runs.
#:
#: The stricter test is why a set is safe where a guess would not be. `MODEL`
#: has one code and is held to it too: both families spell the reference's
#: sentence byte for byte by construction (design notes 554 and 556), so
#: nothing here is weakened by measuring it, and a family that stopped doing
#: so would read `divergence` rather than quietly keep its `proved`.
SELFHOST_TAG_CODES: dict[str, frozenset[str]] = {
    "MODEL": frozenset({"G-MODEL-PLACE"}),
    "COUNCIL": frozenset({"G-COUNCIL-SPLIT", "G-MODEL-PLACE"}),
}

#: The verdicts a cell may carry, strongest first.
PROVED = "proved"
DIVERGENCE = "divergence"
NO_REPRODUCER = "no reproducer"
UNIMPLEMENTED = "unimplemented"
UNMEASURED = "unmeasured"

#: Host-tier cells that are not `proved`, each with the reason a reader needs
#: and nothing else. This is a RATCHET, not a licence: a code that belongs here
#: and is missing fails generation, and an entry here that is no longer needed
#: fails it too, so the list can only change deliberately. Every reason states
#: a fact about the tree that `enforcement_sites` independently confirms — a
#: code with no enforcement site at all is `unimplemented` and needs no entry.
ACKNOWLEDGED: dict[str, str] = {
    "A3": "A3 renames rather than refusing (`docs/guarantees.md`: \"renames, "
          "never refuses\"), so no program is rejected under it and there is "
          "no reproducer to run. The rename transform itself is pinned by the "
          "per-tier reserved-word suites (`backends/*/test_reserved_word_"
          "idents_*.py`).",
    "A5": "A5's obligation is real and has reproducers "
          "(`examples/rejections/a5_compensatable_without_compensate.rvl`, "
          "`examples/rejections/a5_no_inverse_declares_compensate.rvl`), but "
          "the code that carries it is G4 with category `reversibility` - "
          "there is no A5 code - so `revl.diagnostics.classify` reports them "
          "under G4 and no fixture is refused UNDER A5. The rule is the "
          "computer-use reversibility registry's (`src/revl/ui_family.py`); "
          "it is stated and witnessed in `formal/STATUS.md`'s A5 row "
          "(issue #2114).",
    "G-SECRET": "the confidentiality fixtures in `examples/rejections/` are "
                "refused under `G-SECRET-FLOW` (the disclosure-sink half). "
                "`G-SECRET` (the capability-reach half) is enforced in "
                "`src/revl/taint.py` and exercised by the per-tier secret "
                "registry suites, not by a fixture this corpus compiles.",
    "T3": "an open hole is refused at the ADMISSION gate rather than by "
          "`compile_files`, so a hole fixture compiles here and is refused one "
          "stage later; the reproducers live with the gate "
          "(`src/revl/holes.py`, `docs/holes.md`).",
    "T-UNRESOLVED": "refused by the checker (`src/revl/typecheck.py`) for a "
                    "type the compilation does not declare, which is a "
                    "multi-file condition a single-file fixture in this corpus "
                    "cannot set up; the reproducers are the doc fences "
                    "tagged `revl reject T-UNRESOLVED`, compiled by "
                    "`tests/test_doc_examples.py`.",
}

#: Which guarantee a `--check-tier-parity` subject speaks about. The map is
#: TOTAL over `check_roadmap_markers.TIER_SUBJECTS` and generation fails if a
#: subject is added there without a decision here — that is the same
#: "visible rather than silent" rule the rows get. An empty tuple is a
#: decision: the subject names a MECHANISM (an isolation rung, an operator
#: approval, a host-side path check) that no G/A code stands for, so a parity
#: finding about it moves no cell in this matrix.
SUBJECT_CODES: dict[str, tuple[str, ...]] = {
    # docs/guarantees.md groups "the `Secret` families" under G-SECRET and
    # G-SECRET-FLOW, and states redaction at boundaries as their mechanism.
    "secret": ("G-SECRET", "G-SECRET-FLOW"),
    "redact": ("G-SECRET", "G-SECRET-FLOW"),
    "redaction": ("G-SECRET", "G-SECRET-FLOW"),
    # G9's own one-liner is "untrusted data cannot create authority without a
    # declared declassification", and taint flow is where it is enforced.
    "taint": ("G9",),
    "authority": ("G9",),
    # diagnostics._PATTERNS classifies every witnessed-extern refusal as G4.
    "witnessed": ("G4",),
    # G6's origin in docs/guarantees.md is "paper Def. 48, confinement".
    "confinement": ("G6",),
    # G8 is the enumerable boundary surface; the audit surface is its readout
    # (`revl audit`), and attenuation narrows what a declared crossing may do.
    "audit surface": ("G8",),
    "attenuation": ("G8",),
    "capability": ("G8",),
    # Mechanisms, not codes. Named here so the map stays total.
    "jail": (),
    "sandbox": (),
    "approval": (),
    "policy": (),
    "provenance": (),
    "traversal": (),
    "symlink": (),
    "privilege": (),
    "attestation": (),
}

#: Which guarantee each pinned runtime divergence belongs under. The register
#: (`tests/test_cross_tier_execution.py::DIVERGENCES`) is EMPTY on main; an
#: entry added there with no mapping here fails generation rather than being
#: dropped, which is what keeps this armed while it is empty.
RUNTIME_DIVERGENCE_CODES: dict[str, tuple[str, ...]] = {}


class MatrixError(SystemExit):
    """Generation refused. The message names the code and the tiers."""


# --------------------------------------------------------------------------
# source 1: the row set
# --------------------------------------------------------------------------

def guarantee_codes() -> list[str]:
    """Every code the compiler's own register carries, in a stable order.

    G before A before T before the named families, then alphabetically inside
    each, so the table reads the way `docs/guarantees.md` does.
    """
    from revl.diagnostics import GUARANTEES  # noqa: PLC0415: needs sys.path

    def key(code: str) -> tuple:
        m = re.fullmatch(r"([GAT])(\d+)", code)
        if m:
            return ("GAT".index(m.group(1)), int(m.group(2)), code)
        return (3, 0, code)

    return sorted(GUARANTEES, key=key)


def guarantee_text(code: str) -> str:
    from revl.diagnostics import GUARANTEES  # noqa: PLC0415

    return GUARANTEES[code]


def enforcement_sites(code: str) -> list[str]:
    """The reference modules that raise under `code`, read off the source.

    Three spellings, because the compiler uses all three: an explicit
    `code="G9"` keyword on the RevlError, the `(G9)` tag the message convention
    embeds, and a module-level `CODE = "G-MODEL-PLACE"` constant that every
    raise in the file then passes by name. A code with none of the three is not
    enforced by this frontend at all, which is a verdict rather than a gap in
    this tool.

    The constant spelling was added for issue #1190. `src/revl/model_council.py`
    holds its code in `CODE` and passes it through one `_err` helper, so no
    literal reaches a raise site and the module read as "not an enforcement
    site" while it was refusing eleven shapes. That is the wrong direction for
    a matrix to be wrong in: it reported a checked guarantee as unimplemented
    on every tier. The pattern is anchored to a module-level assignment
    (`^[A-Z_]*CODE = "..."`), so naming a code in prose still does not count as
    enforcing it.
    """
    tag = re.compile(r"\(" + re.escape(code) + r"\)")
    keyword = re.compile(r"""code\s*=\s*["']""" + re.escape(code) + r"""["']""")
    constant = re.compile(
        r"""^[A-Z_]*CODE\s*=\s*["']""" + re.escape(code) + r"""["']""",
        re.MULTILINE)
    hits = []
    for path in sorted((ROOT / "src" / "revl").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if keyword.search(text) or tag.search(text) or constant.search(text):
            hits.append(f"src/revl/{path.name}")
    return hits


# --------------------------------------------------------------------------
# source 2: the reproducers, classified by the compiler
# --------------------------------------------------------------------------

def reproducers() -> dict[str, list[str]]:
    """code -> the fixtures the REFERENCE refuses under it, sorted by name.

    Keyed on `classify()`'s answer, never on the filename: `g4_extern_undo_
    wrong_arg_type.rvl` is refused under T1, and a matrix that trusted the
    prefix would have credited G4 with a fixture that proves something else.
    """
    from revl import compile_files  # noqa: PLC0415
    from revl.diagnostics import classify  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    index: dict[str, list[str]] = {}
    for path in sorted(REJECTIONS.glob("*.rvl")):
        try:
            compile_files([str(path)])
        except RevlError as error:
            code = classify(error)["code"]
        except Exception:  # noqa: BLE001 — a crash is not evidence for any code
            continue
        else:
            continue
        index.setdefault(code, []).append(f"examples/rejections/{path.name}")
    return index


_REF_MESSAGES: dict[str, str] = {}


def reference_message(rel: str) -> str:
    """The SENTENCE the reference refuses `rel` with, or `""` if it does not.

    Only `selfhost_verdicts` needs this, and only for a reproducer the gate
    answers under a tag that is not itself a code (`SELFHOST_TAG_CODES`). The
    tag has already said which FAMILY the gate decided; the message is the only
    thing in the tree that says which of the family's guarantees, and it says
    it without this file restating the rule that splits them.

    Cached per path because the caller walks the same fixtures once per code.
    """
    if rel in _REF_MESSAGES:
        return _REF_MESSAGES[rel]
    from revl import compile_files  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    try:
        compile_files([str(ROOT / rel)])
    except RevlError as error:
        message = error.message
    except Exception:  # noqa: BLE001 — a crash is not this file's evidence
        message = ""
    else:
        message = ""
    _REF_MESSAGES[rel] = message
    return message


# --------------------------------------------------------------------------
# source 3: the self-host gate, run over the same reproducers
# --------------------------------------------------------------------------

_ADMIT_CACHE: list = []


def _selfhost_admit():
    """`selfhost/lower.rvl`'s `admit_src`, or None when it will not load.

    Cached for the process: loading it means compiling the self-host lowering
    with the reference frontend and emitting it to python, which is about a
    second, and a test file that builds the matrix several times should pay
    that once.

    Imported from `tools/gate_reference_census.py` rather than restated: that
    file is what `tests/test_gate_crate_admit.py` holds the crate against, so
    the matrix and the census cannot disagree about what the gate decides.
    """
    if _ADMIT_CACHE:
        return _ADMIT_CACHE[0]
    spec = importlib.util.spec_from_file_location(
        "tier_guarantees_census", ROOT / "tools" / "gate_reference_census.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        admit = module.build_selfhost_admit()
    except Exception:  # noqa: BLE001 — a gate that will not load is its own datum
        return None
    _ADMIT_CACHE.append(admit)
    return admit


def selfhost_verdicts(index: dict[str, list[str]]) -> dict[str, tuple]:
    """code -> (verdict, reason) for the `revl` column.

    `admit_src` answers `"<TAG>|<message>"` or `""` for no objection. Three
    outcomes, and the distinction between the last two is the whole point:

      proved         every reproducer is refused under the SAME tag.
      divergence     some are and some are not — a partial port, which is
                     worse than none because the gap is invisible per-case.
      unimplemented  none is. The gate either raises no objection at all (the
                     fail-open direction item 391 measures) or refuses for an
                     unrelated reason, which is not this guarantee holding.

    WHEN THE TAG IS NOT THE CODE. A tag the gate spells that is not itself a
    registered code is resolved through `SELFHOST_TAG_CODES`, and a tag there
    is credited on a STRICTER test than `tag == code`: the gate's message has
    to be the reference's message byte for byte (`reference_message`, the
    agreement test `tools/gate_reference_census.py` already applies to this
    same corpus). The reason is that a tag names a gate FAMILY and a family is
    not a guarantee. `COUNCIL` stands for two codes, so the tag alone cannot
    say which of them a refusal is evidence for, and the sentence can. A
    reproducer the gate refuses under the right family with a different
    sentence therefore counts as a divergence, not as evidence: crediting the
    family would credit whichever of its guarantees the reader assumed, which
    is the fail-open direction this table exists to keep shut.
    """
    admit = _selfhost_admit()
    if admit is None:
        raise MatrixError(
            "tier_guarantees: selfhost/lower.rvl's admit_src would not load, "
            "so the `revl` column cannot be measured. Refusing to generate a "
            "matrix with an unmeasured column rather than render it blank.")

    out: dict[str, tuple[str, str] | tuple[str, str, list[str]]] = {}
    for code, paths in index.items():
        agreed = 0
        # each reproducer the gate does NOT refuse under this code, with what
        # it did instead. A divergence names these rather than counting them,
        # so two branches that each fix a different reproducer change two
        # different lines of the generated block (issue #1768).
        missed: list[str] = []
        other_tags: set[str] = set()
        # the tag the gate actually spelled, when it is not the code itself
        # (`SELFHOST_TAG_CODES`), so the generated sentence names what a reader
        # will see on the wire rather than the code it stands for.
        under: set[str] = set()
        for rel in paths:
            answer = (ROOT / rel).read_text(encoding="utf-8")
            try:
                verdict = admit(answer)
            except Exception:  # noqa: BLE001 — a crash is a divergence, not a pass
                verdict = ""
            tag, _, message = verdict.partition("|")
            stands_for = SELFHOST_TAG_CODES.get(tag, frozenset())
            if tag == code:
                agreed += 1
            elif code in stands_for and message == reference_message(rel):
                agreed += 1
                under.add(tag)
            elif tag:
                other_tags.add(tag)
                missed.append(f"`{rel}`: answered under {tag}")
            else:
                missed.append(f"`{rel}`: admitted")
        if agreed == len(paths):
            spelling = ", ".join(sorted(under)) or code
            out[code] = (PROVED, "the self-host gate refuses every reproducer "
                                 f"for {code} under {spelling}")
        elif agreed:
            out[code] = (DIVERGENCE,
                         f"the self-host gate refuses every other {code} "
                         f"reproducer, and not these",
                         sorted(missed))
        else:
            answered = ("answers every " + code + " reproducer under "
                        + ", ".join(sorted(other_tags)) if other_tags
                        else "raises no objection to any " + code
                        + " reproducer")
            out[code] = (UNIMPLEMENTED,
                         f"the self-host gate {answered} (the self-host "
                         f"frontier, roadmap item 391; the type layer is "
                         f"item 417)")
    return out


# --------------------------------------------------------------------------
# source 4: the divergence registers
# --------------------------------------------------------------------------

def parity_divergences(host_tiers: tuple[str, ...]) -> dict[tuple[str, str], str]:
    """(code, tier) -> reason, from `--check-tier-parity`'s own records.

    A finding that claims closure, cites exactly one tier and never names a
    second is, in that gate's words, a claim that "the finding does not SAY".
    This matrix has to render a cell for the tiers it does not say anything
    about, and `proved` is not that cell.
    """
    import check_roadmap_markers as markers  # noqa: PLC0415 — tools/ on sys.path

    unmapped = sorted(set(markers.TIER_SUBJECTS) - set(SUBJECT_CODES))
    if unmapped:
        raise MatrixError(
            "tier_guarantees: check_roadmap_markers.TIER_SUBJECTS gained "
            f"{', '.join(unmapped)} with no entry in SUBJECT_CODES. Decide "
            "which guarantee each one speaks about, or map it to () to record "
            "that it names a mechanism rather than a code.")

    backends = {alias for alias in markers.TIER_ALIASES}
    out: dict[tuple[str, str], str] = {}
    # The main file plus docs/roadmap-archive/ (tools/roadmap_source.py).
    for record in markers.tier_parity_records(markers.read_roadmap(ROADMAP).text,
                                              backends):
        codes: set[str] = set()
        for subject in record["subjects"]:
            codes.update(SUBJECT_CODES[subject])
        if not codes:
            continue
        # Deliberately NOT the finding's line number: this block is diffed
        # byte for byte, and a line number would re-stale it on every unrelated
        # roadmap edit. The item and label are stable and enough to find it.
        reason = (f"roadmap item {record['item']} {record['label']} claims "
                  f"closure citing only `backends/{record['tier']}/` and never "
                  f"names this tier (`--check-tier-parity`, subjects: "
                  f"{', '.join(record['subjects'])})")
        for code in codes:
            for tier in record["others"]:
                if tier in host_tiers:
                    out[(code, tier)] = reason
    return out


def runtime_divergences() -> dict[tuple[str, str], str]:
    """(code, tier) -> reason, from the pinned cross-tier runtime register.

    `tests/test_cross_tier_execution.py::DIVERGENCES` is a table of per-tier
    behaviour recorded so it cannot drift silently. It is EMPTY on main. An
    entry that appears there with no `RUNTIME_DIVERGENCE_CODES` mapping fails
    generation instead of being dropped, which is what keeps this path armed
    while it has nothing to say.
    """
    spec = importlib.util.spec_from_file_location(
        "tier_guarantees_xtier", ROOT / "tests" / "test_cross_tier_execution.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        raise MatrixError(
            "tier_guarantees: cannot read the runtime divergence register "
            f"tests/test_cross_tier_execution.py ({exc}). Refusing to generate "
            "a matrix that silently reports an unread register as empty.")

    out: dict[tuple[str, str], str] = {}
    for name, entry in sorted(getattr(module, "DIVERGENCES", {}).items()):
        codes = RUNTIME_DIVERGENCE_CODES.get(name)
        if codes is None:
            raise MatrixError(
                f"tier_guarantees: the runtime divergence register pins "
                f"{name!r} with no entry in RUNTIME_DIVERGENCE_CODES. Say "
                f"which guarantee it belongs under, or map it to () if it "
                f"belongs under none.")
        pinned = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else {}
        for code in codes:
            for tier in sorted(pinned):
                out[(code, tier)] = (
                    f"pinned in `tests/test_cross_tier_execution.py::"
                    f"DIVERGENCES` as {name!r}")
    return out


# --------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------

def _evidence(code: str, paths: list[str], sites: list[str]) -> str | None:
    """The one path a cell links to: a reproducer when there is one.

    Prefer a fixture NAMED after the code, and fall back to the first the
    classifier attributed to it. The names are only a reading convenience —
    `g4_extern_undo_wrong_arg_type.rvl` is refused under T1, and the matrix
    still counts it as T1 evidence — but linking `t1_...` from the T1 row when
    one exists saves a reader the double take.
    """
    if paths:
        prefix = code.lower().replace("-", "") + "_"
        named = [p for p in paths if Path(p).name.startswith(prefix)]
        return (named or paths)[0]
    return sites[0] if sites else None


def host_tiers() -> tuple[str, ...]:
    import conformance  # noqa: PLC0415 — tools/ on sys.path

    return conformance.TIERS


def _short(tier: str) -> str:
    from revl.tiers import short_tier  # noqa: PLC0415: needs sys.path

    return short_tier(tier)


def matrix() -> dict:
    """Every guarantee against every tier, with the evidence that decided it.

    Returns

        {"tiers": [...], "rows": [{"code", "text", "evidence", "sites",
                                   "cells": {tier: {"verdict", "why",
                                                    "names"?}}}, ...],
         "acknowledged": {...}}
    """
    tiers = host_tiers()
    codes = guarantee_codes()
    # A tag aliased to a code the compiler's register does not carry would
    # never match a row, so it would sit here reading like a decision while
    # crediting nothing. Same direction as the rest of this file: say so
    # rather than generate around it.
    aliased = {code for names in SELFHOST_TAG_CODES.values() for code in names}
    unregistered = sorted(aliased - set(codes))
    if unregistered:
        raise MatrixError(
            "tier_guarantees: SELFHOST_TAG_CODES aliases a gate tag to "
            f"{', '.join(unregistered)}, which `revl.diagnostics.GUARANTEES` "
            "does not carry. Name a registered code or drop the entry: an "
            "alias to a code with no row credits nothing and reads as if it "
            "did.")
    index = reproducers()
    selfhost = selfhost_verdicts(index)
    parity = parity_divergences(tiers)
    runtime = runtime_divergences()

    rows = []
    missing: list[tuple[str, str]] = []
    for code in codes:
        paths = index.get(code, [])
        sites = enforcement_sites(code)
        row: dict = {"code": code, "text": guarantee_text(code),
                     "evidence": _evidence(code, paths, sites),
                     "reproducers": len(paths), "sites": sites, "cells": {}}

        if paths:
            base = (PROVED, "the reference frontend refuses "
                            f"`{_evidence(code, paths, sites)}` under {code} "
                            f"before any emitter runs")
        else:
            # NOT proved on a host tier, and that needs a reason on the record.
            # Both spellings do: `no reproducer` (the rule is enforced, nothing
            # in the corpus is refused under it) and `unimplemented` (no module
            # raises under the code at all). The second is the more dangerous
            # of the two — the register claims a code the frontend does not
            # enforce — so it is emphatically not the one to let through
            # silently.
            verdict = NO_REPRODUCER if sites else UNIMPLEMENTED
            reason = ACKNOWLEDGED.get(code)
            if reason is None:
                missing.append((code, verdict))
                reason = "UNACKNOWLEDGED"
            base = (verdict, reason)

        for tier in tiers:
            short = _short(tier)
            why = parity.get((code, tier)) or runtime.get((code, tier))
            if why is None:
                row["cells"][short] = {"verdict": base[0], "why": base[1]}
                continue
            # A register overrides the shared frontend verdict, but it does not
            # erase it: when the base was already weaker than `proved`, a
            # reader needs both halves or the cell understates what is missing.
            if base[0] != PROVED:
                why = f"{why}; and {base[1]}"
            row["cells"][short] = {"verdict": DIVERGENCE, "why": why}

        verdict, why, *names = selfhost.get(
            code, (UNIMPLEMENTED, "no reproducer reaches the self-host gate, "
                                  "so its verdict on this code is unmeasured"))
        if not paths:
            verdict, why, names = base[0], base[1], []
        row["cells"][SELFHOST_TIER] = {"verdict": verdict, "why": why}
        if names:
            row["cells"][SELFHOST_TIER]["names"] = names[0]
        rows.append(row)

    if missing:
        raise MatrixError(
            "tier_guarantees: "
            + "; ".join(
                f"guarantee {code} has no reproducer in examples/rejections/ "
                f"and no ACKNOWLEDGED entry, so its row would read "
                f"`{verdict}` on every host tier with no reason"
                for code, verdict in missing)
            + ".\n    Add a reproducer under examples/rejections/, or record "
              "in tools/tier_guarantees.py::ACKNOWLEDGED why this rule has "
              "none. A guarantee must not get a blank row.")

    stale = sorted(set(ACKNOWLEDGED) - {row["code"] for row in rows
                                        if row["reproducers"] == 0})
    if stale:
        raise MatrixError(
            f"tier_guarantees: ACKNOWLEDGED still excuses {', '.join(stale)}, "
            "which now has a reproducer (or is gone from GUARANTEES). Delete "
            "the entry: an excuse that outlives its reason is how a support "
            "table rots.")

    return {"tiers": [_short(t) for t in tiers] + [SELFHOST_TIER], "rows": rows,
            "acknowledged": dict(ACKNOWLEDGED)}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

START = "<!-- GUARANTEE-TIER-MATRIX:START -->"
END = "<!-- GUARANTEE-TIER-MATRIX:END -->"
SUPPORT_START = "<!-- TIER-SUPPORT-MATRIX:START -->"
SUPPORT_END = "<!-- TIER-SUPPORT-MATRIX:END -->"

#: How the explanation list says each verdict, in the issue's own vocabulary.
_PHRASE = {
    DIVERGENCE: "is a **recorded divergence**.",
    NO_REPRODUCER: "has **no reproducer**.",
    UNIMPLEMENTED: "is **unimplemented**.",
}

_CELL = {
    PROVED: "proved",
    DIVERGENCE: "**div**",
    NO_REPRODUCER: "no repro",
    UNIMPLEMENTED: "unimpl",
}


def markdown(data: dict | None = None) -> str:
    """The block, deterministic and free of anything that moves on its own.

    No line numbers, no counts of files, no timings: every one of those would
    re-stale this block on an unrelated edit, and a generated artifact that
    reds the build for somebody else's commit gets regenerated without being
    read, which is the failure mode it exists to prevent.
    """
    data = data or matrix()
    tiers = data["tiers"]
    out: list[str] = []

    out.append("_Generated by `python3 tools/conformance.py --write-readme`. "
               "Do not edit by hand._")
    out.append("")
    out.append("Rows are `revl.diagnostics.GUARANTEES`, the compiler's own "
               "register, so a guarantee cannot exist for the checker and be "
               "missing here. Columns are the six host tiers plus `revl`, the "
               "self-host gate.")
    out.append("")
    out.append("`proved` a reproducer in the tree is refused under this code "
               "by the implementation that decides this column · **`div`** a "
               "recorded divergence: a register says this tier is not covered, "
               "and the reason is listed below · `no repro` the rule is "
               "enforced but nothing in the corpus is refused under it, so "
               "this matrix will not say proved · `unimpl` this column's "
               "implementation does not decide the rule.")
    out.append("")
    out.append("The six host columns share their verdict wherever a register "
               "does not separate them, and that is the claim rather than a "
               "shortcut: every code below is decided by the frontend, which "
               "runs once, before emission, so no emitter ever receives a "
               "program that violates one. A column moves away from its "
               "siblings exactly when a divergence register says it does.")
    out.append("")
    out.append("**What `proved` does NOT claim.** It is a REFUSAL claim: the "
               "implementation deciding that column refuses a violating "
               "program under that code. Where a rule also has a RUNTIME half "
               "(G7's LIFO walk over registered entries, A8's revert-and-"
               "contain, G4's inverse actually running), that half is the "
               "construct matrix's and the per-tier runtime suites' question, "
               "not this one. Reading a `proved` cell as \"the tier's runtime "
               "discharges this at execution time\" would overstate it, and "
               "overstating is the failure a support table is for preventing.")
    out.append("")

    out.append("| guarantee | " + " | ".join(tiers) + " | evidence |")
    out.append("|" + "|".join(["---"] * (len(tiers) + 2)) + "|")
    for row in data["rows"]:
        cells = [_CELL[row["cells"][t]["verdict"]] for t in tiers]
        evidence = row["evidence"]
        link = f"[`{evidence}`](../{evidence})" if evidence else "–"
        out.append(f"| `{row['code']}` | " + " | ".join(cells) + f" | {link} |")

    explained: list[str] = []
    for row in data["rows"]:
        seen: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
        for tier in tiers:
            cell = row["cells"][tier]
            if cell["verdict"] == PROVED:
                continue
            key = (cell["verdict"], cell["why"].rstrip("."),
                   tuple(cell.get("names", ())))
            seen.setdefault(key, []).append(tier)
        for (verdict, detail, names), where in seen.items():
            explained.append(f"- `{row['code']}` on {', '.join(where)} "
                             f"{_PHRASE[verdict]} "
                             f"{detail[:1].upper()}{detail[1:]}"
                             + (":" if names else "."))
            # one reproducer per line, sorted, so a branch that moves one
            # program's verdict touches that program's line only
            explained.extend(f"  - {name}" for name in names)
    if explained:
        out.append("")
        out.append("**Why a cell is not `proved`.** Every non-`proved` cell "
                   "above, with the register or the reason that decided it:")
        out.append("")
        out.extend(explained)

    return "\n".join(out)


def block(data: dict | None = None) -> str:
    return f"{START}\n{markdown(data)}\n{END}"


def totals(data: dict | None = None) -> str:
    """How many cells of each verdict every tier has, for `--show`.

    Not part of the committed block (issue #1768): a per-tier count changes
    on every branch that moves any one cell, so two branches that each move a
    different cell both rewrite the same table row and conflict under a plain
    3-way merge. The rows above already carry every cell; this sums them on
    demand.
    """
    data = data or matrix()
    order = (PROVED, DIVERGENCE, NO_REPRODUCER, UNIMPLEMENTED)
    out = ["| tier | " + " | ".join(_CELL[v].replace("**", "") for v in order)
           + " |", "|---|---|---|---|---|"]
    for tier in data["tiers"]:
        counts = {v: 0 for v in _CELL}
        for row in data["rows"]:
            counts[row["cells"][tier]["verdict"]] += 1
        out.append(f"| {tier} | "
                   + " | ".join(str(counts[v]) for v in order) + " |")
    return "\n".join(out)


def _recovery_checks(tier: str, tests_root: Path) -> list[tuple[str, list[str]]]:
    """Return test files that contain a named crash-recovery test for `tier`.

    The root Python recovery module predates the per-tier modules. The other
    tiers follow `test_<short-tier>_crash_recovery.py`, so adding a check for a
    tier is enough to make it visible here. A filename without a crash test
    function is not evidence.
    """
    import ast

    short = _short(tier)
    paths = ([tests_root / "test_crash_recovery.py"] if tier == "python"
             else [tests_root / f"test_{short}_crash_recovery.py"])
    found = []
    for path in paths:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        cases = sorted({node.name for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith("test_")
                        and "crash" in node.name.lower()})
        if cases:
            found.append((path.relative_to(tests_root.parent).as_posix(), cases))
    return found


def _known_compile_failures(path: Path) -> dict[str, set[str]]:
    """Read the compile-test baseline as data; never import or execute tests."""
    import ast

    if not path.is_file():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "KNOWN_FAILURES"):
            value = ast.literal_eval(node.value)
            return {str(tier): set(cases) for tier, cases in value.items()}
    return {}


def _ci_run_commands(workflow_text: str) -> list[str]:
    """Read executable workflow steps, excluding comments and prose."""
    try:
        import yaml  # noqa: PLC0415 — part of the repository's test extra
        workflow = yaml.safe_load(workflow_text)
    except Exception as exc:  # noqa: BLE001 — malformed CI cannot support a claim
        raise MatrixError(f"tier_guarantees: cannot read the CI workflow: {exc}") from exc
    commands = []
    for job in (workflow or {}).get("jobs", {}).values():
        for step in job.get("steps", ()):
            command = step.get("run")
            if isinstance(command, str):
                commands.append(command)
    return commands


def support_matrix(*, report: dict | None = None,
                   selfhost: dict[str, str] | None = None,
                   tests_root: Path | None = None,
                   ci_text: str | None = None) -> dict:
    """Derive the adopter-facing evidence table from the checks that run it.

    Emit counts and the self-host agreement come from `tools/conformance.py`.
    Compile/execution coverage comes from its validator registries and CI steps.
    Durable recovery is discovered from crash-test modules and the CI test
    collection. Missing evidence is always `unmeasured`, never a positive cell.
    """
    import conformance  # noqa: PLC0415 — tools/ is already on sys.path
    from validate import EXECUTORS, VALIDATORS  # noqa: PLC0415

    tests_root = tests_root or ROOT / "tests"
    if report is None:
        report = conformance.run()
    if selfhost is None:
        selfhost = conformance.selfhost_column()
    if ci_text is None:
        ci_path = ROOT / ".github" / "workflows" / "ci.yml"
        ci_text = ci_path.read_text(encoding="utf-8") if ci_path.is_file() else ""

    compile_test = "tests/test_conformance_validate.py"
    compile_test_path = tests_root / "test_conformance_validate.py"
    ci_commands = _ci_run_commands(ci_text)
    compile_in_ci = any("pytest tests/test_conformance_validate.py -q" in command
                        for command in ci_commands)
    compile_baselines = _known_compile_failures(compile_test_path)
    executable_count = len(conformance.executable_cases())
    execute_in_ci = any("tools/conformance.py --execute --require-execution" in command
                        for command in ci_commands)
    rows = []

    for tier in conformance.TIERS:
        short = _short(tier)
        kinds = [case.get("emit_kind", {}).get(tier) for case in report.get("cases", [])]
        measured = [kind for kind in kinds if kind in ("ok", "limit", "gap")]
        emitted = kinds.count("ok")
        limits = kinds.count("limit")
        gaps = kinds.count("gap")
        validator = VALIDATORS.get(tier)
        known_failures = compile_baselines.get(tier, set())
        if (not validator or not compile_test_path.is_file() or not compile_in_ci
                or len(measured) != len(kinds) or not kinds):
            emit_status = UNMEASURED
        elif gaps or known_failures:
            emit_status = DIVERGENCE
        else:
            emit_status = PROVED
        limit_word = "limit" if limits == 1 else "limits"
        gap_word = "gap" if gaps == 1 else "gaps"
        emit_detail = (f"{emitted} emitted; {limits} classified {limit_word}; "
                       f"{gaps} {gap_word}; "
                       f"{validator.depth if validator else 'no validator'}")
        if known_failures:
            count = len(known_failures)
            noun = "failure" if count == 1 else "failures"
            emit_detail += f"; {count} known compile {noun}"

        executor = EXECUTORS.get(tier)
        if executor and execute_in_ci and executable_count:
            execute_status = PROVED
            execute_detail = f"{executable_count} answer-bearing cases; CI requires execution"
        else:
            execute_status = UNMEASURED
            execute_detail = ("no executor registered" if not executor
                              else "no required execution check is wired in CI")

        recovery_checks = _recovery_checks(tier, tests_root)
        root_suite_in_ci = any("pytest tests/ -q" in command for command in ci_commands)
        # The regular root-suite jobs do not provision cordis-wasm. Its test
        # counts only when the wasm job explicitly collects it alongside the
        # pinned runtime it installs.
        if short == "wasm":
            recovery_in_ci = any(path in command for path, _cases in recovery_checks
                                 for command in ci_commands)
        else:
            recovery_in_ci = root_suite_in_ci or any(
                path in command for path, _cases in recovery_checks
                for command in ci_commands)
        if recovery_checks and recovery_in_ci:
            recovery_status = PROVED
            recovery_detail = "; ".join(
                f"{path}: {', '.join(cases)}" for path, cases in recovery_checks)
        else:
            recovery_status = UNMEASURED
            recovery_detail = ("no crash-recovery test is registered"
                              if not recovery_checks
                              else "crash test exists but is not collected by CI")

        selfhost_row = None
        if selfhost is not None:
            selfhost_row = next((row for row in conformance._summary_rows(report, selfhost)
                                 if row[0] == f"{SELFHOST_TIER} (self-host)"), None)
        if selfhost_row is None:
            selfhost_status, selfhost_detail = UNMEASURED, "self-host comparison did not run"
        else:
            _name, exact, frontier, selfhost_gaps = selfhost_row
            total = exact + frontier + selfhost_gaps
            selfhost_status = DIVERGENCE if selfhost_gaps else PROVED
            selfhost_detail = (f"shared revl measure: {exact}/{total} byte-identical; "
                               f"{frontier} frontier limits; {selfhost_gaps} gaps")

        rows.append({
            "tier": short,
            "emits": {"status": emit_status, "detail": emit_detail,
                      "evidence": [compile_test, "tools/conformance.py"]},
            "executes": {"status": execute_status, "detail": execute_detail,
                         "evidence": ["tools/conformance.py"] if executor else []},
            "durable_recovery": {"status": recovery_status,
                                 "detail": recovery_detail,
                                 "evidence": [path for path, _cases in recovery_checks]},
            "selfhost_agreement": {"status": selfhost_status,
                                   "detail": selfhost_detail,
                                   "evidence": ["tools/conformance.py"]
                                   if selfhost_row else []},
        })

    return {"tiers": [_short(tier) for tier in conformance.TIERS], "rows": rows}


def support_markdown(data: dict | None = None) -> str:
    """Render the evidence table, with every unsupported measurement explicit."""
    data = data or support_matrix()
    out = ["_Generated by `python3 tools/conformance.py --write-readme`. Do not edit by hand._",
           "",
           "A `proved` cell names the check wired for that tier; `divergence` names a "
           "known gap; `unmeasured` means no matching check runs. The self-host "
           "agreement is one shared `revl` pipeline, so its measured count repeats "
           "across the host-tier rows.",
           "",
           "| tier | emits and compiles | executes | durable recovery | self-host agreement |",
           "|---|---|---|---|---|"]

    def cell(value: dict) -> str:
        details = value["detail"]
        links = [f"[`{path}`](../{path})" for path in value["evidence"]]
        return "**" + value["status"] + "**: " + details + (
            " (" + ", ".join(links) + ")" if links else "")

    for row in data["rows"]:
        out.append("| " + row["tier"] + " | " + " | ".join(
            cell(row[key]) for key in
            ("emits", "executes", "durable_recovery", "selfhost_agreement")) + " |")
    return "\n".join(out)


def support_block(data: dict | None = None, *, report: dict | None = None,
                  selfhost: dict[str, str] | None = None) -> str:
    if data is None:
        data = support_matrix(report=report, selfhost=selfhost)
    return f"{SUPPORT_START}\n## Per-tier support\n\n{support_markdown(data)}\n{SUPPORT_END}"


def check_support_block() -> int:
    """The issue-facing `--check` gate for the generated support matrix."""
    path = ROOT / "docs" / "conformance.md"
    text = path.read_text(encoding="utf-8")
    expected = support_block()
    if (SUPPORT_START in text and SUPPORT_END in text
            and text[text.index(SUPPORT_START):text.index(SUPPORT_END) + len(SUPPORT_END)]
            == expected):
        print("docs/conformance.md per-tier support matrix is current.")
        return 0
    print("docs/conformance.md per-tier support matrix is STALE; regenerate with "
          "`python3 tools/conformance.py --write-readme`.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="the matrix as data, evidence and reasons included")
    ap.add_argument("--show", action="store_true",
                    help="the per-tier verdict totals, which the committed "
                         "block leaves out")
    ap.add_argument("--check", action="store_true",
                    help="fail if docs/conformance.md's generated per-tier "
                         "support table is stale")
    args = ap.parse_args(argv)
    if args.check:
        return check_support_block()
    data = matrix()
    if args.show:
        print(totals(data))
        return 0
    print(json.dumps(data, indent=2) if args.json else markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
