#!/usr/bin/env python3
"""Generate `crates/revl-gate` — the embeddable revl admission gate as a rust
library crate (roadmap item 332, design `docs/design/332-embeddable-gate-api.md`,
Stage 3: "the crate, admit-only").

What this mechanizes
--------------------
`tools/bench_selfhost_rust.py` already compiles a self-host stage to rust through
the reference emitter and assembles a cargo crate around it — but a THROWAWAY
one, rebuilt and deleted per bench run. Nothing a third program can `cargo add`
comes out of it. This tool writes the same emission into a COMMITTED library
crate with a hand-written shim over it, so the gate becomes a dependency.

The crate is committed, not generated at install: it must build on a machine
with no Python at all, or item 336 (a single rust binary shipping the compiler)
and item 338 (`cargo add revl`) do not exist. Drift is killed the way the
conformance matrix kills it — `--check` regenerates from the same tree and fails
on any byte difference (`tests/test_gate_crate_drift.py`).

What goes in
------------
* `selfhost/lower.rvl` and its `use` closure (`lexer.rvl`, `parser.rvl`),
  emitted to rust by the reference rust backend. `lower.rvl`'s `admit_src` is
  the native gate: lex -> parse -> the composition/guarantee gate; `""` means
  "nothing to refuse" and `"<TAG>|<message>"` refuses. `admit_src` is the half
  of the self-host pipeline that runs natively on rust today (item 284 made it
  viable); `selfhost/compile.rvl` is deliberately NOT the root, because its
  emitter half still has `@py`-only externs (`string_lit`, `py_repr`, ...) and
  does not emit to rust at all today. That is Stage 4's lane, and the crate says
  so in its own `compile_to`.
* A derived FRONTIER table: the reference language constructs the self-host does
  not cover. Derived, never guessed — the reference side is imported from the
  reference compiler itself (`revl.lexer.KEYWORDS`, `revl.typecheck._BUILTIN_SIG`)
  and the self-host side is read out of the self-host sources. The difference is
  what the crate FAILS CLOSED on. Because the tables are generated, a reference
  keyword or builtin added without a self-host port changes this file, which
  changes the crate, which reds the drift gate.

The security clause
-------------------
The self-host is behind the reference (item 391), so this crate's verdict is
STANDALONE-ONLY and FRONTIER-LIMITED. The two divergence directions are not
symmetric: refusing what the reference admits is an inconvenience; ADMITTING
what the reference refuses is the defect class this arc exists to prevent.

The gap turned out not to be "a few missing constructs" but a whole missing
LAYER, and the crate's surface is shaped by that measurement rather than by the
design's assumption. `admit_src` decides the composition/guarantee layer
(G1..G4, A1, PRELUDE, and parse failures as BAD); it does NOT run the
reference's type layer. Measured: the reference refuses
`fn f() -> Int { return "s" }`, `fn f() -> Int { return undefined_name }` and
`fn f() -> { }`; the self-host gate objects to none of them. So the generated
crate ships NO admission at all:

* `Verdict` has no `Admitted` arm and no `is_admitted()`. The non-refusing
  outcome is `NoObjection`, which means "this gate found nothing it can refuse"
  and never "the reference would admit this";
* `to_json()` emits `"admitted": false` for EVERY arm, so a consumer written
  against the design's fixed `{admitted, code, message}` shape reads this gate
  as "never admits" instead of misreading a no-objection;
* a construct in the derived frontier table -> `Verdict::OutsideFrontier`;
* a source above the size bound the deeply-recursive front end can be trusted
  on (an overflow ABORTS, and an abort cannot be turned back into a refusal);
* a native gate panic -> `Verdict::OutsideFrontier`, caught;
* a wire string that is not `""` and carries no `|` -> `Verdict::OutsideFrontier`;
* `compile_to` -> `Verdict::OutsideFrontier` unconditionally (Stage 4).

A crate that cannot issue an admission cannot commit the false-admit defect.
What it does buy is the sound direction: a local, in-process, Python-free
REFUSAL that byte-agrees with the reference on the covered corpus
(`tests/test_gate_crate_admit.py`).

The manifest arm (issue #346)
-----------------------------
The missing layer above is the TYPE layer, and it is its own lane. It is not
the only way to reach an admission question, though: item 186's ambient gate
asks a different one — given a RUNNING composition plus an incoming text, what
does the composition/guarantee layer say about the UNION? `selfhost/lower.rvl`
answers that today (`admit_ambient(src, manifest)`, pinned against
`admit_src(manifest ++ src)` in `tests/test_selfhost_lower.py`) and
`selfhost/compile.rvl` already threads a manifest through its own `admit_into`.
What was missing on rust was the BINDING, so a rust embed could not ask the
question the py loop (`bench/inprocess_gate_harness.py`) already asks.

`admit_into(source, manifest)` is therefore a real arm, not a stub: both inputs
go to the native fold and what that fold decides is what the crate reports. It
closes the G2/G3 legs of ambient admission — provision disjointness, route
realms, cross-manifest acyclicity — and NOTHING ELSE. The manifest wire carries
the two row kinds the landed wave covers (provision `C/k/r`, requirement `C<k`)
plus the `!halted` header; every OTHER row kind the wire reserves, the
replacement (`-C`) and handoff (`C=k:T`) rows, is REFUSED as `MANIFEST` rather
than ignored, because those waves land with the type layer. It issues no
admission either: a fold verdict is mapped through the same admission-free arms,
so `to_json` still emits `"admitted": false` everywhere.

Usage
-----
    python3 tools/build_gate_crate.py            # (re)generate crates/revl-gate
    python3 tools/build_gate_crate.py --check    # drift gate: fail on any diff
    python3 tools/build_gate_crate.py --out DIR  # generate elsewhere
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# ------------------------------------------------------------------ inputs

# The self-host root whose `admit_src` IS the native gate, and its `use` closure.
# Ordered, because the digest below is order-sensitive.
SELFHOST_ROOT = "selfhost/lower.rvl"
SELFHOST_CLOSURE = ("selfhost/lexer.rvl", "selfhost/parser.rvl", "selfhost/lower.rvl")

# Every input whose content decides a generated byte. Editing any of these
# without regenerating reds the drift gate — which is the point.
DIGEST_INPUTS = (
    *SELFHOST_CLOSURE,
    "backends/rust/emit.py",
    "src/revl/lexer.py",
    "src/revl/typecheck.py",
    "tools/build_gate_crate.py",
)

# The semver of the GATE SURFACE, kept in lockstep with `revl.gate`'s
# `GATE_API_VERSION` (asserted in `generate`, so the two cannot drift apart).
GATE_API_VERSION = "1.0.0"

# The semver of the NAVIGATION surface (`revl_gate::symbols`, item 336 slice 2).
# Independent of the gate api above: it issues no verdicts, and unlike the
# admission surface it has no `revl.gate` twin to stay in lockstep with.
SYMBOLS_API_VERSION = "0.1.0"

# The crate's own version. Independent of the language version, per the design's
# versioning split (`api` is bumped by surface changes only).
CRATE_VERSION = "0.1.0"

# A source larger than this is refused as OutsideFrontier rather than handed to
# the native gate. The emitted parser/checker are deeply recursive and a stack
# exhaustion is an ABORT, which no `catch_unwind` can turn back into a refusal;
# a bound that no corpus program comes near is the honest way to keep the
# fail-closed promise true.
MAX_SOURCE_BYTES = 256 * 1024

# A manifest row wire is folded by `parse_manifest_rows` (`selfhost/lower.rvl`),
# which recurses ONE STACK FRAME PER `;`-SEPARATED ROW. The byte bound above is
# therefore not a stack bound, and the manifest arm proved it: 2_700 rows of
# `A/b/;` are 13 KB, far under MAX_SOURCE_BYTES, and abort a 1 MiB stack, while
# 20_100 rows (100 KB) abort an 8 MiB one. Measured release thresholds are
# ~2_600 rows on 1 MiB and ~20_000 rows on 8 MiB. This is the bound that
# actually bounds the fold: it is checked BEFORE the wire reaches the parser,
# and it is set under half the smallest measured threshold (the wasm crate sets
# no `stack-size`, so the toolchain's 1 MiB default is the floor and it cannot
# be measured in this tree, since wasm32-unknown-unknown is not installed),
# because a stack exhaustion ABORTS and no `catch_unwind` can turn an abort back
# into a refusal.
MANIFEST_ROW_LIMIT = 512

# A source with more than this many items at ONE bracket level is refused as
# OutsideFrontier rather than handed to the native gate. This is a THIRD kind of
# bound, and neither of the two above is it: the emitted parser recurses once
# per SIBLING item, so the aborting class is FLAT expression-level recursion,
# which costs almost no bytes and no depth at all. Measured on the committed
# crate, release: `g(1, 1, ...)` with 11_386 arguments is a 34 KB source, one
# line at depth one, and it aborts a stock 8 MiB main thread; at the 1 MiB stack
# floor the wasm component runs at (the component build sets no `stack-size`)
# the same shape goes down at ~1_400 items, in a 4 KB source. The shape is not
# argument-specific -- `[1, 1, ...]` and a run of `let` statements abort at the
# same counts -- and blank or `//`-comment lines between those statements change
# nothing, because the cost is per item PARSED and not per line. That last
# measurement is why this counts newlines as separators: a statement run has no
# `,` or `;` to count, and counting the line ends is the conservative direction
# (a comment-heavy source can be declined; a dense one cannot be waved through).
#
# The value is set between the largest program this gate DECIDES and the
# smallest measured threshold: `selfhost/checker.rvl` is 604 items, and 1_024
# leaves it 1.7x of headroom under a 1_400-item floor. It is also chosen so that
# it changes no verdict on the whole census corpus (648 cases): the only source
# above it, `selfhost/lower.rvl` at 3_691 items, is already refused by the byte
# bound, which is checked first. A stack exhaustion ABORTS and no `catch_unwind`
# can turn an abort back into a refusal, so a bound no corpus program comes near
# is the honest way to keep the fail-closed promise true.
MAX_LEVEL_ITEMS = 1024

# What the native gate actually decides, in one line, stamped into the crate's
# `COVERED_LAYER`, its README and its provenance so the three cannot disagree.
# Measured, not assumed: `selfhost/lower.rvl`'s `admit_src` runs no type layer,
# so `fn f() -> Int { return "s" }` (which the reference refuses) draws no
# objection from it. That measurement is why the crate ships no admission.
COVERED_LAYER = ("composition + guarantee layer (G1..G4, A1, PRELUDE) and "
                 "parse (BAD); NOT the reference type layer")

# What the gate is willing to ADMIT, in one line, stamped into the crate's
# `ADMITTED_LAYER`, its README and its provenance from this one constant.
#
# The admission arm is not the covered layer read optimistically: it is a far
# SMALLER region, the one where the covered layer is the WHOLE question. A source
# that declares only service method signatures and scalar type aliases carries no
# term the reference type layer decides — no body, no expression, no generic
# head, no alias off the scalar set — so `admit_src` raising no objection to it
# is not a partial answer but the complete one. Measured, not assumed: 3_155
# certified programs drawn over this surface, every one admitted by the
# reference (`tests/test_gate_reference_census.py`).
ADMITTED_LAYER = ("interface declarations only: service method signatures and "
                  "scalar type aliases, over a closed scalar type vocabulary; "
                  "no term the reference type layer decides")


def _load_module(relpath: str, name: str):
    """Load a repo file by path, the way the backends' own tests load them, so
    what we emit with is the file under comparison rather than a re-export."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------- the frontier tables
#
# Derived from BOTH compilers, never hand-listed. The reference side comes from
# the reference compiler's own tables (an import, so it cannot go stale); the
# self-host side is read out of the self-host sources with anchored regexes that
# RAISE when the anchor is gone, because a silently-empty self-host set would
# widen the excluded table (safe) while a silently-empty reference set would
# empty it (unsafe) — only the first failure mode is tolerable, and neither is
# accepted quietly.


def _selfhost_keywords() -> set[str]:
    """The keyword set `selfhost/lexer.rvl`'s `keywords()` returns."""
    src = (ROOT / "selfhost/lexer.rvl").read_text(encoding="utf-8")
    match = re.search(r"fn keywords\(\) -> List\[Str\] \{(.*?)\n\}", src, re.S)
    if match is None:
        raise SystemExit(
            "build_gate_crate: cannot find `fn keywords()` in selfhost/lexer.rvl; "
            "the frontier table would be wrong, refusing to generate")
    words = set(re.findall(r'"([^"\\]*)"', match.group(1)))
    if "service" not in words or len(words) < 20:
        raise SystemExit(
            f"build_gate_crate: selfhost keyword extraction looks broken "
            f"({len(words)} words); refusing to generate")
    return words


def _selfhost_builtin_methods() -> set[str]:
    """The method names `selfhost/lower.rvl`'s `is_builtin_method` accepts —
    lower.py's `_BUILTIN_METHODS` twin, the set whose call lowers to a `builtin`
    node rather than a plain call."""
    src = (ROOT / "selfhost/lower.rvl").read_text(encoding="utf-8")
    match = re.search(r"fn is_builtin_method\(m: Str\) -> Bool \{(.*?)\n\}", src, re.S)
    if match is None:
        raise SystemExit(
            "build_gate_crate: cannot find `fn is_builtin_method` in "
            "selfhost/lower.rvl; the frontier table would be wrong, refusing "
            "to generate")
    names = set(re.findall(r'm == "([^"\\]*)"', match.group(1)))
    if "length" not in names or len(names) < 15:
        raise SystemExit(
            f"build_gate_crate: selfhost builtin extraction looks broken "
            f"({len(names)} names); refusing to generate")
    return names


def frontier_tables() -> dict[str, list[str]]:
    """`{"keywords": [...], "builtins": [...]}` — the reference constructs the
    self-host gate does not cover. Sorted, so the generated bytes are stable."""
    from revl.lexer import KEYWORDS  # noqa: PLC0415
    from revl.typecheck import _BUILTIN_SIG  # noqa: PLC0415

    return {
        "keywords": sorted(set(KEYWORDS) - _selfhost_keywords()),
        "builtins": sorted(set(_BUILTIN_SIG) - _selfhost_builtin_methods()),
    }


# ------------------------------------------------------ the admission tables
#
# The ADMISSION SURFACE is the mirror image of the frontier table. The frontier
# says where this gate may not REFUSE; the admission surface says where it may
# ADMIT — the far smaller region in which a no-objection from the
# composition/guarantee layer is the WHOLE answer, because the source carries no
# term the reference type layer decides.
#
# The tables are derived from the reference compiler for the same reason the
# frontier's are: the surface is defined by what the REFERENCE would check, and a
# hand-listed copy of that would be free to go stale in the unsafe direction.


def admission_tables() -> dict[str, list[str]]:
    """`{"scalars": [...], "reserved": [...], "keywords": [...]}` — the closed
    vocabularies the admission surface is written over, sorted so the generated
    bytes are stable.

    * ``scalars`` — the type names a certified signature may mention, taken from
      the reference's own set of scalar data types
      (`revl.typecheck._CONFIG_DATA_SCALARS`). Every one of them is a concrete
      builtin with no type parameter and no erasure, so a signature written over
      them resolves without a checker: no generic head to instantiate, no `Any`
      to erase, no alias to follow off the surface.
    * ``reserved`` — the type names a certified source may not DECLARE
      (`revl.typecheck._BUILTIN_TYPE_NAMES` plus the reserved opaque
      `Principal`). Shadowing one of these is the reference's business, not this
      gate's, so a source that tries is not certified.
    * ``keywords`` — the reference keyword set (`revl.lexer.KEYWORDS`). A
      certified source's identifiers are checked against it, so the certifier
      cannot mistake a keyword it does not know for a name.
    """
    from revl.lexer import KEYWORDS  # noqa: PLC0415
    from revl.typecheck import (  # noqa: PLC0415
        _BUILTIN_TYPE_NAMES, _CONFIG_DATA_SCALARS, _GENERIC_ARITY, PRINCIPAL)

    scalars = sorted(_CONFIG_DATA_SCALARS)
    reserved = sorted(set(_BUILTIN_TYPE_NAMES) | {PRINCIPAL})
    keywords = sorted(KEYWORDS)
    # Anchored the way the frontier extraction is. A scalar table that picked up
    # a generic head, or that lost the names it is written over, would generate a
    # certifier that admits signatures the reference still has work to do on —
    # the one direction this crate may never drift in.
    missing = {"Int", "Str", "Bool"} - set(scalars)
    if missing or set(scalars) & set(_GENERIC_ARITY):
        raise SystemExit(
            f"build_gate_crate: the admission scalar table looks broken "
            f"({scalars!r}); refusing to generate")
    if not set(scalars) <= set(_BUILTIN_TYPE_NAMES):
        raise SystemExit(
            f"build_gate_crate: an admission scalar is not a reference builtin "
            f"type name ({sorted(set(scalars) - set(_BUILTIN_TYPE_NAMES))!r}); "
            f"refusing to generate")
    if "service" not in keywords or len(keywords) < 20:
        raise SystemExit(
            f"build_gate_crate: the reference keyword table looks broken "
            f"({len(keywords)} words); refusing to generate")
    return {"scalars": scalars, "reserved": reserved, "keywords": keywords}


def admission_surface_id(digest: str) -> str:
    """`ADMISSION_SURFACE_ID` — an identifier of the region this gate is willing
    to ADMIT in, versioned separately from `frontier_id` because the two answer
    different questions: the frontier bounds the refusals, the admission surface
    bounds the admissions. A consumer caching an admission compares this before
    trusting it against a gate built from another tree."""
    return f"admission-interface:{digest[:16]}"


# ------------------------------------------------------ the IR-boundary tables
#
# The known top-level fields and schema revisions of a staged IR document
# (roadmap item 479), DERIVED from the reference frontend's own authority
# (`revl.lower.IR_TOPLEVEL_FIELDS` / `IR_SCHEMA_REVISIONS`) rather than
# hand-listed. The crate's `ir` module refuses an unknown field / unknown
# revision at the IR boundary by name, exactly as `revl.compiler` does; deriving
# the tables from the frontend is what keeps the two tiers from skewing — a
# field or revision added on the Python side without regenerating this crate
# changes the embedded bytes and reds the drift gate in the same wave.


def ir_tables() -> dict:
    """`{"fields": [...], "revisions": [...]}` — the top-level surface and the
    schema revisions a staged IR document may carry, sorted so the generated
    bytes are stable. Read from the reference frontend, so the crate cannot list
    a field the frontend does not (or omit one it does)."""
    from revl.lower import IR_SCHEMA_REVISIONS, IR_TOPLEVEL_FIELDS  # noqa: PLC0415

    fields = sorted(IR_TOPLEVEL_FIELDS)
    # Anchored the way the frontier extraction is: a silently-empty or
    # obviously-wrong set would generate a crate that refuses (or ignores) the
    # wrong shapes, so refuse to generate instead.
    if "ir_version" not in fields or "manifest" not in fields or len(fields) < 8:
        raise SystemExit(
            f"build_gate_crate: IR top-level field extraction looks broken "
            f"({len(fields)} fields); refusing to generate")
    revisions = sorted(IR_SCHEMA_REVISIONS)
    if not revisions or any(not isinstance(r, int) or isinstance(r, bool)
                            for r in revisions):
        raise SystemExit(
            f"build_gate_crate: IR schema revisions look broken ({revisions!r}); "
            f"the crate emits them as an `&[i64]`, refusing to generate")
    return {"fields": fields, "revisions": revisions}


# --------------------------------------------------------------- provenance


def source_digest() -> str:
    """A sha256 over every input that decides a generated byte, as
    `<relpath>\\0<bytes>` records in DIGEST_INPUTS order. This, not the git sha,
    is what the crate stamps: a committed crate whose provenance were the commit
    sha would be stale the instant it was committed, and the drift gate needs a
    value that is a pure function of the tree."""
    digest = hashlib.sha256()
    for rel in DIGEST_INPUTS:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def language_version() -> str:
    """The revl language/package version this gate admits (`gate_version()
    .language`), read out of `pyproject.toml`.

    Deliberately NOT `importlib.metadata.version("revl")` (what `revl.gate`
    uses at runtime): the crate is a committed artifact, so the value stamped
    into it has to be a pure function of the TREE, not of whatever wheel
    happens to be installed on the generating machine. The whole file is not a
    digest input — the version it yields is already stamped into the generated
    bytes, so a version bump reds the drift gate on its own, while an unrelated
    `[project]` edit does not."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit(
            "build_gate_crate: no `version = \"...\"` in pyproject.toml; the "
            "crate cannot stamp a language version it did not read")
    return match.group(1)


def frontier_id(digest: str) -> str:
    """`gate_version().frontier` — an identifier of the COVERED surface, so an
    embedder (and item 337's seam re-admission) can detect that two gates cover
    different surfaces before trusting their agreement. Two gates generated from
    different self-host sources have different ids by construction."""
    return f"selfhost-admit:{digest[:16]}"


# ------------------------------------------------------------ rust codegen
#
# Every rust template below is a RAW python string with `@NAME@` placeholders,
# so what is typed here is byte-for-byte what lands in the crate: no brace
# doubling, no escape laundering between the two languages.


def _rust_str_array(name: str, values: list[str], doc: str) -> str:
    if values:
        items = "\n".join(f'    "{v}",' for v in values)
        body = f"&[\n{items}\n]"
    else:
        body = "&[]"
    return f"{doc}pub(crate) const {name}: &[&str] = {body};\n"


def _rust_pub_str_array(name: str, values: list[str], doc: str) -> str:
    """A `pub const &[&str]` — the IR-boundary tables are part of the crate's
    public surface (a consumer may want to read the known fields), unlike the
    crate-private frontier table."""
    if values:
        items = "\n".join(f'    "{v}",' for v in values)
        body = f"&[\n{items}\n]"
    else:
        body = "&[]"
    return f"{doc}pub const {name}: &[&str] = {body};\n"


def _rust_pub_i64_array(name: str, values: list[int], doc: str) -> str:
    if values:
        items = "\n".join(f"    {v}," for v in values)
        body = f"&[\n{items}\n]"
    else:
        body = "&[]"
    return f"{doc}pub const {name}: &[i64] = {body};\n"


ADMISSION_RS_TEMPLATE = r'''//! The ADMISSION SURFACE — GENERATED by `tools/build_gate_crate.py`.
//! Do not edit; edit the generator and regenerate (`--check` is a CI gate).
//!
//! # The question this module answers
//!
//! `frontier.rs` bounds where this gate may REFUSE. This module bounds the far
//! smaller region where it may ADMIT.
//!
//! The two bounds are not the same shape and must not be confused. The native
//! gate decides the composition/guarantee layer and runs no type layer, so over
//! the language at large a no-objection from it is a PARTIAL answer: the
//! reference may still refuse the same bytes in a layer this gate never ran.
//! That is why [`crate::Verdict`] has no admitting arm and why
//! [`crate::Verdict::NoObjection`] is never a green.
//!
//! There is a region, though, in which the covered layer is the WHOLE question:
//! a source that declares nothing but service method signatures and scalar type
//! aliases carries NO TERM the reference type layer decides. No function body,
//! no expression, no literal, no generic head to instantiate, no alias pointing
//! off the scalar vocabulary. For such a source, "the composition/guarantee
//! gate found nothing to refuse" and "the reference admits this" are the same
//! statement, and the gate may say so.
//!
//! `certify` is the decision procedure for that region. It returns `Some(basis)`
//! only when it has walked the ENTIRE source and accounted for every token; a
//! single byte it cannot place returns `None`, and `None` means the caller falls
//! back to the refusal surface. It never partially certifies, and it never skips
//! what it does not understand — skipping is the wave-through this crate exists
//! to prevent.
//!
//! # What is deliberately NOT in the surface
//!
//! Everything that carries a term: `fn` bodies, `component`, `provide`, `realm`,
//! `use`, `pub`, attributes, literals, record and generic types, aliases of
//! aliases. A source holding any of them is not certified, which costs a caller
//! nothing but the fallback to `NoObjection` — the direction this crate is
//! allowed to err in.
//!
//! # The manifest half
//!
//! [`certify_into`] is deliberately far narrower still, and the reason is a
//! property of the item-186 row wire rather than a gap in this module: the wire
//! carries component names, provision keys and realms, and NO SERVICE SHAPES.
//! So a candidate declaring `service Store { ... }` cannot be certified against
//! a running composition, because the running composition may already hold a
//! DIFFERENT `Store` and the wire cannot say. Measured, not assumed: the
//! reference refuses that exact pair with "service `Store` differs from the
//! running manifest". A candidate that declares nothing can be certified, and
//! nothing else can, until the wire carries the running shapes.

@SCALAR_TYPES@
@RESERVED_TYPE_NAMES@
@REFERENCE_KEYWORDS@
/// An identifier of the region this gate is willing to ADMIT in. Versioned
/// apart from [`crate::FRONTIER_ID`]: the frontier bounds the refusals, this
/// bounds the admissions, and a consumer caching an admission compares THIS
/// before trusting it against a gate built from another tree.
pub(crate) const SURFACE_ID: &str = "@ADMISSION_SURFACE_ID@";

/// The tail every certificate carries, so the two halves of the basis line
/// cannot drift apart.
const BASIS_TAIL: &str =
    "no term the reference type layer decides, and the composition/guarantee gate raised no objection";

/// What a certified source turned out to contain. Counts only: the certificate
/// is evidence that the walk ACCOUNTED for the whole source, and the counts are
/// what make that evidence readable.
struct Shape {
    services: usize,
    aliases: usize,
    methods: usize,
}

fn is_ident_start(byte: u8) -> bool {
    byte.is_ascii_alphabetic() || byte == b'_'
}

fn is_ident_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

/// The source as tokens, or `None` when it holds a byte outside the surface's
/// alphabet.
///
/// The alphabet is the point. A string literal, a number, an `@attribute`, an
/// operator, a `[`, a non-ASCII byte — every one of them returns `None` here,
/// which is how "carries no term the type layer decides" is enforced at the
/// bottom rather than argued about at the top. Comments and whitespace are the
/// only things dropped.
fn tokens(source: &str) -> Option<Vec<&str>> {
    let bytes = source.as_bytes();
    let n = bytes.len();
    let mut out: Vec<&str> = Vec::new();
    let mut i = 0usize;
    while i < n {
        let byte = bytes[i];
        if byte == b' ' || byte == b'\t' || byte == b'\r' || byte == b'\n' {
            i += 1;
            continue;
        }
        if byte == b'/' && i + 1 < n && bytes[i + 1] == b'/' {
            while i < n && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if is_ident_start(byte) {
            let start = i;
            i += 1;
            while i < n && is_ident_byte(bytes[i]) {
                i += 1;
            }
            out.push(&source[start..i]);
            continue;
        }
        if byte == b'-' && i + 1 < n && bytes[i + 1] == b'>' {
            out.push("->");
            i += 2;
            continue;
        }
        out.push(match byte {
            b'{' => "{",
            b'}' => "}",
            b'(' => "(",
            b')' => ")",
            b',' => ",",
            b':' => ":",
            b'=' => "=",
            _ => return None,
        });
        i += 1;
    }
    Some(out)
}

/// A token usable as a declared or bound NAME: an identifier that is not a
/// reference keyword. Checked against the REFERENCE keyword set, not the
/// self-host one, because the question is what the reference would make of the
/// source.
fn is_name(token: &str) -> bool {
    match token.as_bytes().first() {
        Some(&byte) if is_ident_start(byte) => !REFERENCE_KEYWORDS.contains(&token),
        _ => false,
    }
}

/// A name this source may DECLARE: a name that does not shadow a reference
/// builtin type or the reserved opaque `Principal`.
fn is_declarable(token: &str) -> bool {
    is_name(token) && !RESERVED_TYPE_NAMES.contains(&token)
}

fn has_duplicate(names: &[&str]) -> bool {
    for (i, name) in names.iter().enumerate() {
        if names[i + 1..].contains(name) {
            return true;
        }
    }
    false
}

/// Walk the whole source, or refuse to certify it.
///
/// The walk is total by construction: every iteration either consumes a token
/// and advances, or returns `None`. There is no "skip what I do not recognise"
/// branch, which is the property that makes the certificate mean something.
fn shape_of(source: &str) -> Option<Shape> {
    let toks = tokens(source)?;
    let n = toks.len();

    // Pass one: the alias names, so a signature may name an alias declared
    // further down the file. Pass two validates every one of them, so a name
    // collected here that is not a real alias declaration still fails below.
    let mut aliases: Vec<&str> = Vec::new();
    for (i, tok) in toks.iter().enumerate() {
        if *tok == "type" && i + 1 < n {
            aliases.push(toks[i + 1]);
        }
    }
    let known = |name: &str| SCALAR_TYPES.contains(&name) || aliases.contains(&name);

    let mut services: Vec<&str> = Vec::new();
    let mut methods_total = 0usize;
    let mut declared_aliases = 0usize;
    let mut i = 0usize;
    while i < n {
        if toks[i] == "type" {
            // `type <Name> = <Scalar>` — the right-hand side is a SCALAR and
            // never another alias, so the alias graph is one level deep and a
            // cycle (which the reference decides, and this gate does not) is
            // unrepresentable rather than checked.
            if i + 3 >= n || toks[i + 2] != "=" {
                return None;
            }
            if !is_declarable(toks[i + 1]) || !SCALAR_TYPES.contains(&toks[i + 3]) {
                return None;
            }
            declared_aliases += 1;
            i += 4;
            continue;
        }
        if toks[i] != "service" || i + 2 >= n {
            return None;
        }
        if !is_declarable(toks[i + 1]) || toks[i + 2] != "{" {
            return None;
        }
        services.push(toks[i + 1]);
        i += 3;
        let mut methods: Vec<&str> = Vec::new();
        while i < n && toks[i] != "}" {
            if toks[i] != "fn" || i + 1 >= n || !is_name(toks[i + 1]) {
                return None;
            }
            methods.push(toks[i + 1]);
            i += 2;
            if i >= n || toks[i] != "(" {
                return None;
            }
            i += 1;
            let mut params: Vec<&str> = Vec::new();
            while i < n && toks[i] != ")" {
                if !is_name(toks[i]) || i + 2 >= n || toks[i + 1] != ":" {
                    return None;
                }
                if !known(toks[i + 2]) {
                    return None;
                }
                params.push(toks[i]);
                i += 3;
                if i < n && toks[i] == "," {
                    i += 1;
                }
            }
            if i >= n || has_duplicate(&params) {
                return None;
            }
            i += 1; // the `)`
            if i < n && toks[i] == "->" {
                if i + 1 >= n || !known(toks[i + 1]) {
                    return None;
                }
                i += 2;
            }
            methods_total += 1;
        }
        if i >= n || has_duplicate(&methods) {
            return None;
        }
        i += 1; // the `}`
    }
    // The reference refuses a duplicate service and a duplicate method
    // (`duplicate service `A``, `duplicate method `f` in service A`) and the
    // native gate does not, so the certifier carries those two obligations
    // itself. Without them the surface would admit two programs the reference
    // refuses — measured, which is why they are here and not assumed away.
    if has_duplicate(&services) || has_duplicate(&aliases) {
        return None;
    }
    if declared_aliases != aliases.len() {
        return None;
    }
    for alias in &aliases {
        if services.contains(alias) {
            return None;
        }
    }
    Some(Shape {
        services: services.len(),
        aliases: aliases.len(),
        methods: methods_total,
    })
}

/// `Some(basis)` when `source` is inside the admission surface, `None`
/// otherwise. The basis is the certificate's why-trace, for a log or a receipt;
/// it is NOT on the admission wire, which is byte-identical to `revl.gate`'s.
pub(crate) fn certify(source: &str) -> Option<String> {
    let shape = shape_of(source)?;
    Some(format!(
        "admission surface {}: services={} aliases={} methods={}; {}",
        SURFACE_ID, shape.services, shape.aliases, shape.methods, BASIS_TAIL
    ))
}

/// `Some(basis)` when `source` may be admitted INTO the running composition
/// `manifest`, `None` otherwise.
///
/// Narrow, and the reason is the wire rather than this module. An item-186 row
/// carries a component name, a provision key and a realm; it does NOT carry the
/// running composition's service shapes. A candidate declaring
/// `service Store { ... }` may therefore collide with a `Store` the running
/// composition already holds in a different shape, and the reference refuses
/// exactly that ("service `Store` differs from the running manifest") where this
/// gate cannot even see it. So against a NON-EMPTY manifest only a candidate
/// that declares nothing at all is certified.
///
/// The empty manifest is the empty composition, and `crate::issue_admission_into`
/// routes it to `crate::issue_admission` before this is reached.
pub(crate) fn certify_into(source: &str, manifest: &str) -> Option<String> {
    let shape = shape_of(source)?;
    if shape.services > 0 || shape.aliases > 0 {
        return None;
    }
    let (provisions, requirements) = manifest_shape(manifest)?;
    Some(format!(
        "admission surface {}: the candidate declares nothing, and the running composition's {} provision rows resolve its {} requirement rows; {}",
        SURFACE_ID, provisions, requirements, BASIS_TAIL
    ))
}

/// `(provisions, requirements)` for a manifest wire every one of whose rows the
/// admission surface can account for, `None` otherwise.
///
/// Two obligations, both of them the wire's own: every row is a provision
/// (`C/k/r`) or a requirement (`C<k`) — a `!halted` header, a replacement or a
/// handoff row is not certifiable here even though the fold has its own answer
/// for it — and every requirement key is provided by a provision row in the same
/// wire. The second is what stops a bogus wire from being admitted into: the
/// running composition is supposed to be one the reference already admitted, and
/// a dangling requirement says it is not.
fn manifest_shape(manifest: &str) -> Option<(usize, usize)> {
    let mut provided: Vec<&str> = Vec::new();
    let mut required: Vec<&str> = Vec::new();
    for row in manifest.split(';') {
        if row.is_empty() {
            return None;
        }
        if let Some((component, key)) = row.split_once('<') {
            if component.is_empty() || key.is_empty() {
                return None;
            }
            if component.contains('/') || key.contains('/') || key.contains('=') {
                return None;
            }
            if !is_name(component) || !is_name(key) {
                return None;
            }
            required.push(key);
            continue;
        }
        let mut parts = row.splitn(3, '/');
        let component = parts.next()?;
        let key = parts.next()?;
        let realm = parts.next()?;
        if component.is_empty() || key.is_empty() {
            return None;
        }
        if !is_name(component) || !is_name(key) {
            return None;
        }
        if !realm.is_empty() && !is_name(realm) {
            return None;
        }
        provided.push(key);
    }
    for key in &required {
        if !provided.contains(key) {
            return None;
        }
    }
    Some((provided.len(), required.len()))
}

#[cfg(test)]
mod tests {
    use super::*;

    const INTERFACE: &str = "service Store {\n  fn get(key: Str) -> Str\n  fn put(key: Str, value: Str)\n}\n";

    #[test]
    fn an_interface_only_source_is_certified() {
        let basis = certify(INTERFACE).expect("an interface-only source is inside the surface");
        assert!(basis.contains("services=1"), "{}", basis);
        assert!(basis.contains("methods=2"), "{}", basis);
        assert!(basis.contains(SURFACE_ID), "{}", basis);
    }

    #[test]
    fn the_empty_source_is_the_empty_composition_and_is_certified() {
        for source in ["", "   \n", "// just a note\n"] {
            let basis = certify(source).expect("a source with no declarations is admissible");
            assert!(basis.contains("services=0 aliases=0 methods=0"), "{}", basis);
        }
    }

    #[test]
    fn a_scalar_alias_is_certified_and_usable_in_a_signature() {
        let source = "type Key = Str\nservice S {\n  fn get(k: Key) -> Key\n}\n";
        assert!(certify(source).is_some());
    }

    #[test]
    fn an_alias_of_an_alias_is_not_certified() {
        // The alias graph is one level deep by construction, so a cycle cannot
        // be written rather than having to be detected.
        assert!(certify("type A = Str\ntype B = A\n").is_none());
    }

    #[test]
    fn a_term_of_any_kind_leaves_the_surface() {
        for source in [
            "fn id(x: Int) -> Int { return x }",
            "component C provides s: S {\n  provide s {\n    fn f(x) = x\n  }\n}\n",
            "service S {\n  fn f(x: Str) -> Str\n}\nfn g() -> Int { return 1 }",
            "type R = { id: Int }",
            "service S {\n  fn f(x: List[Int]) -> Int\n}\n",
            "use \"./other.rvl\" { S }\n",
            "pub service S {\n  fn f(x: Int) -> Int\n}\n",
        ] {
            assert!(certify(source).is_none(), "must not certify: {:?}", source);
        }
    }

    #[test]
    fn the_two_obligations_the_native_gate_does_not_carry() {
        // The reference refuses both of these and `admit_src` raises no
        // objection to either, so the certifier has to decide them itself or the
        // surface would issue an admission the reference refuses.
        assert!(certify("service A {\n  fn f(x: Int) -> Int\n}\nservice A {\n  fn g(x: Int) -> Int\n}\n").is_none());
        assert!(certify("service A {\n  fn f(x: Int) -> Int\n  fn f(y: Int) -> Int\n}\n").is_none());
    }

    #[test]
    fn a_declaration_may_not_shadow_a_reference_builtin_type() {
        for source in ["service Int {\n}\n", "type Opt = Str\n", "type Principal = Str\n"] {
            assert!(certify(source).is_none(), "must not certify: {:?}", source);
        }
    }

    #[test]
    fn a_type_outside_the_scalar_vocabulary_leaves_the_surface() {
        assert!(certify("service S {\n  fn f(x: Unknown) -> Int\n}\n").is_none());
        assert!(certify("service S {\n  fn f(x: Any) -> Int\n}\n").is_none());
    }

    #[test]
    fn a_non_ascii_byte_leaves_the_surface() {
        assert!(tokens("service Ünicode {}").is_none());
        assert!(certify("service S {\n  fn f(x: Str) -> Str // caf\u{e9}\n}\n").is_some());
    }

    #[test]
    fn a_duplicate_parameter_name_leaves_the_surface() {
        assert!(certify("service S {\n  fn f(x: Int, x: Int) -> Int\n}\n").is_none());
    }

    #[test]
    fn an_unterminated_declaration_leaves_the_surface() {
        for source in ["service S {", "service S {\n  fn f(x: Int\n}", "type A =", "service"] {
            assert!(certify(source).is_none(), "must not certify: {:?}", source);
        }
    }

    // The manifest half.

    #[test]
    fn a_declaration_free_candidate_is_certified_into_a_running_composition() {
        let basis = certify_into("// nothing to add\n", "Kv/store/;App/app/;App<store")
            .expect("a candidate that declares nothing cannot collide with the running shapes");
        assert!(basis.contains("2 provision rows"), "{}", basis);
        assert!(basis.contains("1 requirement rows"), "{}", basis);
    }

    #[test]
    fn an_interface_candidate_is_not_certified_into_a_running_composition() {
        // The wire carries no service shapes, so a declared `Store` may or may
        // not be the running one and this gate cannot tell. Not certified.
        assert!(certify(INTERFACE).is_some());
        assert!(certify_into(INTERFACE, "Kv/store/").is_none());
    }

    #[test]
    fn a_row_the_surface_cannot_account_for_is_not_certified_into() {
        for wire in ["!halted", "Kv/store/;-Kv/store/", "Kv/store=Int", "!wat", "Kv/store/;"] {
            assert!(certify_into("", wire).is_none(), "must not certify into {:?}", wire);
        }
    }

    #[test]
    fn a_dangling_requirement_row_is_not_certified_into() {
        assert!(certify_into("", "App/app/;App<store").is_none());
        assert!(certify_into("", "Kv/store/;App<store").is_some());
    }
}
'''


FRONTIER_RS_TEMPLATE = r'''//! The covered-surface guard — GENERATED by `tools/build_gate_crate.py`.
//! Do not edit; edit the generator and regenerate (`--check` is a CI gate).
//!
//! The self-host compiler this crate is built from is BEHIND the reference
//! implementation (roadmap item 391). This module is the honest boundary of
//! that gap: a source touching anything in the derived tables below is reported
//! `OutsideFrontier` and is NEVER admitted.
//!
//! Scope, stated so nobody over-reads it: this is a LEXICAL guard over the
//! constructs the two compilers demonstrably disagree about at the token level.
//! It is not a proof of agreement — agreement on the covered surface is
//! evidence (the differential corpus in `tests/test_gate_crate_admit.py`, plus
//! the self-host oracles) together with the release discipline that a
//! reference-side admission change lands in the self-host gate in the same
//! wave. What this guard buys is that the KNOWN gaps cannot be walked into
//! silently, and that a newly-opened gap reds the drift gate.

/// The identifier `gate_version().frontier` reports. Two gates with different
/// ids cover different surfaces and their agreement means nothing.
pub const FRONTIER_ID: &str = "@FRONTIER_ID@";

/// Sources above this many bytes are refused rather than decided: the emitted
/// parser/checker are deeply recursive and a stack exhaustion ABORTS, which no
/// `catch_unwind` can turn back into a refusal. A bound no corpus program comes
/// near keeps the fail-closed promise honest.
pub const MAX_SOURCE_BYTES: usize = @MAX_SOURCE_BYTES@;

/// Manifest row wires above this many `;`-separated rows are refused rather
/// than folded, for the same reason [`MAX_SOURCE_BYTES`] refuses a source: the
/// fold recurses one stack frame per row and a stack exhaustion ABORTS, which
/// no `catch_unwind` can turn back into a refusal. The two limits are NOT the
/// same kind of bound: the byte limit above does not bound the manifest, since
/// 2_700 rows of `A/b/;` are 13 KB and already overflow a 1 MiB stack, and both
/// limits are checked before the wire reaches the parser so an embedder cannot
/// spend the host on either side of the door. A manifest wire no corpus comes
/// near keeps the fail-closed promise honest.
pub const MANIFEST_ROW_LIMIT: usize = @MANIFEST_ROW_LIMIT@;

/// Sources with more than this many items at ONE bracket level are refused
/// rather than decided, for the same reason [`MAX_SOURCE_BYTES`] refuses a
/// source: the emitted parser recurses once per SIBLING item and a stack
/// exhaustion ABORTS, which no `catch_unwind` can turn back into a refusal.
///
/// This is a THIRD kind of bound, and neither limit above is it. The aborting
/// class is flat expression-level recursion, which costs almost no bytes and no
/// depth: `g(1, 1, ...)` with 11_386 arguments is a 34 KB source, one line at
/// depth one, and it takes down a stock 8 MiB main thread. At the 1 MiB stack
/// floor the wasm component runs at, ~1_400 items is enough. Depth is NOT what
/// this measures -- the nesting bound inside `admit_src` covers that, and it
/// cannot see siblings by construction. A source no corpus program comes near
/// keeps the fail-closed promise honest.
pub const MAX_LEVEL_ITEMS: usize = @MAX_LEVEL_ITEMS@;

@EXCLUDED_KEYWORDS@
@EXCLUDED_BUILTINS@

/// A word-and-member scan over `source` with string literals and `//` comments
/// blanked out. Returns the first frontier gap found, or `None`.
///
/// Conservative by construction: `@py { ... }` host bodies are scanned like the
/// rest of the text, so a host body mentioning an excluded name costs a false
/// `OutsideFrontier`. That is the safe direction, and the only one this crate is
/// allowed to err in.
pub(crate) fn scan(source: &str) -> Option<String> {
    if source.len() > MAX_SOURCE_BYTES {
        return Some(format!(
            "source is {} bytes, above the {}-byte bound this gate will decide (the native front end is deeply recursive and an overflow aborts rather than refusing); compile it with the reference `revl` toolchain",
            source.len(),
            MAX_SOURCE_BYTES
        ));
    }
    let text = strip_literals(source);
    let items = level_items(&text);
    if items > MAX_LEVEL_ITEMS {
        return Some(format!(
            "source has {} items at one bracket level, above the {}-item bound this gate will decide (the native front end recurses once per sibling item and an overflow aborts rather than refusing); compile it with the reference `revl` toolchain",
            items,
            MAX_LEVEL_ITEMS
        ));
    }
    let bytes = text.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i] as char;
        if !(c.is_ascii_alphanumeric() || c == '_') {
            i += 1;
            continue;
        }
        let start = i;
        while i < bytes.len() {
            let c = bytes[i] as char;
            if c.is_ascii_alphanumeric() || c == '_' {
                i += 1;
            } else {
                break;
            }
        }
        let word = &text[start..i];
        // A member position is a word directly preceded by `.` (the optional
        // chain `?.` ends in the same byte, so it is covered too).
        let member = start > 0 && bytes[start - 1] == b'.';
        if member {
            if EXCLUDED_BUILTINS.contains(&word) {
                return Some(format!(
                    "`.{}()` is a reference stdlib builtin the self-host gate this crate is built from does not cover, so the two compilers would lower this program differently; use the reference `revl` toolchain for it",
                    word
                ));
            }
        } else if EXCLUDED_KEYWORDS.contains(&word) {
            return Some(format!(
                "`{}` is a reference language keyword outside this gate's covered surface; use the reference `revl` toolchain for it",
                word
            ));
        }
    }
    None
}

/// The largest number of sibling items found at any ONE bracket level of
/// `text`: an opening bracket starts a level, a closing bracket ends it, and
/// `,`, `;` and a newline each separate two items within the level they are
/// seen at, so a level holding N siblings reads N and not N-1 (the first item
/// is counted when the level opens).
///
/// This is the stack the emitted parser actually spends. Measured on the
/// generated crate, the aborting class is FLAT SIBLING recursion and it is not
/// argument-specific: `g(1, 1, ...)` with 11_386 arguments is a 34 KB source
/// that aborts a stock 8 MiB stack, `[1, 1, ...]` and a run of `let` statements
/// go down at the same counts, and blank or `//`-comment lines between those
/// statements change nothing (they cost no frames at all). Newlines are counted
/// because a statement run has no `,` or `;` to count; counting them is the
/// conservative direction, since a comment-heavy source can then be declined
/// and a dense one still cannot be waved through. Depth is deliberately NOT
/// measured here -- the nesting bound inside `admit_src` covers it, and it
/// cannot see siblings by construction: the shape that aborts is one line at
/// depth one.
fn level_items(text: &str) -> usize {
    let mut counts: Vec<usize> = vec![1];
    let mut worst = 1usize;
    for b in text.bytes() {
        match b {
            b'(' | b'[' | b'{' => counts.push(1),
            // An unbalanced closer cannot pop the outermost level, so the
            // count never indexes past an empty stack.
            b')' | b']' | b'}' => {
                if counts.len() > 1 {
                    counts.pop();
                }
            }
            b',' | b';' | b'\n' => {
                let last = counts.len() - 1;
                counts[last] += 1;
            }
            _ => continue,
        }
        let last = counts.len() - 1;
        if counts[last] > worst {
            worst = counts[last];
        }
    }
    worst
}

/// Blank out `"..."` string literals and `//` comments so their contents cannot
/// trigger the scan. Replaced with spaces rather than deleted so byte offsets,
/// and therefore the `.`-preceded member test, stay meaningful.
fn strip_literals(source: &str) -> String {
    let bytes = source.as_bytes();
    let mut out = String::with_capacity(source.len());
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'/' && i + 1 < bytes.len() && bytes[i + 1] == b'/' {
            while i < bytes.len() && bytes[i] != b'\n' {
                out.push(' ');
                i += 1;
            }
            continue;
        }
        if b == b'"' {
            out.push(' ');
            i += 1;
            while i < bytes.len() {
                if bytes[i] == b'\\' && i + 1 < bytes.len() {
                    out.push(' ');
                    out.push(' ');
                    i += 2;
                    continue;
                }
                let end = bytes[i] == b'"';
                out.push(' ');
                i += 1;
                if end {
                    break;
                }
            }
            continue;
        }
        // Non-ASCII bytes are copied through byte-wise; they can never start or
        // continue an ASCII word, so the scan is unaffected and offsets hold.
        out.push(b as char);
        i += 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_literal_cannot_trigger_the_scan() {
        assert_eq!(scan("fn f() -> Str { return \".is_digit\" }"), None);
        assert_eq!(scan("// .is_digit()\nfn f(x: Int) -> Int { return x }"), None);
    }

    #[test]
    fn an_excluded_builtin_in_member_position_is_a_gap() {
        // Guarded so the test still says something if the table is ever empty:
        // an empty table is a legitimate generation, not a broken one.
        if let Some(name) = EXCLUDED_BUILTINS.first() {
            let src = format!("fn f(x: Str) -> Bool {{ return x.{}() }}", name);
            assert!(scan(&src).is_some(), "expected a gap for .{}()", name);
        }
    }

    #[test]
    fn an_oversized_source_is_a_gap() {
        let big = "x".repeat(MAX_SOURCE_BYTES + 1);
        assert!(scan(&big).is_some());
    }

    #[test]
    fn too_many_items_at_one_level_is_a_gap() {
        // One line, ~14 KB, depth one: `nesting_depth` sees nothing to refuse
        // and the byte bound sees 5% of its budget, while the parser spends one
        // frame per argument. This is the shape that aborts a 1 MiB stack at
        // ~1_400 items.
        let flat = format!(
            "fn f() -> Int {{ return g({}) }}",
            vec!["1"; MAX_LEVEL_ITEMS + 1].join(", ")
        );
        assert!(flat.len() < MAX_SOURCE_BYTES / 10, "{}", flat.len());
        let reason = scan(&flat).expect("expected a level-items gap");
        assert!(reason.contains(&MAX_LEVEL_ITEMS.to_string()), "{}", reason);
    }

    #[test]
    fn the_level_bound_is_neither_a_byte_nor_a_depth_bound() {
        // A corpus-scale source: 300 sibling statements, each followed by a
        // blank line and a `//` comment. The line ends count (a statement run
        // has no `,` or `;`), which is the conservative direction, and the
        // source is still decided at 5% of the byte bound.
        let mut spaced = String::from("fn f() -> Int {\n");
        for i in 0..300 {
            spaced.push_str(&format!("  let a{} = 1\n\n  // note\n", i));
        }
        spaced.push_str("  return 0\n}\n");
        assert!(spaced.len() < MAX_SOURCE_BYTES / 10, "{}", spaced.len());
        assert_eq!(scan(&spaced), None);
        // And a deep-but-narrow source is the nesting bound's business, not
        // this one's.
        let deep = format!(
            "fn f() -> Int {{ return {}1{} }}",
            "(".repeat(150),
            ")".repeat(150)
        );
        assert_eq!(scan(&deep), None);
    }
}
'''


LIB_RS_TEMPLATE = r'''//! `revl-gate` — the revl admission gate as an embeddable rust library
//! (roadmap item 332, Stage 3; design `docs/design/332-embeddable-gate-api.md`).
//!
//! GENERATED by `tools/build_gate_crate.py` from the self-host compiler. Do not
//! edit by hand: CI regenerates from the same tree and fails on any byte
//! difference (`tests/test_gate_crate_drift.py`).
//!
//! # What this crate is
//!
//! Layer 1 of the gate API, admit-only: [`admit`] is a PURE function of its
//! argument — no disk, no clock, no live state, no cordis runtime boot —
//! returning the self-host compiler's verdict on a STANDALONE program. It is
//! `selfhost/lower.rvl::admit_src` (lex -> parse -> the composition/guarantee
//! gate) compiled to rust through the reference rust backend. [`admit_into`] is
//! the same gate reached across a composition boundary: the verdict on `source`
//! once it is admitted INTO a RUNNING manifest (item 186).
//!
//! ```no_run
//! use revl_gate::{admit, Verdict};
//!
//! match admit("fn id(x: Int) -> Int { return x }") {
//!     // A definitive refusal. Byte-agreeing with the reference compiler on
//!     // the covered corpus: stop here, and show the message as-is.
//!     Verdict::Refused { code, message } => println!("refused ({}): {}", code, message),
//!     // NOT an admission. See "The verdict surface issues no admissions"
//!     // below; ask `issue_admission` for a green.
//!     Verdict::NoObjection => println!("nothing this gate can refuse"),
//!     // The gate declined to decide at all.
//!     Verdict::OutsideFrontier { reason } => println!("undecided: {}", reason),
//! }
//! ```
//!
//! # The verdict surface issues no admissions
//!
//! Read this before wiring the crate into anything.
//!
//! The self-host compiler is behind the reference implementation (roadmap item
//! 391), and the shape of that gap is not "a few missing constructs" — it is a
//! whole missing LAYER. `admit_src` decides the composition and guarantee layer
//! (`G1`..`G4`, `A1`, `PRELUDE`, and parse failures as `BAD`). It does **not**
//! run the reference's type layer. Measured, not assumed: the reference refuses
//! `fn f() -> Int { return "s" }`, `fn f() -> Int { return undefined_name }`
//! and `fn f() -> { }`; the self-host gate raises no objection to any of them.
//!
//! So [`Verdict`] has no admitting arm and no `is_admitted()`. Its non-refusing
//! outcome is [`Verdict::NoObjection`], which means exactly *"this gate found
//! nothing it is able to refuse"* and never *"the reference would admit this"*,
//! and [`Verdict::to_json`] emits `"admitted": false` for EVERY arm. A consumer
//! written against the fixed `{admitted, code, message}` shape
//! (`docs/design/332-embeddable-gate-api.md`) therefore reads the verdict
//! surface as "never admits" rather than misreading a no-objection as an
//! admission; the arm itself is carried in the extra `"verdict"` field.
//!
//! The two divergence directions are not symmetric, and this asymmetry is the
//! whole design: refusing what the reference admits is an inconvenience;
//! ADMITTING what the reference refuses is the defect class the admission-gate
//! arc exists to prevent.
//!
//! # The admission surface (issue #346)
//!
//! An admission is therefore a SECOND, separate question, asked through a
//! separate type and a separate entry point: [`issue_admission`] returns an
//! [`Admission`], not a [`Verdict`]. The split is the point. A host holding a
//! [`Verdict`] cannot accidentally read it as a green — there is no arm to
//! misread — and a host that wants a green has to ask for one explicitly and
//! handle [`Admission::Withheld`].
//!
//! [`Admission::Admitted`] is reachable through exactly one path, and both of
//! its conditions are necessary:
//!
//! 1. [`admit`] returned [`Verdict::NoObjection`] — the composition/guarantee
//!    gate ran and found nothing to refuse. An admission is never issued over a
//!    refusal or over a frontier gap.
//! 2. the source is inside the ADMISSION SURFACE
//!    ([`ADMISSION_SURFACE_ID`], `src/admission.rs`) — the region where the
//!    covered layer is the WHOLE question, because the source carries no term
//!    the reference type layer decides.
//!
//! The surface is deliberately tiny: `service` method signatures and scalar
//! `type` aliases, over a closed scalar vocabulary derived from the reference's
//! own table. No body, no expression, no literal, no generic head. That is not
//! the covered layer read optimistically; it is the sliver of the covered layer
//! where reading it as an admission is sound, and it is measured rather than
//! argued — every certified program in the census corpus is a program the
//! reference admits, and the `false-admission` bucket of
//! `tools/gate_reference_census.py` reds on the first one that is not.
//!
//! A source OUTSIDE the surface is [`Admission::Withheld`] carrying the verdict
//! verbatim, which is the same fail-closed answer the crate gave before the arm
//! existed. Widening the surface is the self-host type layer's lane
//! (`docs/design/457-selfhost-type-layer.md`): each slice it lands is a family
//! the certifier can then account for.
//!
//! ```no_run
//! use revl_gate::{issue_admission, Admission};
//!
//! match issue_admission("service Store { fn get(key: Str) -> Str }") {
//!     // A real admission: `"admitted": true` on the wire, and the basis says
//!     // on what ground.
//!     Admission::Admitted { basis } => println!("admitted: {}", basis),
//!     // No admission. The verdict inside is the refusal surface's answer, and
//!     // a `NoObjection` there is still not a green.
//!     Admission::Withheld { verdict } => println!("withheld: {:?}", verdict),
//! }
//! ```
//!
//! # Fail closed, always
//!
//! [`Verdict::OutsideFrontier`] means *this gate is not entitled to decide*,
//! and the crate returns it whenever:
//!
//! * the source uses a construct in the generated frontier table
//!   (see [`FRONTIER_ID`] and `src/frontier.rs`);
//! * the source is larger than [`MAX_SOURCE_BYTES`] — the emitted front end is
//!   deeply recursive and a stack exhaustion ABORTS, which cannot be turned
//!   back into a refusal;
//! * the source has more than [`MAX_LEVEL_ITEMS`] items at one bracket level —
//!   the emitted parser recurses once per SIBLING item, so a flat
//!   `g(1, 1, …)` a few KB long and one bracket deep exhausts the stack where
//!   neither the byte bound nor the nesting bound can see it, and a stack
//!   exhaustion ABORTS rather than refusing;
//! * the native gate panics while deciding (caught via `catch_unwind`);
//! * the native gate returns a verdict wire shape this crate does not
//!   recognise;
//! * in [`admit_into`], the manifest wire is longer than [`MAX_SOURCE_BYTES`],
//!   or carries more than [`MANIFEST_ROW_LIMIT`] `;`-separated rows, or the
//!   fold's answer is a shape this crate does not recognise. Both manifest
//!   limits are checked before the wire reaches the parser, because the fold
//!   consumes one stack frame per row: the byte limit on its own is NOT a bound
//!   on the fold's stack use, and a stack exhaustion aborts rather than
//!   refusing.
//!
//! # The manifest arm (issue #346)
//!
//! [`admit_into(source, manifest)`](admit_into) answers a different question
//! from [`admit`]: not "is this text well formed on its own", but "does this
//! text compose with the composition that is ALREADY RUNNING". `source` is
//! decided against the union of the manifest and the incoming text, so a key the
//! running composition already holds conflicts (G2), a route into a realm the
//! union does not provide dangles (G2), and a dependency cycle that spans the
//! manifest boundary is a cycle (G3). The manifest arrives as item 186's row
//! wire (`docs/design/186-ambient-admission-guarantees.md`), and the fold is
//! `selfhost/lower.rvl::admit_ambient`, compiled to rust like [`admit`].
//!
//! What this arm decides is the G2/G3 legs of ambient admission and nothing
//! else. The wire has room for row kinds the landed wave does not carry —
//! replacement (`-C`) and handoff (`C=k:T`), both of which need the type layer —
//! and those rows are REFUSED as `MANIFEST` rather than skipped, because a row
//! this gate cannot honour is exactly where a stub that ignored its inputs would
//! wave a program through. An empty `manifest` (`""`) is the empty composition,
//! which makes [`admit_into`] byte-identical to [`admit`]: the arm is a
//! generalisation, not a second implementation.
//!
//! ```no_run
//! use revl_gate::{admit_into, Verdict};
//!
//! // `Kv/store/` is a provision the running composition already holds.
//! let running = "Kv/store/;App/app/;App<store";
//! let candidate = "service Store { fn get(k: Str) -> Str } \
//!                  component CacheLayer provides store: Store { \
//!                    provide store { fn get(k) { return k } } }";
//! match admit_into(candidate, running) {
//!     // A refusal the reference agrees with: this candidate re-provides `store`.
//!     Verdict::Refused { code, message } => println!("refused ({}): {}", code, message),
//!     // NOT an admission. See "The verdict surface issues no admissions"
//!     // above; ask `issue_admission_into` for a green.
//!     Verdict::NoObjection => println!("nothing this gate can refuse"),
//!     // The gate declined to decide at all: a row it cannot honour, a frontier
//!     // gap, or an aborted fold. Fail closed.
//!     Verdict::OutsideFrontier { reason } => println!("undecided: {}", reason),
//! }
//! ```
//!
//! Like [`admit`], this arm NEVER admits. It does not run the reference type
//! layer, so a type-incorrect candidate lands on `NoObjection`; and the
//! requirements a candidate declares are checked for disjointness and
//! acyclicity, not resolved. Use `revl.gate.admit_into` on py when the decision
//! must be an admission.
//!
//! # `compile_to` is Stage 4
//!
//! Exported so the shape is fixed; it refuses unconditionally today, because
//! the self-host emitters still carry `@py`-only helper externs and do not emit
//! to rust at all.
//!
//! # The navigation surface
//!
//! [`symbols`] is a SECOND, independent surface over the same front end: the
//! declarations a document contains and the line each sits on, which is what an
//! editor needs for go-to-definition and hover (roadmap item 336 slice 2). It
//! issues no verdicts and says nothing about whether a program may run — a
//! refused program has declarations to navigate like any other — so it is
//! versioned separately from the admission surface above, by
//! [`SYMBOLS_API_VERSION`] rather than by [`GATE_API_VERSION`] (which is held in
//! lockstep with `revl.gate` on py, and `revl.gate` has no navigation surface).
//! Its own fail-closed rule is the mirror of this one: it answers only what it
//! can answer EXACTLY, and every uncertainty is an absence rather than a guess.
//!
//! # Layer 2: the session surface (item 334 slice 1)
//!
//! See [`session`]. [`session::Session`] is the foundational first slice of the
//! rust host: the generation state machine, the untrusted-author admission entry
//! (`propose`/`admit`, reusing this crate's [`admit`]), and the item-245
//! witnessed-call recording path (`call`/`commit`/`abort`/`unload`). The ACCEPT
//! half of `propose` (activate + health-gate + swap), the witnessed-effect
//! runtime, the WAL, and the approver callback are the remaining slices; a
//! candidate the native gate does not refuse is fail-closed, never waved through.
//!
//! # Two host obligations
//!
//! * The fail-closed panic path uses [`std::panic::catch_unwind`]. A profile
//!   built with `panic = "abort"` defeats it: a native gate abort then takes the
//!   process down instead of producing `OutsideFrontier`. That is loud, not
//!   silent, so it is still not a false admission — but prefer
//!   `panic = "unwind"` in any profile that calls this crate.
//! * The default panic hook still prints to stderr when the fail-closed path
//!   fires. Install your own hook if that noise matters.

#![forbid(unsafe_code)]

// The generated module carries the reference emitter's output verbatim,
// including the self-host's own `test` blocks (which run under `cargo test`).
// It is machine-written, so its style lints are noise in a consumer's build.
#[allow(non_snake_case, unused_braces, clippy::all)]
mod selfhost;

mod admission;
mod frontier;
pub mod ir;
pub mod session;
pub mod symbols;

pub use frontier::{FRONTIER_ID, MANIFEST_ROW_LIMIT, MAX_LEVEL_ITEMS, MAX_SOURCE_BYTES};
pub use ir::{check_ir_boundary, IrRefusal, KNOWN_IR_FIELDS, KNOWN_IR_REVISIONS};

/// The semver of the GATE SURFACE itself (`gate_version().api`). Bumped by
/// surface changes only, independent of the language version. Kept in lockstep
/// with `revl.gate.GATE_API_VERSION` on py; the generator refuses to run if the
/// two disagree.
pub const GATE_API_VERSION: &str = "@GATE_API_VERSION@";

/// The revl language/package version this gate's refusals are drawn from.
pub const LANGUAGE_VERSION: &str = "@LANGUAGE_VERSION@";

/// The semver of the NAVIGATION surface ([`symbols`]), versioned on its own.
/// It is not part of the admission surface [`GATE_API_VERSION`] names and has
/// no twin on py, so the two move independently; the self-host pin both are
/// drawn from is [`FRONTIER_ID`].
pub const SYMBOLS_API_VERSION: &str = "0.1.0";

/// What this gate actually decides, in one line. The reference type layer is
/// deliberately absent — see the crate docs, "The verdict surface issues no
/// admissions", and [`ADMITTED_LAYER`] for the sliver it is sound to admit in.
pub const COVERED_LAYER: &str = "@COVERED_LAYER@";

/// The `code` a frontier gap reports on the wire.
pub const FRONTIER_CODE: &str = "FRONTIER";

/// An identifier of the region [`issue_admission`] is willing to ADMIT in.
///
/// Versioned apart from [`FRONTIER_ID`] because the two bound different things:
/// the frontier bounds where this gate may REFUSE, this bounds where it may
/// ADMIT. A host caching an admission compares THIS value before trusting the
/// cached green against a gate built from another tree — two gates with
/// different admission surfaces admitted under different rules.
pub const ADMISSION_SURFACE_ID: &str = admission::SURFACE_ID;

/// What [`issue_admission`] is willing to admit, in one line. Read it before
/// treating an [`Admission::Withheld`] as a defect: outside this region the
/// honest answer is to withhold.
pub const ADMITTED_LAYER: &str = "@ADMITTED_LAYER@";

/// The three values a host can branch on (design "Versioning").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateVersion {
    /// Semver of the gate surface. Bumped by surface changes only.
    pub api: &'static str,
    /// The revl language/package version this gate's refusals are drawn from.
    pub language: &'static str,
    /// An identifier of the COVERED surface. Compare before trusting two gates'
    /// agreement: different ids cover different languages.
    pub frontier: &'static str,
    /// The layer this gate decides, as prose. See [`COVERED_LAYER`].
    pub layer: &'static str,
}

/// A verdict from the native gate.
///
/// Three arms, none of which is an admission. `code` is API (the guarantee tags
/// are append-only; an existing code never changes meaning); `message` is the
/// gate's why-trace verbatim at this version and is NOT promised stable across
/// versions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// The gate refuses this program. `code` is the guarantee tag (`G1`..`G4`,
    /// `A1`, `PRELUDE`, `BAD`, ...); `message` is the diagnostic verbatim, and
    /// byte-agrees with the reference compiler's on the covered corpus.
    Refused { code: String, message: String },
    /// The gate found nothing it is able to refuse.
    ///
    /// **This is not an admission.** This gate does not run the reference type
    /// layer, so a type-incorrect program lands here. Get a reference verdict
    /// before running anything.
    NoObjection,
    /// The gate is NOT ENTITLED TO DECIDE this program at all: it is outside
    /// the covered frontier, or the native gate could not complete.
    OutsideFrontier { reason: String },
}

impl Verdict {
    /// True for a definitive refusal. This is the arm worth acting on: it
    /// agrees with the reference compiler on the covered corpus.
    ///
    /// There is deliberately no `is_admitted()` — see the crate docs.
    pub fn is_refused(&self) -> bool {
        matches!(self, Verdict::Refused { .. })
    }

    /// True when the gate declined to decide (a frontier gap).
    pub fn is_undecided(&self) -> bool {
        matches!(self, Verdict::OutsideFrontier { .. })
    }

    /// The arm's stable wire name: `"refused"`, `"no_objection"`, or
    /// `"outside_frontier"`.
    pub fn kind(&self) -> &'static str {
        match self {
            Verdict::Refused { .. } => "refused",
            Verdict::NoObjection => "no_objection",
            Verdict::OutsideFrontier { .. } => "outside_frontier",
        }
    }

    /// The guarantee tag for a refusal; `Some(FRONTIER_CODE)` for a frontier
    /// gap; `None` for a no-objection.
    pub fn code(&self) -> Option<&str> {
        match self {
            Verdict::Refused { code, .. } => Some(code),
            Verdict::NoObjection => None,
            Verdict::OutsideFrontier { .. } => Some(FRONTIER_CODE),
        }
    }

    /// The why-trace (or the frontier reason). Verbatim, never rewritten.
    pub fn message(&self) -> Option<&str> {
        match self {
            Verdict::Refused { message, .. } => Some(message),
            Verdict::NoObjection => None,
            Verdict::OutsideFrontier { reason } => Some(reason),
        }
    }

    /// The design's fixed `{"admitted", "code", "message"}` shape, plus the
    /// `"verdict"` arm name.
    ///
    /// `"admitted"` is `false` for EVERY arm, because this gate issues no
    /// admissions. A consumer written against the fixed three-field shape
    /// therefore reads this gate as "never admits" — the fail-closed reading —
    /// instead of mistaking a no-objection for an admission. The real signal is
    /// `"verdict"`.
    pub fn to_json(&self) -> String {
        let mut out = String::from("{\"verdict\":");
        out.push_str(&json_string(self.kind()));
        out.push_str(",\"admitted\":false,\"code\":");
        match self.code() {
            Some(code) => out.push_str(&json_string(code)),
            None => out.push_str("null"),
        }
        out.push_str(",\"message\":");
        match self.message() {
            Some(message) => out.push_str(&json_string(message)),
            None => out.push_str("null"),
        }
        out.push('}');
        out
    }
}

/// The answer to the ADMISSION question (issue #346) — a different question
/// from [`Verdict`], carried in a different type so the two cannot be confused.
///
/// [`Verdict`] answers *"is there something here I can refuse"*. This answers
/// *"may this run"*, and only one of its arms says yes. A host that needs a
/// green asks [`issue_admission`] and handles [`Admission::Withheld`]; a host
/// that only wants a local refusal keeps using [`admit`] and never sees this
/// type at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Admission {
    /// The gate ISSUES an admission: [`admit`] raised no objection AND the
    /// source is inside the admission surface, so the covered layer was the
    /// whole question. `basis` is the certificate's why-trace — which surface,
    /// and what it accounted for. It is not on the wire.
    Admitted { basis: String },
    /// No admission. `verdict` is the refusal surface's answer verbatim, and a
    /// [`Verdict::NoObjection`] in here is still not a green: it means the gate
    /// found nothing to refuse and was not entitled to admit either.
    Withheld { verdict: Verdict },
}

impl Admission {
    /// True only for an ISSUED admission. This is the one call a host may treat
    /// as a green light, and only within [`ADMISSION_SURFACE_ID`].
    pub fn is_admitted(&self) -> bool {
        matches!(self, Admission::Admitted { .. })
    }

    /// The certificate's why-trace for an issued admission.
    pub fn basis(&self) -> Option<&str> {
        match self {
            Admission::Admitted { basis } => Some(basis),
            Admission::Withheld { .. } => None,
        }
    }

    /// The withheld answer's verdict; `None` for an issued admission.
    pub fn verdict(&self) -> Option<&Verdict> {
        match self {
            Admission::Admitted { .. } => None,
            Admission::Withheld { verdict } => Some(verdict),
        }
    }

    /// The arm's stable wire name: `"admitted"`, or the withheld verdict's own
    /// [`Verdict::kind`].
    pub fn kind(&self) -> &'static str {
        match self {
            Admission::Admitted { .. } => "admitted",
            Admission::Withheld { verdict } => verdict.kind(),
        }
    }

    /// The design's fixed `{"admitted", "code", "message"}` shape plus the
    /// `"verdict"` arm name.
    ///
    /// An issued admission serialises `{"verdict":"admitted","admitted":true,
    /// "code":null,"message":null}` — BYTE-IDENTICAL to what `revl.gate`'s own
    /// `Verdict.to_json()` writes for a py admission, so a seam comparing the
    /// two tiers' wires (item 337) compares equal bytes rather than two
    /// spellings of the same yes. The basis is deliberately off the wire: it is
    /// evidence for a log, not part of the contract.
    ///
    /// A withheld answer serialises the verdict verbatim, so switching a
    /// consumer from [`admit`] to [`issue_admission`] changes nothing about the
    /// bytes it already handled.
    pub fn to_json(&self) -> String {
        match self {
            Admission::Admitted { .. } => String::from(
                "{\"verdict\":\"admitted\",\"admitted\":true,\"code\":null,\"message\":null}",
            ),
            Admission::Withheld { verdict } => verdict.to_json(),
        }
    }
}

/// The target tiers `compile_to` names. Both refuse today (Stage 4).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Py,
    Rust,
}

/// The native gate's verdict for `source`.
///
/// Pure and disk-pure. Returns [`Verdict::Refused`] only where the self-host
/// gate refuses — a verdict that byte-agrees with the reference compiler on the
/// covered corpus. Everything else is [`Verdict::NoObjection`] (not an
/// admission) or [`Verdict::OutsideFrontier`] (declined). No input produces an
/// admission, by construction.
pub fn admit(source: &str) -> Verdict {
    if let Some(reason) = frontier::scan(source) {
        return Verdict::OutsideFrontier { reason };
    }
    let owned = source.to_string();
    // The emitted stages are total over the surface they were written for, and
    // "written for" is exactly the thing this crate refuses to assume. An abort
    // inside the native gate must become a refusal to decide, not a verdict.
    let wire = match std::panic::catch_unwind(move || selfhost::admit_src(owned)) {
        Ok(wire) => wire,
        Err(_) => {
            return Verdict::OutsideFrontier {
                reason: String::from(
                    "the native gate aborted while deciding this source, so no verdict was reached; this is a frontier gap — ask the reference `revl` toolchain",
                ),
            }
        }
    };
    verdict_from_wire(&wire)
}

/// Rows in a manifest wire, counted the way the fold consumes it: one segment
/// per `;` boundary plus the trailing one, so `A/b/;` is two rows. The empty
/// wire is the empty composition, not one empty row.
fn manifest_rows(manifest: &str) -> usize {
    if manifest.is_empty() {
        return 0;
    }
    manifest.matches(';').count() + 1
}

/// The native gate's verdict for `source` once it is admitted INTO the running
/// composition `manifest` (item 186's ambient gate; issue #346).
///
/// `manifest` is the row wire (`docs/design/186-ambient-admission-guarantees.md`):
/// `C/k/r` for a provision (`r` is the realm, `""` for shared), `C<k` for a
/// requirement, `!halted` for a halted composition, joined by `;`. The empty
/// string is the empty composition, so `admit_into(source, "")` is
/// `admit(source)` byte for byte — this arm generalises [`admit`] rather than
/// re-implementing it.
///
/// The decision is the native fold `selfhost/lower.rvl::admit_ambient`,
/// compiled to rust like [`admit`]: the incoming text is decided as the
/// standalone gate decides it, and then the LINK is recomputed over the UNION
/// of the manifest and the incoming text. That union is where a key the running
/// composition already holds surfaces (G2 provision conflict), where a route
/// into a realm the union does not provide dangles (G2), and where a dependency
/// cycle spanning the manifest boundary is a cycle (G3). Refusals come out
/// ordered exactly as the single-source composition of `manifest ++ source`
/// orders them.
///
/// What this arm does NOT decide, stated here rather than discovered later:
///
/// * the reference TYPE layer — a type-incorrect candidate is
///   [`Verdict::NoObjection`] here, exactly as it is in [`admit`];
/// * the row kinds the wire reserves for the deferred waves — a replacement row
///   (`-C`) or a handoff row (`C=k:T`) is REFUSED with the fold's own `MANIFEST`
///   code, never skipped. Skipping a row this gate cannot honour is the
///   wave-through this crate exists to prevent;
/// * it does not RESOLVE the requirements a candidate declares; it checks them
///   for disjointness and acyclicity. A `requires` the union does not provide is
///   a no-objection, and the reference is the only tier that decides it.
///
/// As with [`admit`], no input produces an admission: the arm is
/// `Refused` / `NoObjection` / `OutsideFrontier` like the rest of the surface.
pub fn admit_into(source: &str, manifest: &str) -> Verdict {
    if let Some(reason) = frontier::scan(source) {
        return Verdict::OutsideFrontier { reason };
    }
    // The fold parses the manifest with the same deeply-recursive front end, so
    // the bound that guards the source guards the wire too — and a manifest over
    // it is declined rather than risked.
    if manifest.len() > MAX_SOURCE_BYTES {
        return Verdict::OutsideFrontier {
            reason: format!(
                "manifest is {} bytes, above the {}-byte bound this gate will decide (the native front end is deeply recursive and an overflow aborts rather than refusing); ask the reference `revl` toolchain",
                manifest.len(),
                MAX_SOURCE_BYTES
            ),
        };
    }
    // A byte bound is not a stack bound: `selfhost::admit_ambient` fans the wire
    // into `parse_manifest_rows`, which recurses ONE FRAME PER ROW, so a 13 KB
    // wire of empty rows overflows a 1 MiB stack. Measured on a release build:
    // 2_700 rows abort a 1 MiB stack, 20_100 rows (100 KB) abort the 8 MiB
    // default. Counted and refused HERE, ahead of the parser, because an
    // overflow aborts and `catch_unwind` below cannot see it.
    let rows = manifest_rows(manifest);
    if rows > MANIFEST_ROW_LIMIT {
        return Verdict::OutsideFrontier {
            reason: format!(
                "manifest carries {} rows, above the {}-row bound this gate will fold (the fold recurses one stack frame per row, so the byte bound is not a bound on its stack use, and an overflow aborts rather than refusing); ask the reference `revl` toolchain",
                rows,
                MANIFEST_ROW_LIMIT
            ),
        };
    }
    let owned = source.to_string();
    let wire_rows = manifest.to_string();
    // Same contract as `admit`: the emitted stages are total over the surface
    // they were written for, and "written for" is the thing this crate refuses
    // to assume. An abort must become a refusal to decide, not a verdict.
    let wire = match std::panic::catch_unwind(move || selfhost::admit_ambient(owned, wire_rows)) {
        Ok(wire) => wire,
        Err(_) => {
            return Verdict::OutsideFrontier {
                reason: String::from(
                    "the native fold aborted while admitting this source into the running manifest, so no verdict was reached; this is a frontier gap — ask the reference `revl` toolchain",
                ),
            }
        }
    };
    verdict_from_wire(&wire)
}

/// The ADMISSION question for `source` (issue #346): may this run?
///
/// Two conditions, both necessary, and in this order:
///
/// 1. [`admit`] must return [`Verdict::NoObjection`]. A refusal or a frontier
///    gap is withheld as it stands — an admission is never issued over the
///    refusal surface's head.
/// 2. `source` must be inside the ADMISSION SURFACE ([`ADMISSION_SURFACE_ID`]),
///    the region where the covered layer is the WHOLE question because the
///    source carries no term the reference type layer decides.
///
/// Outside that region the answer is [`Admission::Withheld`] carrying the
/// verdict, which is exactly what a consumer of [`admit`] already handles. See
/// [`ADMITTED_LAYER`] for what the region is, and the crate docs for why it is
/// this small.
pub fn issue_admission(source: &str) -> Admission {
    let verdict = admit(source);
    if verdict == Verdict::NoObjection {
        if let Some(basis) = admission::certify(source) {
            return Admission::Admitted { basis };
        }
    }
    Admission::Withheld { verdict }
}

/// The ADMISSION question for `source` once it is admitted INTO the running
/// composition `manifest` (issue #346) — the shape an agent loop actually needs.
///
/// The empty manifest is the empty composition, so `issue_admission_into(src,
/// "")` is [`issue_admission`] byte for byte.
///
/// Against a NON-EMPTY manifest the surface is far narrower, and the reason is
/// the item-186 row wire rather than a gap in the certifier: a row carries a
/// component name, a provision key and a realm, and NO SERVICE SHAPES. A
/// candidate declaring `service Store { ... }` may collide with a `Store` the
/// running composition already holds in a different shape — the reference
/// refuses that pair with "service `Store` differs from the running manifest"
/// — and no amount of care on this side can see it in the wire. So only a
/// candidate that declares nothing is certified against a running composition,
/// and everything else is withheld with the fold's verdict.
///
/// Widening this is not the type layer alone: the WIRE has to carry the running
/// composition's declared shapes first. That is the remaining half of issue
/// #346, and naming it here is cheaper than rediscovering it.
pub fn issue_admission_into(source: &str, manifest: &str) -> Admission {
    if manifest.is_empty() {
        return issue_admission(source);
    }
    let verdict = admit_into(source, manifest);
    if verdict == Verdict::NoObjection {
        if let Some(basis) = admission::certify_into(source, manifest) {
            return Admission::Admitted { basis };
        }
    }
    Admission::Withheld { verdict }
}

/// Parse the self-host gate's internal `"<TAG>|<message>"` protocol into the
/// structured verdict, message verbatim, splitting at the FIRST `|` only so a
/// message carrying `|` survives intact.
///
/// `""` is a no-objection. Anything non-empty with no `|` is a shape this crate
/// does not recognise, and an unrecognised shape is a frontier gap.
fn verdict_from_wire(wire: &str) -> Verdict {
    if wire.is_empty() {
        return Verdict::NoObjection;
    }
    match wire.find('|') {
        Some(bar) => Verdict::Refused {
            code: wire[..bar].to_string(),
            message: wire[bar + 1..].to_string(),
        },
        None => Verdict::OutsideFrontier {
            reason: format!(
                "the native gate returned an unrecognised verdict shape ({:?}); treating it as undecided",
                wire
            ),
        },
    }
}

/// Verdict plus emitted target source — **Stage 4, not available**.
///
/// Always `Err(Verdict::OutsideFrontier)` today: the self-host emitters
/// (`selfhost/emit_py.rvl`, `selfhost/emit_rust.rvl`) still carry `@py`-only
/// helper externs (`string_lit`, `num_str`, `py_repr`, `mangle`) and do not emit
/// to rust at all, so no native emitter exists to call. The signature is fixed
/// here so its arrival is additive.
pub fn compile_to(_source: &str, tier: Tier) -> Result<String, Verdict> {
    let tier_name = match tier {
        Tier::Py => "py",
        Tier::Rust => "rust",
    };
    Err(Verdict::OutsideFrontier {
        reason: format!(
            "compile_to({}) is not available in this crate: the self-host emitters still depend on @py-only helper externs, so there is no native emitter to run (roadmap item 332 Stage 4). Emit with the reference `revl compile --backend {}`.",
            tier_name, tier_name
        ),
    })
}

/// The gate's version surface. Compare `frontier` before trusting agreement
/// between two gates, and read `layer` before trusting a non-refusal.
pub fn gate_version() -> GateVersion {
    GateVersion {
        api: GATE_API_VERSION,
        language: LANGUAGE_VERSION,
        frontier: FRONTIER_ID,
        layer: COVERED_LAYER,
    }
}

/// Minimal JSON string encoder — the boundary carries strings only, so this is
/// the whole serialisation need and the shim takes no serde dependency for it.
fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod wire_tests {
    use super::*;

    #[test]
    fn an_empty_wire_is_a_no_objection_and_not_an_admission() {
        assert_eq!(verdict_from_wire(""), Verdict::NoObjection);
        assert!(!verdict_from_wire("").is_refused());
        // the fixed three-field shape reads false for every arm
        assert!(verdict_from_wire("").to_json().contains("\"admitted\":false"));
    }

    #[test]
    fn an_unrecognised_shape_is_undecided() {
        assert!(verdict_from_wire("something-unexpected").is_undecided());
    }

    #[test]
    fn a_message_carrying_a_bar_survives_intact() {
        match verdict_from_wire("G3|a -> b | c") {
            Verdict::Refused { code, message } => {
                assert_eq!(code, "G3");
                assert_eq!(message, "a -> b | c");
            }
            other => panic!("expected a refusal, got {:?}", other),
        }
    }

    #[test]
    fn every_arm_serialises_as_not_admitted() {
        let arms = [
            Verdict::NoObjection,
            Verdict::Refused { code: String::from("G4"), message: String::from("m") },
            Verdict::OutsideFrontier { reason: String::from("r") },
        ];
        for arm in arms {
            assert!(
                arm.to_json().contains("\"admitted\":false"),
                "{} must serialise as not admitted: {}",
                arm.kind(),
                arm.to_json()
            );
        }
    }

    #[test]
    fn json_escapes_a_quote_and_a_newline() {
        assert_eq!(json_string("a\"b\nc"), "\"a\\\"b\\nc\"");
    }

    // The manifest arm (issue #346).

    /// The running composition the ambient tests admit into: `Kv` provides
    /// `store`, `App` provides `app` and requires `store`.
    const RUNNING: &str = "Kv/store/;App/app/;App<store";

    /// A component that re-provides `store`, the key `Kv` already holds.
    const AMBIENT_CONFLICT: &str = "service Cache { fn lookup(key: Str) -> Str }\n\
component CacheLayer requires store: Store provides store: Store {\n\
  provide store {\n\
    fn get(key) = key\n\
    fn bump(n) = n\n\
    fn put(key, value) = value\n\
  }\n\
}\n";

    #[test]
    fn an_empty_manifest_is_the_standalone_gate() {
        for source in [
            "fn id(x: Int) -> Int { return x }",
            AMBIENT_CONFLICT,
            "component X provides { fn = }",
        ] {
            assert_eq!(
                admit_into(source, ""),
                admit(source),
                "an empty manifest must be the empty composition, decided exactly as the standalone gate decides it"
            );
        }
    }

    #[test]
    fn the_manifest_arm_refuses_an_ambient_provision_conflict() {
        match admit_into(AMBIENT_CONFLICT, RUNNING) {
            Verdict::Refused { code, message } => {
                assert_eq!(code, "G2");
                assert!(message.contains("provision conflict"), "{}", message);
                assert!(message.contains("Kv") && message.contains("CacheLayer"), "{}", message);
            }
            other => panic!("expected an ambient G2 refusal, got {:?}", other),
        }
    }

    #[test]
    fn the_manifest_arm_reads_the_manifest_rather_than_ignoring_it() {
        // The same bytes ask a DIFFERENT question without the running
        // composition: standalone, the candidate requires a key it provides
        // itself (G3). A stub that ignored its manifest argument could not
        // produce two different codes here.
        match admit(AMBIENT_CONFLICT) {
            Verdict::Refused { code, .. } => assert_eq!(code, "G3"),
            other => panic!("expected a standalone G3 refusal, got {:?}", other),
        }
        match admit_into(AMBIENT_CONFLICT, "Kv/store/") {
            Verdict::Refused { code, .. } => assert_eq!(code, "G2"),
            other => panic!("expected an ambient G2 refusal, got {:?}", other),
        }
    }

    #[test]
    fn a_halted_composition_refuses_every_admission() {
        match admit_into("fn id(x: Int) -> Int { return x }", "Kv/store/;!halted") {
            Verdict::Refused { code, .. } => assert_eq!(code, "HALTED"),
            other => panic!("a halted composition must refuse, got {:?}", other),
        }
    }

    #[test]
    fn a_deferred_manifest_row_is_refused_not_skipped() {
        // Replacement (item 186) belongs to a wave this crate has not landed, and
        // a row it cannot honour is REFUSED rather than dropped: skipping a row
        // is the wave-through this crate exists to prevent.
        for rows in [
            "Kv/store/;-Kv/store/",
            "Kv/store/;Kv/store=Int",
            "!paused",
        ] {
            match admit_into("fn id(x: Int) -> Int { return x }", rows) {
                Verdict::Refused { code, .. } => assert_eq!(code, "MANIFEST"),
                other => panic!("expected a MANIFEST refusal for {:?}, got {:?}", rows, other),
            }
        }
    }

    #[test]
    fn the_manifest_arm_never_admits() {
        let arms = [
            admit_into("fn id(x: Int) -> Int { return x }", RUNNING),
            admit_into(AMBIENT_CONFLICT, RUNNING),
            admit_into(AMBIENT_CONFLICT, "!halted"),
            admit_into("fn id(x: Int) -> Int { return x }", ""),
        ];
        for arm in arms {
            assert!(
                arm.to_json().contains("\"admitted\":false"),
                "{} must serialise as not admitted: {}",
                arm.kind(),
                arm.to_json()
            );
        }
    }

    #[test]
    fn an_unrecognised_manifest_row_is_refused_rather_than_skipped() {
        // A row shape the wire does not define: refused as MANIFEST (a real
        // refusal), never skipped over.
        match admit_into("fn id(x: Int) -> Int { return x }", "!wat") {
            Verdict::Refused { code, .. } => assert_eq!(code, "MANIFEST"),
            other => panic!("expected a MANIFEST refusal, got {:?}", other),
        }
    }

    // The row bound (the manifest half of the fail-closed story: the byte bound
    // above is not a stack bound).

    fn is_row_bound_refusal(verdict: &Verdict) -> bool {
        match verdict {
            Verdict::OutsideFrontier { reason } => {
                reason.contains("row") && reason.contains(&MANIFEST_ROW_LIMIT.to_string())
            }
            _ => false,
        }
    }

    #[test]
    fn manifest_rows_counts_the_segments_the_fold_folds() {
        // The count has to be the one the fold actually walks, or the bound
        // bounds nothing: `parse_manifest_rows` consumes a segment per `;` and
        // one more for the tail, and the empty wire is the empty composition
        // rather than one empty row.
        assert_eq!(manifest_rows(""), 0);
        assert_eq!(manifest_rows("A/b/"), 1);
        assert_eq!(manifest_rows("A/b/;"), 2);
        assert_eq!(manifest_rows("A/b/;B/c/"), 2);
        assert_eq!(manifest_rows("A/b/;B/c/;"), 3);
        assert_eq!(manifest_rows("!halted;"), 2);
    }

    #[test]
    fn a_manifest_over_the_row_bound_is_a_gap_and_not_an_abort() {
        let wire = "A/b/;".repeat(MANIFEST_ROW_LIMIT + 1);
        // The point of the case, and the false claim this bound retires: the
        // wire is a few KB, far under MAX_SOURCE_BYTES, and it used to ABORT a
        // 1 MiB stack inside the fold (2_700 rows of it, measured).
        assert!(wire.len() < MAX_SOURCE_BYTES / 10);
        assert_eq!(manifest_rows(&wire), MANIFEST_ROW_LIMIT + 2);
        let verdict = admit_into("fn id(x: Int) -> Int { return x }", &wire);
        assert!(
            is_row_bound_refusal(&verdict),
            "a manifest this long must be declined by the row bound, not folded: {:?}",
            verdict,
        );
        assert!(verdict.is_undecided());
        assert_eq!(verdict.code(), Some("FRONTIER"));
        assert!(verdict.to_json().contains("\"admitted\":false"));
    }

    #[test]
    fn the_row_bound_is_checked_before_the_wire_is_folded() {
        // Rows the fold REFUSES (a deferred replacement row, code MANIFEST)
        // repeated past the row bound. Reading MANIFEST here would mean the
        // parser had already walked the wire, which is the stack the bound
        // exists to keep: the answer must be the row bound's.
        let wire = "Kv/store/;-Kv/store/;".repeat(MANIFEST_ROW_LIMIT + 1);
        let verdict = admit_into("fn id(x: Int) -> Int { return x }", &wire);
        assert!(
            is_row_bound_refusal(&verdict),
            "the row bound must be checked before the fold, got {:?}",
            verdict,
        );
    }

    #[test]
    fn a_manifest_under_the_row_bound_is_still_folded() {
        // Non-vacuity in the other direction: a ceiling, not a wall. The wire
        // below the bound is folded exactly as it was before the bound existed,
        // and a conflict in it is still found.
        let under = "A/b/;".repeat(MANIFEST_ROW_LIMIT - 1);
        assert!(manifest_rows(&under) <= MANIFEST_ROW_LIMIT);
        let verdict = admit_into("fn id(x: Int) -> Int { return x }", &under);
        assert!(
            !is_row_bound_refusal(&verdict),
            "a manifest under the bound must still be decided: {:?}",
            verdict,
        );
        assert_eq!(verdict, Verdict::NoObjection);
        assert_eq!(admit_into("fn id(x: Int) -> Int { return x }", ""), verdict);
    }

    #[test]
    fn a_manifest_at_the_row_bound_is_still_folded() {
        // The boundary itself, from both sides: MANIFEST_ROW_LIMIT rows are
        // folded, MANIFEST_ROW_LIMIT + 1 rows are refused. Without this pair
        // the bound could be off by a whole wire.
        let at = "A/b/;".repeat(MANIFEST_ROW_LIMIT - 1);
        assert_eq!(manifest_rows(&at), MANIFEST_ROW_LIMIT);
        let over = format!("{}A/b/;", at);
        assert_eq!(manifest_rows(&over), MANIFEST_ROW_LIMIT + 1);
        assert_eq!(
            admit_into("fn id(x: Int) -> Int { return x }", &at),
            Verdict::NoObjection
        );
        assert!(is_row_bound_refusal(&admit_into(
            "fn id(x: Int) -> Int { return x }",
            &over
        )));
    }

    // The admission surface (issue #346).

    /// A source inside the admission surface: interface declarations only.
    const CERTIFIABLE: &str = "service Store {\n  fn get(key: Str) -> Str\n}\n";

    #[test]
    fn the_verdict_surface_still_has_no_admitting_arm() {
        // The split is the whole safety story: a host holding a `Verdict` has no
        // arm it could misread as a green, whatever the admission surface grows
        // into.
        for source in [CERTIFIABLE, "fn id(x: Int) -> Int { return x }", ""] {
            let verdict = admit(source);
            assert!(!verdict.is_refused());
            assert_eq!(verdict, Verdict::NoObjection);
            assert!(verdict.to_json().contains("\"admitted\":false"));
        }
    }

    #[test]
    fn an_interface_only_source_is_admitted_and_says_so_on_the_wire() {
        let issued = issue_admission(CERTIFIABLE);
        match &issued {
            Admission::Admitted { basis } => {
                assert!(basis.contains(ADMISSION_SURFACE_ID), "{}", basis);
            }
            other => panic!("expected an issued admission, got {:?}", other),
        }
        assert!(issued.is_admitted());
        assert_eq!(issued.kind(), "admitted");
        assert_eq!(
            issued.to_json(),
            "{\"verdict\":\"admitted\",\"admitted\":true,\"code\":null,\"message\":null}"
        );
    }

    #[test]
    fn a_source_outside_the_surface_is_withheld_with_the_verdict_verbatim() {
        // The type-layer gap, which is the whole reason the surface is this
        // small: the reference refuses this and the covered layer cannot see it,
        // so the honest answer is to withhold rather than to admit.
        for source in [
            "fn f() -> Int { return \"s\" }",
            "fn f() -> Int { return undefined_name }",
            "fn f() -> { }",
        ] {
            let issued = issue_admission(source);
            assert!(!issued.is_admitted(), "must not admit {:?}", source);
            assert_eq!(issued.to_json(), admit(source).to_json());
            assert_eq!(issued.verdict(), Some(&admit(source)));
        }
    }

    #[test]
    fn an_admission_is_never_issued_over_a_refusal_or_a_frontier_gap() {
        // Condition 1, held from the outside: every source the refusal surface
        // does not answer `NoObjection` to is withheld, so the arm cannot be
        // reached past a refusal however the surface is widened later.
        let over_bound = "x".repeat(MAX_SOURCE_BYTES + 1);
        // a G3 refusal, a BAD parse refusal, and a frontier gap
        for source in [AMBIENT_CONFLICT, "service S { fn f(", over_bound.as_str()] {
            let verdict = admit(source);
            assert_ne!(verdict, Verdict::NoObjection, "{:?}", &source[..17.min(source.len())]);
            assert_eq!(issue_admission(source), Admission::Withheld { verdict });
        }
    }

    #[test]
    fn an_empty_manifest_is_the_standalone_admission_question() {
        for source in [CERTIFIABLE, AMBIENT_CONFLICT, "fn id(x: Int) -> Int { return x }"] {
            assert_eq!(
                issue_admission_into(source, ""),
                issue_admission(source),
                "an empty manifest is the empty composition on the admission surface too"
            );
        }
    }

    #[test]
    fn a_declaring_candidate_is_withheld_against_a_running_composition() {
        // The wire carries no service shapes, so the same bytes that are
        // ADMITTED standalone are WITHHELD against a running composition. This
        // is the remaining half of issue #346, and it is a refusal to guess
        // rather than an oversight.
        assert!(issue_admission(CERTIFIABLE).is_admitted());
        let into = issue_admission_into(CERTIFIABLE, RUNNING);
        assert!(!into.is_admitted());
        assert_eq!(into.verdict(), Some(&Verdict::NoObjection));
        assert!(into.to_json().contains("\"admitted\":false"));
    }

    #[test]
    fn a_candidate_that_declares_nothing_is_admitted_into_a_running_composition() {
        let into = issue_admission_into("// nothing to add\n", RUNNING);
        match &into {
            Admission::Admitted { basis } => {
                assert!(basis.contains("provision rows"), "{}", basis)
            }
            other => panic!("expected an issued admission, got {:?}", other),
        }
        assert!(into.is_admitted());
    }

    #[test]
    fn a_halted_or_deferred_manifest_row_is_never_admitted_into() {
        for rows in ["!halted", "Kv/store/;-Kv/store/", "!paused", "Kv/store/;Kv/store=Int"] {
            let into = issue_admission_into("// nothing to add\n", rows);
            assert!(!into.is_admitted(), "must not admit into {:?}", rows);
            assert!(into.to_json().contains("\"admitted\":false"));
        }
    }

    #[test]
    fn the_withheld_wire_is_the_verdict_wire_on_every_arm() {
        // Switching a consumer from `admit` to `issue_admission` may only ADD
        // the admitted wire; every other answer has to be byte-identical to what
        // it already handled.
        for source in [
            "fn id(x: Int) -> Int { return x }",
            AMBIENT_CONFLICT,
            "component X provides { fn = }",
            "service Store {\n  fn get(k: Str) -> Str\n  fn get(k: Str) -> Str\n}\n",
        ] {
            let issued = issue_admission(source);
            if !issued.is_admitted() {
                assert_eq!(issued.to_json(), admit(source).to_json(), "{:?}", source);
            }
        }
    }
}
'''


SESSION_RS = r'''//! Layer 2, the session surface — item 334 slice 1, the foundational runtime.
//!
//! GENERATED by `tools/build_gate_crate.py`. Do not edit by hand.
//!
//! The design (`docs/design/332-embeddable-gate-api.md`, "Layer 2: the session
//! surface") splits the gate API on purity. Layer 1, the verdict surface
//! ([`crate::admit`]), is pure functions of their arguments. Layer 2 is
//! stateful and address-space-bound — a live composition, an owner, a deferral
//! queue, witnessed escrow, a WAL — with the operation set
//! `{load, admit, admit_into, propose, call, commit, abort, unload}`, and it is
//! where the 243/244/245/246/322 guarantees (witnessed effects, session commit,
//! approvals, six-tier crash recovery) live. `revl.gate.Gate` on py is the
//! reference layer-2 surface this mirrors.
//!
//! # What slice 1 is
//!
//! This is the FOUNDATIONAL first slice of roadmap item 334's rust host: a real
//! [`Session`] state machine over one live composition in one process, carrying
//!
//! * the GENERATION state ([`Session::load`], [`Session::generation`]): a fresh
//!   boot installs generation 1 with a fresh item-245 owner frame;
//! * the untrusted-author admission ENTRY ([`Session::propose`],
//!   [`Session::admit`], [`Session::admit_into`]): the item-334 `propose`
//!   verb's decision half wired in the design's order — halt-dominance first,
//!   then the FORBIDDEN-GRANT rule, then the decision compile — reusing
//!   [`crate::admit`] (and, across a composition boundary, [`crate::admit_into`])
//!   for that last step, so a refusal is the reference why-trace returned as
//!   DATA and the live composition is untouched;
//! * the WITNESSED-CALL recording path ([`Session::call`], [`Session::commit`],
//!   [`Session::abort`], [`Session::unload`]): the item-245 three-way effect
//!   split (class (a) witnessed with a checked inverse, class (b) a deferred
//!   tail, class (c) an irreversible emission gated on the approver) recorded
//!   into the session frame, with `abort` replaying the witnessed inverses LIFO
//!   residue-free and `commit` discharging the deferrals.
//!
//! # What slice 1 is NOT (the honest boundary, item 445)
//!
//! Layer 1 issues NO admission ([`crate::Verdict`] has no `Admitted` arm — the
//! native gate does not run the reference type layer), and this tier has no
//! cordis runtime, no WAL and no approver CALLBACK yet. So the ACCEPT half of
//! `propose` — compile the candidate to a runnable composition, activate it, run
//! the item-334 post-activation health gate, and hot-swap generation N+1 into
//! the live process — is NOT here: a candidate the native gate does not REFUSE
//! is fail-closed (`admitted = false`, `code = "NO_ADMISSION"`), never waved
//! through. The remaining slices, in order:
//!
//! * slice 2 — the rust witnessed-effect runtime the [`Crossing`] classes model
//!   here as data: real host externs paired with checked inverses over cordis-rs;
//! * slice 3 — the health-gated swap (`_abort_swap` back to generation N on a
//!   FAILED/PENDING successor) and live-state migration across the swap;
//! * slice 4 — the item-322 WAL and `revl.gate.recover`, the crash half of the
//!   revert guarantee;
//! * slice 5 — the item-246 approver seam as a host callback (slice 1 gates
//!   class (c) on a caller-supplied boolean and fails closed without it).
//!
//! Until those land, [`crate::admit`] is the entitled decision this session can
//! make, and `Session` fails closed on everything it cannot yet do.

use crate::{admit, admit_into, Verdict};
use std::fmt;

/// The service names that reach the decider — the admit/swap/owner-state control
/// surface (item 334 FORBIDDEN-GRANT rule, mirroring `revl.gate._DECIDER_SERVICES`).
///
/// Granting any of these to an untrusted candidate would hand it the loop's own
/// re-entrant-admit plumbing: the stdlib `Admission` service (provided by
/// component `AdmitGate`) reaches `host_admit`, whose host body decides
/// admissions, so a candidate granted it reaches the decider through a granted
/// host body with NO `extern` of its own — the non-extern path the untrusted
/// profile alone does not close. [`Session::propose`] REJECTS a granted set
/// naming any of these, before any decision, which is how "re-entrant propose is
/// deferred" is ENFORCED rather than merely documented. The STRUCTURAL half (a
/// walk of the composed IR for a decider crossing reached under any name) needs
/// a lowered IR and rides the runtime slice; slice 1 enforces the name check.
pub const DECIDER_SERVICES: [&str; 2] = ["Admission", "AdmitGate"];

/// A live composition — the item-245 `Session.ir`, reduced to what the session
/// contract reads: the component names and the keys they provide.
///
/// [`Session::load`] takes one already-decided (the py `Session.load` takes an
/// already-admitted `ir` dict — a trusted input, no gate). The untrusted door is
/// [`Session::propose`], never `load`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Composition {
    /// The component names composing this generation.
    pub components: Vec<String>,
    /// The keys this generation actually provides — what a [`Session::call`]
    /// resolves against, and what a health-gated swap will hold a successor to.
    pub provided_keys: Vec<String>,
}

impl Composition {
    /// A composition of the given components providing the given keys.
    pub fn new<C, K>(components: C, provided_keys: K) -> Self
    where
        C: IntoIterator<Item = String>,
        K: IntoIterator<Item = String>,
    {
        Composition {
            components: components.into_iter().collect(),
            provided_keys: provided_keys.into_iter().collect(),
        }
    }

    /// Whether this generation provides `key`.
    pub fn provides(&self, key: &str) -> bool {
        self.provided_keys.iter().any(|k| k == key)
    }
}

/// A witnessed host effect paired with its checked inverse (items 243/244). The
/// inverse is what [`Session::abort`] replays to undo the effect residue-free.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WitnessedEffect {
    /// The effect that fired (its identity, for the teardown log).
    pub effect: String,
    /// The checked inverse that undoes it.
    pub inverse: String,
}

/// What a [`Session::call`] crossing does, in the item-245 three-way split.
///
/// This is the classification the witnessed runtime (slice 2) will derive from a
/// real emission; slice 1 takes it as data so the frame machinery — the star of
/// this slice — can be built and tested ahead of the runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Crossing {
    /// Class (a): a witnessed effect with a checked inverse. Recorded to the
    /// frame's LIFO inverse stack; reverted on `abort`, made permanent on `commit`.
    Witnessed {
        /// The effect that fired.
        effect: String,
        /// Its checked inverse.
        inverse: String,
    },
    /// Class (b): a deferrable irreversible tail. It does NOT fire now — it is
    /// queued and fires only at `commit`; `abort` DROPS the queue unfired.
    Deferred {
        /// A description of the tail, for the commit enumeration.
        tail: String,
    },
    /// Class (c): an irreversible emission that must be approved per crossing.
    /// `approved` is the caller's stand-in for the item-246 approver callback
    /// (slice 5): a `false` here fails the call CLOSED. An approved crossing is
    /// recorded as enumerable residue — `abort` names it, it does not undo it.
    Irreversible {
        /// The emission that crossed.
        emission: String,
        /// Whether the (stand-in) approver said yes. `false` fails closed.
        approved: bool,
    },
}

/// The item-245 session owner/frame: the witnessed-effect escrow and the
/// deferral queue for the live generation.
#[derive(Debug, Default)]
struct Frame {
    /// Witnessed effects, in the order they fired — `abort` replays the inverses
    /// in REVERSE (LIFO).
    witnessed: Vec<WitnessedEffect>,
    /// Class-(b) deferred tails, fired at `commit`, dropped at `abort`.
    deferrals: Vec<String>,
    /// Class-(c) irreversible emissions the approver let through — enumerable
    /// residue that neither `commit` nor `abort` can undo.
    irreversible: Vec<String>,
}

impl Frame {
    fn is_baseline(&self) -> bool {
        self.witnessed.is_empty() && self.deferrals.is_empty() && self.irreversible.is_empty()
    }
}

/// A precondition failure — a programming error, not a verdict on a candidate.
/// Mirrors py `SessionError`/`GateError` (a RAISE, distinct from the verdict a
/// refusal returns as data).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionError {
    /// A stable machine code, e.g. `"NOT_LOADED"`, `"NO_SUCH_KEY"`.
    pub code: String,
    /// The human message.
    pub message: String,
}

impl fmt::Display for SessionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for SessionError {}

impl SessionError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        SessionError { code: code.to_string(), message: message.into() }
    }
}

/// The outcome of [`Session::admit`] — the item-330 per-turn additive admission.
/// A refusal (`admitted` false) carries the repair signal as `code`/`message`
/// and never touches the running composition. Mirrors py `AdmitResult`.
///
/// On this tier `admitted` is always false: layer 1 issues no admission (see the
/// module docs). The actionable arm is a `Refused` verdict surfaced as data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdmitOutcome {
    /// Whether the turn was admitted. Always false on this tier (fail-closed).
    pub admitted: bool,
    /// The guarantee tag / machine code, when refused or declined.
    pub code: Option<String>,
    /// The why-trace verbatim, when refused or declined.
    pub message: Option<String>,
    /// The keys the admitted turn provides. Empty when not admitted.
    pub keys: Vec<String>,
}

impl AdmitOutcome {
    /// The py `AdmitResult.as_dict` wire shape.
    pub fn to_json(&self) -> String {
        format!(
            "{{\"admitted\":{},\"code\":{},\"message\":{},\"keys\":{}}}",
            self.admitted,
            opt_json(&self.code),
            opt_json(&self.message),
            list_json(&self.keys),
        )
    }
}

/// The outcome of [`Session::propose`] — the item-334 self-extending crossing.
/// Every arm is DATA (a refusal is the repair signal, never a raised error the
/// loop cannot catch). Mirrors py `ProposeResult`; slice 1 populates the
/// terminal REFUSAL/DECLINE arms and never the `swapped` arm (the accept half is
/// the runtime slice).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProposeOutcome {
    /// Whether the candidate was admitted. Always false until the runtime slice.
    pub admitted: bool,
    /// Whether the swap took (gen N+1 live). Always false in slice 1.
    pub swapped: bool,
    /// Whether an admitted candidate failed to activate and reverted to gen N.
    /// Always false in slice 1 (no activation happens).
    pub reverted: bool,
    /// The machine code: `"HALTED"`, `"FORBIDDEN_GRANT"`, a refusal guarantee
    /// tag (`"G2"`, `"BAD"`, ...), `"FRONTIER"`, or `"NO_ADMISSION"`.
    pub code: Option<String>,
    /// The why-trace / halt message, verbatim.
    pub message: Option<String>,
    /// The keys the swapped-in generation provides. Empty until a swap takes.
    pub keys: Vec<String>,
}

impl ProposeOutcome {
    /// The py `ProposeResult.as_dict` wire shape (the slice-1 subset).
    pub fn to_json(&self) -> String {
        format!(
            "{{\"admitted\":{},\"swapped\":{},\"reverted\":{},\"code\":{},\"message\":{},\"keys\":{}}}",
            self.admitted,
            self.swapped,
            self.reverted,
            opt_json(&self.code),
            opt_json(&self.message),
            list_json(&self.keys),
        )
    }

    fn refused(code: &str, message: impl Into<String>) -> Self {
        ProposeOutcome {
            admitted: false,
            swapped: false,
            reverted: false,
            code: Some(code.to_string()),
            message: Some(message.into()),
            keys: Vec::new(),
        }
    }
}

/// What a single [`Session::call`] recorded into the frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallReport {
    /// The key that was called.
    pub key: String,
    /// The method that was called.
    pub method: String,
    /// The effect class recorded: `"witnessed"`, `"deferred"`, or `"irreversible"`.
    pub class: &'static str,
}

/// The result of [`Session::commit`]: the item-245 discharge.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommitReport {
    /// How many witnessed effects were made permanent.
    pub witnessed_committed: usize,
    /// The class-(b) deferred tails fired at commit, in queue order.
    pub deferrals_fired: Vec<String>,
    /// The class-(c) irreversible emissions this session performed — enumerated,
    /// not undone.
    pub irreversible: Vec<String>,
}

/// The result of [`Session::abort`]: the item-245 revert (EDGE 2).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AbortReport {
    /// The witnessed inverses replayed, in LIFO order (last effect first).
    pub inverses_replayed: Vec<String>,
    /// How many deferred tails were DROPPED unfired.
    pub deferrals_dropped: usize,
    /// The class-(c) irreversible residue that could not be reverted (each was
    /// approved when it fired). Empty means the revert was residue-free.
    pub residue: Vec<String>,
}

impl AbortReport {
    /// Whether the abort left the world residue-free — the item-245 R4 property
    /// for a session that only ever used classes (a) and (b).
    pub fn residue_free(&self) -> bool {
        self.residue.is_empty()
    }
}

/// The result of [`Session::unload`]: a teardown that STRANDS rather than
/// unwinds (the py `unload` contract — see the `HALTED` propose message).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnloadReport {
    /// Witnessed effects left owed and not discharged.
    pub stranded_witnessed: usize,
    /// Deferred tails abandoned.
    pub stranded_deferrals: usize,
}

/// One live composition, driven step by step — the rust mirror of
/// `revl.mcp.session.Session` (item 334 slice 1).
#[derive(Debug, Default)]
pub struct Session {
    generation: u64,
    ir: Option<Composition>,
    frame: Frame,
    halted: Option<String>,
}

impl Session {
    /// A fresh session: generation 0, nothing loaded, not halted.
    pub fn new() -> Self {
        Session::default()
    }

    /// Which generation is live: 0 before the first `load`, then 1, and every
    /// swap moves it on (the swap itself is the runtime slice).
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Whether a composition is live.
    pub fn loaded(&self) -> bool {
        self.ir.is_some()
    }

    /// The live composition, if one is loaded.
    pub fn live(&self) -> Option<&Composition> {
        self.ir.as_ref()
    }

    /// Whether this session has been E-STOPPED (item 443). A halt DOMINATES: it
    /// refuses `load`, `propose` and `call`.
    pub fn halted(&self) -> bool {
        self.halted.is_some()
    }

    /// The keys the live generation provides, or empty when nothing is loaded.
    pub fn provided_keys(&self) -> Vec<String> {
        self.ir.as_ref().map(|c| c.provided_keys.clone()).unwrap_or_default()
    }

    /// E-STOP the session (item 443): the instance is dead and every registered
    /// entry is stranded. The way back is recovery, never another verb.
    pub fn estop(&mut self, reason: impl Into<String>) {
        self.halted = Some(reason.into());
    }

    /// Install the FIRST generation. Takes an already-decided composition — the
    /// trusted door (py `Session.load` takes an already-admitted `ir`); the
    /// untrusted door is [`Session::propose`]. Installs a fresh item-245 owner
    /// frame BEFORE the generation is live, so every crossing it later builds
    /// joins this generation's frame. Returns the new generation number.
    ///
    /// Refuses if a composition is already loaded (slice 1 has no re-load; a
    /// running generation is replaced through `propose`/swap, the runtime slice)
    /// or if the session is halted.
    pub fn load(&mut self, composition: Composition) -> Result<u64, SessionError> {
        if let Some(reason) = &self.halted {
            return Err(SessionError::new(
                "HALTED",
                format!("load refused: the session was E-STOPPED ({reason})"),
            ));
        }
        if self.ir.is_some() {
            return Err(SessionError::new(
                "ALREADY_LOADED",
                "load refused: a composition is already live; replace it through \
                 `propose` (the runtime slice), not a second `load`",
            ));
        }
        self.ir = Some(composition);
        self.frame = Frame::default();
        self.generation = 1;
        Ok(self.generation)
    }

    /// The item-330 per-turn additive admission. Runs the layer-1 decision over
    /// `source` and returns the verdict as DATA; the running composition is
    /// untouched. `granted` bounds the turn's reach in the reference compiler; on
    /// this tier the decision is layer-1's, which only ever refuses or declines.
    ///
    /// Requires a loaded composition (a turn composes INTO one).
    pub fn admit(&self, source: &str, _granted: &[&str]) -> Result<AdmitOutcome, SessionError> {
        self.require_loaded("admit")?;
        if let Some(reason) = &self.halted {
            return Err(SessionError::new(
                "HALTED",
                format!("admit refused: the session was E-STOPPED ({reason})"),
            ));
        }
        let (code, message) = decision(source);
        Ok(AdmitOutcome { admitted: false, code, message, keys: Vec::new() })
    }

    /// The per-turn admission asked ACROSS the composition boundary: admit
    /// `source` INTO the running manifest `manifest` (item 186's ambient gate,
    /// issue #346), returning the fold's verdict as DATA. The live generation is
    /// untouched, exactly as in [`Session::admit`].
    ///
    /// `manifest` is the ROW WIRE the item-186 wire defines — `C/k/r` for a
    /// provision (`r` the realm, `""` for shared), `C<k` for a requirement,
    /// `!halted` for a halted composition, joined by `;` — the same input
    /// [`crate::admit_into`] takes. It is a PARAMETER and not a projection of
    /// [`Session::live`] on purpose: a [`Composition`] carries the component
    /// names and the keys the generation provides, and a manifest also needs the
    /// requirements and the realms. Synthesising rows out of what this session
    /// holds would be inventing a running composition, and an admission arm that
    /// invents its inputs is the defect class this crate exists to prevent.
    /// Build the wire where the composition is actually known
    /// (`revl.manifest_wire(ir)` on py, or the wire directly).
    ///
    /// Wiring, in the order the rest of this surface uses: a HALT dominates
    /// (item 443) and is checked before the candidate is judged, then the
    /// decision itself. Requires a loaded composition (a candidate composes INTO
    /// one). `admitted` is `false` on every outcome this tier can produce —
    /// including the `NO_ADMISSION` decline — because the native gate issues no
    /// admission (`revl.gate.admit_into` on py is the admitting tier).
    pub fn admit_into(
        &self,
        source: &str,
        manifest: &str,
    ) -> Result<AdmitOutcome, SessionError> {
        self.require_loaded("admit_into")?;
        if let Some(reason) = &self.halted {
            return Err(SessionError::new(
                "HALTED",
                format!("admit_into refused: the session was E-STOPPED ({reason})"),
            ));
        }
        let (code, message) = decision_into(source, manifest);
        Ok(AdmitOutcome { admitted: false, code, message, keys: Vec::new() })
    }

    /// The item-334 `propose` verb's DECISION half: admit an AGENT-authored
    /// candidate under the untrusted-author profile, wired in the design's order.
    ///
    /// 1. HALT DOMINANCE (item 443 + 334 slice 2), checked FIRST: a halted
    ///    session returns `HALTED` — the candidate is never judged, and this is
    ///    not the retry-shaped `SWAP_REVERTED`.
    /// 2. FORBIDDEN-GRANT (item 334), before any decision, independent of the
    ///    operator: a `granted` set naming a decider service ([`DECIDER_SERVICES`])
    ///    returns `FORBIDDEN_GRANT`. This ENFORCES "re-entrant propose is deferred".
    /// 3. The STANDALONE decision: [`crate::admit`] over `source`. A refusal is
    ///    the reference why-trace returned as data (the repair signal), the live
    ///    composition untouched.
    ///
    /// The ACCEPT half — compile to a runnable composition, activate, health-gate
    /// and swap — is the runtime slice; a source the native gate does not refuse
    /// is fail-closed with `NO_ADMISSION` here, never waved through.
    pub fn propose(&self, source: &str, granted: &[&str]) -> Result<ProposeOutcome, SessionError> {
        self.require_loaded("propose")?;

        // 1. HALT DOMINANCE — ahead of everything else.
        if let Some(reason) = &self.halted {
            return Ok(ProposeOutcome::refused(
                "HALTED",
                format!(
                    "propose refused: this session was E-STOPPED ({reason}) and the \
                     instance is dead. Nothing was decided and nothing was swapped; \
                     the halt dominates, so this is not a verdict on the candidate \
                     and not a revert to a running generation — every registered \
                     entry is stranded. The way back is recovery, not a better \
                     candidate."
                ),
            ));
        }

        // 2. FORBIDDEN-GRANT — the name check, before any decision.
        let mut forbidden: Vec<&str> = granted
            .iter()
            .copied()
            .filter(|g| DECIDER_SERVICES.contains(g))
            .collect();
        forbidden.sort_unstable();
        forbidden.dedup();
        if !forbidden.is_empty() {
            return Ok(ProposeOutcome::refused(
                "FORBIDDEN_GRANT",
                format!(
                    "propose refused: the granted set names a gate/session/\
                     admit-control service ({}). Granting a decider service would \
                     let the untrusted candidate reach host_admit/swap/owner state \
                     through a granted host body with no `extern` of its own — the \
                     non-extern path the untrusted profile does not close. \
                     Re-entrant propose is deferred and this rule enforces it \
                     (item 334); drop the decider service from `granted`.",
                    forbidden.join(", ")
                ),
            ));
        }

        // 3. The STANDALONE decision — layer 1's entitled verdict.
        match admit(source) {
            Verdict::Refused { code, message } => {
                // The DECISION refused the candidate; the why-trace is data, the
                // live composition untouched.
                Ok(ProposeOutcome::refused_from(Some(code), message))
            }
            Verdict::OutsideFrontier { reason } => {
                Ok(ProposeOutcome::refused(crate::FRONTIER_CODE, reason))
            }
            Verdict::NoObjection => {
                // The native gate raised no objection, but layer 1 issues no
                // admission and this tier cannot activate or swap a candidate.
                // Fail closed rather than wave it through.
                Ok(ProposeOutcome::refused(
                    "NO_ADMISSION",
                    "propose declined: the native gate raised no objection, but \
                     this tier issues no admission (it does not run the reference \
                     type layer) and has no runtime to activate or swap a \
                     candidate. The standalone type-layer decision and the rust \
                     witnessed runtime are item 334's remaining slices. Get a \
                     reference admission (`revl.gate.Gate.propose` on py) before \
                     running anything.",
                ))
            }
        }
    }

    /// Record a witnessed crossing an in-flight call to `key.method` performed,
    /// into this generation's item-245 frame. Class (a) is escrowed with its
    /// inverse, class (b) is queued, class (c) is admitted only with the
    /// (stand-in) approver's yes and otherwise fails CLOSED.
    ///
    /// Refuses if nothing is loaded, if halted, or if `key` is not one the live
    /// generation provides (the py `AdmitHandle.call` key check).
    pub fn call(
        &mut self,
        key: &str,
        method: &str,
        crossing: Crossing,
    ) -> Result<CallReport, SessionError> {
        self.require_loaded("call")?;
        if let Some(reason) = &self.halted {
            return Err(SessionError::new(
                "HALTED",
                format!("call refused: the session was E-STOPPED ({reason})"),
            ));
        }
        let ir = self.ir.as_ref().expect("loaded above");
        if !ir.provides(key) {
            return Err(SessionError::new(
                "NO_SUCH_KEY",
                format!(
                    "call refused: key {key:?} is not one the live composition \
                     provides (provides: {})",
                    if ir.provided_keys.is_empty() {
                        "none".to_string()
                    } else {
                        ir.provided_keys.join(", ")
                    }
                ),
            ));
        }
        let class = match crossing {
            Crossing::Witnessed { effect, inverse } => {
                self.frame.witnessed.push(WitnessedEffect { effect, inverse });
                "witnessed"
            }
            Crossing::Deferred { tail } => {
                self.frame.deferrals.push(tail);
                "deferred"
            }
            Crossing::Irreversible { emission, approved } => {
                if !approved {
                    return Err(SessionError::new(
                        "APPROVER_REQUIRED",
                        format!(
                            "call refused: the crossing {emission:?} is an \
                             irreversible (class c) emission and needs the host's \
                             approver yes; none was given, so the gate fails \
                             closed (item 246). No effect fired."
                        ),
                    ));
                }
                self.frame.irreversible.push(emission);
                "irreversible"
            }
        };
        Ok(CallReport { key: key.to_string(), method: method.to_string(), class })
    }

    /// The item-245 session commit: make the witnessed effects permanent, FIRE
    /// the deferred tails, and enumerate the irreversible residue. Clears the
    /// frame back to baseline.
    pub fn commit(&mut self) -> Result<CommitReport, SessionError> {
        self.require_loaded("commit")?;
        let witnessed_committed = self.frame.witnessed.len();
        let deferrals_fired = std::mem::take(&mut self.frame.deferrals);
        let irreversible = std::mem::take(&mut self.frame.irreversible);
        self.frame.witnessed.clear();
        Ok(CommitReport { witnessed_committed, deferrals_fired, irreversible })
    }

    /// The item-245 session abort (EDGE 2): replay the witnessed inverses LIFO,
    /// DROP the deferral queue unfired, and surface any approved-class-(c)
    /// residue. Clears the frame back to baseline. The process stays alive.
    pub fn abort(&mut self) -> AbortReport {
        let inverses_replayed: Vec<String> = self
            .frame
            .witnessed
            .iter()
            .rev()
            .map(|w| w.inverse.clone())
            .collect();
        let deferrals_dropped = self.frame.deferrals.len();
        let residue = std::mem::take(&mut self.frame.irreversible);
        self.frame.witnessed.clear();
        self.frame.deferrals.clear();
        debug_assert!(self.frame.is_baseline() || !residue.is_empty());
        AbortReport { inverses_replayed, deferrals_dropped, residue }
    }

    /// Tear the composition down, STRANDING rather than unwinding: outstanding
    /// witnessed effects are left owed and not discharged (the py `unload`
    /// contract). Returns the session to generation 0.
    pub fn unload(&mut self) -> UnloadReport {
        let report = UnloadReport {
            stranded_witnessed: self.frame.witnessed.len(),
            stranded_deferrals: self.frame.deferrals.len(),
        };
        self.frame = Frame::default();
        self.ir = None;
        self.generation = 0;
        report
    }

    fn require_loaded(&self, verb: &str) -> Result<(), SessionError> {
        if self.ir.is_none() {
            return Err(SessionError::new(
                "NOT_LOADED",
                format!("{verb} refused: nothing is loaded; load a base composition first"),
            ));
        }
        Ok(())
    }
}

impl ProposeOutcome {
    fn refused_from(code: Option<String>, message: String) -> Self {
        ProposeOutcome {
            admitted: false,
            swapped: false,
            reverted: false,
            code,
            message: Some(message),
            keys: Vec::new(),
        }
    }
}

/// Map a layer-1 verdict to the `(code, message)` an admit-style outcome carries.
/// Every arm yields `admitted = false` at the call site: layer 1 issues no
/// admission. `NoObjection` is DECLINED (fail-closed), not admitted.
fn decision(source: &str) -> (Option<String>, Option<String>) {
    declined_or(admit(source), false)
}

/// The same mapping for the manifest arm ([`admit_into`]). One difference: a
/// no-objection there means "the union fold found nothing to refuse", which is
/// still not an admission (the type layer has not run, and a `requires` is not
/// resolved), so it is declined with the same `NO_ADMISSION` code and a message
/// that names the manifest.
fn decision_into(source: &str, manifest: &str) -> (Option<String>, Option<String>) {
    declined_or(admit_into(source, manifest), true)
}

/// The shared fail-closed mapping: a refusal and a frontier gap pass through
/// verbatim (the refusal IS the reference why-trace), and a no-objection becomes
/// a decline.
fn declined_or(verdict: Verdict, into_manifest: bool) -> (Option<String>, Option<String>) {
    match verdict {
        Verdict::Refused { code, message } => (Some(code), Some(message)),
        Verdict::OutsideFrontier { reason } => {
            (Some(crate::FRONTIER_CODE.to_string()), Some(reason))
        }
        Verdict::NoObjection => (
            Some("NO_ADMISSION".to_string()),
            Some(if into_manifest {
                "declined: the native fold raised no objection to this candidate \
                 against the running manifest, but this tier issues no admission \
                 (it does not run the reference type layer, and it does not \
                 resolve the candidate's requirements). Get a reference \
                 admission on py before running anything."
                    .to_string()
            } else {
                "declined: the native gate raised no objection, but this tier \
                 issues no admission (it does not run the reference type layer). \
                 Get a reference admission on py before running anything."
                    .to_string()
            }),
        ),
    }
}

fn opt_json(value: &Option<String>) -> String {
    match value {
        Some(s) => json_string(s),
        None => "null".to_string(),
    }
}

fn list_json(values: &[String]) -> String {
    let mut out = String::from("[");
    for (i, v) in values.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&json_string(v));
    }
    out.push(']');
    out
}

/// A minimal JSON string escaper (the crate's layer-1 `json_string` is private).
fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    // A composition helper: two components providing one key `tool`.
    fn base() -> Composition {
        Composition::new(
            ["Provider".to_string(), "Tool".to_string()],
            ["tool".to_string()],
        )
    }

    // A source the layer-1 gate REFUSES (G2: two providers of one service),
    // taken from the crate's own agreement tests.
    const G2_SRC: &str = "service S { fn op(x: Str) -> Str } \
component A provides s: S { provide s { fn op(x) { return x } } } \
component B provides s: S { provide s { fn op(x) { return x } } }";

    // A source the layer-1 gate raises NO objection to.
    const CLEAN_SRC: &str = "fn id(x: Int) -> Int { return x }";

    #[test]
    fn new_session_is_empty() {
        let s = Session::new();
        assert_eq!(s.generation(), 0);
        assert!(!s.loaded());
        assert!(!s.halted());
        assert!(s.live().is_none());
    }

    #[test]
    fn load_installs_generation_one() {
        let mut s = Session::new();
        assert_eq!(s.load(base()).unwrap(), 1);
        assert!(s.loaded());
        assert_eq!(s.generation(), 1);
        assert_eq!(s.provided_keys(), vec!["tool".to_string()]);
    }

    #[test]
    fn load_refuses_a_second_composition() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let err = s.load(base()).unwrap_err();
        assert_eq!(err.code, "ALREADY_LOADED");
    }

    #[test]
    fn propose_requires_a_loaded_base() {
        let s = Session::new();
        let err = s.propose(CLEAN_SRC, &[]).unwrap_err();
        assert_eq!(err.code, "NOT_LOADED");
    }

    #[test]
    fn propose_halt_dominates() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.estop("operator halt");
        let out = s.propose(CLEAN_SRC, &[]).unwrap();
        assert!(!out.admitted);
        assert!(!out.swapped);
        assert_eq!(out.code.as_deref(), Some("HALTED"));
    }

    #[test]
    fn propose_halt_is_checked_before_forbidden_grant() {
        // A halted session with a forbidden grant still reports HALTED, not
        // FORBIDDEN_GRANT: the halt dominates and is checked first.
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.estop("operator halt");
        let out = s.propose(CLEAN_SRC, &["Admission"]).unwrap();
        assert_eq!(out.code.as_deref(), Some("HALTED"));
    }

    #[test]
    fn propose_forbidden_grant_rejects_decider_services() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        for svc in DECIDER_SERVICES {
            let out = s.propose(CLEAN_SRC, &[svc]).unwrap();
            assert!(!out.admitted);
            assert_eq!(out.code.as_deref(), Some("FORBIDDEN_GRANT"));
            assert!(out.message.unwrap().contains(svc));
        }
    }

    #[test]
    fn propose_surfaces_a_refusal_as_data() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.propose(G2_SRC, &[]).unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("G2"));
        assert!(out.message.is_some());
        // The live composition is untouched.
        assert_eq!(s.generation(), 1);
        assert_eq!(s.provided_keys(), vec!["tool".to_string()]);
    }

    #[test]
    fn propose_fails_closed_on_no_objection() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.propose(CLEAN_SRC, &[]).unwrap();
        assert!(!out.admitted);
        assert!(!out.swapped);
        assert_eq!(out.code.as_deref(), Some("NO_ADMISSION"));
    }

    #[test]
    fn admit_surfaces_a_refusal_as_data() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.admit(G2_SRC, &[]).unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("G2"));
    }

    // A source whose only fault is AMBIENT: it re-provides `store`, which the
    // running manifest already holds. It also requires the key it provides, so
    // judged STANDALONE the same source is a G3 (cycle) rather than a G2
    // (provision conflict) — which is exactly how the two arms are told apart.
    const AMBIENT_G2_SRC: &str = "service Store { fn get(key: Str) -> Str } \
service Cache { fn lookup(key: Str) -> Str } \
component CacheLayer requires store: Store provides store: Store { \
provide store { fn get(key) { return key } fn bump(n) { return n } \
fn put(key, value) { return value } } }";

    #[test]
    fn admit_into_requires_a_loaded_base() {
        let s = Session::new();
        let err = s.admit_into(CLEAN_SRC, "Kv/store/").unwrap_err();
        assert_eq!(err.code, "NOT_LOADED");
    }

    #[test]
    fn admit_into_halt_dominates() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.estop("operator halt");
        let err = s.admit_into(CLEAN_SRC, "Kv/store/").unwrap_err();
        assert_eq!(err.code, "HALTED");
    }

    #[test]
    fn admit_into_surfaces_the_ambient_refusal_as_data() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.admit_into(AMBIENT_G2_SRC, "Kv/store/").unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("G2"));
        assert!(out.message.unwrap().contains("provision conflict"));
        // The live composition is untouched.
        assert_eq!(s.generation(), 1);
    }

    #[test]
    fn admit_into_reads_the_manifest_it_is_given() {
        // The SAME candidate is a G2 against a manifest that already provides
        // `store` and a G3 when judged standalone: the parameter is read, not
        // ignored.
        let mut s = Session::new();
        s.load(base()).unwrap();
        let ambient = s.admit_into(AMBIENT_G2_SRC, "Kv/store/").unwrap();
        assert_eq!(ambient.code.as_deref(), Some("G2"));
        let standalone = s.admit_into(AMBIENT_G2_SRC, "").unwrap();
        assert_eq!(standalone.code.as_deref(), Some("G3"));
    }

    #[test]
    fn admit_into_fails_closed_on_a_deferred_manifest_row() {
        // A manifest row the fold cannot honour is REFUSED, never skipped.
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.admit_into(CLEAN_SRC, "Kv/store=Int").unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("MANIFEST"));
    }

    #[test]
    fn admit_into_fails_closed_on_no_objection() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let out = s.admit_into(CLEAN_SRC, "Kv/store/").unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("NO_ADMISSION"));
    }

    #[test]
    fn admit_into_fails_closed_on_a_manifest_over_the_row_bound() {
        // The row bound reaches this door too, and it has to: `Session::admit_into`
        // funnels into `crate::admit_into`, so the refusal shape must be the
        // crate's own, not a session one, or an embedder driving the session
        // surface would get a different answer for the same wire.
        let mut s = Session::new();
        s.load(base()).unwrap();
        let wire = "A/b/;".repeat(crate::MANIFEST_ROW_LIMIT + 1);
        let out = s.admit_into(CLEAN_SRC, &wire).unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("FRONTIER"));
        let reason = match crate::admit_into(CLEAN_SRC, &wire) {
            Verdict::OutsideFrontier { reason } => reason,
            other => panic!("expected the crate's own frontier gap, got {:?}", other),
        };
        assert_eq!(out.message.as_deref(), Some(reason.as_str()));
        assert!(reason.contains(&crate::MANIFEST_ROW_LIMIT.to_string()), "{}", reason);
        // The live composition is untouched, exactly as on every other refusal.
        assert_eq!(s.generation(), 1);
    }

    #[test]
    fn admit_into_still_folds_a_manifest_under_the_row_bound() {
        // Non-vacuity at this door: the SAME wire one row shorter is folded as
        // before, so the bound is a ceiling and not a wall.
        let mut s = Session::new();
        s.load(base()).unwrap();
        let under = "A/b/;".repeat(crate::MANIFEST_ROW_LIMIT - 1);
        let out = s.admit_into(CLEAN_SRC, &under).unwrap();
        assert!(!out.admitted);
        assert_eq!(out.code.as_deref(), Some("NO_ADMISSION"));
    }

    #[test]
    fn call_records_a_witnessed_effect() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let report = s
            .call(
                "tool",
                "run",
                Crossing::Witnessed {
                    effect: "write(/tmp/x)".to_string(),
                    inverse: "rm(/tmp/x)".to_string(),
                },
            )
            .unwrap();
        assert_eq!(report.class, "witnessed");
    }

    #[test]
    fn call_rejects_an_unknown_key() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let err = s
            .call("nope", "run", Crossing::Deferred { tail: "t".to_string() })
            .unwrap_err();
        assert_eq!(err.code, "NO_SUCH_KEY");
    }

    #[test]
    fn call_fails_closed_on_an_unapproved_irreversible_crossing() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        let err = s
            .call(
                "tool",
                "send",
                Crossing::Irreversible { emission: "email".to_string(), approved: false },
            )
            .unwrap_err();
        assert_eq!(err.code, "APPROVER_REQUIRED");
    }

    #[test]
    fn abort_replays_inverses_lifo_and_is_residue_free() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.call(
            "tool",
            "a",
            Crossing::Witnessed { effect: "e1".to_string(), inverse: "i1".to_string() },
        )
        .unwrap();
        s.call(
            "tool",
            "b",
            Crossing::Witnessed { effect: "e2".to_string(), inverse: "i2".to_string() },
        )
        .unwrap();
        s.call("tool", "c", Crossing::Deferred { tail: "tail".to_string() }).unwrap();
        let report = s.abort();
        // LIFO: the last effect's inverse replays first.
        assert_eq!(report.inverses_replayed, vec!["i2".to_string(), "i1".to_string()]);
        assert_eq!(report.deferrals_dropped, 1);
        assert!(report.residue_free());
    }

    #[test]
    fn abort_enumerates_approved_irreversible_residue() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.call(
            "tool",
            "send",
            Crossing::Irreversible { emission: "wire-transfer".to_string(), approved: true },
        )
        .unwrap();
        let report = s.abort();
        assert!(!report.residue_free());
        assert_eq!(report.residue, vec!["wire-transfer".to_string()]);
    }

    #[test]
    fn commit_discharges_deferrals_and_counts_witnessed() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.call(
            "tool",
            "a",
            Crossing::Witnessed { effect: "e1".to_string(), inverse: "i1".to_string() },
        )
        .unwrap();
        s.call("tool", "b", Crossing::Deferred { tail: "flush".to_string() }).unwrap();
        let report = s.commit().unwrap();
        assert_eq!(report.witnessed_committed, 1);
        assert_eq!(report.deferrals_fired, vec!["flush".to_string()]);
        // A subsequent abort has nothing to replay: the frame is baseline.
        assert!(s.abort().inverses_replayed.is_empty());
    }

    #[test]
    fn unload_strands_and_resets_to_generation_zero() {
        let mut s = Session::new();
        s.load(base()).unwrap();
        s.call(
            "tool",
            "a",
            Crossing::Witnessed { effect: "e1".to_string(), inverse: "i1".to_string() },
        )
        .unwrap();
        let report = s.unload();
        assert_eq!(report.stranded_witnessed, 1);
        assert!(!s.loaded());
        assert_eq!(s.generation(), 0);
    }

    #[test]
    fn call_refuses_when_not_loaded() {
        let mut s = Session::new();
        let err = s
            .call("tool", "run", Crossing::Deferred { tail: "t".to_string() })
            .unwrap_err();
        assert_eq!(err.code, "NOT_LOADED");
    }

    #[test]
    fn propose_outcome_json_shape() {
        let out = ProposeOutcome::refused("HALTED", "dead");
        let json = out.to_json();
        assert!(json.contains("\"admitted\":false"));
        assert!(json.contains("\"swapped\":false"));
        assert!(json.contains("\"code\":\"HALTED\""));
        assert!(json.contains("\"keys\":[]"));
    }

    #[test]
    fn admit_outcome_json_shape() {
        let out = AdmitOutcome {
            admitted: false,
            code: Some("G2".to_string()),
            message: Some("two providers".to_string()),
            keys: Vec::new(),
        };
        let json = out.to_json();
        assert!(json.contains("\"admitted\":false"));
        assert!(json.contains("\"code\":\"G2\""));
    }
}
'''



TESTS_ADMIT_RS = r'''//! The crate's own agreement + fail-closed tests (`cargo test`).
//!
//! GENERATED by `tools/build_gate_crate.py`. Do not edit by hand.
//!
//! These run with no Python anywhere: the expected verdicts are pinned here and
//! checked against the reference compiler by `tests/test_gate_crate_admit.py`
//! in the repo (which drives a standalone consumer crate over the full
//! differential corpus). This file's job is to prove that a consumer holding
//! only the crate gets the refusals, and — the load-bearing half — that no
//! input path produces something a caller could read as an admission.

use revl_gate::{
    admit, admit_into, compile_to, gate_version, Tier, Verdict, MANIFEST_ROW_LIMIT,
    MAX_LEVEL_ITEMS, MAX_SOURCE_BYTES,
};

// ------------------------------------------------------ refusals that agree

#[test]
fn an_undeclared_emission_is_refused_with_its_guarantee_tag() {
    let src = "extern emission fn audit_write(msg: Str) -> Int = @py { return 1 } \
service Cache { fn put(key: Str) } \
component C provides cache: Cache { \
  provide cache { fn put(key) { let n = audit_write(key) } } \
}";
    match admit(src) {
        Verdict::Refused { code, message } => {
            assert_eq!(code, "G4");
            assert!(!message.is_empty(), "a refusal must carry its why-trace");
        }
        other => panic!("expected a G4 refusal, got {:?}", other),
    }
}

#[test]
fn a_provision_conflict_is_refused() {
    let src = "service S { fn op(x: Str) -> Str } \
component A provides s: S { provide s { fn op(x) { return x } } } \
component B provides s: S { provide s { fn op(x) { return x } } }";
    match admit(src) {
        Verdict::Refused { code, .. } => assert_eq!(code, "G2"),
        other => panic!("expected a G2 refusal, got {:?}", other),
    }
}

#[test]
fn unparseable_source_is_refused_as_bad() {
    match admit("@@@ not revl @@@") {
        Verdict::Refused { code, .. } => assert_eq!(code, "BAD"),
        other => panic!("expected a BAD refusal, got {:?}", other),
    }
}

// ------------------------------ the verdict surface issues no admissions

#[test]
fn a_clean_program_gets_a_no_objection_which_is_not_an_admission() {
    let verdict = admit("fn id(x: Int) -> Int { return x }");
    assert_eq!(verdict, Verdict::NoObjection);
    assert!(!verdict.is_refused());
    assert!(verdict.to_json().contains("\"admitted\":false"));
}

/// The measured reason `Verdict` has no admitting arm, and the measured reason
/// the admission surface is as narrow as it is.
///
/// This gate decides the composition/guarantee layer, not the reference type
/// layer, so every program below is one the REFERENCE compiler refuses and this
/// gate raises no objection to. The crate must therefore never let a caller
/// read a non-refusal as an admission — which is why the arm is `NoObjection`,
/// why there is no `is_admitted()`, and why `to_json` reports
/// `"admitted": false` on every arm.
#[test]
fn type_layer_programs_are_not_refused_here_and_must_not_read_as_admitted() {
    let reference_refuses_all_of_these = [
        // return type / body type mismatch
        "fn f() -> Int { return \"s\" }",
        // an undeclared name in a function body
        "fn f() -> Int { return undefined_name }",
        // a return arrow with no return type at all
        "fn f() -> { }",
        // a declared return type with no returning body
        "fn f() -> Int { }",
    ];
    for src in reference_refuses_all_of_these {
        let verdict = admit(src);
        assert!(
            !verdict.is_refused(),
            "this pins the KNOWN gap; if the self-host gained the type layer, \
update the crate docs and this test: {}",
            src
        );
        // the load-bearing half: nothing here may read as an admission
        assert_eq!(verdict.kind(), "no_objection");
        assert!(verdict.to_json().contains("\"admitted\":false"));
        assert_eq!(verdict.code(), None);
    }
}

// ------------------------------------------------------------- fail closed

#[test]
fn a_construct_outside_the_frontier_is_declined() {
    // Built from the GENERATED table, never from a hand-picked construct: item
    // 391 ported `.is_digit()` and `.str()` into the self-host lowering, which
    // emptied the builtin row and left the two tests that named them asserting
    // a gap that no longer exists. An empty table is a legitimate generation
    // (the two front ends agree on the whole lexical surface today), so the
    // size bound below carries the fail-closed property on its own.
    let name = match frontier_probe() {
        Some(name) => name,
        None => return,
    };
    let src = format!("fn f(x: Str) -> Bool {{ return x.{}() }}", name);
    match admit(&src) {
        Verdict::OutsideFrontier { reason } => {
            assert!(reason.contains(name), "reason must name the gap: {}", reason);
        }
        other => panic!("a frontier construct must not be decided, got {:?}", other),
    }
}

/// One name from the generated frontier table, or `None` when the table is
/// empty. GENERATED alongside the table itself, so it can never name a
/// construct the self-host has since ported.
fn frontier_probe() -> Option<&'static str> {
    @FRONTIER_PROBE@
}

#[test]
fn an_oversized_source_is_declined_rather_than_risked() {
    let big = "fn id(x: Int) -> Int { return x } ".repeat(20_000);
    assert!(big.len() > MAX_SOURCE_BYTES);
    let verdict = admit(&big);
    assert!(verdict.is_undecided());
    assert_eq!(verdict.code(), Some("FRONTIER"));
}

#[test]
fn a_frontier_gap_reads_as_not_admitted_on_the_wire() {
    // The always-live trigger: the size bound. It does not depend on the
    // generated lexical table having an entry left in it.
    let verdict = admit(&"fn id(x: Int) -> Int { return x } ".repeat(20_000));
    assert_eq!(verdict.code(), Some("FRONTIER"));
    assert_eq!(verdict.kind(), "outside_frontier");
    assert!(verdict.to_json().contains("\"admitted\":false"));
}

// The size bound is not a ROW bound either. `admit_into` hands its manifest to
// the fold, and the fold consumes one stack frame per `;`-separated row before
// it decides anything: 2_700 rows of `A/b/;` are 13 KB, far under
// MAX_SOURCE_BYTES, and ABORT a 1 MiB stack, while the 8 MiB default goes down
// at ~20_000 rows. A rust stack overflow aborts, which `catch_unwind` cannot
// turn back into a verdict, so the row count is bounded ahead of the parser.
// The probes read the bound out of the crate rather than restating it, for the
// same reason `frontier_probe` is generated.

#[test]
fn a_manifest_over_the_row_bound_is_declined_rather_than_risked() {
    // The always-live trigger, and the one this bound exists for.
    let wire = "A/b/;".repeat(MANIFEST_ROW_LIMIT + 1);
    assert!(wire.len() < MAX_SOURCE_BYTES / 10);
    let verdict = admit_into("fn id(x: Int) -> Int { return x }", &wire);
    assert!(verdict.is_undecided());
    assert_eq!(verdict.code(), Some("FRONTIER"));
    assert_eq!(verdict.kind(), "outside_frontier");
    assert!(verdict.to_json().contains("\"admitted\":false"));
    match verdict {
        Verdict::OutsideFrontier { reason } => {
            // The refusal states the bound rather than describing a resource
            // failure: an embedder has to be able to act on it.
            assert!(reason.contains(&MANIFEST_ROW_LIMIT.to_string()), "{}", reason);
            assert!(reason.contains("row"), "{}", reason);
        }
        other => panic!("a row count over the bound must not be decided, got {:?}", other),
    }
}

#[test]
fn a_manifest_under_the_row_bound_is_still_folded() {
    // Non-vacuity in the other direction: a ceiling, not a wall. The wire below
    // the bound is decided exactly as it was before the bound existed.
    let under = "A/b/;".repeat(MANIFEST_ROW_LIMIT - 1);
    let verdict = admit_into("fn id(x: Int) -> Int { return x }", &under);
    assert_ne!(verdict.kind(), "outside_frontier", "{:?}", verdict);
    assert_eq!(verdict, Verdict::NoObjection);
    // And the empty manifest is still the standalone gate, byte for byte.
    assert_eq!(
        admit_into("fn id(x: Int) -> Int { return x }", ""),
        admit("fn id(x: Int) -> Int { return x }")
    );
}

// The byte bound is not a SHAPE bound, and the shape it misses most widely is
// the flat one. The emitted parser recurses once per SIBLING item, so a source
// one bracket level deep — a call, a list, a run of `let` statements — spends
// one stack frame per item while costing almost no bytes: measured on this
// crate, `g(1, 1, ...)` with 11_386 arguments is a 34 KB source and ABORTs a
// stock 8 MiB stack, and at the 1 MiB floor the wasm component runs at (the
// component build sets no `stack-size`) ~1_400 items is enough. Blank and
// `//`-comment lines between those statements change nothing, because the cost
// is per item parsed and not per line. `nesting_depth` cannot see this shape at
// all — it collapses sibling depth by construction — so the bound lives in the
// frontier scan, ahead of the descent, and the probes read it out of the crate
// rather than restating it.

/// A source whose statement body holds exactly `items` items at one bracket
/// level: `items - 1` `let`s, one per line, and the `return` that ends the body.
/// Nothing else sits at that level, so the count is the count.
fn flat_body(items: usize) -> String {
    // `items` siblings on one line, no nesting worth counting: the shape whose
    // frames are one per item. The count at the argument level is exactly
    // `items`, so the two tests below sit on either side of the bound.
    format!("fn f() -> Int {{ return g({}) }}", vec!["1"; items].join(", "))
}

#[test]
fn a_source_over_the_level_bound_is_declined_rather_than_risked() {
    let src = flat_body(MAX_LEVEL_ITEMS + 1);
    // The point of the case: flat and shallow, and a small fraction of the byte
    // bound. The 1 MiB stack is the wasm component's floor and is deliberately
    // too small for this shape to descend — the refusal has to land BEFORE the
    // parser sees it, or this test takes the process down instead of failing.
    assert!(src.len() < MAX_SOURCE_BYTES / 10);
    let verdict = std::thread::Builder::new()
        .stack_size(1 << 20)
        .spawn(move || admit(&src))
        .expect("spawn the probe")
        .join()
        .expect("the probe panicked");
    assert!(verdict.is_undecided(), "{:?}", verdict);
    assert_eq!(verdict.code(), Some("FRONTIER"));
    assert_eq!(verdict.kind(), "outside_frontier");
    assert!(verdict.to_json().contains("\"admitted\":false"));
    match verdict {
        Verdict::OutsideFrontier { reason } => {
            // The refusal states the bound rather than describing a resource
            // failure: an embedder has to be able to act on it.
            assert!(reason.contains(&MAX_LEVEL_ITEMS.to_string()), "{}", reason);
            assert!(reason.contains("items"), "{}", reason);
        }
        other => panic!("a level over the bound must not be decided, got {:?}", other),
    }
}

#[test]
fn a_source_at_the_level_bound_is_still_decided() {
    // Non-vacuity in the other direction: a ceiling, not a wall. A body at the
    // bound is decided exactly as it was before the bound existed.
    let verdict = admit(&flat_body(MAX_LEVEL_ITEMS));
    assert_ne!(verdict.kind(), "outside_frontier", "{:?}", verdict);
    assert_eq!(verdict, Verdict::NoObjection);
}

// The front end is recursive descent, so nesting costs stack. The byte bound
// above does not cover the SHAPE: a source can sit far under MAX_SOURCE_BYTES
// and still nest thousands of levels deep. The self-host bounds the nesting
// (`selfhost/parser.rvl`'s `nesting_limit`, measured ahead of any descent in
// `lower.rvl`'s `nesting_depth`), so a deep source is a clean refusal that
// names the bound — never a stack-exhaustion abort, which no `catch_unwind`
// could turn back into a verdict. These pin that: over-deep is refused, well
// under the byte bound, for both the expression ladder (nested `(...)`) and
// the statement descent (nested `if` blocks).

fn is_nesting_refusal(v: &Verdict) -> bool {
    match v {
        Verdict::Refused { code, message } => {
            code == "BAD" && message.contains("nesting is deeper than the parser's limit")
        }
        _ => false,
    }
}

#[test]
fn a_deeply_nested_expression_is_refused_not_aborted() {
    let depth = 5_000;
    let src = format!(
        "fn f() -> Int {{ return {}0{} }}",
        "(".repeat(depth),
        ")".repeat(depth),
    );
    // The point of the case: deep, yet a small fraction of the byte bound.
    assert!(src.len() < MAX_SOURCE_BYTES / 10);
    let verdict = admit(&src);
    assert!(
        is_nesting_refusal(&verdict),
        "a source this deep must be a clean nesting refusal, not {:?}",
        verdict,
    );
    assert!(verdict.is_refused());
    assert_ne!(verdict, Verdict::NoObjection);
}

#[test]
fn a_deeply_nested_statement_block_is_refused_not_aborted() {
    let depth = 5_000;
    let mut src = String::from("fn f() -> Int {\n");
    for _ in 0..depth {
        src.push_str("if (true) {\n");
    }
    src.push_str("return 0\n");
    for _ in 0..depth {
        src.push_str("}\n");
    }
    src.push_str("return 0\n}\n");
    assert!(src.len() < MAX_SOURCE_BYTES);
    let verdict = admit(&src);
    assert!(
        is_nesting_refusal(&verdict),
        "a block nest this deep must be a clean nesting refusal, not {:?}",
        verdict,
    );
    assert!(verdict.is_refused());
}

#[test]
fn a_moderately_nested_source_is_not_refused_for_depth() {
    // Non-vacuity in the other direction: a nesting well within the bound must
    // not trip the depth refusal, so the bound is a ceiling and not a wall.
    let depth = 20;
    let src = format!(
        "fn f() -> Int {{ return {}0{} }}",
        "(".repeat(depth),
        ")".repeat(depth),
    );
    let verdict = admit(&src);
    assert!(
        !is_nesting_refusal(&verdict),
        "a shallow nesting must not be refused for depth, got {:?}",
        verdict,
    );
}

#[test]
fn compile_to_refuses_on_both_tiers() {
    for tier in [Tier::Py, Tier::Rust] {
        match compile_to("fn id(x: Int) -> Int { return x }", tier) {
            Err(Verdict::OutsideFrontier { reason }) => {
                assert!(reason.contains("not available"), "{}", reason);
            }
            other => panic!("compile_to must fail closed, got {:?}", other),
        }
    }
}

// ---------------------------------------------------------------- versions

#[test]
fn the_version_surface_names_the_frontier_and_the_layer() {
    let version = gate_version();
    assert!(!version.api.is_empty());
    assert!(!version.language.is_empty());
    assert!(
        version.frontier.starts_with("selfhost-admit:"),
        "the frontier id must say which gate this is: {}",
        version.frontier
    );
    assert!(
        version.layer.contains("NOT the reference type layer"),
        "the layer string must be explicit about what is missing: {}",
        version.layer
    );
}
'''


SYMBOLS_RS = r'''//! The native NAVIGATION surface: the declarations the self-host front end
//! finds in a document, with the source line each was declared on.
//!
//! GENERATED by `tools/build_gate_crate.py`. Do not edit by hand.
//!
//! # Why this is a separate surface from [`crate::admit`]
//!
//! [`crate::admit`] answers "may this program be refused". This module answers
//! "what did the front end declare, and where" — the input an editor needs for
//! go-to-definition and for the signature half of hover (roadmap item 336
//! slice 2, design `docs/design/336-native-single-binary-tooling.md`).
//!
//! The two surfaces have opposite risk profiles, and that is why navigation may
//! run natively while diagnostics may not. A diagnostics engine that answers
//! NOTHING where the reference refuses shows green on refused code — the
//! editor's false-admit, the defect class this arc exists to prevent. A
//! navigation engine that answers nothing merely fails to jump: the developer
//! notices immediately and nothing unsafe was claimed. So this module's
//! contract is deliberately asymmetric:
//!
//! * it answers only what it can answer EXACTLY, and
//! * every uncertainty — a construct it cannot parse, a name that might be
//!   shadowed by a local, a signature it cannot spell the way the reference
//!   spells it — becomes "no answer", never a guess.
//!
//! A caller that treats [`Symbols::Undecided`] and a missing name as "ask the
//! reference" therefore never sees a WRONG location; it only sees a slower one.
//!
//! # What it covers
//!
//! The self-host front end (`selfhost/lower.rvl::parse_prog`) recognises
//! top-level `fn`, `extern ... fn`, `service` and `component` declarations. It
//! does not model `pub`, `verified`, `type` declarations, fn type parameters,
//! parameters or `let` bindings, so those are NOT in the table, and a document
//! using the ones it cannot parse at all yields [`Symbols::Undecided`].
//!
//! # How the lines are recovered
//!
//! `parse_prog` returns declared NAMES but discards their source lines, so the
//! lines (and the parameter spellings a signature needs) are read back off the
//! self-host LEXER's token stream, which carries a line per token. The token
//! walk is not a second parser: its result must agree with `parse_prog`'s name
//! set exactly, and any disagreement discards the whole table
//! ([`Symbols::Undecided`]). The self-host parser stays the authority; the walk
//! only recovers what the parser's own record shape drops.

use serde_json::Value;

use crate::frontier;
use crate::selfhost;

/// What a symbol was declared as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SymbolKind {
    Fn,
    Extern,
    Service,
    Component,
}

impl SymbolKind {
    /// The wire name, matching the reference LSP's `Symbol.kind` spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            SymbolKind::Fn => "fn",
            SymbolKind::Extern => "extern",
            SymbolKind::Service => "service",
            SymbolKind::Component => "component",
        }
    }
}

/// One declaration an editor can jump to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    /// The declared name.
    pub name: String,
    pub kind: SymbolKind,
    /// One-based line of the declaring KEYWORD (`fn`, `extern`, `service`,
    /// `component`), which is the line the reference front end records.
    pub line: i64,
    /// The signature to show on hover, spelled exactly as the reference spells
    /// it — or `None` when this crate cannot guarantee that spelling, in which
    /// case a caller must ask the reference rather than show an approximation.
    pub detail: Option<String>,
}

/// The declarations of one document, or a refusal to answer for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Symbols {
    /// Every top-level declaration this crate is willing to resolve. A name
    /// ABSENT from a table is not "undeclared": it is "not resolvable by this
    /// crate", and the caller must ask the reference.
    Table(Vec<Symbol>),
    /// This crate is not entitled to answer navigation for this document at
    /// all: a frontier gap, a source over [`crate::MAX_SOURCE_BYTES`], a parse
    /// the self-host front end could not complete, an internal abort, or a
    /// token walk that disagreed with the parser.
    Undecided { reason: String },
}

impl Symbols {
    /// The declaration of `name`, or `None` when this crate will not resolve
    /// it. `None` always means "ask the reference", never "no such name".
    pub fn get(&self, name: &str) -> Option<&Symbol> {
        match self {
            Symbols::Table(rows) => rows.iter().find(|row| row.name == name),
            Symbols::Undecided { .. } => None,
        }
    }

    /// True when this crate declined to answer for the document at all.
    pub fn is_undecided(&self) -> bool {
        matches!(self, Symbols::Undecided { .. })
    }

    /// The rows, empty for an undecided document.
    pub fn rows(&self) -> &[Symbol] {
        match self {
            Symbols::Table(rows) => rows,
            Symbols::Undecided { .. } => &[],
        }
    }
}

/// The document's top-level declarations as the self-host front end sees them.
///
/// Pure and disk-pure, like [`crate::admit`]: no disk, no clock, no cordis
/// runtime. Fails closed to [`Symbols::Undecided`] on every uncertainty.
pub fn symbols(source: &str) -> Symbols {
    if let Some(reason) = frontier::scan(source) {
        return Symbols::Undecided { reason };
    }
    let owned = source.to_string();
    // Same reason `admit` catches: the emitted front end is deeply recursive
    // and total only over the surface it was written for, and "written for" is
    // the thing this crate refuses to assume.
    match std::panic::catch_unwind(move || collect(owned)) {
        Ok(symbols) => symbols,
        Err(_) => Symbols::Undecided {
            reason: String::from(
                "the native front end aborted while reading this document's declarations; \
                 this is a frontier gap — ask the reference `revl` toolchain",
            ),
        },
    }
}

fn undecided(reason: &str) -> Symbols {
    Symbols::Undecided {
        reason: reason.to_string(),
    }
}

/// A token as this module reads it: the generated records give their fields no
/// rust visibility, so the shim reads them through the `serde` derive the
/// emitter puts on every record.
struct Tok {
    kind: String,
    text: String,
    line: i64,
}

fn tokens_of(raw: &[selfhost::Token]) -> Option<Vec<Tok>> {
    let value = serde_json::to_value(raw).ok()?;
    let rows = value.as_array()?.clone();
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        out.push(Tok {
            kind: row.get("kind")?.as_str()?.to_string(),
            text: row.get("text")?.as_str()?.to_string(),
            line: row.get("line")?.as_i64()?,
        });
    }
    Some(out)
}

fn collect(source: String) -> Symbols {
    let raw = selfhost::lex_src(source.clone());
    let program = match serde_json::to_value(selfhost::parse_prog(source)) {
        Ok(value) => value,
        Err(_) => return undecided("the native front end's program record could not be read"),
    };
    // `parse_prog` records a parse problem in `bad` and keeps going. The
    // reference's own symbol builder returns an EMPTY table when the parse
    // fails ("a parse failure yields an empty table rather than raising"), and
    // a half-parsed program is exactly where a token walk would invent a
    // declaration, so this crate declines the document instead.
    if program.get("bad").and_then(Value::as_str).unwrap_or("") != "" {
        return undecided(
            "the native front end could not parse this document, so it resolves no symbols",
        );
    }
    let Some(tokens) = tokens_of(&raw) else {
        return undecided("the native front end's token stream could not be read");
    };

    let Some(walk) = walk_declarations(&tokens) else {
        return undecided("the native token walk could not read this document's declarations");
    };
    if !walk.agrees_with(&program) {
        // The walk is only allowed to recover what the parser dropped. If the
        // two disagree about WHAT was declared, the walk is reading a shape the
        // parser read differently, and no row of it can be trusted.
        return undecided(
            "the native token walk and the native parser disagree about this document's \
             declarations, so none of them is resolvable here",
        );
    }

    let locals = local_names(&tokens);
    let mut rows: Vec<Symbol> = Vec::new();
    for decl in &walk.decls {
        // A name declared at top level AND used as a parameter, a `let`
        // binding, a config field, a `requires` bind or a record key anywhere
        // in the document may be shadowed at the cursor. The reference resolves
        // the innermost scope first; this crate cannot see scopes, so it drops
        // the name rather than risk jumping past a local declaration.
        if locals.contains(&decl.name) {
            continue;
        }
        // `type` declarations are invisible to the self-host parser but are
        // globals to the reference, and the reference's later insert wins. A
        // name that is also a `type` would resolve to the type there.
        if walk.type_names.contains(&decl.name) {
            continue;
        }
        if walk.duplicated(&decl.name) {
            continue;
        }
        rows.push(Symbol {
            name: decl.name.clone(),
            kind: decl.kind,
            line: decl.line,
            detail: signature(&tokens, &raw, decl),
        });
    }
    Symbols::Table(rows)
}

// ------------------------------------------------------------- the token walk

struct Decl {
    name: String,
    kind: SymbolKind,
    line: i64,
    /// Index of the declaration's `fn` keyword, for the signature reader.
    /// Meaningless for a service/component.
    fn_at: usize,
    /// The `pure` / `acquire` / `emission` classification of an extern, and
    /// whether it was marked `async`.
    classification: Option<String>,
    is_async: bool,
}

struct Walk {
    decls: Vec<Decl>,
    type_names: Vec<String>,
}

impl Walk {
    fn agrees_with(&self, program: &Value) -> bool {
        let mut parsed: Vec<String> = Vec::new();
        for key in ["fns", "svcs", "comps"] {
            let Some(rows) = program.get(key).and_then(Value::as_array) else {
                return false;
            };
            for row in rows {
                match row.get("name").and_then(Value::as_str) {
                    Some(name) => parsed.push(name.to_string()),
                    None => return false,
                }
            }
        }
        let mut walked: Vec<String> = self.decls.iter().map(|d| d.name.clone()).collect();
        parsed.sort();
        walked.sort();
        parsed == walked
    }

    fn duplicated(&self, name: &str) -> bool {
        self.decls.iter().filter(|d| d.name == name).count() > 1
    }
}

/// Every top-level declaration in the token stream, or `None` if the braces do
/// not balance (in which case nothing here is trustworthy).
fn walk_declarations(tokens: &[Tok]) -> Option<Walk> {
    let mut decls = Vec::new();
    let mut type_names = Vec::new();
    let mut depth: i64 = 0;
    let mut index = 0usize;
    while index < tokens.len() {
        let token = &tokens[index];
        match token.kind.as_str() {
            "{" | "[" | "(" => {
                depth += 1;
                index += 1;
                continue;
            }
            "}" | "]" | ")" => {
                depth -= 1;
                if depth < 0 {
                    return None;
                }
                index += 1;
                continue;
            }
            _ => {}
        }
        if depth != 0 || token.kind != "kw" {
            index += 1;
            continue;
        }
        match token.text.as_str() {
            "service" | "component" => {
                let name = ident_at(tokens, index + 1)?;
                decls.push(Decl {
                    name,
                    kind: if token.text == "service" {
                        SymbolKind::Service
                    } else {
                        SymbolKind::Component
                    },
                    line: token.line,
                    fn_at: index,
                    classification: None,
                    is_async: false,
                });
            }
            "type" => type_names.push(ident_at(tokens, index + 1)?),
            "fn" => {
                let name = ident_at(tokens, index + 1)?;
                decls.push(Decl {
                    name,
                    kind: SymbolKind::Fn,
                    line: token.line,
                    fn_at: index,
                    classification: None,
                    is_async: false,
                });
            }
            "extern" => {
                // `extern [pure|acquire|emission] [async] fn name(...)`, flags
                // in any order — the order `p_extern` accepts.
                let mut cursor = index + 1;
                let mut classification = None;
                let mut is_async = false;
                while let Some(flag) = tokens.get(cursor) {
                    if flag.kind != "kw" {
                        break;
                    }
                    match flag.text.as_str() {
                        "pure" | "acquire" | "emission" => {
                            classification = Some(flag.text.clone());
                        }
                        "async" => is_async = true,
                        _ => break,
                    }
                    cursor += 1;
                }
                let head = tokens.get(cursor)?;
                if head.kind != "kw" || head.text != "fn" {
                    return None;
                }
                decls.push(Decl {
                    name: ident_at(tokens, cursor + 1)?,
                    kind: SymbolKind::Extern,
                    // the reference records the `extern` keyword's line
                    line: token.line,
                    fn_at: cursor,
                    classification,
                    is_async,
                });
                index = cursor + 1;
            }
            _ => {}
        }
        index += 1;
    }
    Some(Walk { decls, type_names })
}

fn ident_at(tokens: &[Tok], index: usize) -> Option<String> {
    let token = tokens.get(index)?;
    if token.kind == "ident" {
        Some(token.text.clone())
    } else {
        None
    }
}

/// An OVER-approximation of every name that might be a local somewhere in the
/// document: any identifier annotated with a type (`name: T` — parameters,
/// config fields, `requires` binds, record keys) and every identifier bound by
/// a `let` (including the destructuring form, whose names run from the `let` to
/// the `=`).
///
/// Over-approximating is the safe direction: an extra name here only makes this
/// crate defer to the reference. Under-approximating would let a global shadow
/// a local and produce a WRONG jump, which is the one thing this module may
/// never do.
fn local_names(tokens: &[Tok]) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for (index, token) in tokens.iter().enumerate() {
        if token.kind == "ident"
            && tokens.get(index + 1).map(|next| next.kind.as_str()) == Some(":")
        {
            names.push(token.text.clone());
        }
        if token.kind == "kw" && token.text == "let" {
            let mut cursor = index + 1;
            while let Some(bound) = tokens.get(cursor) {
                if bound.kind == "=" || bound.kind == "eof" || bound.line != token.line {
                    break;
                }
                if bound.kind == "ident" {
                    names.push(bound.text.clone());
                }
                cursor += 1;
            }
        }
    }
    names.sort();
    names.dedup();
    names
}

// -------------------------------------------------------------- the signature

/// The hover signature for one declaration, spelled the way the reference LSP
/// spells it, or `None` when this crate cannot guarantee that spelling.
fn signature(tokens: &[Tok], raw: &[selfhost::Token], decl: &Decl) -> Option<String> {
    match decl.kind {
        // The reference renders these as the bare keyword and name, so they are
        // exact by construction.
        SymbolKind::Service => Some(format!("service {}", decl.name)),
        SymbolKind::Component => Some(format!("component {}", decl.name)),
        SymbolKind::Fn => {
            let public = decl.fn_at.checked_sub(1)
                .and_then(|index| tokens.get(index))
                .is_some_and(|token| token.kind == "kw" && token.text == "pub");
            let prefix = if public { "pub " } else { "" };
            Some(format!("{prefix}fn {}", callable_tail(tokens, raw, decl)?))
        }
        SymbolKind::Extern => {
            // The reference spells the classification first and `async` after
            // it, whatever order the source used.
            let classification = decl.classification.as_ref()?;
            let asynchronous = if decl.is_async { " async" } else { "" };
            Some(format!(
                "extern {classification}{asynchronous} fn {}",
                callable_tail(tokens, raw, decl)?
            ))
        }
    }
}

/// `name(p: T, q: U) -> R` for a `fn`/`extern` whose `fn` keyword sits at
/// `decl.fn_at`, using the self-host parser's OWN parameter and type readers so
/// the spellings are the front end's, not this shim's.
fn callable_tail(tokens: &[Tok], raw: &[selfhost::Token], decl: &Decl) -> Option<String> {
    // `fn name (` — anything else (a type-parameter list, say) is a shape the
    // self-host signature readers do not model, so no signature is offered.
    if tokens.get(decl.fn_at + 2)?.kind != "(" {
        return None;
    }
    let parsed =
        serde_json::to_value(selfhost::params_at(raw.to_vec(), decl.fn_at as i64 + 3)).ok()?;
    if parsed.get("ok")?.as_bool()? != true {
        return None;
    }
    let mut rendered: Vec<String> = Vec::new();
    for param in parsed.get("ps")?.as_array()? {
        let name = param.get("name")?.as_str()?;
        let spelling = param.get("ty")?.as_str()?;
        if spelling.is_empty() {
            // an unannotated parameter; the reference renders its own
            // placeholder for that, which this crate will not guess
            return None;
        }
        rendered.push(format!("{name}: {spelling}"));
    }
    let after = parsed.get("i")?.as_i64()?;
    let mut returns = String::new();
    if tokens.get(after as usize).map(|t| t.kind.as_str()) == Some("arrow") {
        let ty = serde_json::to_value(selfhost::type_at(raw.to_vec(), after + 1)).ok()?;
        if ty.get("ok")?.as_bool()? != true {
            return None;
        }
        returns = format!(" -> {}", ty.get("ty")?.as_str()?);
    }
    Some(format!(
        "{}({}){returns}",
        decl.name,
        rendered.join(", ")
    ))
}
'''

TESTS_SYMBOLS_RS = r'''//! The navigation surface's own tests (`cargo test`), run with no Python on
//! the machine. GENERATED by `tools/build_gate_crate.py` — do not edit by hand.
//!
//! `tests/admit.rs` pins the verdict surface. This file pins the other half of
//! the contract that makes [`revl_gate::symbols::symbols`] safe to put behind
//! an editor: it answers only what it can answer exactly, and every uncertainty
//! is an absence, never a guess. The reference-agreement half — that an answer
//! here is byte-identical to `python -P -m revl.lsp`'s (the `-P` is the
//! PYTHONSAFEPATH safety bit, issue #317) — lives in
//! `crates/revl-lsp/tests/reference_agreement.rs`, because it needs the
//! reference.

use revl_gate::symbols::{symbols, SymbolKind, Symbols};

const CLEAN: &str = "\
extern pure fn parse_port(raw: Str) -> Int = @py { return int(raw) }
extern emission async fn publish(topic: Str, payload: Map[Str, Int]) = @py { pass }
fn pick(rows: List[Str], fallback: Str?) -> Str {
  return fallback ?? rows[0]
}
service Clock {
  fn now() -> Int
}
component Ticker {
}
";

fn table(source: &str) -> Symbols {
    let found = symbols(source);
    assert!(!found.is_undecided(), "expected a table, got {found:?}");
    found
}

#[test]
fn every_top_level_declaration_is_found_with_its_own_line() {
    let found = table(CLEAN);
    let mut rows: Vec<(&str, &str, i64)> = found
        .rows()
        .iter()
        .map(|row| (row.name.as_str(), row.kind.as_str(), row.line))
        .collect();
    rows.sort();
    assert_eq!(
        rows,
        vec![
            ("Clock", "service", 6),
            ("Ticker", "component", 9),
            ("parse_port", "extern", 1),
            ("pick", "fn", 3),
            ("publish", "extern", 2),
        ]
    );
}

#[test]
fn a_method_inside_a_service_is_not_a_top_level_declaration() {
    // `now` is a method on `Clock`, and the reference's symbol table carries
    // only module-level declarations plus a scope's own locals. A brace-blind
    // walk would report it as a global `fn`.
    assert!(table(CLEAN).get("now").is_none());
}

#[test]
fn a_signature_is_spelled_the_way_the_reference_spells_it() {
    let found = table(CLEAN);
    let detail = |name: &str| found.get(name).unwrap().detail.clone();
    assert_eq!(
        detail("pick").as_deref(),
        // `Str?` renders as the desugared `Opt[Str]`
        Some("fn pick(rows: List[Str], fallback: Opt[Str]) -> Str")
    );
    assert_eq!(
        detail("parse_port").as_deref(),
        Some("extern pure fn parse_port(raw: Str) -> Int")
    );
    // classification first, then `async`, whatever order the source used, and
    // no `-> ` at all for a declaration with no return type
    assert_eq!(
        detail("publish").as_deref(),
        Some("extern emission async fn publish(topic: Str, payload: Map[Str, Int])")
    );
    assert_eq!(detail("Clock").as_deref(), Some("service Clock"));
    assert_eq!(detail("Ticker").as_deref(), Some("component Ticker"));
}

#[test]
fn an_async_flag_before_the_classification_still_reads_in_reference_order() {
    let source = "extern async emission fn publish(topic: Str) = @py { pass }\n";
    assert_eq!(
        table(source).get("publish").unwrap().detail.as_deref(),
        Some("extern emission async fn publish(topic: Str)")
    );
}

#[test]
fn an_unclassified_extern_gets_no_signature() {
    // the reference spells the classification into the signature; with none in
    // the source there is nothing to spell, so no signature is offered
    let found = table("extern fn f() = @py { pass }\n");
    let symbol = found.get("f").expect("the declaration is still navigable");
    assert_eq!(symbol.kind, SymbolKind::Extern);
    assert_eq!(symbol.detail, None);
}

#[test]
fn a_construct_the_front_end_cannot_parse_makes_the_whole_document_undecided() {
    for source in [
        "verified fn f() -> Int {\n  return 1\n}\n",
        "fn broken( {\n",
    ] {
        let found = symbols(source);
        assert!(found.is_undecided(), "{source:?} produced {found:?}");
        assert!(found.get("f").is_none());
        assert!(found.rows().is_empty());
    }
}

#[test]
fn public_and_cached_functions_preserve_reference_signatures() {
    for (source, expected) in [
        ("pub fn f() -> Int { return 1 }\n", "pub fn f() -> Int"),
        ("fn f() -> Int cache pure { return 1 }\n", "fn f() -> Int"),
        ("pub fn f() -> Int cache pure { return 1 }\n", "pub fn f() -> Int"),
    ] {
        let found = table(source);
        let symbol = found.get("f").unwrap();
        assert_eq!(symbol.line, 1);
        assert_eq!(symbol.detail.as_deref(), Some(expected));
    }
}

#[test]
fn a_name_that_is_also_a_local_is_left_to_the_reference() {
    // the reference resolves the innermost scope first, and this crate cannot
    // see scopes, so a name it might have to lose to a local is not resolved
    // here at all
    assert!(table("fn total(total: Int) -> Int {\n  return total\n}\n")
        .get("total")
        .is_none());
    assert!(table("fn seed() -> Int {\n  let seed = 1\n  return seed\n}\n")
        .get("seed")
        .is_none());
    assert!(table("fn parts() -> Int {\n  let {parts, rest} = split()\n  return 1\n}\n")
        .get("parts")
        .is_none());
}

#[test]
fn a_type_declaration_takes_a_name_back_from_a_fn() {
    // `type` is invisible to the self-host parser but is a module-level
    // declaration to the reference, and the reference's later insert wins
    assert!(table("type Row = { id: Int }\n\nfn Row() -> Int {\n  return 1\n}\n")
        .get("Row")
        .is_none());
}

#[test]
fn a_duplicated_name_is_not_resolved() {
    assert!(table("fn f() -> Int {\n  return 1\n}\nservice f {\n}\n")
        .get("f")
        .is_none());
}

#[test]
fn a_frontier_gap_and_an_oversized_source_are_undecided() {
    // The lexical probe is GENERATED from the frontier table, never hand-picked:
    // this test used to name `.codepoint_at()`, item 391 ported it, and the
    // assertion outlived the gap it was asserting. `None` here means both
    // lexical rows are empty at this generation, which is a legitimate state —
    // the size bound below is the always-live trigger.
    let probe: Option<&str> = @FRONTIER_PROBE@;
    if let Some(name) = probe {
        let src = format!("fn f(s: Str) -> Int {{\n  return s.{}(0)\n}}\n", name);
        let gap = symbols(&src);
        assert!(gap.is_undecided(), "{gap:?}");
    }
    let huge = "// pad\n".repeat(revl_gate::MAX_SOURCE_BYTES / 7 + 1);
    assert!(symbols(&huge).is_undecided());
}

#[test]
fn an_empty_document_has_an_empty_table_rather_than_a_refusal() {
    let found = symbols("");
    assert_eq!(found, Symbols::Table(Vec::new()));
    assert!(found.get("anything").is_none());
}

#[test]
fn the_navigation_surface_issues_no_verdict() {
    // Navigation is not admission. Nothing here reports on whether a program
    // may run, so no caller can mistake a populated table for a green light:
    // `CLEAN` and a program the gate REFUSES both yield ordinary tables.
    let refused = "service S { fn op(x: Str) -> Str }\n\
                   component A provides s: S { provide s { fn op(x) { return x } } }\n\
                   component B provides s: S { provide s { fn op(x) { return x } } }\n";
    assert!(revl_gate::admit(refused).is_refused(), "the fixture must be refused");
    assert!(!symbols(refused).is_undecided(),
            "a refused program still has declarations to navigate");
    assert!(symbols(refused).get("A").is_some());
    assert!(symbols(refused).get("B").is_some());
}
'''

CARGO_TOML_TEMPLATE = r'''# GENERATED by tools/build_gate_crate.py — do not edit by hand.
[package]
name = "revl-gate"
version = "@CRATE_VERSION@"
edition = "2021"
description = "The revl admission gate as an embeddable library (layer 1, admit-only, frontier-limited)"
license = "Apache-2.0"

[lib]
name = "revl_gate"
path = "src/lib.rs"

[dependencies]
# The emitted self-host module speaks cordis-rs's value layer (`cordis::Value`),
# so the crate GRAPH carries cordis-rs. No cordis RUNTIME participates in a
# verdict: `admit` never constructs a Context, never plugs, never boots. That
# distinction is the design's "the crate's purity is runtime-purity, not
# graph-purity" — taken in the open, and item 336's call to change.
cordis = { package = "cordis-rs", version = "0.6" }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
'''


README_TEMPLATE = r"""# revl-gate

The revl admission gate as an embeddable rust library. Roadmap item 332,
Stage 3; design: `docs/design/332-embeddable-gate-api.md`.

**GENERATED — do not edit by hand.** Every file in this directory is written by
`tools/build_gate_crate.py` from the self-host compiler sources. CI regenerates
from the same tree and fails on any byte difference
(`tests/test_gate_crate_drift.py`). To change the crate, change the generator.

    python3 tools/build_gate_crate.py            # regenerate
    python3 tools/build_gate_crate.py --check    # the drift gate

## What it gives you

```rust
use revl_gate::{admit, Verdict};

match admit(source) {
    // Definitive, and byte-agreeing with the reference on the covered corpus.
    Verdict::Refused { code, message } => reject(code, message),
    // NOT an admission — see below.
    Verdict::NoObjection => ask_the_reference(source),
    // The gate declined to decide at all.
    Verdict::OutsideFrontier { reason } => ask_the_reference(source),
}
```

`admit` is a pure function: no disk, no clock, no live state, no cordis runtime
boot. It is `selfhost/lower.rvl`'s `admit_src` — the native lex / parse /
composition-guarantee chain — compiled to rust through the reference rust
backend.

`admit_into` is the same gate across a composition boundary: the verdict on a
candidate once it is admitted INTO a RUNNING composition (item 186).

```rust
use revl_gate::{admit_into, Verdict};

// The running composition, in item 186's row wire: `Kv` provides `store`,
// `App` provides `app` and requires `store`.
let running = "Kv/store/;App/app/;App<store";

match admit_into(candidate, running) {
    Verdict::Refused { code, message } => reject(code, message),
    Verdict::NoObjection => ask_the_reference(candidate),
    Verdict::OutsideFrontier { reason } => ask_the_reference(candidate),
}
```

The crate builds with no Python on the machine. That is why the generated source
is committed rather than produced at install time (items 336 and 338 depend on
it).

## The verdict surface issues no admissions

Read this before wiring the crate into anything.

The self-host compiler is behind the reference implementation (roadmap item
391), and the gap is not "a few missing constructs" — it is a whole missing
LAYER. `admit_src` decides the composition and guarantee layer (`G1`..`G4`,
`A1`, `PRELUDE`, and parse failures as `BAD`). It does **not** run the
reference's type layer. Measured, not assumed: the reference refuses all of

    fn f() -> Int { return "s" }
    fn f() -> Int { return undefined_name }
    fn f() -> { }

and the self-host gate raises no objection to any of them.

So `Verdict` has no admitting arm and no `is_admitted()`. Its non-refusing arm
is `Verdict::NoObjection`, meaning *"this gate found nothing it is able to
refuse"* — never *"the reference would admit this"*. On the wire, `to_json()`
emits `"admitted": false` for **every** arm, so a consumer written against the
design's fixed `{admitted, code, message}` shape reads the verdict surface as
"never admits" rather than misreading a no-objection. The arm itself travels in
the extra `"verdict"` field
(`"refused"` / `"no_objection"` / `"outside_frontier"`).

The asymmetry is the whole design: refusing what the reference admits is an
inconvenience; **admitting what the reference refuses is the defect class the
admission-gate arc exists to prevent.**

## The admission surface (issue #346)

An admission is a SECOND question, asked through a second type so the two cannot
be confused: `issue_admission(source)` returns an `Admission`, not a `Verdict`.

```rust
match revl_gate::issue_admission("service Store { fn get(key: Str) -> Str }") {
    revl_gate::Admission::Admitted { basis } => println!("admitted: {basis}"),
    revl_gate::Admission::Withheld { verdict } => println!("withheld: {verdict:?}"),
}
```

`Admission::Admitted` is reachable through exactly one path, and both conditions
are necessary:

1. `admit(source)` returned `Verdict::NoObjection` — the composition/guarantee
   gate ran and found nothing to refuse. An admission is never issued over a
   refusal or a frontier gap.
2. the source is inside the ADMISSION SURFACE (`ADMISSION_SURFACE_ID`,
   `src/admission.rs`) — the region where the covered layer is the WHOLE
   question, because the source carries no term the type layer decides.

The surface is deliberately tiny, and its one line is `ADMITTED_LAYER`:

    @ADMITTED_LAYER@

That is not the covered layer read optimistically; it is the sliver of it where
reading a no-objection as an admission is sound. No body, no expression, no
literal, no generic head. A source outside it is `Admission::Withheld` carrying
the verdict verbatim, so switching a consumer from `admit` to `issue_admission`
can only ADD the admitted wire — every other answer is byte-identical to the one
it already handled. Widening the surface is the self-host type layer's lane
(`docs/design/457-selfhost-type-layer.md`).

`issue_admission_into(source, manifest)` asks the same question against a running
composition. The empty manifest is the empty composition, so it is
`issue_admission` byte for byte. Against a NON-EMPTY manifest only a candidate
that DECLARES NOTHING is admitted, and the reason is the item-186 row wire rather
than the certifier: a row carries a component name, a provision key and a realm,
and no service shapes, so a declared `service Store` may collide with a `Store`
the running composition already holds in a different shape and the wire cannot
say. The reference refuses exactly that pair. Carrying the running shapes on the
wire is the remaining half of issue #346.

An issued admission serialises `{"verdict":"admitted","admitted":true,
"code":null,"message":null}` — byte-identical to `revl.gate`'s own wire for a py
admission, so a seam comparing the two tiers compares equal bytes. The basis is
off the wire on purpose: it is evidence for a log, not part of the contract.

## Fail closed at the frontier

`Verdict::OutsideFrontier` means *this gate is not entitled to decide*, and the
crate returns it whenever:

* the source uses a construct in the generated frontier table below;
* the source is larger than the bound the gate will decide (a stack overflow in
  the deeply-recursive native front end ABORTS, and an abort cannot be turned
  back into a refusal);
* the source has more items at one bracket level than the gate will decide —
  the emitted parser recurses once per SIBLING item, so a flat `g(1, 1, …)` a
  few KB long and one bracket deep exhausts the stack where neither the size
  bound nor the nesting bound can see it. The bound is
  `revl_gate::MAX_LEVEL_ITEMS`;
* the native gate panics while deciding (caught via `catch_unwind`);
* the native gate returns a verdict wire shape this crate does not recognise;
* in `admit_into`, the manifest wire is longer than the bound the gate will
  decide, or carries more `;`-separated rows than the gate will fold, or the
  fold returns a shape this crate does not recognise. Both manifest limits are
  checked BEFORE the wire reaches the parser, because the byte bound is not a
  row bound: the fold consumes one stack frame per row, so 2 700 rows of
  `A/b/;` are 13 KB and still take a 1 MiB stack down. The row half of the pair
  is `revl_gate::MANIFEST_ROW_LIMIT`.

### The generated frontier table at this generation

Not hand-listed. `tools/build_gate_crate.py` computes it as the difference
between the reference compiler's own tables and the self-host sources, so a
reference construct added without a self-host port changes these bytes and reds
the drift gate.

* Reference keywords the self-host does not lex: @KEYWORD_LINE@
* Reference stdlib builtins the self-host does not lower as builtins:
  @BUILTIN_LINE@

## The manifest arm (issue #346)

`admit_into(source, manifest)` asks a different question from `admit`: not "is
this text well formed on its own", but "does this text compose with the
composition that is ALREADY RUNNING". `source` is decided against the UNION of
the manifest and the incoming text, so a key the running composition already
holds conflicts (`G2`), a route into a realm the union does not provide dangles
(`G2`), and a dependency cycle spanning the manifest boundary is a cycle
(`G3`). The decision is the native fold `selfhost/lower.rvl::admit_ambient`,
compiled to rust like `admit`, and the manifest arrives as item 186's row wire
(`docs/design/186-ambient-admission-guarantees.md`): `C/k/r` for a provision
(`r` the realm, `""` for shared), `C<k` for a requirement, `!halted` for a
halted composition, joined by `;`. The empty manifest is the empty composition,
so `admit_into(source, "")` is `admit(source)` byte for byte — the arm
generalises `admit` rather than re-implementing it.

Two honest limits, both fail-closed:

* **It closes the `G2`/`G3` legs and nothing else.** The reference TYPE layer is
  its own lane (the self-host compiler has no type layer yet), so a
  type-incorrect candidate is a no-objection here, exactly as in `admit`. This
  arm does not RESOLVE the requirements a candidate declares either; it checks
  them for disjointness and acyclicity. The reference remains the only tier that
  admits.
* **A row it cannot honour is REFUSED, never skipped.** The wire reserves row
  kinds for waves that have not landed — replacement (`-C`) and handoff
  (`C=k:T`), both of which need the type layer. Those rows come back as a
  `MANIFEST` refusal. Ignoring a row would be the wave-through this crate exists
  to prevent.

## What is deliberately absent

* **`compile_to` output.** Exported, and it refuses unconditionally: the
  self-host emitters still carry `@py`-only helper externs and do not emit to
  rust. Stage 4's lane.
* **The reference type layer.** Still absent, in `admit` and in `admit_into`
  alike: neither arm issues an admission. That lane is the type layer's, not the
  manifest parameter's.
* **The deferred manifest rows.** Replacement and handoff rows are refused, for
  the reason in the section above: they need the type layer.
* **Layer 2 (the session surface).** `revl_gate::session::Session` is item 334's
  foundational first slice: the generation state machine, the untrusted-author
  admission entry (`propose`/`admit`/`admit_into`), and the item-245
  witnessed-call recording path (`call`/`commit`/`abort`/`unload`). The
  accept-and-swap half, the witnessed-effect runtime, the WAL and the approver
  callback are later slices; a candidate the native gate does not refuse is
  fail-closed, never admitted. `Session::admit_into` takes the manifest wire as
  a PARAMETER, not as a projection of the loaded composition: a manifest also
  needs requirements and realms, and synthesising rows out of what the session
  holds would be inventing a running composition.

## Host obligations

* Build the calling profile with `panic = "unwind"`. Under `panic = "abort"` the
  fail-closed panic path cannot run and a native gate abort takes the process
  down instead. Loud, so still not a false admission — but not the intended
  behaviour.
* The default panic hook prints to stderr when the fail-closed path fires.
  Install your own hook if that matters.

## The navigation surface

```rust
use revl_gate::symbols::{symbols, Symbols};

match symbols(source) {
    // Every top-level declaration this crate will resolve, and the line each
    // was declared on. A name that is ABSENT is not "undeclared" — it is "not
    // resolvable here", and the caller must ask the reference.
    Symbols::Table(rows) => navigate(rows),
    // Not entitled to answer for this document at all.
    Symbols::Undecided { reason } => ask_the_reference(source),
}
```

A second surface over the same front end, for editor navigation (roadmap item
336 slice 2): go-to-definition and the signature half of hover. It issues no
verdicts — a program the gate REFUSES still has declarations to navigate — so it
is versioned by `SYMBOLS_API_VERSION`, not by the gate api above.

Its fail-closed rule mirrors the gate's, pointed at the risk navigation actually
carries. A navigation engine that answers nothing merely fails to jump; one that
answers WRONGLY sends a developer to the wrong declaration. So it answers only
what it can answer exactly, and everything else is an absence: a construct the
self-host parser cannot read makes the whole document `Undecided`, a name a
parameter or `let` might shadow is dropped (this crate cannot see scopes), and a
signature it cannot spell the way the reference spells it comes back as
`detail: None`.

## Versions

    revl_gate::gate_version()
    // api      "@GATE_API_VERSION@"
    // language "@LANGUAGE_VERSION@"
    // frontier "@FRONTIER_ID@"
    // layer    "@COVERED_LAYER@"

`api` is the gate surface semver (bumped by surface changes only); the
navigation surface carries its own, `SYMBOLS_API_VERSION`. `language` is
the revl version this gate's refusals are drawn from. `frontier` identifies the
COVERED surface: two gates with different frontier ids cover different
languages, and their agreement carries no information. `layer` says in prose
what the gate decides. Codes are append-only; message text is not promised
stable across versions.
"""

IR_RS_TEMPLATE = r'''//! The IR-boundary decode guard — GENERATED by `tools/build_gate_crate.py`.
//! Do not edit; edit the generator and regenerate (`--check` is a CI gate).
//!
//! The rust mirror of `revl.compiler._refuse_unknown_ir_fields` and
//! `_refuse_unknown_schema_revision` (roadmap item 479).
//!
//! A previously-compiled IR document re-entering a gate is exactly the seam
//! where a gate-crate / frontend SKEW goes undetected: an unknown field or an
//! unrecognised schema revision that one tier silently ignores while the other
//! refuses is a wave-through of an unaccounted-for member (cf. the recurring
//! gate-crate-drift class). So this tier fails closed the way the Python
//! frontend does — it REFUSES BY NAME rather than ignoring — and the two tiers
//! are held to the same known-field / known-revision sets by
//! `tests/test_gate_ir_boundary_drift.py`.
//!
//! The known sets below are DERIVED from `revl.lower.IR_TOPLEVEL_FIELDS` and
//! `revl.lower.IR_SCHEMA_REVISIONS` — the frontend's own authority on "what a
//! staged IR document knows" — so a field or revision added on the Python side
//! without regenerating this crate changes these bytes and reds the drift gate
//! in the same wave.
//!
//! This guard only ever REFUSES; it issues no admission and decides nothing
//! about a composition, so it is not the manifest-spanning `admit_into` the
//! crate deliberately omits. It refuses a document whose top-level SHAPE this
//! frontend does not know, and nothing more.

use serde_json::Value;

@KNOWN_IR_FIELDS@
@KNOWN_IR_REVISIONS@

/// The `code` an unknown-top-level-field refusal reports on the wire.
pub const IR_UNKNOWN_FIELD_CODE: &str = "IR_UNKNOWN_FIELD";
/// The `code` an unknown-schema-revision refusal reports on the wire.
pub const IR_UNKNOWN_REVISION_CODE: &str = "IR_UNKNOWN_REVISION";
/// The `code` a document that is not decodable as a JSON object reports.
pub const IR_MALFORMED_CODE: &str = "IR_MALFORMED";

/// A refusal from the IR boundary.
///
/// `code` is API (append-only; an existing code never changes meaning);
/// `message` is the diagnostic and byte-agrees with the Python frontend's on
/// the covered shapes (`tests/test_gate_ir_boundary_drift.py`). This is NEVER
/// an admission — see the module docs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IrRefusal {
    /// The guarantee code (`IR_UNKNOWN_FIELD`, `IR_UNKNOWN_REVISION`,
    /// `IR_MALFORMED`).
    pub code: String,
    /// The diagnostic, naming the offending field(s) or revision.
    pub message: String,
}

impl IrRefusal {
    /// The design's fixed `{"admitted", "code", "message"}` wire shape.
    ///
    /// `"admitted"` is `false`: an IR-boundary refusal is never an admission,
    /// so a consumer of the fixed triple fails closed. Mirrors
    /// [`crate::Verdict::to_json`].
    pub fn to_json(&self) -> String {
        let mut out = String::from("{\"admitted\":false,\"code\":");
        out.push_str(&crate::json_string(&self.code));
        out.push_str(",\"message\":");
        out.push_str(&crate::json_string(&self.message));
        out.push('}');
        out
    }
}

/// Decode-guard a staged IR document at the boundary.
///
/// `Ok(())` means the document's top-level SHAPE is one this frontend knows —
/// NOT that the program may run (that is the reference type layer, which this
/// crate never runs). Any unknown top-level field, or a present-but-unrecognised
/// `ir_version`, is refused BY NAME; a document that is not a decodable JSON
/// object is refused as malformed. Fields are checked before the revision,
/// matching the Python frontend's order, so a document that is wrong in both
/// ways reports the field refusal — the same one the reference reports.
pub fn check_ir_boundary(document: &str) -> Result<(), IrRefusal> {
    let value: Value = match serde_json::from_str(document) {
        Ok(value) => value,
        Err(err) => {
            return Err(IrRefusal {
                code: String::from(IR_MALFORMED_CODE),
                message: format!(
                    "staged IR is not decodable JSON ({}); this frontend refuses a document it cannot decode at the IR boundary rather than guessing its shape",
                    err
                ),
            })
        }
    };
    let object = match value.as_object() {
        Some(object) => object,
        None => {
            return Err(IrRefusal {
                code: String::from(IR_MALFORMED_CODE),
                message: String::from(
                    "staged IR top level is not a JSON object; this frontend refuses a document that is not an IR document at the IR boundary rather than guessing its shape",
                ),
            })
        }
    };
    refuse_unknown_ir_fields(object)?;
    refuse_unknown_schema_revision(object)?;
    Ok(())
}

fn is_known_field(field: &str) -> bool {
    KNOWN_IR_FIELDS.iter().any(|known| *known == field)
}

/// Refuse a document carrying a top-level field this frontend does not know,
/// naming the field(s) sorted so the diagnostic is deterministic. The mirror of
/// `_refuse_unknown_ir_fields`.
fn refuse_unknown_ir_fields(
    object: &serde_json::Map<String, Value>,
) -> Result<(), IrRefusal> {
    let mut unknown: Vec<&str> = object
        .keys()
        .map(|key| key.as_str())
        .filter(|key| !is_known_field(key))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort_unstable();
    let named = unknown
        .iter()
        .map(|field| format!("`{}`", field))
        .collect::<Vec<_>>()
        .join(", ");
    let plural = if unknown.len() > 1 { "s" } else { "" };
    let known = KNOWN_IR_FIELDS.join(", ");
    Err(IrRefusal {
        code: String::from(IR_UNKNOWN_FIELD_CODE),
        message: format!(
            "staged IR carries unknown top-level field{} {}; this frontend refuses an IR document with a field it does not know rather than ignoring it (known fields: {})",
            plural, named, known
        ),
    })
}

/// Refuse a document stamped with a schema revision this frontend does not
/// know, naming the revision. The mirror of `_refuse_unknown_schema_revision`:
/// a missing `ir_version`, or a `null` one, passes — only a PRESENT,
/// unrecognised revision is refused.
fn refuse_unknown_schema_revision(
    object: &serde_json::Map<String, Value>,
) -> Result<(), IrRefusal> {
    let revision = match object.get("ir_version") {
        None => return Ok(()),
        Some(Value::Null) => return Ok(()),
        Some(revision) => revision,
    };
    if let Some(known) = as_known_revision(revision) {
        if KNOWN_IR_REVISIONS.contains(&known) {
            return Ok(());
        }
    }
    let known = KNOWN_IR_REVISIONS
        .iter()
        .map(|revision| revision.to_string())
        .collect::<Vec<_>>()
        .join(", ");
    Err(IrRefusal {
        code: String::from(IR_UNKNOWN_REVISION_CODE),
        message: format!(
            "staged IR declares unknown schema revision `ir_version` {}; this frontend refuses an IR document at a revision it does not know rather than decoding it under the wrong one (known revisions: {})",
            repr_revision(revision),
            known
        ),
    })
}

/// The integer value of a revision for the membership test, matching Python's
/// numeric equality (`2.0 in {1, 2, 3}` is true): an integer, or an integral
/// float. A non-integral or non-numeric revision has no integer value and is
/// refused.
fn as_known_revision(value: &Value) -> Option<i64> {
    if let Some(int) = value.as_i64() {
        return Some(int);
    }
    if let Some(uint) = value.as_u64() {
        if uint <= i64::MAX as u64 {
            return Some(uint as i64);
        }
    }
    if let Some(float) = value.as_f64() {
        if float.fract() == 0.0 && float.abs() < 9.007_199_254_740_992e15 {
            return Some(float as i64);
        }
    }
    None
}

/// A best-effort Python-`repr` of the offending revision, so the diagnostic
/// reads the same on both tiers for the revisions that actually occur
/// (integers): a JSON integer renders as its decimal digits, matching
/// `repr(int)`. Other JSON scalars render close to `repr` for the message's
/// sake but are not promised byte-identical across tiers.
fn repr_revision(value: &Value) -> String {
    match value {
        Value::Number(number) => number.to_string(),
        Value::String(text) => format!("'{}'", text),
        Value::Bool(flag) => String::from(if *flag { "True" } else { "False" }),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_known_shape_at_a_known_revision_passes() {
        assert!(check_ir_boundary(
            "{\"ir_version\": 3, \"services\": {}, \"components\": []}"
        )
        .is_ok());
    }

    #[test]
    fn a_missing_or_null_revision_passes() {
        assert!(check_ir_boundary("{\"services\": {}, \"components\": []}").is_ok());
        assert!(check_ir_boundary("{\"ir_version\": null}").is_ok());
    }

    #[test]
    fn an_unknown_top_level_field_is_refused_by_name() {
        let refusal =
            check_ir_boundary("{\"ir_version\": 3, \"wobble\": 1}").unwrap_err();
        assert_eq!(refusal.code, IR_UNKNOWN_FIELD_CODE);
        assert!(refusal.message.contains("`wobble`"), "{}", refusal.message);
        assert!(refusal.to_json().contains("\"admitted\":false"));
    }

    #[test]
    fn unknown_fields_are_named_sorted_and_pluralised() {
        let refusal = check_ir_boundary("{\"zebra\": 1, \"alpha\": 2}").unwrap_err();
        assert!(
            refusal
                .message
                .contains("unknown top-level fields `alpha`, `zebra`"),
            "{}",
            refusal.message
        );
    }

    #[test]
    fn an_unknown_schema_revision_is_refused_by_name() {
        let refusal = check_ir_boundary("{\"ir_version\": 7}").unwrap_err();
        assert_eq!(refusal.code, IR_UNKNOWN_REVISION_CODE);
        assert!(
            refusal.message.contains("`ir_version` 7"),
            "{}",
            refusal.message
        );
    }

    #[test]
    fn the_field_check_precedes_the_revision_check() {
        let refusal =
            check_ir_boundary("{\"ir_version\": 7, \"wobble\": 1}").unwrap_err();
        assert_eq!(refusal.code, IR_UNKNOWN_FIELD_CODE);
    }

    #[test]
    fn a_non_object_document_is_refused_as_malformed() {
        assert_eq!(
            check_ir_boundary("[1, 2, 3]").unwrap_err().code,
            IR_MALFORMED_CODE
        );
        assert_eq!(
            check_ir_boundary("not json").unwrap_err().code,
            IR_MALFORMED_CODE
        );
    }
}
'''

TESTS_IR_RS = r'''//! The IR-boundary decode guard's own integration tests (`cargo test`), run
//! with no Python in the loop (roadmap item 479).
//!
//! The crate refuses an unknown top-level field / unknown schema revision at
//! the IR boundary BY NAME, the mirror of the Python frontend. This drives the
//! public surface from a consumer's vantage; the cross-tier agreement — that
//! both tiers refuse the SAME shapes and derive the SAME known sets — is held
//! by `tests/test_gate_ir_boundary_drift.py`, which needs no toolchain.

use revl_gate::{check_ir_boundary, KNOWN_IR_FIELDS, KNOWN_IR_REVISIONS};

#[test]
fn a_known_document_passes_the_boundary() {
    assert!(check_ir_boundary(
        "{\"ir_version\": 3, \"services\": {}, \"components\": []}"
    )
    .is_ok());
}

#[test]
fn an_unknown_field_is_refused_by_name_with_the_wire_shape() {
    let refusal = check_ir_boundary("{\"services\": {}, \"surprise\": 1}").unwrap_err();
    assert_eq!(refusal.code, "IR_UNKNOWN_FIELD");
    assert!(refusal.message.contains("`surprise`"), "{}", refusal.message);
    assert!(
        refusal
            .message
            .contains("refuses an IR document with a field it does not know"),
        "{}",
        refusal.message
    );
    assert!(refusal.message.contains("known fields:"), "{}", refusal.message);
    // never an admission
    assert!(refusal.to_json().contains("\"admitted\":false"));
}

#[test]
fn an_unknown_revision_is_refused_naming_the_known_set() {
    let refusal = check_ir_boundary("{\"ir_version\": 99}").unwrap_err();
    assert_eq!(refusal.code, "IR_UNKNOWN_REVISION");
    assert!(refusal.message.contains("`ir_version` 99"), "{}", refusal.message);
    let known: Vec<String> = KNOWN_IR_REVISIONS.iter().map(|r| r.to_string()).collect();
    assert!(
        refusal.message.contains(&format!("known revisions: {}", known.join(", "))),
        "{}",
        refusal.message
    );
}

#[test]
fn a_non_object_document_fails_closed() {
    assert_eq!(check_ir_boundary("42").unwrap_err().code, "IR_MALFORMED");
    assert_eq!(check_ir_boundary("{").unwrap_err().code, "IR_MALFORMED");
}

#[test]
fn the_known_field_set_is_the_frontend_surface() {
    // a spot-check that the derived set carries the load-bearing members; the
    // exact set is pinned against the Python frontend in the drift test.
    assert!(KNOWN_IR_FIELDS.contains(&"ir_version"));
    assert!(KNOWN_IR_FIELDS.contains(&"manifest"));
    assert!(KNOWN_IR_FIELDS.contains(&"components"));
}
'''

GITIGNORE = """# GENERATED by tools/build_gate_crate.py — do not edit by hand.
/target
Cargo.lock
"""


def render_generated_json(digest: str, fid: str, language: str,
                          tables: dict[str, list[str]], ir: dict,
                          admission: dict[str, list[str]]) -> str:
    payload = {
        "generator": "tools/build_gate_crate.py",
        "crate": "revl-gate",
        "crate_version": CRATE_VERSION,
        "gate_api_version": GATE_API_VERSION,
        "language_version": language,
        "frontier": fid,
        "source_digest": digest,
        "selfhost_root": SELFHOST_ROOT,
        "selfhost_closure": list(SELFHOST_CLOSURE),
        "digest_inputs": list(DIGEST_INPUTS),
        "frontier_excluded_keywords": tables["keywords"],
        "frontier_excluded_builtins": tables["builtins"],
        # The IR-boundary surface (item 479), derived from the reference
        # frontend so the crate cannot drift from it silently. `revl_gate::ir`
        # refuses a document carrying a field / revision outside these sets.
        "ir_toplevel_fields": ir["fields"],
        "ir_schema_revisions": ir["revisions"],
        "max_source_bytes": MAX_SOURCE_BYTES,
        "max_level_items": MAX_LEVEL_ITEMS,
        "layer": "1 (verdict surface), admit-only",
        # The ADMISSION surface (issue #346), versioned apart from the frontier:
        # the frontier bounds where the gate may REFUSE, this bounds where it may
        # ADMIT. The tables are derived from the reference compiler's own, so a
        # scalar the reference stops treating as a scalar cannot stay in the
        # certifier's vocabulary silently.
        "admission_surface": admission_surface_id(digest),
        "admitted_layer": ADMITTED_LAYER,
        "admission_scalar_types": admission["scalars"],
        "admission_reserved_type_names": admission["reserved"],
        "symbols_api_version": SYMBOLS_API_VERSION,
        "navigation_surface": "revl_gate::symbols — declarations and their lines; issues no verdicts",
        "covered_layer": COVERED_LAYER,
        # The gate DOES issue admissions now, through `issue_admission` and its
        # own `Admission` type — and only inside `admitted_layer`. The VERDICT
        # surface still has no admitting arm, which is what keeps a host that
        # holds a `Verdict` from reading one as a green.
        "issues_admissions": True,
        "verdict_arms": ["refused", "no_objection", "outside_frontier"],
        "admission_arms": ["admitted", "withheld"],
        "admission_arm": (
            "revl_gate::issue_admission(source) / issue_admission_into(source, "
            "manifest) -> Admission. `Admitted` requires BOTH that `admit` "
            "returned NoObjection and that the source is inside "
            "admission_surface; everything else is `Withheld` carrying the "
            "verdict verbatim. Against a non-empty manifest only a candidate "
            "that declares nothing is admitted: the item-186 row wire carries no "
            "service shapes, so a declared service may collide with a running "
            "one and the wire cannot say."
        ),
        "manifest_arm": (
            "revl_gate::admit_into(source, manifest) — the item-186 ambient gate "
            "as a binding: the union fold's G2/G3 legs. Refuses the deferred "
            "manifest rows (replacement `-C`, handoff `C=k:T`) rather than "
            "skipping them, and issues no admission."
        ),
        "note": ("Regenerate with `python3 tools/build_gate_crate.py`. The "
                 "source_digest is a pure function of digest_inputs, not a git "
                 "sha, so `--check` can verify the committed crate against the "
                 "tree it was generated from. The VERDICT surface admits "
                 "nothing, by construction: that surface decides "
                 "the composition/guarantee layer, not the reference type layer, "
                 "so its non-refusing arm is `no_objection` and never an "
                 "admission — across the manifest boundary too. Admissions are "
                 "issued only through the separate `Admission` surface, inside "
                 "`admitted_layer`, where the covered layer is the whole "
                 "question (issue #346)."),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------- generation

DEFAULT_OUT = ROOT / "crates" / "revl-gate"


def render(tables: dict[str, list[str]], digest: str, fid: str, language: str,
           selfhost_rs: str, ir: dict,
           admission: dict[str, list[str]]) -> dict[str, str]:
    """Every generated file as {relpath: content}. Pure: same inputs, same
    bytes, which is what makes the drift gate meaningful."""
    keyword_line = (", ".join(f"`{k}`" for k in tables["keywords"])
                    or "(none at this generation)")
    builtin_line = (", ".join(f"`.{n}()`" for n in tables["builtins"])
                    or "(none at this generation)")
    # The crate's own integration test needs a construct that is actually in the
    # table; when the table is empty there is none, and the size bound carries
    # the fail-closed property alone. Generated rather than hand-picked: item 391
    # ported `.is_digit()` and `.str()`, and the hand-picked probes that named
    # them went on asserting a gap that had closed.
    probe = (f'Some("{tables["builtins"][0]}")' if tables["builtins"] else "None")
    return {
        "Cargo.toml": CARGO_TOML_TEMPLATE.replace("@CRATE_VERSION@", CRATE_VERSION),
        ".gitignore": GITIGNORE,
        "GENERATED.json": render_generated_json(digest, fid, language, tables, ir,
                                               admission),
        "README.md": (README_TEMPLATE
                      .replace("@KEYWORD_LINE@", keyword_line)
                      .replace("@BUILTIN_LINE@", builtin_line)
                      .replace("@GATE_API_VERSION@", GATE_API_VERSION)
                      .replace("@LANGUAGE_VERSION@", language)
                      .replace("@COVERED_LAYER@", COVERED_LAYER)
                      .replace("@ADMITTED_LAYER@", ADMITTED_LAYER)
                      .replace("@FRONTIER_ID@", fid)),
        "src/lib.rs": (LIB_RS_TEMPLATE
                       .replace("@GATE_API_VERSION@", GATE_API_VERSION)
                       .replace("@COVERED_LAYER@", COVERED_LAYER)
                       .replace("@ADMITTED_LAYER@", ADMITTED_LAYER)
                       .replace("@LANGUAGE_VERSION@", language)),
        "src/admission.rs": (ADMISSION_RS_TEMPLATE
                             .replace("@ADMISSION_SURFACE_ID@",
                                      admission_surface_id(digest))
                             .replace("@SCALAR_TYPES@", _rust_str_array(
                                 "SCALAR_TYPES", admission["scalars"],
                                 "/// The type names a certified signature may mention. Derived from the\n"
                                 "/// reference's own scalar data set (`revl.typecheck._CONFIG_DATA_SCALARS`):\n"
                                 "/// every one is a concrete builtin with no type parameter and no erasure, so\n"
                                 "/// a signature written over them resolves with no checker to run.\n"))
                             .replace("@RESERVED_TYPE_NAMES@", _rust_str_array(
                                 "RESERVED_TYPE_NAMES", admission["reserved"],
                                 "/// The type names a certified source may not DECLARE. Derived from\n"
                                 "/// `revl.typecheck._BUILTIN_TYPE_NAMES` plus the reserved opaque\n"
                                 "/// `Principal`. Shadowing one of these is the reference's business, so a\n"
                                 "/// source that tries is not certified here.\n"))
                             .replace("@REFERENCE_KEYWORDS@", _rust_str_array(
                                 "REFERENCE_KEYWORDS", admission["keywords"],
                                 "/// The REFERENCE keyword set (`revl.lexer.KEYWORDS`), not the self-host one.\n"
                                 "/// The question the certifier asks is what the REFERENCE would make of the\n"
                                 "/// source, so a keyword the self-host has not ported must still not be\n"
                                 "/// mistaken for a name.\n"))),
        "src/frontier.rs": (FRONTIER_RS_TEMPLATE
                            .replace("@FRONTIER_ID@", fid)
                            .replace("@MAX_SOURCE_BYTES@", str(MAX_SOURCE_BYTES))
                            .replace("@MANIFEST_ROW_LIMIT@", str(MANIFEST_ROW_LIMIT))
                            .replace("@MAX_LEVEL_ITEMS@", str(MAX_LEVEL_ITEMS))
                            .replace("@EXCLUDED_KEYWORDS@", _rust_str_array(
                                "EXCLUDED_KEYWORDS", tables["keywords"],
                                "/// Reference language keywords the self-host front end does not lex.\n"
                                "/// Derived as `revl.lexer.KEYWORDS - selfhost/lexer.rvl::keywords()`.\n"
                                "/// Empty is a legitimate value (the two lexers agree today); it becomes\n"
                                "/// non-empty the moment the reference grows a keyword the self-host has\n"
                                "/// not ported, and the drift gate makes that visible in the same wave.\n"))
                            .replace("@EXCLUDED_BUILTINS@", _rust_str_array(
                                "EXCLUDED_BUILTINS", tables["builtins"],
                                "/// Reference stdlib builtin methods the self-host lowering does not treat\n"
                                "/// as builtins. Derived as `revl.typecheck._BUILTIN_SIG -\n"
                                "/// selfhost/lower.rvl::is_builtin_method`. A call to one of these lowers\n"
                                "/// differently in the two compilers, so the crate refuses to decide the\n"
                                "/// program at all rather than risk deciding it wrongly.\n"))),
        "src/ir.rs": (IR_RS_TEMPLATE
                      .replace("@KNOWN_IR_FIELDS@", _rust_pub_str_array(
                          "KNOWN_IR_FIELDS", ir["fields"],
                          "/// The complete top-level surface of a staged IR document this frontend\n"
                          "/// knows, sorted. Derived from `revl.lower.IR_TOPLEVEL_FIELDS`: a field\n"
                          "/// outside it is one no schema revision this frontend understands has ever\n"
                          "/// emitted, so a document carrying it is refused by name rather than having\n"
                          "/// the field silently ignored.\n"))
                      .replace("@KNOWN_IR_REVISIONS@", _rust_pub_i64_array(
                          "KNOWN_IR_REVISIONS", ir["revisions"],
                          "/// The immutable set of IR schema revisions this frontend understands,\n"
                          "/// sorted. Derived from `revl.lower.IR_SCHEMA_REVISIONS`. A document stamped\n"
                          "/// with a revision outside it was emitted by a newer/forked frontend or a\n"
                          "/// drifted gate, and is refused rather than decoded under the wrong one.\n"))),
        "src/selfhost.rs": selfhost_rs,
        "src/session.rs": SESSION_RS,
        "src/symbols.rs": SYMBOLS_RS,
        "tests/admit.rs": TESTS_ADMIT_RS.replace("@FRONTIER_PROBE@", probe),
        "tests/ir.rs": TESTS_IR_RS,
        "tests/symbols.rs": TESTS_SYMBOLS_RS.replace("@FRONTIER_PROBE@", probe),
    }


def build() -> dict[str, str]:
    """Compile the self-host gate to rust and render every crate file."""
    rustemit = _load_module("backends/rust/emit.py", "rustemit_gate_crate")
    from revl import compile_files  # noqa: PLC0415

    # `revl.gate`'s api semver and this generator's must not drift apart: the
    # crate and the wheel are two spellings of ONE surface.
    from revl.gate import GATE_API_VERSION as PY_API  # noqa: PLC0415
    if PY_API != GATE_API_VERSION:
        raise SystemExit(
            f"build_gate_crate: gate api semver mismatch — revl.gate says "
            f"{PY_API!r}, this generator says {GATE_API_VERSION!r}. The crate "
            f"and the wheel are one surface; bump both or neither.")

    ir_document = compile_files([str(ROOT / SELFHOST_ROOT)])
    selfhost_rs = rustemit.emit(ir_document)
    tables = frontier_tables()
    ir = ir_tables()
    admission = admission_tables()
    digest = source_digest()
    return render(tables, digest, frontier_id(digest), language_version(),
                  selfhost_rs, ir, admission)


def generate(out: Path) -> dict[str, str]:
    """Write the whole crate under `out`. Returns {relpath: content}."""
    files = build()
    for rel, content in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return files


def drift(committed: Path) -> list[str]:
    """The list of drift problems between `committed` and a fresh generation —
    empty when they agree byte for byte. Conformance-matrix discipline: a
    committed generated artifact that differs from a fresh generation is drift,
    and drift is a red."""
    with tempfile.TemporaryDirectory(prefix="revl_gate_crate_check_") as tmp:
        fresh = Path(tmp)
        files = generate(fresh)
        problems = []
        for rel in sorted(files):
            have = committed / rel
            if not have.exists():
                problems.append(f"MISSING    {rel}")
            elif not filecmp.cmp(have, fresh / rel, shallow=False):
                problems.append(f"DIFFERS    {rel}")
        if committed.exists():
            for path in sorted(committed.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(committed).as_posix()
                if rel.startswith("target/") or rel == "Cargo.lock":
                    continue
                if rel not in files:
                    problems.append(f"UNEXPECTED {rel}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the revl-gate rust crate from the self-host gate.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="crate directory (default: crates/revl-gate)")
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed crate differs from a fresh "
                             "generation (the CI drift gate)")
    args = parser.parse_args(argv[1:])
    if args.check:
        problems = drift(args.out)
        if problems:
            print(f"gate crate DRIFT at {args.out}:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            print("\nRegenerate with: python3 tools/build_gate_crate.py",
                  file=sys.stderr)
            return 1
        print(f"gate crate is in sync with the tree ({args.out}).")
        return 0
    files = generate(args.out)
    print(f"wrote {len(files)} files to {args.out}")
    for rel in sorted(files):
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
