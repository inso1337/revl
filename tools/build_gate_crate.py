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
* `compile_to` -> `Verdict::OutsideFrontier` unconditionally (Stage 4);
* `admit_into` -> not defined at all; manifest-spanning admission does not exist
  on the self-host path and a fake would be worse than its absence.

A crate that cannot issue an admission cannot commit the false-admit defect.
What it does buy is the sound direction: a local, in-process, Python-free
REFUSAL that byte-agrees with the reference on the covered corpus
(`tests/test_gate_crate_admit.py`).

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

# What the native gate actually decides, in one line, stamped into the crate's
# `COVERED_LAYER`, its README and its provenance so the three cannot disagree.
# Measured, not assumed: `selfhost/lower.rvl`'s `admit_src` runs no type layer,
# so `fn f() -> Int { return "s" }` (which the reference refuses) draws no
# objection from it. That measurement is why the crate ships no admission.
COVERED_LAYER = ("composition + guarantee layer (G1..G4, A1, PRELUDE) and "
                 "parse (BAD); NOT the reference type layer")


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
//! gate) compiled to rust through the reference rust backend.
//!
//! ```no_run
//! use revl_gate::{admit, Verdict};
//!
//! match admit("fn id(x: Int) -> Int { return x }") {
//!     // A definitive refusal. Byte-agreeing with the reference compiler on
//!     // the covered corpus: stop here, and show the message as-is.
//!     Verdict::Refused { code, message } => println!("refused ({}): {}", code, message),
//!     // NOT an admission. See "This gate issues no admissions" below.
//!     Verdict::NoObjection => println!("nothing this gate can refuse"),
//!     // The gate declined to decide at all.
//!     Verdict::OutsideFrontier { reason } => println!("undecided: {}", reason),
//! }
//! ```
//!
//! # This gate issues no admissions
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
//! So there is no `Verdict::Admitted` arm, and no `is_admitted()`. The
//! non-refusing outcome is [`Verdict::NoObjection`], which means exactly
//! *"this gate found nothing it is able to refuse"* and never *"the reference
//! would admit this"*. A host that must ADMIT — because it is about to run the
//! program — still has to get a reference verdict (`revl compile`, or
//! `revl.gate.admit` on py). What this crate buys is the other direction: a
//! local, in-process, allocation-cheap REFUSAL that agrees with the reference
//! byte for byte on the covered corpus, with no round trip and no Python.
//!
//! The two divergence directions are not symmetric, and this asymmetry is the
//! whole design: refusing what the reference admits is an inconvenience;
//! ADMITTING what the reference refuses is the defect class the admission-gate
//! arc exists to prevent. A crate that cannot issue an admission cannot commit
//! that defect.
//!
//! On the wire, [`Verdict::to_json`] therefore emits `"admitted": false` for
//! EVERY arm. A consumer written against the fixed `{admitted, code, message}`
//! shape (`docs/design/332-embeddable-gate-api.md`) reads this gate as
//! "never admits" rather than misreading a no-objection as an admission; the
//! arm itself is carried in the extra `"verdict"` field.
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
//! * the native gate panics while deciding (caught via `catch_unwind`);
//! * the native gate returns a verdict wire shape this crate does not
//!   recognise.
//!
//! # Standalone-only
//!
//! There is no `admit_into`. Admission INTO a running composition spans a
//! manifest (G2/G3 over the live composition), which the self-host pipeline has
//! no parameter for; a stub that ignored the manifest would be exactly the
//! wave-through this crate exists to prevent. Use `revl.gate.admit_into` on py.
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
//! # Layer 2 is reserved, not implemented
//!
//! See [`session`]. That runtime half is roadmap item 334's deliverable; the
//! module exists so landing it is additive.
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

mod frontier;
pub mod session;
pub mod symbols;

pub use frontier::{FRONTIER_ID, MAX_SOURCE_BYTES};

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
/// deliberately absent — see the crate docs, "This gate issues no admissions".
pub const COVERED_LAYER: &str = "@COVERED_LAYER@";

/// The `code` a frontier gap reports on the wire.
pub const FRONTIER_CODE: &str = "FRONTIER";

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
}
'''


SESSION_RS = r'''//! Layer 2, the session surface — **RESERVED, not implemented here**.
//!
//! GENERATED by `tools/build_gate_crate.py`. Do not edit by hand.
//!
//! The design (`docs/design/332-embeddable-gate-api.md`, "Layer 2: the session
//! surface") splits the gate API on purity. Layer 1, the verdict surface, is
//! pure functions of their arguments and is what this crate ships. Layer 2 is
//! stateful and address-space-bound — a live composition, an owner, a deferral
//! queue, witnessed escrow, a WAL — with the operation set
//! `{load, admit, call, commit, abort, unload}`, and it is where the
//! 243/244/245/246/322 guarantees (witnessed effects, session commit,
//! approvals, six-tier crash recovery) actually live.
//!
//! On rust that runtime half is roadmap item 334's deliverable ("gate plus
//! witnessed runtime in one process, accept-and-revert live"), not item 332's.
//! This module exists so that landing it is ADDITIVE: the path
//! `revl_gate::session` is claimed, empty, and documented, rather than being a
//! stub that pretends to hold a session.
//!
//! There is deliberately nothing here to call. A layer-2 stub that accepted a
//! `load` and did nothing would be a wave-through of exactly the kind
//! [`crate::Verdict::OutsideFrontier`] exists to prevent, so the absence is the
//! honest surface until 334 lands.
//!
//! Until then the reference layer-2 surface is `revl.gate.Gate` on py.
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

use revl_gate::{admit, compile_to, gate_version, Tier, Verdict, MAX_SOURCE_BYTES};

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

// --------------------------------------- the gate issues no admissions, ever

#[test]
fn a_clean_program_gets_a_no_objection_which_is_not_an_admission() {
    let verdict = admit("fn id(x: Int) -> Int { return x }");
    assert_eq!(verdict, Verdict::NoObjection);
    assert!(!verdict.is_refused());
    assert!(verdict.to_json().contains("\"admitted\":false"));
}

/// The measured reason `Verdict::Admitted` does not exist.
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
    // `.is_digit()` is a reference stdlib builtin the self-host lowering does
    // not treat as a builtin, so it sits in the generated frontier table.
    let src = "fn f(s: Str) -> Bool { return s.charAt(0).is_digit() }";
    match admit(src) {
        Verdict::OutsideFrontier { reason } => {
            assert!(reason.contains("is_digit"), "reason must name the gap: {}", reason);
        }
        other => panic!("a frontier construct must not be decided, got {:?}", other),
    }
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
    let verdict = admit("fn f(s: Str) -> Bool { return s.charAt(0).is_digit() }");
    assert_eq!(verdict.code(), Some("FRONTIER"));
    assert_eq!(verdict.kind(), "outside_frontier");
    assert!(verdict.to_json().contains("\"admitted\":false"));
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
        SymbolKind::Fn => Some(format!("fn {}", callable_tail(tokens, raw, decl)?)),
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
        serde_json::to_value(selfhost::params_at(raw, decl.fn_at as i64 + 3)).ok()?;
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
        let ty = serde_json::to_value(selfhost::type_at(raw, after + 1)).ok()?;
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
    // `pub` is not a declaration the self-host front end reads
    for source in [
        "pub fn f() -> Int {\n  return 1\n}\n",
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
    // an excluded builtin is a construct the two compilers lower differently
    let gap = symbols("fn f(s: Str) -> Int {\n  return s.codepoint_at(0)\n}\n");
    assert!(gap.is_undecided(), "{gap:?}");
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
cordis = { package = "cordis-rs", version = "0.3" }
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

The crate builds with no Python on the machine. That is why the generated source
is committed rather than produced at install time (items 336 and 338 depend on
it).

## This gate issues no admissions

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

So there is no `Verdict::Admitted` and no `is_admitted()`. The non-refusing arm
is `Verdict::NoObjection`, meaning *"this gate found nothing it is able to
refuse"* — never *"the reference would admit this"*. A host that must ADMIT,
because it is about to run the program, still needs a reference verdict
(`revl compile`, or `revl.gate.admit` on py). What this crate buys is the other
direction: a local, in-process, Python-free REFUSAL that agrees with the
reference byte for byte on the covered corpus.

The asymmetry is the whole design: refusing what the reference admits is an
inconvenience; **admitting what the reference refuses is the defect class the
admission-gate arc exists to prevent.** A crate that cannot issue an admission
cannot commit that defect.

On the wire, `to_json()` emits `"admitted": false` for **every** arm, so a
consumer written against the design's fixed `{admitted, code, message}` shape
reads this gate as "never admits" rather than misreading a no-objection. The
arm itself travels in the extra `"verdict"` field
(`"refused"` / `"no_objection"` / `"outside_frontier"`).

## Fail closed at the frontier

`Verdict::OutsideFrontier` means *this gate is not entitled to decide*, and the
crate returns it whenever:

* the source uses a construct in the generated frontier table below;
* the source is larger than the bound the gate will decide (a stack overflow in
  the deeply-recursive native front end ABORTS, and an abort cannot be turned
  back into a refusal);
* the native gate panics while deciding (caught via `catch_unwind`);
* the native gate returns a verdict wire shape this crate does not recognise.

### The generated frontier table at this generation

Not hand-listed. `tools/build_gate_crate.py` computes it as the difference
between the reference compiler's own tables and the self-host sources, so a
reference construct added without a self-host port changes these bytes and reds
the drift gate.

* Reference keywords the self-host does not lex: @KEYWORD_LINE@
* Reference stdlib builtins the self-host does not lower as builtins:
  @BUILTIN_LINE@

## What is deliberately absent

* **`admit_into`.** Admission INTO a running composition spans a manifest
  (G2/G3 over the live composition). The self-host pipeline has no manifest
  parameter, and a stub that ignored one would be the wave-through this crate
  exists to prevent. Use `revl.gate.admit_into` on py.
* **`compile_to` output.** Exported, and it refuses unconditionally: the
  self-host emitters still carry `@py`-only helper externs and do not emit to
  rust. Stage 4's lane.
* **Layer 2 (the session surface).** `revl_gate::session` is a reserved,
  documented, EMPTY module. The witnessed runtime half is roadmap item 334.

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

GITIGNORE = """# GENERATED by tools/build_gate_crate.py — do not edit by hand.
/target
Cargo.lock
"""


def render_generated_json(digest: str, fid: str, language: str,
                          tables: dict[str, list[str]]) -> str:
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
        "max_source_bytes": MAX_SOURCE_BYTES,
        "layer": "1 (verdict surface), admit-only",
        "symbols_api_version": SYMBOLS_API_VERSION,
        "navigation_surface": "revl_gate::symbols — declarations and their lines; issues no verdicts",
        "covered_layer": COVERED_LAYER,
        "issues_admissions": False,
        "verdict_arms": ["refused", "no_objection", "outside_frontier"],
        "note": ("Regenerate with `python3 tools/build_gate_crate.py`. The "
                 "source_digest is a pure function of digest_inputs, not a git "
                 "sha, so `--check` can verify the committed crate against the "
                 "tree it was generated from. `issues_admissions` is false by "
                 "construction: this gate decides the composition/guarantee "
                 "layer, not the reference type layer, so its non-refusing arm "
                 "is `no_objection` and never an admission."),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------- generation

DEFAULT_OUT = ROOT / "crates" / "revl-gate"


def render(tables: dict[str, list[str]], digest: str, fid: str, language: str,
           selfhost_rs: str) -> dict[str, str]:
    """Every generated file as {relpath: content}. Pure: same inputs, same
    bytes, which is what makes the drift gate meaningful."""
    keyword_line = (", ".join(f"`{k}`" for k in tables["keywords"])
                    or "(none at this generation)")
    builtin_line = (", ".join(f"`.{n}()`" for n in tables["builtins"])
                    or "(none at this generation)")
    return {
        "Cargo.toml": CARGO_TOML_TEMPLATE.replace("@CRATE_VERSION@", CRATE_VERSION),
        ".gitignore": GITIGNORE,
        "GENERATED.json": render_generated_json(digest, fid, language, tables),
        "README.md": (README_TEMPLATE
                      .replace("@KEYWORD_LINE@", keyword_line)
                      .replace("@BUILTIN_LINE@", builtin_line)
                      .replace("@GATE_API_VERSION@", GATE_API_VERSION)
                      .replace("@LANGUAGE_VERSION@", language)
                      .replace("@COVERED_LAYER@", COVERED_LAYER)
                      .replace("@FRONTIER_ID@", fid)),
        "src/lib.rs": (LIB_RS_TEMPLATE
                       .replace("@GATE_API_VERSION@", GATE_API_VERSION)
                       .replace("@COVERED_LAYER@", COVERED_LAYER)
                       .replace("@LANGUAGE_VERSION@", language)),
        "src/frontier.rs": (FRONTIER_RS_TEMPLATE
                            .replace("@FRONTIER_ID@", fid)
                            .replace("@MAX_SOURCE_BYTES@", str(MAX_SOURCE_BYTES))
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
        "src/selfhost.rs": selfhost_rs,
        "src/session.rs": SESSION_RS,
        "src/symbols.rs": SYMBOLS_RS,
        "tests/admit.rs": TESTS_ADMIT_RS,
        "tests/symbols.rs": TESTS_SYMBOLS_RS,
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

    ir = compile_files([str(ROOT / SELFHOST_ROOT)])
    selfhost_rs = rustemit.emit(ir)
    tables = frontier_tables()
    digest = source_digest()
    return render(tables, digest, frontier_id(digest), language_version(),
                  selfhost_rs)


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
