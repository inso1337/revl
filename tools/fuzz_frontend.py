#!/usr/bin/env python3
"""Frontend fuzzer: the reference lexer/parser and the self-hosted gate, driven
against each other and against a hardened runtime.

WHY THIS EXISTS. The backends already have a differential fuzzer
(`tools/fuzz_cross_tier.py`, item 292) that generates *valid* programs and
compares what the tiers compute. Nothing generated *invalid* ones, and nothing
attacked the frontend, so two whole classes of defect had no way to be found:

  * a frontend that CRASHES instead of refusing. A `RevlError` is the language
    saying no, and every caller handles it; a bare `IndexError` out of the
    parser is an unhandled fault in a library, and in `revl.gate` it is an
    unhandled fault in a security surface.
  * a self-host that DISAGREES with the reference on an input nobody wrote by
    hand. The oracle corpus is hand-written, so it only reaches the code paths
    someone thought of; a line-coverage measurement put 53.7% of the reference
    emitter's statements outside it entirely.

WHAT IT CHECKS. Three stages, each with its own oracle:

  reference  The reference frontend must never raise anything but `RevlError`
             on any byte string. A refusal is the correct answer; a traceback
             is a bug. `RecursionError` is counted separately and reported, not
             failed: the reference parser is recursive-descent and a deeply
             nested input hitting Python's own limit is a resource bound, not a
             logic defect.

  lexer      `selfhost/lexer.rvl` vs `src/revl/lexer.py`, token for token, in
             the canonical shape tests/test_selfhost_lexer.py uses. Either both
             produce the same tokens, or both reject.

  gate       `selfhost/lower.rvl`'s `admit_src` — the shipped admission gate,
             the thing `crates/revl-gate` embeds — vs `compile_source`. The
             comparison is COARSE ON PURPOSE: admit-vs-refuse, not the
             diagnostic. Message agreement is what the hand-written corpus in
             tests/test_selfhost_lower.py already pins; what no corpus can pin
             is that a mutated program does not slip *through* the gate. A
             self-host that admits what the reference refuses is a gate bypass
             and is reported as such.

THE HARDENED RUNTIME. The self-host stages run the python-emitted self-host
through an AST rewrite that routes every subscript through `_revl_idx`, which
refuses a negative or past-the-end index instead of doing what python does.
This matters because python is the ONLY tier that is quiet here:

    python  xs[-1]   -> the LAST element, silently
    rust    xs[(-1) as usize]  -> index out of bounds: len is 11 but the
                                  index is 18446744073709551615
    go      xs[-1]   -> panic: index out of range
    java    xs.get(-1) -> IndexOutOfBoundsException

So a negative index is a real cross-tier divergence that the python tier — the
tier every oracle test runs on — cannot see. The rewrite makes it visible at
python speed, which is how a fault that only a `cargo test` had ever tripped
over becomes a ten-character reproducer.

INPUTS. Three sources, mixed: mutations of the 800-odd `.rvl` files in the tree
(the only way to reach deep code paths cheaply), splices of two corpus files at
declaration boundaries (which reaches the linker and the wiring checks), and
raw random bytes (which is what the decoder and the lexer's edge handling
actually need). Every finding is shrunk by line-then-chunk delta debugging
before it is reported, because a 400-byte reproducer nobody can read is worth
much less than the same bug at six characters.

USAGE

    python3 tools/fuzz_frontend.py --seconds 60
    python3 tools/fuzz_frontend.py --stage gate --seconds 600 --seed 7
    python3 tools/fuzz_frontend.py --iterations 200 --quiet   # CI smoke

Exit status is 1 when a finding survives shrinking, so a short budget can be
run as a test. A long campaign is a thing you run deliberately — do not wire
one into CI.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import random
import string
import sys
import time
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.lexer import lex as reference_lex  # noqa: E402


# --------------------------------------------------------------- hardened run

class IndexFault(Exception):
    """A self-host list/string index that is negative or past the end.

    Not an exception the language has — the pure stratum has none. It is this
    harness observing, on the python tier, a read that every other tier traps.
    """


def _revl_idx(target, index):
    """Every subscript in the emitted self-host, in Load position.

    Only list/str reads with an integer index are judged: a dict read is a
    record or map field and its key space is not ordinal, and a `typing`
    subscript (`list[int]` in an annotation) must pass through untouched.
    """
    if isinstance(target, (list, str)) and isinstance(index, int) \
            and not isinstance(index, bool):
        if index < 0 or index >= len(target):
            raise IndexFault(
                f"index {index} out of range for a {type(target).__name__} "
                f"of length {len(target)}")
    return target[index]


class _GuardSubscripts(ast.NodeTransformer):
    def visit_Subscript(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load):
            return node
        if isinstance(node.slice, ast.Slice):
            return node  # a slice clamps in every tier; not the fault class
        return ast.copy_location(
            ast.Call(func=ast.Name(id="_revl_idx", ctx=ast.Load()),
                     args=[node.value, node.slice], keywords=[]),
            node)


def build_selfhost(stage_file: str, *, guard: bool = True) -> dict:
    """Compile `selfhost/<stage_file>`, emit python, exec it, return its globals.

    Same shape as the `_exec_emitted` helper every tests/test_selfhost_*.py
    uses (the component in the file makes the emitted module import the
    cordis-py `runtime` adapter; the pure functions under test never touch it,
    so a lazy stub suffices) — plus the optional subscript hardening.
    """
    from revl import compile_files

    ir = compile_files([str(ROOT / "selfhost" / stage_file)])
    spec = importlib.util.spec_from_file_location(
        f"fuzz_emit_{stage_file}", ROOT / "backends" / "python" / "emit.py")
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)

    tree = ast.parse(emitter.emit(ir))
    if guard:
        tree = ast.fix_missing_locations(_GuardSubscripts().visit(tree))

    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {"_revl_idx": _revl_idx}
        exec(compile(tree, f"selfhost_{stage_file}.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


# ------------------------------------------------------------------ findings

@dataclass(frozen=True)
class Signature:
    """What makes two findings the same bug: the stage, the kind of fault, and
    where it happened. Deliberately NOT the input — the point of a signature is
    that a thousand inputs collapse onto one line of a report."""

    stage: str
    kind: str
    where: str

    def __str__(self) -> str:
        return f"{self.stage}/{self.kind}" + (f" @ {self.where}" if self.where else "")


@dataclass
class Finding:
    signature: Signature
    source: str
    detail: str
    hits: int = 1

    def as_dict(self) -> dict:
        return {"stage": self.signature.stage, "kind": self.signature.kind,
                "where": self.signature.where, "detail": self.detail,
                "hits": self.hits, "source": self.source,
                "source_len": len(self.source)}


def _selfhost_frame(exc_info) -> str:
    """The innermost self-host function on the traceback — the revl `fn` name,
    because the emitted python keeps it."""
    for frame in reversed(traceback.extract_tb(exc_info[2])):
        if frame.filename.startswith("selfhost_"):
            return frame.name
    return ""


def _crash_site(limit: int = 3) -> str:
    """`file:line` of the innermost reference frame, so two IndexErrors from
    different call sites are two findings, not one."""
    frames = traceback.extract_tb(sys.exc_info()[2])
    if not frames:
        return ""
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno}"


# ------------------------------------------------------------------- oracles
#
# Each oracle is a pure function of the input: it returns a Signature and a
# detail string when the input is a finding, and None when it is not. Shrinking
# needs exactly this shape — "does the smaller input still do the same thing".


def _escape_part(s: str) -> str:
    """Mirror of `esc` in selfhost/lexer.rvl: a part payload containing the "|"
    join separator (e.g. `${a || b}`) would otherwise be unrecoverable."""
    return s.replace("%", "%%").replace("|", "%p")


def _canonical_reference_tokens(tokens):
    """Reference tokens in the shape the revl lexer produces (ints as digits,
    template parts serialized, eof text ""). None when the input contains a
    host body, which the self-hosted lexer does not claim to lex."""
    out = []
    for t in tokens:
        if t.kind == "eof":
            out.append(("eof", "", t.line))
        elif t.kind == "int":
            out.append(("int", str(t.value), t.line))
        elif t.kind == "float":
            out.append(("float", repr(float(t.value)), t.line))
        elif t.kind == "template":
            out.append(("template", "|".join(
                ("t:" if k == "text" else "v:") + _escape_part(s)
                for k, s in t.value), t.line))
        elif t.kind == "hostbody":
            return None
        else:
            out.append((t.kind, str(t.value), t.line))
    return out


def _canonical_selfhost_tokens(tokens):
    return [(t["kind"],
             repr(float(t["text"])) if t["kind"] == "float" else t["text"],
             t["line"]) for t in tokens]


class ReferenceStage:
    """The reference frontend must refuse, not crash."""

    name = "reference"

    def __init__(self, _cache):
        pass

    def check(self, src: str):
        try:
            compile_source(src, "fuzz.rvl")
        except RevlError:
            return None
        except RecursionError:
            return Signature("reference", "recursion", ""), "python recursion limit"
        except Exception as exc:  # the finding: an unhandled fault
            return (Signature("reference", type(exc).__name__, _crash_site()),
                    str(exc)[:200])
        return None


class LexerStage:
    """selfhost/lexer.rvl against src/revl/lexer.py, token for token."""

    name = "lexer"

    def __init__(self, cache):
        self._lex = cache.selfhost("lexer.rvl")["lex_src"]

    def check(self, src: str):
        try:
            want = _canonical_reference_tokens(reference_lex(src, "fuzz.rvl"))
            refused = False
        except RevlError:
            want, refused = None, True
        except RecursionError:
            return None
        except Exception as exc:
            return (Signature("lexer", "reference-" + type(exc).__name__,
                              _crash_site()), str(exc)[:200])
        if want is None and not refused:
            return None  # host body: out of the self-hosted lexer's slice

        try:
            got = _canonical_selfhost_tokens(self._lex(src))
        except IndexFault as exc:
            return (Signature("lexer", "index-fault", _selfhost_frame(sys.exc_info())),
                    str(exc))
        except RecursionError:
            return None
        except Exception as exc:
            return (Signature("lexer", "selfhost-" + type(exc).__name__,
                              _selfhost_frame(sys.exc_info())), str(exc)[:200])

        errors = [text for kind, text, _ in got if kind == "error"]
        if refused:
            return None if errors else (
                Signature("lexer", "false-admit", ""),
                "reference refuses, self-host lexes it clean")
        if errors:
            # An Int literal past the 64-bit edge is the one refusal the two
            # implementations are ALLOWED to place differently. The reference
            # lexes it — python ints are unbounded — and refuses it a stage
            # later, in typecheck's `_reject_int_literal_range`
            # (examples/rejections/t20_int_literal_range.rvl). The self-hosted
            # lexer folds in Int, which traps at that edge on every tier, so it
            # has to decide at lex time. Same verdict on the program, different
            # stage; not a divergence.
            if all(text == "int literal out of 64-bit range" for text in errors):
                return None
            return (Signature("lexer", "false-reject", errors[0]),
                    "reference lexes it, self-host emits an error token")
        if got != want:
            for i, (a, b) in enumerate(zip(got, want)):
                if a != b:
                    return (Signature("lexer", "token-mismatch", f"#{i}"),
                            f"self-host {a} vs reference {b}")
            return (Signature("lexer", "token-count", ""),
                    f"self-host {len(got)} tokens vs reference {len(want)}")
        return None


# The guarantees `selfhost/lower.rvl`'s `admit_src` claims to decide. A
# reference refusal OUTSIDE this set is a rule the gate never implemented (the
# type layer, the extern classification, the syntax the gate's own reader does
# not read), so the gate admitting it says nothing — the gate is a slice, and
# it is a slice on purpose.
#
# tests/test_selfhost_lower.py's `_classify` is the full version, and it is the
# one to reach for: it maps a refusal onto the tag AND compares the message,
# which is what pins the gate's diagnostics to the reference's. This is the
# coarse half of the same map — enough to answer "is this refusal the gate's
# job at all", which is the only question a fuzzer can ask soundly about an
# input no one curated.
_IN_SLICE_MARKERS = (
    "(G3)", "(A1)",
    "provision conflict",
    "must precede every effect, emit, await, and provide statement",
    "applies to required keys only", "is intercepted twice in",
    "`await` is only allowed in a component body",
    "declares it async", "declares it not async",
    "is not a declared requirement of",
    "names an unknown component", "must be bound to a handle",
    "is not a declared provision of", "declares more than one `handoff`",
    "is not a declared requirement or provision of", "is isolated twice in",
    "routes a *required* key", "is already isolated to a single realm in",
    "is routed twice in", "unknown routing strategy", "multi-realm bind of",
    "declared plain, but this implementation reaches",
    "must be marked `emit`", "emits through",
)


def refusal_is_in_slice(exc: RevlError) -> bool:
    """Whether the reference's refusal is one of the guarantees the gate decides."""
    if exc.code in ("G4", "A1"):
        return True
    return any(marker in exc.message for marker in _IN_SLICE_MARKERS)


class GateStage:
    """selfhost/lower.rvl's `admit_src` — the gate `crates/revl-gate` embeds.

    Two properties, and only two, because only these are sound against an
    implementation that is deliberately a SLICE of the language:

      * it must not FAULT. A gate whose answer can be a python traceback, a
        rust panic, or a wrapped index is a gate a `catch`-less consumer cannot
        call, and `crates/revl-gate` is exactly such a consumer.
      * it must not ADMIT what the reference refuses under a guarantee the gate
        claims (`refusal_is_in_slice`). That is the bypass direction, and it is
        the direction the gate's own contract calls fail-closed.

    The other direction — the gate refusing what the reference admits — is NOT
    a finding here. It is what an incomplete slice looks like from the outside,
    and it is common: on the plain `examples/` corpus the gate already refuses
    eleven of thirty-three files the reference accepts. Reporting it would bury
    the two real properties under the known gap, so it is counted instead.
    """

    name = "gate"

    def __init__(self, cache):
        self._admit = cache.selfhost("lower.rvl")["admit_src"]
        self.false_rejects = 0

    def check(self, src: str):
        in_slice_refusal = False
        try:
            compile_source(src, "fuzz.rvl")
            reference_admits = True
        except RevlError as exc:
            reference_admits = False
            in_slice_refusal = refusal_is_in_slice(exc)
        except RecursionError:
            return None
        except Exception:
            return None  # a reference crash is the reference stage's finding

        try:
            verdict = self._admit(src)
        except IndexFault as exc:
            return (Signature("gate", "index-fault", _selfhost_frame(sys.exc_info())),
                    str(exc))
        except OverflowError as exc:
            return (Signature("gate", "int-overflow", _selfhost_frame(sys.exc_info())),
                    str(exc)[:200])
        except RecursionError:
            return None
        except Exception as exc:
            return (Signature("gate", "selfhost-" + type(exc).__name__,
                              _selfhost_frame(sys.exc_info())), str(exc)[:200])

        selfhost_admits = verdict == ""
        if reference_admits and not selfhost_admits:
            self.false_rejects += 1
            return None
        if selfhost_admits and in_slice_refusal:
            return (Signature("gate", "false-admit", ""),
                    "reference refuses under a guarantee the gate decides, "
                    "gate admits")
        return None


STAGES = {s.name: s for s in (ReferenceStage, LexerStage, GateStage)}


class _StageCache:
    """The self-host build costs seconds; every stage that needs one shares it."""

    def __init__(self):
        self._built: dict[str, dict] = {}

    def selfhost(self, stage_file: str) -> dict:
        if stage_file not in self._built:
            self._built[stage_file] = build_selfhost(stage_file)
        return self._built[stage_file]


# -------------------------------------------------------------- input sources

_MUTATION_CHARS = list("(){}[]<>,.;:!?=+-*/%&|^~\"'`\\\n\t $@#0123456789"
                       "abcxyzABC_")

# The words that steer the mutator toward the grammar rather than away from it.
# Splicing a real keyword in is what reaches the declaration and wiring paths;
# random punctuation alone plateaus at "the lexer said no" almost immediately.
_KEYWORDS = (
    "fn", "component", "composition", "let", "var", "return", "emit", "await",
    "effect", "provide", "isolate", "intercept", "route", "handoff", "spawn",
    "type", "use", "extern", "test", "requires", "provides", "match", "if",
    "else", "while", "for", "realm", "async", "pure", "host", "approval",
    "lease", "timer", "subscribe", "stream", "config", "cache", "service",
    "method", "row", "endorse", "hole", "break", "continue", "fail",
    "lifecycle", "fault", "prop", "assert", "in", "as", "Int", "Str", "Bool",
    "Float", "List", "Opt", "Result", "Map",
)


def load_corpus() -> list[str]:
    """Every `.rvl` in the tree: examples (valid programs), rejections (the
    refusals), fixtures, and the self-host itself, which is the largest and
    most feature-dense revl program that exists."""
    seen: dict[str, str] = {}
    for sub in ("examples", "tests/fixtures", "selfhost", "stdlib", "demo",
                "backends", "tck"):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.rvl")):
            try:
                seen[str(path)] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return list(seen.values())


def _mutate(src: str, rng: random.Random) -> str:
    for _ in range(rng.randint(1, 5)):
        if not src:
            src = "x"
        op = rng.randint(0, 5)
        i = rng.randrange(len(src))
        if op == 0:                                    # insert a character
            src = src[:i] + rng.choice(_MUTATION_CHARS) + src[i:]
        elif op == 1:                                  # delete a character
            src = src[:i] + src[i + 1:]
        elif op == 2:                                  # overwrite a character
            src = src[:i] + rng.choice(_MUTATION_CHARS) + src[i + 1:]
        elif op == 3:                                  # cut a run
            j = min(len(src), i + rng.randint(1, 80))
            src = src[:i] + src[j:]
        elif op == 4:                                  # splice in a keyword
            src = src[:i] + " " + rng.choice(_KEYWORDS) + " " + src[i:]
        else:                                          # copy a run
            j = rng.randrange(len(src))
            src = src[:i] + src[j:min(len(src), j + rng.randint(1, 60))] + src[i:]
    return src


def _window(src: str, rng: random.Random, limit: int) -> str:
    """A slice of a big file. Mutating a 3000-line self-host source wastes the
    budget re-lexing the same prefix; a window keeps every iteration cheap and
    still lands inside real syntax."""
    if len(src) <= limit:
        return src
    start = rng.randrange(0, len(src) - limit // 2)
    return src[start:start + rng.randint(limit // 8, limit)]


def _splice(corpus: list[str], rng: random.Random) -> str:
    """Two files joined at a declaration boundary. This is what reaches the
    linker: duplicate provisions, dangling requirements, two compositions in
    one file — the wiring checks (G1/G2/G3) a single mutated file rarely
    perturbs."""
    a, b = rng.choice(corpus), rng.choice(corpus)
    a_lines, b_lines = a.split("\n"), b.split("\n")
    return ("\n".join(a_lines[:rng.randrange(1, len(a_lines) + 1)]) + "\n"
            + "\n".join(b_lines[rng.randrange(0, len(b_lines)):]))


def _random_text(rng: random.Random) -> str:
    """Raw bytes as text: what the decoder and the lexer's edge handling need,
    and what a grammar-aware generator by construction never produces."""
    alphabet = rng.choice([
        _MUTATION_CHARS,
        list(string.printable),
        list("\x00\x01\x7f﻿   ​¡é日本語𝕏"),
        list("{}[]()"),
        list("\"'`\\"),
    ])
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 200)))


def make_input(corpus: list[str], rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.60:
        return _mutate(_window(rng.choice(corpus), rng, 1400), rng)
    if roll < 0.80:
        return _mutate(_splice(corpus, rng), rng)
    if roll < 0.90:
        return _window(rng.choice(corpus), rng, 1400)
    return _random_text(rng)


# ---------------------------------------------------------------- the shrinker

def shrink(src: str, oracle, signature: Signature, budget: float = 20.0) -> str:
    """Line-then-chunk delta debugging down to the same signature.

    Lines first because a program is a list of declarations and dropping whole
    ones converges fast; characters after, because the interesting reproducers
    ("fn t])->t[") are not line-shaped at all. Time-bounded: a shrink that will
    not converge must not eat the campaign.
    """
    deadline = time.monotonic() + budget

    def same(candidate: str) -> bool:
        if not candidate:
            return False
        try:
            result = oracle(candidate)
        except Exception:
            return False
        return result is not None and result[0] == signature

    current = src
    progress = True
    while progress and time.monotonic() < deadline:
        progress = False

        lines = current.split("\n")
        i = 0
        while i < len(lines) and time.monotonic() < deadline:
            candidate = "\n".join(lines[:i] + lines[i + 1:])
            if same(candidate):
                lines, progress = lines[:i] + lines[i + 1:], True
            else:
                i += 1
        current = "\n".join(lines)

        size = max(1, len(current) // 2)
        while size >= 1 and time.monotonic() < deadline:
            i = 0
            while i < len(current) and time.monotonic() < deadline:
                candidate = current[:i] + current[i + size:]
                if same(candidate):
                    current, progress = candidate, True
                else:
                    i += 1
            size //= 2
    return current


# ---------------------------------------------------------------- the campaign

@dataclass
class Campaign:
    stages: list
    corpus: list[str]
    findings: dict = field(default_factory=dict)
    iterations: int = 0
    recursion_hits: int = 0

    def run(self, rng: random.Random, deadline: float, limit: int,
            quiet: bool) -> None:
        while self.iterations < limit and time.monotonic() < deadline:
            self.iterations += 1
            src = make_input(self.corpus, rng)
            for stage in self.stages:
                result = stage.check(src)
                if result is None:
                    continue
                signature, detail = result
                if signature.kind == "recursion":
                    self.recursion_hits += 1
                    continue
                if signature in self.findings:
                    self.findings[signature].hits += 1
                    continue
                minimal = shrink(src, stage.check, signature)
                self.findings[signature] = Finding(signature, minimal, detail)
                if not quiet:
                    print(f"  FOUND {signature}: {detail}\n"
                          f"        {minimal!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", default="all",
                        choices=["all", *STAGES],
                        help="which oracle to run (default: all)")
    parser.add_argument("--seconds", type=float, default=30.0,
                        help="time budget (default: 30)")
    parser.add_argument("--iterations", type=int, default=10 ** 9,
                        help="cap on inputs, whichever bound hits first")
    parser.add_argument("--seed", type=int, default=0,
                        help="deterministic under this seed")
    parser.add_argument("--json", metavar="PATH",
                        help="write the findings to PATH as JSON")
    parser.add_argument("--quiet", action="store_true",
                        help="only the summary")
    args = parser.parse_args()

    names = list(STAGES) if args.stage == "all" else [args.stage]
    cache = _StageCache()
    started = time.monotonic()
    stages = [STAGES[n](cache) for n in names]
    build_seconds = time.monotonic() - started

    corpus = load_corpus()
    if not args.quiet:
        print(f"revl frontend fuzzer — stages {', '.join(names)}, "
              f"seed {args.seed}, budget {args.seconds}s")
        print(f"  corpus {len(corpus)} .rvl files, "
              f"self-host built in {build_seconds:.1f}s", flush=True)

    campaign = Campaign(stages, corpus)
    campaign.run(random.Random(args.seed),
                 time.monotonic() + args.seconds, args.iterations, args.quiet)
    elapsed = time.monotonic() - started

    # The counted-not-reported numbers ride in the summary rather than the
    # findings, because the budget spent and the ground covered are the result
    # when nothing is found — and "nothing found" is a real result.
    counted = [f"{campaign.recursion_hits} recursion-limit hits"] \
        if campaign.recursion_hits else []
    gate = next((s for s in stages if isinstance(s, GateStage)), None)
    if gate is not None and gate.false_rejects:
        counted.append(f"{gate.false_rejects} gate slice-gap refusals")

    print(f"\n{campaign.iterations} inputs in {elapsed:.1f}s "
          f"({campaign.iterations / max(elapsed, 1e-9):.0f}/s), "
          f"{len(campaign.findings)} distinct findings"
          + ("; " + ", ".join(counted) if counted else ""))
    for finding in campaign.findings.values():
        print(f"\n  {finding.signature}  (x{finding.hits})")
        print(f"    {finding.detail}")
        print(f"    {finding.source!r}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"seed": args.seed, "iterations": campaign.iterations,
             "seconds": round(elapsed, 2),
             "findings": [f.as_dict() for f in campaign.findings.values()]},
            indent=2), encoding="utf-8")

    return 1 if campaign.findings else 0


if __name__ == "__main__":
    sys.exit(main())
