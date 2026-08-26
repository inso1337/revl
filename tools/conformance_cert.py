#!/usr/bin/env python3
"""Cross-runtime conformance certificate (roadmap item 306) — generate + verify.

revl's central claim is not "six backends" but "one composition means the same
thing on six runtimes". Item 292's differential fuzzer and the cross-tier
execution suite turn that claim into evidence *at test time*. This tool packages
that evidence into a portable, tamper-evident artifact so an EXTERNAL consumer —
a registry (item 49), `truc ship`, a deployment attestation, an academic
artifact — can trust the result WITHOUT re-running the six-tier tests.

The certificate is a signed JSON document:

    { kind, version, corpus, source_hash, ir_hash,
      tiers[], cases, passed, per_tier{}, semantic_differences[],
      runtime_versions{}, timestamp, signer, key_id, signature }

It records, over a fixed corpus of self-checking probes:

  * ``source_hash`` / ``ir_hash`` — the canonical hashes (item 127's
    ``attest.canonical_hash``) of the corpus source and of its compiled IR, so a
    verifier can prove the cert is about *this exact* corpus.
  * ``tiers`` — the runtimes that actually RAN at least one case here. Only
    available tiers run; an unavailable tier is recorded honestly in
    ``runtime_versions`` with the reason, never counted as a false pass.
  * ``cases`` / ``passed`` — the count of case×tier executions that actually
    ran and how many passed. These reflect what ran, never a fabricated total.
  * ``semantic_differences`` — every tier that ran a case but disagreed with the
    reference (a build failure of admitted code, or a wrong value): the item-292
    divergence classes. A conforming corpus over conforming tiers has none.
  * ``runtime_versions`` — doctor-style version probing (item 291's
    ``doctor.diagnose``) of every candidate tier.

Signing reuses item 127's ``attest`` primitives: ``resolve_key`` for the key
(``--key`` / ``REVL_ATTEST_KEY_FILE`` / ``REVL_ATTEST_KEY``, never a hardcoded
secret), ``canonical_hash`` for the content hashes, ``key_id`` for the non-secret
key fingerprint, and an HMAC-SHA256 over the canonical bytes of the whole cert
body (everything but ``signature``) — so tampering with any recorded field,
count, or difference breaks the signature exactly as a tampered hash does.

Execution is NOT reinvented: every case runs through ``revl.test.RUNNERS`` and is
classified by item 292's ``fuzz_cross_tier.check_tier`` (pass / skip / refusal /
build / value). The corpus is embedded in the cert, so verification is fully
self-contained: it recompiles the corpus (pure Python, always available — it is
compilation, not the six-tier test run the cert exists to avoid), recomputes the
two hashes, and checks the signature. No tier is re-run at verify time.

    python3 tools/conformance_cert.py generate --out cert.json
    python3 tools/conformance_cert.py generate --tiers py,ts,go --out cert.json
    python3 tools/conformance_cert.py generate --slow --out cert.json   # + rust/java
    python3 tools/conformance_cert.py verify cert.json

This is a FIRST CUT: tools-only, no new ``revl`` CLI verb (item 305 owns CLI
wiring). Honesty over coverage — the cert states which tiers ran and never
records a pass a tier did not earn.
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from revl import compile_source  # noqa: E402
from revl import attest  # noqa: E402  — canonical_hash / resolve_key / key_id
from revl import doctor  # noqa: E402  — runtime version probing (item 291)
from revl.test import RUNNERS  # noqa: E402  — the execution machinery (not reinvented)

# item 292's per-tier classifier (pass / skip / refusal / build / value). Reused
# so "did this tier diverge?" is answered by exactly one implementation.
import fuzz_cross_tier  # noqa: E402

CERT_KIND = "revl.conformance-cert"
CERT_VERSION = "1.0"
SIGN_ALG = "hmac-sha256"
HASH_ALG = "sha256"

# The reference oracle, mirroring the fuzzer: the py tier needs no external
# toolchain, so it is the ground truth a value/build divergence is measured
# against. A cert cannot be generated if py itself cannot run.
REFERENCE = "py"

# Every candidate runtime, fastest first. rust/java shell out to cargo/javac per
# case (slow), so they are attempted only under --slow or an explicit --tiers.
ALL_TIERS = ("py", "ts", "go", "wasm", "rust", "java")
SLOW_TIERS = ("rust", "java")


# ==========================================================================
# the corpus — a fixed set of self-checking, cross-tier-safe probes
# ==========================================================================
#
# Each case is a self-contained program whose `test` blocks encode the semantic
# claim, so a tier that merely COMPILES the case still fails if it MEANS the
# wrong thing (the cross-tier execution suite's discipline). The default corpus
# is deliberately the least common denominator every one of the six emitters can
# lower and run — Int/Bool/Str/List over pure functions, no Float (wasm has no
# Float value representation and would honestly *refuse* it, not run it) — so a
# green cert exercises as many tiers as the box has toolchains for.

DEFAULT_CORPUS: dict[str, str] = {
    "int arithmetic traps at the bound": (
        "pub fn add(a: Int, b: Int) -> Int { return a + b }\n"
        'test "small arithmetic" { assert add(2, 2) == 4 }\n'
        'test "multiplication is exact" { assert 1000000 * 1000000 == 1000000000000 }\n'
        'test "near the bound" { assert 9223372036854775807 - 1 == 9223372036854775806 }\n'
    ),
    "integer remainder takes the dividend's sign": (
        'test "rem, negative dividend" { assert (0 - 7) % 3 == 0 - 1 }\n'
        'test "rem, negative divisor"  { assert 7 % (0 - 3) == 1 }\n'
        'test "mod is never negative"  { assert (0 - 7).mod(3) == 2 }\n'
    ),
    "boolean logic and comparison": (
        "pub fn both(a: Bool, b: Bool) -> Bool { return a && b }\n"
        'test "and" { assert both(true, true) && !both(true, false) }\n'
        'test "ordering" { assert 1 < 2 && !(2 < 1) }\n'
    ),
    "string concat and code-point length": (
        'pub fn greet(who: Str) -> Str { return "hi " + who }\n'
        'test "concatenation" { assert greet("revl") == "hi revl" }\n'
        'test "length counts code points" { assert "abc".length() == 3 }\n'
        'test "lexicographic order" { assert "Z" < "a" }\n'
    ),
    "list value equality and length": (
        "pub fn nums() -> List[Int] { return [1, 2, 3] }\n"
        'test "length" { assert nums().length() == 3 }\n'
        'test "lists compare by value" { assert [1, 2] == [1, 2] }\n'
        'test "order matters" { assert [1, 2] != [2, 1] }\n'
    ),
}


def load_corpus(path: str | None) -> dict[str, str]:
    """The corpus to certify: the built-in default, or a JSON `{name: source}`
    object from *path*. A custom corpus must still be self-checking (each source
    carries its own `test` blocks) — the tool runs whatever tests are present."""
    if path is None:
        return dict(DEFAULT_CORPUS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SystemExit(f"cannot read corpus {path}: {error}")
    if not isinstance(raw, dict) or not raw or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise SystemExit(f"corpus {path} must be a non-empty JSON object of "
                         "name -> source strings")
    return raw


# ==========================================================================
# hashing — the corpus's stable identity (reusing attest.canonical_hash)
# ==========================================================================

def source_hash(corpus: dict[str, str]) -> str:
    """Content hash of the corpus SOURCE — a pure function of the (name, source)
    pairs, independent of dict order (canonical_hash sorts keys)."""
    return attest.canonical_hash(corpus)


def ir_hash(corpus: dict[str, str]) -> str:
    """Content hash of the corpus IR — every case compiled to its admitted IR,
    keyed by case name. This is the identity a divergence-free cert binds: two
    corpora with the same source hash to the same IR, and a change to any case's
    lowering changes this hash. Compilation is pure Python, so recomputing it at
    verify time is cheap and needs no runtime — it is not the six-tier test run
    the cert exists to let a consumer skip."""
    irs = {name: compile_source(src, f"{name}.rvl") for name, src in corpus.items()}
    return attest.canonical_hash(irs)


# ==========================================================================
# tier availability — probe each candidate once, honestly
# ==========================================================================

_PROBE = ('pub fn probe() -> Int { return 1 }\n'
          'test "probe" { assert probe() == 1 }\n')


def available_tiers(candidates: list[str]) -> tuple[list[str], dict[str, str]]:
    """Probe each candidate tier once with a trivial self-checking program.

    A tier that PASSES is available; a tier that SKIPS is unavailable with the
    reason its own runner reports (an honest toolchain gap, never a fail); a
    tier that FAILS the trivial probe is unavailable-because-broken. Reuses
    RUNNERS directly — the same execution path the cases take."""
    probe_ir = compile_source(_PROBE, "conformance_probe.rvl")
    ran: list[str] = []
    reasons: dict[str, str] = {}
    for tier in candidates:
        if tier not in RUNNERS:
            reasons[tier] = "unknown tier"
            continue
        outcome, message, _ = fuzz_cross_tier._run_tier(tier, probe_ir)
        if outcome == "pass":
            ran.append(tier)
        elif outcome == "skip":
            reasons[tier] = f"toolchain unavailable: {message}"
        else:
            reasons[tier] = f"probe did not pass ({outcome}): {message}"
    return ran, reasons


# ==========================================================================
# runtime versions — doctor-style probing (item 291)
# ==========================================================================

def runtime_versions() -> dict[str, dict]:
    """Per-tier runtime version + availability, from `doctor.diagnose()`. Keyed
    by tier so the cert records exactly which runtime produced which result."""
    report = doctor.diagnose()
    out: dict[str, dict] = {"compiler": {"version": report.revl_version,
                                         "available": True, "detail": "revl compiler"}}
    for check in report.checks:
        if check.tier is None:
            continue
        out[check.tier] = {
            "version": check.version,
            "available": bool(check.available),
            "detail": check.detail,
        }
    return out


# ==========================================================================
# generate
# ==========================================================================

def _blank_tally() -> dict[str, int]:
    return {"ran": 0, "passed": 0, "skipped": 0, "refused": 0, "differences": 0}


def generate(corpus: dict[str, str], tiers: list[str], *,
             corpus_name: str, key: bytes, now=None,
             signer: str | None = None) -> dict:
    """Run the corpus across the given available tiers and build a SIGNED cert.

    `tiers` are the runtimes to execute (already filtered to available). Every
    case runs through item 292's `check_tier`; the outcome is tallied honestly:

        pass            -> ran + passed
        build / value   -> ran + a recorded semantic_difference (a divergence)
        refusal         -> refused (a declared capability boundary, not a pass)
        skip            -> skipped (case-specific toolchain gap, not a pass)

    `cases` and `passed` count case×tier executions that actually RAN (pass or
    divergence); refusals and skips are excluded from both, so neither number
    can overstate what the tiers did."""
    per_tier = {tier: _blank_tally() for tier in tiers}
    differences: list[dict] = []
    cases = passed = 0

    for name, src in corpus.items():
        for tier in tiers:
            outcome, message, stdout = fuzz_cross_tier.check_tier(tier, src)
            tally = per_tier[tier]
            if outcome == "pass":
                tally["ran"] += 1
                tally["passed"] += 1
                cases += 1
                passed += 1
            elif outcome in ("build", "value"):
                tally["ran"] += 1
                tally["differences"] += 1
                cases += 1
                differences.append({
                    "case": name,
                    "tier": tier,
                    "kind": outcome,  # 'build' or 'value'
                    "signature": fuzz_cross_tier.divergence_signature(message, stdout),
                    "detail": message.strip().splitlines()[0][:200] if message.strip() else "",
                })
            elif outcome == "refusal":
                tally["refused"] += 1
            else:  # 'skip'
                tally["skipped"] += 1

    ran_tiers = sorted(t for t in tiers if per_tier[t]["ran"] > 0)

    body = {
        "kind": CERT_KIND,
        "version": CERT_VERSION,
        "sign_alg": SIGN_ALG,
        "hash_alg": HASH_ALG,
        "corpus": corpus_name,
        "source_hash": source_hash(corpus),
        "ir_hash": ir_hash(corpus),
        "case_count": len(corpus),
        "tiers": ran_tiers,
        "cases": cases,
        "passed": passed,
        "per_tier": per_tier,
        "semantic_differences": differences,
        "runtime_versions": runtime_versions(),
        # The corpus travels IN the cert so verification is self-contained: a
        # verifier recomputes source_hash/ir_hash from exactly these sources.
        "corpus_sources": dict(corpus),
        "timestamp": attest._now_iso(now),
        "signer": signer,
        "key_id": attest.key_id(bytes(key)),
    }
    body["signature"] = _sign(body, bytes(key))
    return body


# ==========================================================================
# signing / verifying — HMAC over the canonical cert body (reusing attest)
# ==========================================================================

SIGNATURE_FIELD = "signature"


def _sign(cert: dict, key: bytes) -> str:
    """HMAC-SHA256 over the canonical bytes of the cert with its `signature`
    member removed. Uses attest's canonical serialization (sorted keys, compact
    separators), so any altered/dropped/added member — a flipped pass count, a
    dropped difference, a swapped hash — makes the recomputed signature diverge.
    """
    signed = {k: v for k, v in cert.items() if k != SIGNATURE_FIELD}
    return hmac.new(key, attest._canonical_bytes(signed), sha256).hexdigest()


def verify(cert: dict, key: bytes) -> tuple[bool, str]:
    """Verify a conformance cert with `key`, WITHOUT re-running any tier.

    Three independent checks, reported distinctly and in order:

      * **signature** — the HMAC over the whole cert body must match. A wrong
        key, or any field altered after signing (a count, a hash, a recorded
        difference, the embedded corpus), fails here.
      * **source hash** — the corpus embedded in the cert must hash to the
        recorded `source_hash`. (Redundant with the signature for a self-signed
        cert, but it localizes *what* drifted, and holds even if a verifier
        supplies its own corpus in a future extension.)
      * **ir hash** — recompiling the embedded corpus must reproduce `ir_hash`.
        This is compilation only — pure Python, always available — not the
        six-tier test run the cert exists to let a consumer skip.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no signing key provided"
    if not isinstance(cert, dict):
        return False, "certificate is not an object"

    given = cert.get(SIGNATURE_FIELD)
    if not isinstance(given, str):
        return False, "certificate has no signature"
    for required in ("source_hash", "ir_hash", "corpus_sources"):
        if required not in cert:
            return False, f"certificate missing required member '{required}'"

    expected = _sign(cert, bytes(key))
    if not hmac.compare_digest(expected, given):
        return False, ("signature mismatch: wrong key, or the certificate was "
                       "tampered with after signing")

    corpus = cert["corpus_sources"]
    if not isinstance(corpus, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in corpus.items()):
        return False, "embedded corpus_sources is not a name -> source object"

    recomputed_src = source_hash(corpus)
    if not hmac.compare_digest(recomputed_src, cert["source_hash"]):
        return False, ("source hash mismatch: the embedded corpus does not hash "
                       f"to the recorded source_hash (recorded {cert['source_hash'][:12]}…, "
                       f"now {recomputed_src[:12]}…)")

    try:
        recomputed_ir = ir_hash(corpus)
    except Exception as error:  # noqa: BLE001 — a corpus that no longer compiles is invalid
        return False, f"ir hash mismatch: the embedded corpus does not compile: {error}"
    if not hmac.compare_digest(recomputed_ir, cert["ir_hash"]):
        return False, ("ir hash mismatch: the embedded corpus compiles to a "
                       f"different IR than certified (recorded {cert['ir_hash'][:12]}…, "
                       f"now {recomputed_ir[:12]}…)")

    return True, ("valid: signature authentic and the embedded corpus matches "
                  "the certified source and IR hashes")


# ==========================================================================
# rendering
# ==========================================================================

def render(cert: dict) -> str:
    lines = [
        f"conformance certificate: {cert.get('kind')} v{cert.get('version')}",
        f"  corpus:      {cert.get('corpus')} ({cert.get('case_count')} case(s))",
        f"  source {cert.get('hash_alg', '?')}: {cert.get('source_hash')}",
        f"  ir     {cert.get('hash_alg', '?')}: {cert.get('ir_hash')}",
        f"  tiers ran:   {', '.join(cert.get('tiers') or []) or '(none)'}",
        f"  executions:  {cert.get('passed')} / {cert.get('cases')} passed",
    ]
    for tier, tally in sorted((cert.get("per_tier") or {}).items()):
        extra = []
        if tally.get("refused"):
            extra.append(f"{tally['refused']} refused")
        if tally.get("skipped"):
            extra.append(f"{tally['skipped']} skipped")
        if tally.get("differences"):
            extra.append(f"{tally['differences']} DIFFER")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        lines.append(f"    {tier:<5} {tally.get('passed', 0)}/{tally.get('ran', 0)} "
                     f"passed{suffix}")
    diffs = cert.get("semantic_differences") or []
    if diffs:
        lines.append(f"  semantic differences: {len(diffs)}")
        for d in diffs:
            lines.append(f"    - {d['tier']} {d['kind']} on {d['case']!r}: {d.get('signature', '')}")
    else:
        lines.append("  semantic differences: none")
    versions = cert.get("runtime_versions") or {}
    lines.append("  runtime versions:")
    for tier, info in sorted(versions.items()):
        mark = "" if info.get("available") else "  (unavailable)"
        lines.append(f"    {tier:<9} {info.get('version') or '?'}{mark}")
    lines.append(f"  signed:      {cert.get('timestamp')}  "
                 f"({cert.get('sign_alg')}, key {cert.get('key_id')})")
    if cert.get("signer"):
        lines.append(f"  signer:      {cert['signer']}")
    lines.append(f"  signature:   {cert.get('signature')}")
    return "\n".join(lines)


# ==========================================================================
# CLI (tool only — no `revl` verb this slice; item 305 owns CLI wiring)
# ==========================================================================

def _resolve_signer(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    import os  # noqa: PLC0415
    return os.environ.get(attest.SIGNER_ENV)


def cmd_generate(args) -> int:
    corpus = load_corpus(args.corpus)
    corpus_name = args.name or (args.corpus or "default")

    requested = [t.strip() for t in args.tiers.split(",")] if args.tiers else None
    if requested is not None:
        candidates = requested
    else:
        candidates = [t for t in ALL_TIERS if args.slow or t not in SLOW_TIERS]
    ran, reasons = available_tiers(candidates)
    if REFERENCE not in ran:
        raise SystemExit(
            f"the {REFERENCE} reference tier is not runnable here: "
            f"{reasons.get(REFERENCE, 'unknown')} — cannot generate a cert")

    try:
        key = attest.resolve_key(args.key)
    except Exception as error:  # noqa: BLE001 — surface the key problem plainly
        raise SystemExit(str(error))

    cert = generate(corpus, ran, corpus_name=corpus_name, key=key,
                    signer=_resolve_signer(args.signer))

    if not args.quiet:
        for tier, why in sorted(reasons.items()):
            print(f"  tier {tier}: not executed — {why}", file=sys.stderr)

    text = json.dumps(cert, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        if not args.quiet:
            print(render(cert), file=sys.stderr)
            print(f"\nwrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_verify(args) -> int:
    try:
        cert = json.loads(Path(args.cert).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SystemExit(f"cannot read certificate {args.cert}: {error}")
    try:
        key = attest.resolve_key(args.key)
    except Exception as error:  # noqa: BLE001
        raise SystemExit(str(error))
    ok, reason = verify(cert, key)
    glyph = "VALID" if ok else "INVALID"
    print(f"certificate: {glyph} — {reason}")
    if not ok:
        return 1
    if args.verbose:
        print(render(cert))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="run the corpus and emit a signed cert")
    gen.add_argument("--corpus", help="JSON {name: source} corpus file "
                     "(default: the built-in corpus)")
    gen.add_argument("--name", help="corpus name recorded in the cert")
    gen.add_argument("--tiers", help="comma-separated tiers to attempt "
                     "(default: auto-detect the fast tiers; add --slow for rust/java)")
    gen.add_argument("--slow", action="store_true",
                     help="also attempt the rust and java tiers (cargo/javac per case)")
    gen.add_argument("--key", help="signing key file (else REVL_ATTEST_KEY_FILE / "
                     "REVL_ATTEST_KEY)")
    gen.add_argument("--signer", help="optional signer label (else REVL_ATTEST_SIGNER)")
    gen.add_argument("--out", help="write the cert JSON here (default: stdout)")
    gen.add_argument("--quiet", action="store_true")
    gen.set_defaults(func=cmd_generate)

    ver = sub.add_parser("verify", help="verify a signed cert without re-running tiers")
    ver.add_argument("cert", help="the certificate JSON to verify")
    ver.add_argument("--key", help="signing key file (else REVL_ATTEST_KEY_FILE / "
                     "REVL_ATTEST_KEY)")
    ver.add_argument("--verbose", action="store_true", help="print the cert on success")
    ver.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
