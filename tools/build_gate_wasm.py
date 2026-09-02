#!/usr/bin/env python3
"""Generate `crates/revl-gate-wasm` — the revl admission gate as a WASI-P2
component (roadmap item 335, design `docs/design/335-wasm-edge-gate.md`,
slices 0-2: "the WIT world + the artifact" and "the layer-1 verdict in wasm,
vector-pinned").

What this mechanizes
--------------------
Item 332 Stage 3 turned the self-host gate into a COMMITTED rust crate
(`crates/revl-gate`, `tools/build_gate_crate.py`). This tool wraps that crate,
unchanged, in a component-model shim so the same verdict runs where there is no
Python and no native toolchain — only a wasm runtime: a browser page, an edge
worker, a CDN node, `wasmtime`. Cut A of the design, via the rust lane:

    selfhost/lower.rvl -> (rust backend) -> crates/revl-gate -> (rustc wasm32)
      -> core module -> (wasm-tools component new) -> revl_gate.wasm

The shim adds NO reach the crate lacks. It adds portability, an interface a
component-model host can call without knowing rust, and the two structural
properties the edge needs: an EMPTY import section and a version surface.

The empty import section, and why the target is wasm32-unknown-unknown
----------------------------------------------------------------------
The design's headline soundness mechanism is that the gate world imports
NOTHING: no clock, no filesystem, no random, no host function. That makes the
verdict a total, deterministic function of its arguments, provable from the
artifact's own import section, which is what makes a conformance vector mean
anything (agreement measured once holds everywhere) and what makes the gate
pass item 289's least-authority bar reflexively.

The design named `wasm32-wasip2` first and `wasm32-unknown-unknown` second. We
build the second, because on wasip2 the rust standard library links the WASI
world (`wasi:cli`, `wasi:io`, `wasi:filesystem`, ...) into every artifact and
the import section is then NOT empty — the empty-import property and the wasip2
target are mutually exclusive, and the design is unambiguous about which of the
two is load-bearing. `wasm32-unknown-unknown` needs no adapter here precisely
because there is nothing to adapt: with no imports, `wasm-tools component new`
wraps the core module directly.

The cost, taken in the open: on every wasm target the rust panic strategy is
`abort`, so the crate's `catch_unwind` fail-closed path does not catch. A native
gate panic TRAPS the instance instead of returning `outside_frontier`. A trap is
loud and it is not a verdict, so it is still not a false admission — but it is a
host obligation, stated in the generated README and held by
`tests/test_gate_wasm_vector.py` (a trap is recorded as its own outcome and the
vector requires zero of them).

What this gate can say
----------------------
Exactly what `crates/revl-gate` can say, which is: nothing that admits. The
self-host gate decides the composition/guarantee layer (G1..G4, A1, PRELUDE,
and parse failures as BAD) and does NOT run the reference type layer, so its
non-refusing arm is `no_objection` and never an admission
(`tools/build_gate_crate.py`, "The security clause"). The WIT `verdict` record
therefore reports `admitted: false` on every arm and carries the arm name in
`kind`, so a host reading only the design's fixed `{admitted, code, message}`
triple reads this gate as "never admits" — the fail-closed reading — rather than
mistaking a no-objection for an admission.

`admit-artifact` (design cut B) is exported so the shape is fixed and its
arrival is additive, and it fails closed today: the item-289 chain's `declared
caps` leg reads the G8 boundary projection, which the reference derives with a
whole-IR reachability walk (`revl.__main__._boundary`) that has no native port.
A shim that guessed the declared set would be the wave-through this arc exists
to prevent.

Usage
-----
    python3 tools/build_gate_wasm.py             # (re)generate the crate source
    python3 tools/build_gate_wasm.py --check     # drift gate: fail on any diff
    python3 tools/build_gate_wasm.py --wasm      # build+measure the component
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

DEFAULT_OUT = ROOT / "crates" / "revl-gate-wasm"
GATE_CRATE = ROOT / "crates" / "revl-gate"

# ------------------------------------------------------------------ constants

# The semver of the GATE SURFACE, kept in lockstep with `revl.gate`'s
# `GATE_API_VERSION` and the rust crate's (asserted in `build`, so the three
# spellings of one surface cannot drift apart).
GATE_API_VERSION = "1.0.0"

# This crate's own version, independent of the language version.
CRATE_VERSION = "0.1.0"

# The WIT package/world the component exports. `revl:gate@1.0.0` tracks the gate
# SURFACE semver, not the language version.
WIT_PACKAGE = "revl:gate@1.0.0"
WIT_WORLD = "gate"

# The binding generator. Pinned exactly: the `component-type` custom section it
# writes is what `wasm-tools component new` reads, so an unpinned bump could
# change the artifact under a drift gate that only watches source.
WIT_BINDGEN_REQ = "0.61.1"

# The build target. See the module docstring: wasip2's std brings WASI imports
# and the empty-import property is the one that is load-bearing.
WASM_TARGET = "wasm32-unknown-unknown"

# Every input whose content decides a generated byte.
DIGEST_INPUTS = (
    "crates/revl-gate/GENERATED.json",
    "tools/build_gate_wasm.py",
)

# The one sentence saying what this gate decides, stamped into the rust source,
# the README and the provenance from here so the three cannot disagree. Read out
# of the rust crate's provenance rather than restated, because the wasm gate
# decides EXACTLY what the crate decides.
def covered_layer() -> str:
    return _gate_crate_meta()["covered_layer"]


def _gate_crate_meta() -> dict:
    """The rust crate's provenance. The wasm gate is a packaging of that crate,
    so its frontier id, language version and covered layer are the crate's —
    read, never restated, so a crate regeneration propagates here and reds this
    crate's drift gate in the same wave."""
    path = GATE_CRATE / "GENERATED.json"
    if not path.is_file():
        raise SystemExit(
            f"build_gate_wasm: {path} is missing. Generate the rust crate first: "
            f"python3 tools/build_gate_crate.py")
    return json.loads(path.read_text(encoding="utf-8"))


def source_digest() -> str:
    """A pure function of `DIGEST_INPUTS`, order-sensitive, so `--check` can
    verify the committed crate against the tree it was generated from without a
    git sha."""
    digest = hashlib.sha256()
    for rel in DIGEST_INPUTS:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------- WIT

WIT_TEMPLATE = """// GENERATED by tools/build_gate_wasm.py — do not edit by hand.
//
// The revl admission gate as a component-model world (roadmap item 335,
// docs/design/335-wasm-edge-gate.md §2). Layer 1 of the item-332 gate API
// verbatim: strings and flat records on the boundary, never a host object
// graph, so the same shape crosses a crate ABI and a component boundary
// unchanged.
//
// THE IMPORT LIST IS EMPTY, and that is the point. The gate imports no clock,
// no filesystem, no random, no host function, so its verdict is a total,
// deterministic function of its arguments — provable from the artifact's own
// import section, not from a promise. A build that grows an import fails CI
// (tests/test_gate_wasm_vector.py).

package %(package)s;

world %(world)s {
  /// A verdict from the gate.
  ///
  /// `admitted` is FALSE ON EVERY ARM. This gate is `crates/revl-gate` packaged
  /// for wasm, and that gate decides the composition/guarantee layer
  /// (G1..G4, A1, PRELUDE, and parse failures as BAD) and NOT the reference
  /// type layer, so it has no admission to give. A host reading only the fixed
  /// {admitted, code, message} triple therefore reads this gate as "never
  /// admits" — the fail-closed reading — instead of mistaking a no-objection
  /// for an admission. The real signal is `kind`.
  record verdict {
    /// Always false. See above.
    admitted: bool,
    /// The arm: "refused" | "no-objection" | "outside-frontier".
    kind: string,
    /// The guarantee tag for a refusal ("G1".."G4", "A1", "PRELUDE", "BAD"),
    /// "FRONTIER" when the gate declined to decide, absent for a no-objection.
    code: option<string>,
    /// The why-trace verbatim. Byte-agrees with the reference compiler's
    /// diagnostic on the covered corpus; not promised stable across versions.
    message: option<string>,
  }

  /// Frontend admission over source. Frontier-scoped (the self-host coverage
  /// frontier, pinned in `gate-version`); out-of-surface constructs decline
  /// with an "outside-frontier" verdict, fail closed.
  export admit: func(source: string) -> verdict;

  /// The same verdict as `admit`, serialized in the item-332 wire shape
  /// (`{"verdict", "admitted", "code", "message"}`) — byte-identical to the
  /// rust crate's `Verdict::to_json`. This is the surface the conformance
  /// vector compares, so cross-tier agreement is a byte comparison rather than
  /// a re-encoding that could paper over a difference.
  export admit-json: func(source: string) -> string;

  /// Artifact admission: the item-289 least-authority chain over a compiled IR
  /// and a policy, with the artifact's own import-section capabilities.
  ///
  /// NOT AVAILABLE in this build, and it declines rather than guesses: the
  /// `declared caps` leg of the chain reads the G8 boundary projection, which
  /// the reference derives with a whole-IR reachability walk that has no native
  /// port yet (design slice 3). The signature is fixed here so its arrival is
  /// additive.
  export admit-artifact: func(ir: string, policy: string,
                              imports: list<string>) -> verdict;

  /// `{"api", "language", "frontier", "layer", "tier"}` as json — the item-332
  /// versioning surface, so a host can detect coverage and skew before trusting
  /// a cached edge gate's verdicts.
  export gate-version: func() -> string;
}
"""


def render_wit() -> str:
    return WIT_TEMPLATE % {"package": WIT_PACKAGE, "world": WIT_WORLD}


# --------------------------------------------------------------- Cargo.toml

CARGO_TEMPLATE = """# GENERATED by tools/build_gate_wasm.py — do not edit by hand.
[package]
name = "revl-gate-wasm"
version = "%(crate_version)s"
edition = "2021"
description = "The revl admission gate as a WASI-P2 component (layer 1, refusal-only, empty imports)"
license = "Apache-2.0"

[lib]
name = "revl_gate_wasm"
path = "src/lib.rs"
# A component is built from a cdylib: `wasm-tools component new` wraps the core
# module the linker produces.
crate-type = ["cdylib"]

# Its own workspace root, so a stray Cargo.toml above this directory cannot
# adopt the crate and change what gets built — and so the release profile below
# is actually the one that applies.
[workspace]

[dependencies]
# The gate itself, unchanged. This shim adds no reach: it packages the crate's
# verdict for a component-model host.
revl-gate = { path = "../revl-gate" }
# The binding generator. Pinned exactly (`=`): the `component-type` custom
# section it writes is what `wasm-tools component new` reads, so a silent bump
# would change the artifact behind a drift gate that watches source only.
wit-bindgen = "=%(wit_bindgen)s"

[profile.release]
# An edge artifact is judged on bytes and cold start, so size wins over speed.
opt-level = "s"
lto = true
codegen-units = 1
strip = true
# Redundant but explicit: every wasm target aborts on panic anyway, and the
# generated README states the consequence (a native gate panic traps the
# instance instead of returning `outside-frontier`).
panic = "abort"
"""


def render_cargo_toml() -> str:
    return CARGO_TEMPLATE % {"crate_version": CRATE_VERSION,
                             "wit_bindgen": WIT_BINDGEN_REQ}


# ------------------------------------------------------------------- lib.rs

LIB_TEMPLATE = r'''//! `revl-gate-wasm` — the revl admission gate as a WASI-P2 component
//! (roadmap item 335, design `docs/design/335-wasm-edge-gate.md`).
//!
//! GENERATED by `tools/build_gate_wasm.py`. Do not edit by hand: CI regenerates
//! from the same tree and fails on any byte difference
//! (`tests/test_gate_wasm_drift.py`).
//!
//! # What this is
//!
//! A shim, and deliberately nothing more. Every verdict comes from
//! [`revl_gate`], the committed rust crate item 332 Stage 3 generates from
//! `selfhost/lower.rvl`. This file translates that crate's `Verdict` into the
//! `revl:gate` world's record and back, so a component-model host (wasmtime,
//! jco in a browser, Spin, wasmCloud) can call the gate without knowing rust.
//!
//! The shim adds NO reach. The world imports nothing at all — no clock, no
//! filesystem, no random, no host function — so a verdict is a total,
//! deterministic function of its arguments, and that is provable from the
//! artifact's import section rather than promised in prose.
//!
//! # This gate issues no admissions
//!
//! `admitted` is `false` on every arm, exactly as in the rust crate. The
//! self-host gate decides the composition/guarantee layer and not the reference
//! type layer, so its non-refusing arm means *"this gate found nothing it is
//! able to refuse"* and never *"the reference would admit this"*. See the crate
//! docs for the measurement behind that.
//!
//! # Panics trap; they do not become verdicts
//!
//! `revl_gate::admit` catches a native gate panic and returns
//! `OutsideFrontier`. On wasm the rust panic strategy is `abort`, so
//! `catch_unwind` does not catch and a panic TRAPS the instance instead. A trap
//! is loud, and it is not a verdict, so it is still not a false admission — but
//! a host must treat a trap as "no verdict was reached" and fail closed on it.
//!
//! # `unsafe`
//!
//! Not forbidden at the crate level, and that is not an oversight: the
//! canonical-ABI glue `wit_bindgen::generate!` expands into this crate reads and
//! writes the component boundary's linear-memory buffers and is necessarily
//! `unsafe`. Every line written by hand below is safe rust, and the gate itself
//! (`revl-gate`) carries `#![forbid(unsafe_code)]`.

wit_bindgen::generate!({
    path: "wit",
    world: "@WORLD@",
});

/// The `code` an `outside-frontier` verdict reports on the wire. The rust
/// crate's `FRONTIER_CODE`, re-stated at the boundary so the two wire shapes
/// carry the same token.
const FRONTIER_CODE: &str = revl_gate::FRONTIER_CODE;

/// The tier this packaging runs on, reported by `gate-version` so a host can
/// tell a wasm gate's version surface from the wheel's or the crate's.
const TIER: &str = "wasm";

struct Gate;

impl Guest for Gate {
    /// The native gate's verdict for `source`, as the world's record.
    fn admit(source: String) -> Verdict {
        lift(revl_gate::admit(&source))
    }

    /// The same verdict, in the item-332 wire shape, byte-identical to the rust
    /// crate's `Verdict::to_json`. One serializer, two tiers.
    fn admit_json(source: String) -> String {
        revl_gate::admit(&source).to_json()
    }

    /// Item-289 artifact admission — declines, and says why.
    ///
    /// The chain is `host imports subset-of declared caps subset-of
    /// policy-allowed`. Leg 1's left side is right here (`imports`, read off
    /// the artifact itself) and leg 2's right side is the caller's `policy`,
    /// but the set in the middle — the declared caps — is the G8 boundary
    /// projection, which the reference derives with a whole-IR reachability
    /// walk (`revl.__main__._boundary`) that has no native port. Guessing it
    /// would be exactly the wave-through this arc exists to prevent, so this
    /// declines instead. Design slice 3.
    fn admit_artifact(_ir: String, _policy: String, _imports: Vec<String>) -> Verdict {
        Verdict {
            admitted: false,
            kind: String::from("outside-frontier"),
            code: Some(String::from(FRONTIER_CODE)),
            message: Some(String::from(
                "admit-artifact is not available in this gate: the item-289 chain's `declared caps` leg is the G8 boundary projection, which the reference derives from the whole IR and the native gate has no port of; deciding it here would mean guessing the declared set. Ask the reference `revl` toolchain (revl.least_authority) for an artifact verdict.",
            )),
        }
    }

    /// The version surface a host compares before trusting a cached edge gate.
    fn gate_version() -> String {
        let v = revl_gate::gate_version();
        let mut out = String::from("{\"api\":");
        out.push_str(&json_string(v.api));
        out.push_str(",\"language\":");
        out.push_str(&json_string(v.language));
        out.push_str(",\"frontier\":");
        out.push_str(&json_string(v.frontier));
        out.push_str(",\"layer\":");
        out.push_str(&json_string(v.layer));
        out.push_str(",\"tier\":");
        out.push_str(&json_string(TIER));
        out.push('}');
        out
    }
}

/// The crate's `Verdict` as the world's record. `admitted` is hard-coded
/// `false`: there is no arm that could set it true, and writing the constant
/// here means a future arm cannot flip it by accident.
fn lift(verdict: revl_gate::Verdict) -> Verdict {
    Verdict {
        admitted: false,
        kind: String::from(wire_kind(&verdict)),
        code: verdict.code().map(String::from),
        message: verdict.message().map(String::from),
    }
}

/// The arm's name in WIT spelling. The crate's `kind()` uses rust's
/// `snake_case`; WIT identifiers are `kebab-case`, and the record's `kind`
/// field follows the interface it is read through. The json surface
/// (`admit-json`) keeps the crate's spelling verbatim, so neither wire is a
/// re-encoding of the other.
fn wire_kind(verdict: &revl_gate::Verdict) -> &'static str {
    match verdict.kind() {
        "refused" => "refused",
        "no_objection" => "no-objection",
        _ => "outside-frontier",
    }
}

/// Minimal JSON string encoder for `gate-version`. The boundary carries strings
/// only, so this is the whole serialization need and the shim takes no serde
/// dependency for it.
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

export!(Gate);
'''


def render_lib_rs() -> str:
    return LIB_TEMPLATE.replace("@WORLD@", WIT_WORLD)


# ------------------------------------------------------------------- README

README_TEMPLATE = """<!-- GENERATED by tools/build_gate_wasm.py — do not edit by hand. -->
# revl-gate-wasm

The revl admission gate as a **WASI-P2 component**: roadmap item 335, design
`docs/design/335-wasm-edge-gate.md`. It packages `crates/revl-gate` (item 332
Stage 3) so the same verdict runs where there is no Python and no native
toolchain, only a wasm runtime — a browser page, an edge worker, a serverless
function, a CDN node.

    selfhost/lower.rvl -> crates/revl-gate -> rustc %(target)s
      -> wasm-tools component new -> revl_gate.wasm

## This gate issues no admissions

Read this before wiring the component into anything.

`admitted` is `false` on **every** arm. The self-host gate this is built from
decides the composition and guarantee layer (`G1`..`G4`, `A1`, `PRELUDE`, and
parse failures as `BAD`) and does **not** run the reference type layer, so its
non-refusing arm is `no-objection`, which means *"this gate found nothing it is
able to refuse"* and never *"the reference would admit this"*. The covered
layer, in one line:

> %(covered_layer)s

A host that must ADMIT — because it is about to run the program — still needs a
reference verdict. What the component buys is the other direction: a local,
in-process, Python-free REFUSAL that byte-agrees with the reference on the
covered corpus, with no round trip and no cold-start interpreter.

The two divergence directions are not symmetric, and that asymmetry is the whole
design: refusing what the reference admits is an inconvenience; ADMITTING what
the reference refuses is the defect class this arc exists to prevent. A gate that
cannot issue an admission cannot commit that defect.

## The interface

`wit/%(world)s.wit` is the whole surface: `admit`, `admit-json`,
`admit-artifact` (declines today), `gate-version`.

    wasmtime run --invoke 'admit-json("fn f() -> Int { return 1 }")' revl_gate.wasm

In a browser or node, `jco transpile revl_gate.wasm` produces the JS shim:

```js
import { admit } from "./revl_gate.js";   // jco output

const v = admit(source);
if (v.kind === "refused") refuse(v.message);   // the refusal is the repair signal
```

## The import section is empty, and that is the mechanism

The component imports nothing: no clock, no filesystem, no random, no host
function. So a verdict is a total, deterministic function of its arguments, and
that is provable from the artifact rather than promised in prose — two hosts
running this artifact on the same input get the same verdict. It is also item
289's least-authority chain applied to the gate itself: an artifact whose import
section proves it can consult nothing but its arguments.

`tests/test_gate_wasm_vector.py` holds the property structurally: a build that
grows an import is a red.

This is why the build target is `%(target)s` rather than `wasm32-wasip2`. On
wasip2 the rust standard library links the WASI world into every artifact and
the import section is no longer empty; the two cannot both be had, and the
empty-import property is the load-bearing one.

## Two host obligations

* **A panic traps; it does not become a verdict.** On every wasm target the rust
  panic strategy is `abort`, so the crate's `catch_unwind` fail-closed path does
  not catch, and a native gate panic traps the instance instead of returning
  `outside-frontier`. A trap is loud and it is not a verdict, so it is still not
  a false admission — but a host must treat a trap as "no verdict was reached"
  and fail closed on it.
* **A verdict is a decision, not an enforcement.** The gate returns a verdict;
  the browser's loader, the worker's dispatcher or the CDN's serving path is the
  code that must refuse to instantiate, execute or serve on a refusal. A gate
  whose verdict nobody consults gates nothing.

## Building

    python3 tools/build_gate_wasm.py --wasm

Needs `cargo` with the `%(target)s` target installed and `wasm-tools`. The crate
SOURCE is committed (and drift-gated); the `.wasm` is not, because a rust
artifact is byte-reproducible only against one exact toolchain and a committed
binary would red on every rustc bump. What is pinned instead is the source, the
`wit-bindgen` version (exactly), and a conformance vector run against a freshly
built artifact.

## Provenance

`GENERATED.json` records the digest inputs, the gate api semver, the frontier id
this packaging inherits from `crates/revl-gate`, and the target it is built for.
The frontier id is the crate's, unchanged: two gates with different ids cover
different surfaces and their agreement means nothing.
"""


def render_readme(meta: dict) -> str:
    return README_TEMPLATE % {
        "target": WASM_TARGET,
        "world": WIT_WORLD,
        "covered_layer": meta["covered_layer"],
    }


# --------------------------------------------------------------- .gitignore

GITIGNORE = """# GENERATED by tools/build_gate_wasm.py — do not edit by hand.
/target
Cargo.lock
"""


# ------------------------------------------------------------ GENERATED.json

def render_generated_json(digest: str, meta: dict) -> str:
    payload = {
        "crate": "revl-gate-wasm",
        "crate_version": CRATE_VERSION,
        "generator": "tools/build_gate_wasm.py",
        "wraps": "crates/revl-gate",
        "digest_inputs": list(DIGEST_INPUTS),
        "source_digest": digest,
        # inherited from the rust crate, never restated: the wasm gate decides
        # exactly what that crate decides
        "frontier": meta["frontier"],
        "language_version": meta["language_version"],
        "covered_layer": meta["covered_layer"],
        "issues_admissions": False,
        "verdict_kinds": ["refused", "no-objection", "outside-frontier"],
        "gate_api_version": GATE_API_VERSION,
        "layer": "1 (verdict surface), refusal-only",
        "wit_package": WIT_PACKAGE,
        "wit_world": WIT_WORLD,
        "wit_bindgen": WIT_BINDGEN_REQ,
        "target": WASM_TARGET,
        "empty_imports": True,
        "exports": ["admit", "admit-json", "admit-artifact", "gate-version"],
        "unavailable_exports": {
            "admit-artifact": "the item-289 chain's declared-caps leg is the G8 "
                              "boundary projection, which has no native port "
                              "(design slice 3)",
        },
        "note": (
            "Regenerate with `python3 tools/build_gate_wasm.py`. The crate SOURCE "
            "is committed and drift-gated; the .wasm artifact is not, because a "
            "rust binary is byte-reproducible only against one exact toolchain. "
            "`issues_admissions` is false by construction: this is "
            "`crates/revl-gate` packaged for wasm, and that gate decides the "
            "composition/guarantee layer and not the reference type layer."
        ),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------------------ assembly

def build() -> dict[str, str]:
    """Render every crate file. Returns {relpath: content}."""
    # `revl.gate`, the rust crate and this generator are three spellings of ONE
    # surface, so their api semver is one value.
    from revl.gate import GATE_API_VERSION as PY_API  # noqa: PLC0415
    if PY_API != GATE_API_VERSION:
        raise SystemExit(
            f"build_gate_wasm: gate api semver mismatch — revl.gate says "
            f"{PY_API!r}, this generator says {GATE_API_VERSION!r}. The wheel, "
            f"the crate and the component are one surface; bump all or none.")
    meta = _gate_crate_meta()
    if meta["gate_api_version"] != GATE_API_VERSION:
        raise SystemExit(
            f"build_gate_wasm: gate api semver mismatch — crates/revl-gate says "
            f"{meta['gate_api_version']!r}, this generator says "
            f"{GATE_API_VERSION!r}.")
    if meta["issues_admissions"]:
        raise SystemExit(
            "build_gate_wasm: crates/revl-gate now reports that it issues "
            "admissions. This shim hard-codes `admitted: false` on every arm "
            "and its README says the gate cannot admit; both have to be "
            "revisited deliberately before the component can carry an "
            "admission to the edge.")
    digest = source_digest()
    return {
        "Cargo.toml": render_cargo_toml(),
        "GENERATED.json": render_generated_json(digest, meta),
        "README.md": render_readme(meta),
        ".gitignore": GITIGNORE,
        f"wit/{WIT_WORLD}.wit": render_wit(),
        "src/lib.rs": render_lib_rs(),
    }


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
    empty when they agree byte for byte."""
    with tempfile.TemporaryDirectory(prefix="revl_gate_wasm_check_") as tmp:
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


# ------------------------------------------------------------- the toolchain
#
# Same honesty discipline as the rust tier: a missing toolchain SKIPS with the
# reason, and a green always means a real artifact was really built. Never a
# hollow green.

def cargo_binary() -> str | None:
    return os.environ.get("REVL_WASM_CARGO") or shutil.which("cargo")


def wasm_tools_binary() -> str | None:
    return shutil.which("wasm-tools")


def wasmtime_binary() -> str | None:
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path.home() / ".wasmtime" / "bin" / "wasmtime"
    return str(fallback) if fallback.is_file() else None


def toolchain_reason() -> str | None:
    """Why the component cannot be built here, or None when it can.

    `REVL_WASM_CARGO` names a cargo whose toolchain carries the wasm target,
    for a machine (like a stock Homebrew rust) whose default cargo does not.
    """
    cargo = cargo_binary()
    if cargo is None:
        return "cargo not found (set REVL_WASM_CARGO to a cargo with the wasm target)"
    if wasm_tools_binary() is None:
        return "wasm-tools not found (needed to wrap the core module into a component)"
    probe = subprocess.run([cargo, "build", "--target", WASM_TARGET, "--help"],
                           capture_output=True, text=True, timeout=120, check=False)
    if probe.returncode != 0:
        return f"cargo cannot use --target {WASM_TARGET}"
    rustc = subprocess.run([cargo, "rustc", "--version", "--verbose"],
                           capture_output=True, text=True, timeout=120, check=False)
    del rustc
    if not _target_std_present(cargo):
        return (f"the {WASM_TARGET} standard library is not installed for this "
                f"toolchain (rustup target add {WASM_TARGET}; or set "
                f"REVL_WASM_CARGO to a cargo that has it)")
    return None


def _target_std_present(cargo: str) -> bool:
    """Is `core` available for the wasm target? Compiles a `#![no_std]`-free
    stub in a temp crate, which is the only answer that is not a guess."""
    with tempfile.TemporaryDirectory(prefix="revl_wasm_probe_") as tmp:
        work = Path(tmp)
        (work / "src").mkdir()
        (work / "src" / "lib.rs").write_text("pub fn probe() -> u32 { 1 }\n",
                                             encoding="utf-8")
        (work / "Cargo.toml").write_text(
            '[package]\nname = "probe"\nversion = "0.0.0"\nedition = "2021"\n'
            '\n[workspace]\n\n[lib]\ncrate-type = ["cdylib"]\n', encoding="utf-8")
        done = subprocess.run(
            [cargo, "build", "--offline", "--target", WASM_TARGET],
            cwd=work, capture_output=True, text=True, timeout=600, check=False)
        return done.returncode == 0


def build_component(crate_dir: Path, out: Path | None = None) -> Path:
    """Compile the crate to a core module and wrap it into a component.

    Returns the path to the `.wasm` component. Raises `RuntimeError` with the
    tool's own message on failure — the caller decides skip vs fail.
    """
    reason = toolchain_reason()
    if reason is not None:
        raise RuntimeError(reason)
    cargo = cargo_binary()
    # No PYTHONPATH, no VIRTUAL_ENV: the component must build with nothing from
    # this repo's Python on the path. That is the "no Python installed" claim,
    # held as tightly as a test on a machine that does have Python can hold it.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    args = [cargo, "build", "--release", "--target", WASM_TARGET]
    done = subprocess.run([*args, "--offline"], cwd=crate_dir, env=env,
                          capture_output=True, text=True, timeout=3600, check=False)
    if done.returncode != 0 and _is_offline_resolve_failure(done):
        done = subprocess.run(args, cwd=crate_dir, env=env, capture_output=True,
                              text=True, timeout=3600, check=False)
    if done.returncode != 0:
        raise RuntimeError("cargo build failed:\n"
                           + (done.stderr or done.stdout or "")[-4000:])
    core = crate_dir / "target" / WASM_TARGET / "release" / "revl_gate_wasm.wasm"
    if not core.is_file():
        raise RuntimeError(f"the core module was not produced at {core}")
    out = Path(out) if out is not None else crate_dir / "target" / "revl_gate.wasm"
    out.parent.mkdir(parents=True, exist_ok=True)
    wrapped = subprocess.run(
        [wasm_tools_binary(), "component", "new", str(core), "-o", str(out)],
        capture_output=True, text=True, timeout=600, check=False)
    if wrapped.returncode != 0:
        raise RuntimeError("wasm-tools component new failed:\n"
                           + (wrapped.stderr or wrapped.stdout or "")[-4000:])
    return out


_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode", "without the offline flag",
    "--offline was specified", "registry index was not found",
    "no matching package", "failed to select a version",
)
_REAL_FAILURE_MARKERS = ("error[e", "could not compile", "panicked at")


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    """A networked retry is allowed only when the offline attempt failed to
    RESOLVE a crate, never to launder a compile failure into a retry. Same
    policy as `tests/test_gate_crate_admit.py` and `tools/bench_selfhost_rust.py`."""
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(marker in blob for marker in _REAL_FAILURE_MARKERS):
        return False
    return any(marker in blob for marker in _OFFLINE_RESOLVE_MARKERS)


def component_imports(component: Path) -> list[str]:
    """The component's import list, read out of the artifact with `wasm-tools
    component wit`. Empty is the property the design makes load-bearing."""
    binary = wasm_tools_binary()
    if binary is None:
        raise RuntimeError("wasm-tools not found")
    done = subprocess.run([binary, "component", "wit", str(component)],
                          capture_output=True, text=True, timeout=300, check=False)
    if done.returncode != 0:
        raise RuntimeError("wasm-tools component wit failed:\n"
                           + (done.stderr or done.stdout or ""))
    imports = []
    for line in done.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped[len("import "):].rstrip(";"))
    return imports


def invoke(component: Path, expression: str, *, fuel: int | None = None,
           timeout: int = 300) -> subprocess.CompletedProcess:
    """`wasmtime run --invoke <expression>` on the component. Returns the raw
    completed process so a caller can tell a refusal from a trap."""
    binary = wasmtime_binary()
    if binary is None:
        raise RuntimeError("wasmtime not found")
    args = [binary, "run"]
    if fuel is not None:
        args += ["-W", f"fuel={fuel}"]
    args += ["--invoke", expression, str(component)]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=False)


def wave_string(value: str) -> str:
    """`value` as a WAVE string literal, for `wasmtime run --invoke`."""
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u{%x}" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def unwave_string(out: str) -> str:
    """The payload of a WAVE string result line, unescaped."""
    text = out.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            result.append(ch)
            i += 1
            continue
        i += 1
        esc = text[i]
        if esc == "u":
            close = text.index("}", i)
            result.append(chr(int(text[i + 2:close], 16)))
            i = close + 1
        else:
            result.append({"n": "\n", "r": "\r", "t": "\t",
                           '"': '"', "\\": "\\"}.get(esc, esc))
            i += 1
    return "".join(result)


def measure_fuel(component: Path, expression: str, *, ceiling: int = 1 << 34) -> int | None:
    """The least fuel budget under which `expression` completes, by bisection.

    `wasmtime -W fuel=N` traps when the budget runs out, so the smallest N that
    does NOT trap is the invocation's fuel cost. Returns None when the call does
    not complete even at `ceiling` (or traps for a reason that is not fuel).
    """
    if invoke(component, expression, fuel=ceiling).returncode != 0:
        return None
    low, high = 0, ceiling
    while low < high:
        mid = (low + high) // 2
        if invoke(component, expression, fuel=mid).returncode == 0:
            high = mid
        else:
            low = mid + 1
    return low


# ---------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the revl-gate wasm component crate.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="crate directory (default: crates/revl-gate-wasm)")
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed crate differs from a fresh "
                             "generation (the CI drift gate)")
    parser.add_argument("--wasm", action="store_true",
                        help="build the component and report its measured size, "
                             "import list and per-verdict fuel")
    args = parser.parse_args(argv[1:])

    if args.check:
        problems = drift(args.out)
        if problems:
            print(f"gate wasm crate DRIFT at {args.out}:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            print("\nRegenerate with: python3 tools/build_gate_wasm.py",
                  file=sys.stderr)
            return 1
        print(f"gate wasm crate is in sync with the tree ({args.out}).")
        return 0

    if args.wasm:
        reason = toolchain_reason()
        if reason is not None:
            print(f"cannot build the component: {reason}", file=sys.stderr)
            return 2
        component = build_component(args.out)
        size = component.stat().st_size
        imports = component_imports(component)
        print(f"component: {component}")
        print(f"size:      {size} bytes ({size / 1024:.1f} KiB)")
        print(f"imports:   {imports or 'none (the empty-import property holds)'}")
        if wasmtime_binary() is not None:
            probe = 'admit-json("fn f() -> Int { return 1 }")'
            fuel = measure_fuel(component, probe)
            print(f"fuel:      {fuel if fuel is not None else 'not measurable'} "
                  f"(one `admit` over a one-line program)")
        return 0

    files = generate(args.out)
    print(f"wrote {len(files)} files to {args.out}")
    for rel in sorted(files):
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
