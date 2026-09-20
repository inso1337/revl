#!/usr/bin/env python3
"""The gate/reference verdict census: every place `crates/revl-gate` and the
reference compiler disagree, enumerated instead of stumbled upon.

WHY THIS EXISTS
---------------
`crates/revl-gate` embeds `selfhost/lower.rvl`'s `admit_src`. Its contract has
two halves, and only one of them was ever measured:

  * SOUND REFUSAL — every refusal the crate issues is a real reference refusal,
    with the same code and the same message. `tests/test_gate_crate_admit.py`
    holds this over the hand-written oracle corpus.
  * NO BYPASS — the crate never raises `no_objection` for a program the
    reference refuses under a guarantee the crate claims to decide. Nothing
    held this. `tools/fuzz_frontend.py --stage gate` can *stumble* on a bypass,
    but a fuzzer reports the inputs it happened to draw; it does not enumerate
    a surface, and three separate bypass families were each found one at a time
    that way.

This tool enumerates. It runs the gate and the reference over the SAME inputs
and buckets every outcome, so the answer to "how far apart are these two" is a
table rather than an anecdote. Three of the buckets are defects, the rest are
the honest shape of a gate that covers one layer of a larger language.

THE BUCKETS
-----------
Ordered by how much they matter.

  ``false-admission``
      THE ISSUED-ADMISSION DIRECTION, the most dangerous class the gate guards
      against: the gate ISSUES an admission (`"admitted": true` on the wire) for
      a program the reference refuses for ANY reason. This is a LIVE guard, not a
      scaffold: the gate has an admission arm (`revl_gate::issue_admission`,
      issue #346), both engines below exercise it, and `ADMISSION_PROGRAMS` puts
      real certified programs and one-token-outside near misses in the corpus so
      the bucket is measured on every run rather than only in principle. Zero
      tolerance: no named-list allowance, never baselined. Distinct from
      `false-admit`, which is a failure to REFUSE (a no-objection), not a wrongly
      issued admission.

  ``false-admit/<tag>``
      THE BYPASS DIRECTION. The reference refuses under a guarantee this gate
      claims (`_classify` names it G1..G4 / A1 / PRELUDE / SPAWN / HANDOFF /
      ROUTE / BOOT) and the gate raises no objection. Never acceptable; the
      baseline has no room for it and `--check` fails on one, full stop.

  ``tag-mismatch/<ref>-><gate>`` / ``msg-mismatch/<tag>``
      Both refuse, but not with the same verdict. The crate promises its
      refusals are the reference's verbatim, so these break the contract a
      consumer acts on even though they are not bypasses.

  ``false-reject/<tag>``
      The gate refuses a program the reference admits. Not a bypass — this is
      what an incomplete slice looks like from the outside, and it is the
      direction the crate is explicitly allowed to err in. It is still tracked
      exactly, because a FIX that "works" by refusing more would land here and
      nowhere else. (A change in this repo once caused an 85-file
      false-rejection regression with every unit test green.)

  ``no-objection-out-of-slice``
      The reference refuses for a reason outside the covered layer — the type
      layer, mostly. Documented, by design, not a defect: see the crate's "The
      verdict surface issues no admissions". A no-objection is not a green, so an
      out-of-slice reference refusal against one is forgiven here; the same
      reference refusal against an ISSUED admission is not, and lands in
      ``false-admission`` above.

  ``refuse-out-of-slice/<tag>`` / ``agree-*`` / ``frontier-declined``
      The rest. Both refuse for unrelated reasons; both agree; or the frontier
      guard declined to decide before the gate ran.

WHAT "THE GATE" MEANS HERE
--------------------------
Two engines, same buckets:

  ``--engine selfhost`` (default) runs `selfhost/lower.rvl`'s `admit_src`
      through the python backend, preceded by a python mirror of the crate's
      `frontier::scan` and followed by a python mirror of its
      `admission::certify`, both of whose TABLES ARE IMPORTED from
      `tools/build_gate_crate.py`. Seconds, no toolchain, so it can run on
      every PR. The admission mirror is what makes the `false-admission` bucket
      above a live guard in the job that has no cargo.

  ``--engine crate`` builds the real crate and asks it `issue_admission`, which
      is the verdict wire verbatim wherever it withholds and the admission wire
      where it does not. Slow and needs cargo; it is what proves the fast engine
      is not lying.

`tests/test_gate_crate_admit.py::test_the_two_engines_agree` pins the two
against each other so the cheap one stays honest.

USAGE
-----
    python3 tools/gate_reference_census.py                  # the census
    python3 tools/gate_reference_census.py --check          # the CI gate
    python3 tools/gate_reference_census.py --record         # re-baseline
    python3 tools/gate_reference_census.py --all            # + bench artifacts
    python3 tools/gate_reference_census.py --fuzz 20000 --seed 7
    python3 tools/gate_reference_census.py --engine crate --check

Exit status is 1 when `--check` finds any difference from the baseline.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BASELINE = ROOT / "tools" / "gate_reference_census_baseline.json"

# The corpus directories the baseline is recorded over. Deliberately NOT the
# whole tree: `bench/results/**` is model-generated output that is regenerated
# in bulk, so pinning it here would red an unrelated bench rerun. `--all` adds
# it back for a manual sweep, which is where those files earn their keep — they
# are the densest source of real-world revl in the repo and they found the
# duplicate-callee G4 message divergence.
CORPUS_DIRS = (
    "examples",
    "tests/fixtures",
    "selfhost",
    "stdlib",
    "demo",
    "tck",
    "backends",
    "dogfood",
)
EXTRA_DIRS = ("bench", "registry", "playground", "site", "docs", "forks")

# Directories that can hold an INSTALLED copy of the tree, or build output.
# Walking them counts the same corpus twice under a different path: the
# `frontend-cordis` job runs `backends/python/setup.sh`, which creates
# `backends/python/.venv` with revl installed, and `backends` is a census
# directory -- so the walk descended into site-packages and reported 22
# phantom "new divergences" that were the tree's own programs seen again.
_SKIP_DIRS = frozenset({
    "target", ".git", ".venv", "venv", "site-packages",
    "node_modules", ".cordis-py", "__pycache__",
})


# ------------------------------------------------------------- the reference

def _reference():
    """`(tag, message)` for a source: `("", "")` when the reference admits.

    The classifier is IMPORTED from the self-host lowering oracle, not copied:
    it is the same map that decides what `tests/test_selfhost_lower.py` calls
    agreement, and two copies would be free to drift into disagreeing about
    what the gate even claims to decide.
    """
    from revl.compiler import compile_source
    from revl.errors import RevlError

    try:
        import test_selfhost_lower as oracle
    except ImportError as exc:  # pragma: no cover - environment, not logic
        raise SystemExit(
            f"gate_reference_census: cannot import the reference classifier "
            f"from tests/test_selfhost_lower.py ({exc}). It needs pytest on "
            f"the path; run this from the repo with the dev environment.")

    def ref(src: str):
        try:
            compile_source(src, "census.rvl")
        except RevlError as exc:
            return (oracle._classify(exc), exc.message)
        return ("", "")

    return ref, oracle


# ------------------------------------------------------------ the fast engine

def build_selfhost_admit():
    """`selfhost/lower.rvl`'s `admit_src`, emitted to python and executed.

    The `_exec_emitted` shape every `tests/test_selfhost_*.py` uses: the
    component in the file makes the emitted module import the cordis-py
    `runtime` adapter, and the pure gate never touches it, so a lazy stub does.
    """
    from revl import compile_files

    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "census_pyemit", ROOT / "backends" / "python" / "emit.py")
    emitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emitter)

    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(emitter.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["admit_src"]


def build_frontier_scan():
    """A python mirror of `crates/revl-gate/src/frontier.rs::scan`.

    The TABLES are imported from the generator that writes the rust, so the two
    cannot list different constructs, and so are the two RESOURCE bounds — only
    the scanning itself is restated, and `tests/test_gate_reference_census.py`
    holds it against the rust on the cases `frontier.rs`'s own unit tests cover.
    """
    spec = importlib.util.spec_from_file_location(
        "census_build_gate_crate", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    tables = generator.frontier_tables()
    return make_frontier_scan(tables["keywords"], tables["builtins"],
                              max_bytes=generator.MAX_SOURCE_BYTES,
                              max_level_items=generator.MAX_LEVEL_ITEMS)


def make_frontier_scan(keywords, builtins, max_bytes: int = 262144,
                       max_level_items: int = 1024):
    """The scan itself, over the given tables. Split out so a test can drive it
    with the rust's own table values."""
    excluded_keywords = set(keywords)
    excluded_builtins = set(builtins)

    def _strip_literals(raw: bytes) -> bytes:
        """`"..."` and `//` blanked to spaces — replaced, not deleted, so the
        `.`-preceded member test still sees the right neighbouring byte."""
        out = bytearray()
        i, n = 0, len(raw)
        while i < n:
            if raw[i] == 0x2F and i + 1 < n and raw[i + 1] == 0x2F:
                while i < n and raw[i] != 0x0A:
                    out.append(0x20)
                    i += 1
                continue
            if raw[i] == 0x22:
                out.append(0x20)
                i += 1
                while i < n:
                    if raw[i] == 0x5C and i + 1 < n:
                        out += b"  "
                        i += 2
                        continue
                    closing = raw[i] == 0x22
                    out.append(0x20)
                    i += 1
                    if closing:
                        break
                continue
            out.append(raw[i])
            i += 1
        return bytes(out)

    def _is_word(byte: int) -> bool:
        return (48 <= byte <= 57 or 65 <= byte <= 90
                or 97 <= byte <= 122 or byte == 0x5F)

    def _level_items(text: bytes) -> int:
        """Sibling items at the widest bracket level — see the rust
        `frontier.rs::level_items` for what this measures and why."""
        counts = [1]
        worst = 1
        for b in text:
            if b in (0x28, 0x5B, 0x7B):
                counts.append(1)
            elif b in (0x29, 0x5D, 0x7D):
                if len(counts) > 1:
                    counts.pop()
            elif b in (0x2C, 0x3B, 0x0A):
                counts[-1] += 1
            else:
                continue
            if counts[-1] > worst:
                worst = counts[-1]
        return worst

    def scan(source: str):
        raw = source.encode("utf-8", "surrogatepass")
        if len(raw) > max_bytes:
            return f"source is {len(raw)} bytes, above the {max_bytes}-byte bound"
        text = _strip_literals(raw)
        items = _level_items(text)
        if items > max_level_items:
            return (f"source has {items} items at one bracket level, above the "
                    f"{max_level_items}-item bound")
        i, n = 0, len(text)
        while i < n:
            if not _is_word(text[i]):
                i += 1
                continue
            start = i
            while i < n and _is_word(text[i]):
                i += 1
            word = text[start:i].decode("ascii")
            if start > 0 and text[start - 1] == 0x2E:
                if word in excluded_builtins:
                    return f"`.{word}()` is outside the covered surface"
            elif word in excluded_keywords:
                return f"`{word}` is outside the covered surface"
        return None

    return scan


def build_admission_certify():
    """A python mirror of `crates/revl-gate/src/admission.rs::certify`.

    The ADMISSION half of the fast engine, and the reason the `false-admission`
    bucket is a live guard rather than a scaffold: the crate's `issue_admission`
    upgrades a no-objection to an ISSUED admission exactly where this returns a
    basis, so the cheap engine that runs on every PR sees the same admissions the
    shipped crate issues.

    Its TABLES are imported from the generator that writes the rust, so the two
    cannot be written over different vocabularies; only the walk is restated, and
    `tests/test_gate_reference_census.py` holds it against the cases
    `admission.rs`'s own unit tests cover. The BASIS TEXT is deliberately not
    mirrored — it is off the wire, so the crate engine cannot see it either and a
    wording difference is not a divergence.
    """
    spec = importlib.util.spec_from_file_location(
        "census_build_gate_crate_admission", ROOT / "tools" / "build_gate_crate.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return make_admission_certify(generator.admission_tables())


def make_admission_certify(tables):
    """The certifier itself, over the given tables. Split out so a test can drive
    it with the rust's own table values.

    Returns `certify(source) -> bool`: True when the source is inside the
    admission surface. The walk is total by construction — every step either
    consumes a token or returns False — because "skip what I do not recognise" is
    the wave-through the surface exists to prevent.

    This mirrors the STANDALONE `certify`, which is the question
    `revl_gate::issue_admission` asks and the one the census corpus can pose: a
    census case is a source, not a source plus a running composition. So every
    signature a component is typed over has to come from the source's own text,
    exactly as it does in the rust when `running` is `None`."""
    scalars = frozenset(tables["scalars"])
    reserved = frozenset(tables["reserved"])
    keywords = frozenset(tables["keywords"])

    def tokens(source: str):
        """The source as tokens, or None for a byte outside the alphabet. A
        literal, a number, an operator, a `[`, an `@` or a non-ASCII byte all
        return None, which is how "carries no term this certifier does not type
        itself" is enforced at the bottom rather than argued at the top. `.` is
        in the alphabet only because a provide-method body reaches a required
        service through one."""
        out = []
        i, n = 0, len(source)
        while i < n:
            ch = source[i]
            if ch in " \t\r\n":
                i += 1
                continue
            if ch == "/" and i + 1 < n and source[i + 1] == "/":
                while i < n and source[i] != "\n":
                    i += 1
                continue
            if ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
                start = i
                i += 1
                while i < n and (source[i] == "_" or source[i].isascii()
                                 and source[i].isalnum()):
                    i += 1
                out.append(source[start:i])
                continue
            if ch == "-" and i + 1 < n and source[i + 1] == ">":
                out.append("->")
                i += 2
                continue
            if ch in "{}(),:=.":
                out.append(ch)
                i += 1
                continue
            return None
        return out

    def is_name(token: str) -> bool:
        head = token[:1]
        if not (head == "_" or ("a" <= head <= "z") or ("A" <= head <= "Z")):
            return False
        return token not in keywords

    def is_declarable(token: str) -> bool:
        return is_name(token) and token not in reserved

    def matching_brace(toks, open_at):
        """The matching `}` for the `{` at `open_at`, by DEPTH. Depth is what
        makes a mis-sliced block impossible: a nested brace the body grammar
        does not allow is handed to the body walk, which refuses it, rather than
        shortening the range and leaving the tail to be re-read."""
        depth, i, n = 0, open_at, len(toks)
        while i < n:
            if toks[i] == "{":
                depth += 1
            elif toks[i] == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    def parse_component(toks, at):
        """`component <Name> (requires|provides <key>: <Service>)* { (provide
        <key> { … })* }`, as `(component, next index)`."""
        n = len(toks)
        i = at + 1
        if i >= n or not is_declarable(toks[i]):
            return None
        name = toks[i]
        i += 1
        reqs, provs = [], []
        while i < n and toks[i] in ("requires", "provides"):
            if (i + 3 >= n or not is_name(toks[i + 1]) or toks[i + 2] != ":"
                    or not is_name(toks[i + 3])):
                return None
            (reqs if toks[i] == "requires" else provs).append(
                (toks[i + 1], toks[i + 3]))
            i += 4
        if i >= n or toks[i] != "{":
            return None
        close = matching_brace(toks, i)
        if close is None:
            return None
        i += 1
        blocks = []
        while i < close:
            # `provide` is the ONLY statement a certified component body carries.
            if (toks[i] != "provide" or i + 2 >= close or not is_name(toks[i + 1])
                    or toks[i + 2] != "{"):
                return None
            inner = matching_brace(toks, i + 2)
            if inner is None or inner >= close:
                return None
            blocks.append((toks[i + 1], i + 3, inner))
            i = inner + 1
        return {"name": name, "reqs": reqs, "provs": provs,
                "blocks": blocks}, close + 1

    def parse(toks):
        """The source as declarations, or None. Shape only; the obligations
        across declarations are `check`'s."""
        n = len(toks)
        aliases = [toks[i + 1] for i, tok in enumerate(toks)
                   if tok == "type" and i + 1 < n]
        known = scalars | set(aliases)
        services, components = [], []
        declared_aliases = 0
        i = 0
        while i < n:
            if toks[i] == "type":
                if i + 3 >= n or toks[i + 2] != "=":
                    return None
                if not is_declarable(toks[i + 1]) or toks[i + 3] not in scalars:
                    return None
                declared_aliases += 1
                i += 4
                continue
            if toks[i] == "component":
                parsed = parse_component(toks, i)
                if parsed is None:
                    return None
                comp, i = parsed
                components.append(comp)
                continue
            if toks[i] != "service" or i + 2 >= n:
                return None
            if not is_declarable(toks[i + 1]) or toks[i + 2] != "{":
                return None
            name = toks[i + 1]
            i += 3
            methods = []
            while i < n and toks[i] != "}":
                # An operation carrying a MARKING (`emission fn`, `async fn`,
                # …) does not match here and leaves the surface whole.
                if toks[i] != "fn" or i + 1 >= n or not is_name(toks[i + 1]):
                    return None
                mname = toks[i + 1]
                i += 2
                if i >= n or toks[i] != "(":
                    return None
                i += 1
                params = []
                while i < n and toks[i] != ")":
                    if not is_name(toks[i]) or i + 2 >= n or toks[i + 1] != ":":
                        return None
                    if toks[i + 2] not in known:
                        return None
                    params.append((toks[i], toks[i + 2]))
                    i += 3
                    if i < n and toks[i] == ",":
                        i += 1
                pnames = [p[0] for p in params]
                if i >= n or len(set(pnames)) != len(pnames):
                    return None
                i += 1  # the `)`
                ret = None
                if i < n and toks[i] == "->":
                    if i + 1 >= n or toks[i + 1] not in known:
                        return None
                    ret = toks[i + 1]
                    i += 2
                methods.append({"name": mname, "params": params, "ret": ret})
            mnames = [m["name"] for m in methods]
            if i >= n or len(set(mnames)) != len(mnames):
                return None
            i += 1  # the `}`
            services.append({"name": name, "methods": methods})
        return {"services": services, "aliases": aliases,
                "components": components, "declared_aliases": declared_aliases}

    def type_expr(toks, at, end, env, comp, text):
        """The type of one body expression and the index past it, or None.

        Two productions and no more: a bound PARAMETER read, and a call
        `<required key>.<op>(<expr>, …)` on a service the component requires.
        No arm guesses a type, which is what makes the return comparison in
        `check_method` a real check."""
        if at >= end or not is_name(toks[at]):
            return None
        if at + 1 >= end or toks[at + 1] != ".":
            bound = dict(env).get(toks[at])
            return None if bound is None else (bound, at + 1)
        svc = dict(comp["reqs"]).get(toks[at])
        if svc is None:
            return None
        if at + 3 >= end or not is_name(toks[at + 2]) or toks[at + 3] != "(":
            return None
        decl = next((s for s in text if s["name"] == svc), None)
        if decl is None:
            return None
        sig = next((m for m in decl["methods"] if m["name"] == toks[at + 2]), None)
        if sig is None or sig["ret"] is None:
            return None
        params = sig["params"]
        i, args = at + 4, 0
        while i < end and toks[i] != ")":
            if args > 0:
                if toks[i] != ",":
                    return None
                i += 1
            typed = type_expr(toks, i, end, env, comp, text)
            if typed is None:
                return None
            ty, i = typed
            if args >= len(params) or params[args][1] != ty:
                return None
            args += 1
        if i >= end or args != len(params):
            return None
        return sig["ret"], i + 1

    def check_method(toks, at, end, decl, comp, text):
        """`fn <op>(<param>, …) = <expr>` against the operation it implements,
        as the index past it. No parameter annotation, no return annotation, no
        block body: each is a form the reference has its own rules for."""
        params, ret = decl["params"], decl["ret"]
        if ret is None or ret not in scalars:
            return None
        if any(ty not in scalars for _, ty in params):
            return None
        i = at + 2  # past `fn <op>`
        if i >= end or toks[i] != "(":
            return None
        i += 1
        env = []
        while i < end and toks[i] != ")":
            if env:
                if toks[i] != ",":
                    return None
                i += 1
                if i >= end:
                    return None
            if not is_name(toks[i]):
                return None
            if len(env) >= len(params) or params[len(env)][0] != toks[i]:
                return None
            env.append(params[len(env)])
            i += 1
        if i >= end or len(env) != len(params):
            return None
        i += 1  # the `)`
        if i >= end or toks[i] != "=":
            return None
        typed = type_expr(toks, i + 1, end, env, comp, text)
        if typed is None or typed[0] != ret:
            return None
        return typed[1]

    def check_block(toks, block, decl, comp, text):
        """One `provide <key> { … }` body: exactly the declared operations, each
        typed. A missing one, an extra one and a repeated one are all reference
        refusals the composition gate does not make."""
        key, start, end = block
        seen, i = [], start
        while i < end:
            if toks[i] != "fn" or i + 1 >= end or not is_name(toks[i + 1]):
                return None
            name = toks[i + 1]
            op = next((m for m in decl["methods"] if m["name"] == name), None)
            if op is None or name in seen:
                return None
            seen.append(name)
            i = check_method(toks, i, end, op, comp, text)
            if i is None:
                return None
        if i != end or len(seen) != len(decl["methods"]):
            return None
        return len(seen)

    def check_component(toks, comp, text):
        """One component against the service table it is written over, as the
        number of provide-method bodies typed."""
        keys = [k for k, _ in comp["reqs"] + comp["provs"]]
        if len(set(keys)) != len(keys):
            return None
        block_keys = [b[0] for b in comp["blocks"]]
        if len(set(block_keys)) != len(block_keys) \
                or len(block_keys) != len(comp["provs"]):
            return None
        # Standalone, every service a component names has to be in the text: the
        # rust reads an AMBIENT one off the manifest wire, and there is no wire
        # here.
        for _, svc in comp["reqs"]:
            if not any(s["name"] == svc for s in text):
                return None
        bodies = 0
        for key, svc in comp["provs"]:
            decl = next((s for s in text if s["name"] == svc), None)
            block = next((b for b in comp["blocks"] if b[0] == key), None)
            if decl is None or block is None:
                return None
            typed = check_block(toks, block, decl, comp, text)
            if typed is None:
                return None
            bodies += typed
        return bodies

    def certify(source: str) -> bool:
        toks = tokens(source)
        if toks is None:
            return False
        parsed = parse(toks)
        if parsed is None:
            return False
        services = [s["name"] for s in parsed["services"]]
        components = [c["name"] for c in parsed["components"]]
        aliases = parsed["aliases"]
        # The reference refuses a duplicate service, method, alias and
        # component, and the native gate does not make all of those refusals, so
        # the certifier carries them itself.
        if len(set(services)) != len(services):
            return False
        if len(set(aliases)) != len(aliases) \
                or parsed["declared_aliases"] != len(aliases):
            return False
        if len(set(components)) != len(components):
            return False
        # One namespace per name, conservatively.
        if set(services) & set(aliases) or set(components) & set(services) \
                or set(components) & set(aliases):
            return False
        for comp in parsed["components"]:
            if check_component(toks, comp, parsed["services"]) is None:
                return False
        return True

    return certify


class SelfhostEngine:
    """`admit_src` behind the crate's frontier guard, in-process."""

    name = "selfhost"

    def __init__(self):
        self._scan = build_frontier_scan()
        self._admit = build_selfhost_admit()
        self._certify = build_admission_certify()

    def verdicts(self, sources):
        for src in sources:
            reason = self._scan(src)
            if reason is not None:
                yield ("frontier", reason)
                continue
            try:
                wire = self._admit(src)
            except RecursionError:
                yield ("recursion", "")
                continue
            except Exception as exc:  # a gate fault is a finding of its own
                yield ("fault", f"{type(exc).__name__}: {exc}"[:200])
                continue
            if wire == "":
                # The crate's `issue_admission` upgrades a no-objection to an
                # ISSUED admission exactly here, so the fast engine does too:
                # otherwise the `false-admission` bucket would be blind to the
                # arm on every PR. The payload is the wire's — an issued
                # admission carries no code and no message.
                if self._certify(src):
                    yield ("admitted", ("", ""))
                else:
                    yield ("no_objection", "")
            elif "|" in wire:
                code, message = wire.split("|", 1)
                yield ("refused", (code, message))
            else:
                yield ("frontier", f"unrecognised wire shape {wire!r}")


# ---------------------------------------------------------- the real crate

_CONSUMER_MAIN = r'''use std::io::Read;

fn main() {
    let mut blob = String::new();
    std::io::stdin().read_to_string(&mut blob).expect("read stdin");
    for source in blob.split('\0') {
        // The ADMISSION question, not just the refusal one. `issue_admission`
        // returns the verdict's own wire verbatim wherever it withholds, so this
        // is a superset of `admit(...).to_json()`: same bytes everywhere except
        // the sources the gate actually admits, which is the direction the
        // `false-admission` bucket exists to measure.
        println!("{}", revl_gate::issue_admission(source).to_json());
    }
}
'''


class CrateEngine:
    """The committed `crates/revl-gate`, asked through a standalone consumer.

    The same shape `tests/test_gate_crate_admit.py` builds — a crate whose only
    dependency is `revl-gate` by path, fed NUL-separated sources on stdin (a
    revl source can never contain a NUL) and answering one JSON verdict per
    program, in order.
    """

    name = "crate"

    def __init__(self, workdir: Path | None = None):
        from revl.run_rust import rust_runtime_reason

        reason = rust_runtime_reason()
        if reason is not None:
            raise SystemExit(
                f"gate_reference_census --engine crate needs a resolvable "
                f"cordis-rs toolchain: {reason}")
        self._dir = Path(workdir or tempfile.mkdtemp(prefix="gate-census-"))
        self._binary = self._build()

    def _build(self) -> Path:
        crate = (ROOT / "crates" / "revl-gate").as_posix()
        src = self._dir / "src"
        src.mkdir(parents=True, exist_ok=True)
        (self._dir / "Cargo.toml").write_text(
            "[package]\n"
            'name = "revl_gate_census"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n'
            "\n[workspace]\n"
            "\n[dependencies]\n"
            f'revl-gate = {{ path = "{crate}" }}\n')
        (src / "main.rs").write_text(_CONSUMER_MAIN)
        for offline in (True, False):
            argv = ["cargo", "build", "--quiet"]
            if offline:
                argv.append("--offline")
            done = subprocess.run(argv, cwd=self._dir, capture_output=True,
                                  text=True)
            if done.returncode == 0:
                return self._dir / "target" / "debug" / "revl_gate_census"
            if not offline:
                raise SystemExit(
                    "gate_reference_census: could not build the consumer "
                    f"crate:\n{done.stderr[-4000:]}")
        raise SystemExit("unreachable")

    def verdicts(self, sources):
        sources = list(sources)
        done = subprocess.run([str(self._binary)], input="\0".join(sources),
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(
                f"gate_reference_census: the consumer aborted "
                f"({done.returncode}); a `panic = \"abort\"` profile or a gate "
                f"abort:\n{done.stderr[-2000:]}")
        lines = done.stdout.splitlines()
        if len(lines) != len(sources):
            raise SystemExit(
                f"gate_reference_census: the consumer answered {len(lines)} "
                f"verdicts for {len(sources)} sources")
        for line in lines:
            payload = json.loads(line)
            kind = payload["verdict"]
            # `"admitted": true` is the wire's own signal of an ISSUED admission
            # and is read FIRST: it is `false` on every arm today (the crate has
            # no `Admitted`), so this never fires now, but it is what lets the
            # `false-admission` bucket catch the arm the day it opens, whatever
            # the arm's `verdict` string turns out to be named.
            if payload.get("admitted") is True:
                yield ("admitted", (payload.get("code") or "",
                                    payload.get("message") or ""))
            elif kind == "refused":
                yield ("refused", (payload["code"], payload["message"]))
            elif kind == "no_objection":
                yield ("no_objection", "")
            else:
                yield ("frontier", payload.get("message", ""))


ENGINES = {"selfhost": SelfhostEngine, "crate": CrateEngine}


# ------------------------------------------------------------------- corpus

def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# The ADMISSION-SURFACE programs (issue #346), hand-written for the same reason
# the oracle's are: nothing in the tree sits ON this boundary. Every real `.rvl`
# in the repo declares a component or an `fn` body, so a census over the tree
# alone measures the admission arm on ZERO inputs — a live guard that never
# fires, which is indistinguishable from the scaffold it replaced.
#
# Each entry is either INSIDE the surface (the gate must issue an admission, and
# the reference must admit it, or the `false-admission` bucket reds) or a NEAR
# MISS chosen to sit one token outside it. The near misses are the load-bearing
# half: `duplicate_service` and `duplicate_method` are refused by the reference
# and drawn no objection by the native gate, so a certifier that skipped its own
# two obligations would issue an admission the reference refuses and this census
# would say so by name.
#
# They are deliberately NOT files under `examples/` or `tests/fixtures/`: those
# trees are walked by other suites with their own contracts, and a census corpus
# entry has no business changing what they hold.
ADMISSION_PROGRAMS = (
    ("empty_composition", ""),
    ("comment_only", "// nothing to declare\n"),
    ("interface_one_service",
     "service Store {\n  fn get(key: Str) -> Str\n  fn put(key: Str, value: Str)\n}\n"),
    ("interface_two_services",
     "service Clock {\n  fn now() -> Int\n}\n\nservice Log {\n  fn write(line: Str)\n}\n"),
    ("interface_scalar_alias",
     "type Key = Str\ntype Size = Int32\n\nservice Cache {\n"
     "  fn read(k: Key) -> Size\n}\n"),
    ("interface_empty_service", "service Marker {\n}\n"),
    # --- the provide-method half of the surface (issue #346, 457 T6) ---
    ("component_identity_body",
     "service S {\n  fn a(x: Int) -> Int\n}\n"
     "component C provides s: S {\n  provide s {\n    fn a(x) = x\n  }\n}\n"),
    ("component_requires_and_calls",
     "service Store {\n  fn get(key: Str) -> Str\n}\n"
     "service Cache {\n  fn lookup(key: Str) -> Str\n}\n"
     "component Kv provides store: Store {\n"
     "  provide store {\n    fn get(key) = key\n  }\n}\n"
     "component CacheLayer requires store: Store provides cache: Cache {\n"
     "  provide cache {\n    fn lookup(key) = store.get(key)\n  }\n}\n"),
    ("component_no_provision", "component Idle {\n}\n"),
    # --- near misses: one token outside the surface each ---
    ("near_miss_duplicate_service",
     "service Dup {\n  fn a(x: Int) -> Int\n}\nservice Dup {\n  fn b(x: Int) -> Int\n}\n"),
    ("near_miss_duplicate_method",
     "service Dup {\n  fn a(x: Int) -> Int\n  fn a(y: Int) -> Int\n}\n"),
    ("near_miss_fn_body", "service S {\n  fn a(x: Int) -> Int\n}\n"
                          "fn twice(x: Int) -> Int { return x + x }\n"),
    ("near_miss_generic_type", "service S {\n  fn all() -> List[Int]\n}\n"),
    ("near_miss_record_alias", "type Row = { id: Int }\n"),
    ("near_miss_shadows_builtin", "type Int = Str\n"),
    ("near_miss_unknown_type", "service S {\n  fn a(x: Mystery) -> Int\n}\n"),
    # --- near misses in the provide-method half ---
    ("near_miss_component_unknown_service",
     "component C provides s: S {\n  provide s {\n    fn a(x) = x\n  }\n}\n"),
    # The RETURN near miss (`fn a(x: Int) -> Str` implemented by `fn a(x) = x`)
    # is deliberately NOT a corpus entry, and the reason is worth writing down:
    # the reference refuses it `T1` and `admit_src` raises no objection, so it is
    # a `false-admit/T1` — a real, pre-existing gap in the native gate's type
    # layer (docs/design/457 T4), not something this surface introduced. Putting
    # it in the corpus would widen a FAIL-OPEN baseline with a hand-written
    # program that is not in the tree. The certifier's obligation to catch it is
    # held directly instead, in
    # `tests/test_gate_reference_census.py::test_the_admission_mirror_matches_
    # the_rust` and in `admission.rs`'s own unit tests.
    ("near_miss_component_missing_method",
     "service S {\n  fn a(x: Int) -> Int\n  fn b(x: Int) -> Int\n}\n"
     "component C provides s: S {\n  provide s {\n    fn a(x) = x\n  }\n}\n"),
    ("near_miss_component_literal_body",
     "service S {\n  fn a(x: Int) -> Int\n}\n"
     "component C provides s: S {\n  provide s {\n    fn a(x) = 1\n  }\n}\n"),
    ("near_miss_component_effect",
     "service S {\n  fn a(x: Int) -> Int\n}\n"
     "component C provides s: S {\n  let m = effect Map.new() undo m.drop()\n"
     "  provide s {\n    fn a(x) = x\n  }\n}\n"),
)


def load_corpus(oracle, *, everything: bool = False):
    """`[(case_id, source)]` — every `.rvl` in the census directories, plus the
    oracle's own hand-written programs, which are the only inputs in the tree
    that were WRITTEN to sit on a guarantee boundary, plus `ADMISSION_PROGRAMS`,
    which are the only ones written to sit on the ADMISSION boundary."""
    cases: list[tuple[str, str]] = []
    for sub in CORPUS_DIRS + (EXTRA_DIRS if everything else ()):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.rvl")):
            if _SKIP_DIRS & set(path.parts):
                continue
            text = _read(path)
            if text is not None:
                cases.append((str(path.relative_to(ROOT)), text))
    for name, src in oracle.ACCEPTED_PROGRAMS:
        cases.append((f"oracle-accept:{name}", src))
    for entry in oracle.REJECTED_PROGRAMS:
        cases.append((f"oracle-reject:{entry[0]}", entry[1]))
    for name, src in ADMISSION_PROGRAMS:
        cases.append((f"admission:{name}", src))
    return cases


def load_fuzz(count: int, seed: int, corpus):
    """Mutated inputs from `tools/fuzz_frontend.py`'s generators, reused rather
    than restated so the census and the fuzzer draw from the same distribution.
    Fuzz cases are never baselined — they are a sweep, not a gate."""
    import random

    spec = importlib.util.spec_from_file_location(
        "census_fuzz", ROOT / "tools" / "fuzz_frontend.py")
    fuzz = importlib.util.module_from_spec(spec)
    # Registered before it executes: the module defines dataclasses, and
    # `@dataclass` looks its own module up in `sys.modules` while the class body
    # is still being processed.
    sys.modules[spec.name] = fuzz
    spec.loader.exec_module(fuzz)
    rng = random.Random(seed)
    texts = [src for _, src in corpus if src.strip()]
    out = []
    for i in range(count):
        base = rng.choice(texts)
        out.append((f"fuzz:{seed}:{i}", fuzz._mutate(base, rng)))
    return out


# ------------------------------------------------------------------ census

# The bucket prefixes a divergence lands in. `false-admit` is the bypass and is
# never baselined; the rest are recorded exactly so a change has to justify
# every entry it adds or removes.
HARD = "false-admit"

# The ISSUED-ADMISSION bypass, distinct from `false-admit`. The gate HAS an
# admission arm now (`revl_gate::issue_admission`, issue #346): it upgrades a
# no-objection to an issued admission where the source is inside the admission
# surface, which is the region carrying no term the reference type layer decides.
# So this bucket is a LIVE guard on every run, and the thing it guards is the most
# dangerous class the gate can commit: a host reading a rust admission as a green
# and running code the reference never admitted. Both engines exercise the arm and
# `ADMISSION_PROGRAMS` keeps real certified programs in the corpus, so an empty
# bucket here is a measurement rather than a vacuum. Unlike `false-admit` it has
# NO named-list allowance and is NEVER baselined: zero tolerance.
ADMISSION = "false-admission"

# Buckets `--record` must never write, so their allowance can never grow to one.
# `--check` fails on any member regardless of the baseline.
NEVER_BASELINED = (ADMISSION,)

TRACKED = ("false-admission", "false-admit", "tag-mismatch", "msg-mismatch",
           "false-reject", "gate-fault")


def bucket(ref, gate) -> str:
    """The one bucket a `(reference, gate)` pair belongs to."""
    ref_tag, ref_msg = ref
    kind, payload = gate
    out_of_slice = ref_tag.startswith("OUT:")

    if kind == "frontier":
        return "frontier-declined"
    if kind == "recursion":
        return "recursion"
    if kind == "fault":
        return "gate-fault"
    if kind == "admitted":
        # The gate ISSUED an admission. Sound only when the reference admits the
        # same bytes; a reference refusal for ANY reason — in-slice, out-of-slice,
        # type layer, anything — is a false admission, no allowance. This is
        # strictly stronger than the `no_objection` case below, which forgives an
        # out-of-slice refusal because a no-objection is explicitly NOT a green.
        return "agree-admit" if ref_tag == "" else ADMISSION
    if kind == "no_objection":
        if ref_tag == "":
            return "agree-admit"
        return "no-objection-out-of-slice" if out_of_slice else f"{HARD}/{ref_tag}"

    code, message = payload
    if ref_tag == "":
        return f"false-reject/{code}"
    if out_of_slice:
        return f"refuse-out-of-slice/{code}"
    if code != ref_tag:
        return f"tag-mismatch/{ref_tag}->{code}"
    if message != ref_msg:
        return f"msg-mismatch/{ref_tag}"
    return f"agree-refuse/{ref_tag}"


def run(cases, engine, reference):
    """`{bucket: [case_id]}` plus a `details` map for the tracked buckets."""
    buckets: dict[str, list[str]] = {}
    details: dict[str, dict] = {}
    refs = []
    for _, src in cases:
        try:
            refs.append(reference(src))
        except RecursionError:
            refs.append(("OUT:reference recursion limit", ""))
        except Exception as exc:  # the reference stage's finding, not ours
            refs.append((f"OUT:reference fault {type(exc).__name__}", str(exc)))
    for (case_id, src), ref, gate in zip(cases, refs, engine.verdicts(
            src for _, src in cases)):
        name = bucket(ref, gate)
        buckets.setdefault(name, []).append(case_id)
        if name.split("/", 1)[0] in TRACKED:
            carries_pair = gate[0] in ("refused", "admitted")
            details[case_id] = {
                "bucket": name,
                "reference": {"tag": ref[0], "message": ref[1]},
                "gate": {"kind": gate[0],
                         "code": gate[1][0] if carries_pair else "",
                         "message": (gate[1][1] if carries_pair
                                     else gate[1] if isinstance(gate[1], str)
                                     else "")},
            }
    return buckets, details


def report(buckets: dict[str, list[str]], *, examples: int = 4) -> str:
    lines = []
    total = sum(len(v) for v in buckets.values())
    lines.append(f"gate/reference census over {total} programs")
    lines.append("")
    for name in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
        ids = buckets[name]
        mark = "!!" if name.split("/", 1)[0] in TRACKED else "  "
        lines.append(f"{mark} {len(ids):6d}  {name}")
        if mark == "!!":
            for case_id in ids[:examples]:
                lines.append(f"              {case_id}")
            if len(ids) > examples:
                lines.append(f"              ... and {len(ids) - examples} more")
    return "\n".join(lines)


def compare(buckets: dict[str, list[str]], baseline: dict) -> list[str]:
    """The `--check` verdict: every difference from the baseline, in both
    directions. A NEW divergence is a regression; a divergence that is GONE
    means the baseline is stale and has to be re-recorded, which is how a fix
    proves itself rather than quietly widening the allowance."""
    problems = []
    want = baseline.get("buckets", {})
    for name, ids in sorted(buckets.items()):
        head = name.split("/", 1)[0]
        if head not in TRACKED:
            continue
        # A never-baselined bucket (`false-admission`) fails on EVERY member,
        # independent of the baseline: `--record` cannot write one, so a baseline
        # that lists one is either stale or hand-edited and buys no tolerance.
        seen = set() if head in NEVER_BASELINED else set(want.get(name, []))
        new = sorted(set(ids) - seen)
        for case_id in new:
            if head in NEVER_BASELINED:
                label = "FALSE ADMISSION (issued admission the reference refuses)"
            elif name.startswith(HARD):
                label = "NEW BYPASS"
            else:
                label = "new divergence"
            problems.append(f"{label} {name}: {case_id}")
    for name, ids in sorted(want.items()):
        gone = sorted(set(ids) - set(buckets.get(name, [])))
        for case_id in gone:
            problems.append(
                f"no longer diverges {name}: {case_id} — re-record the "
                f"baseline (python3 tools/gate_reference_census.py --record)")
    return problems


def bypasses(buckets: dict[str, list[str]]) -> list[str]:
    """Every case in a `false-admit` bucket, sorted. The open bypass surface.

    Keyed on the bucket PREFIX (`false-admit/<tag>`), not `startswith`, so the
    `false-admission` bucket — which shares `false-admit` as a string prefix —
    is not swept in here. Its cases are the ISSUED-admission surface, reported
    separately by `false_admissions`."""
    out: list[str] = []
    for name, ids in buckets.items():
        if name.split("/", 1)[0] == HARD:
            out += ids
    return sorted(out)


def false_admissions(buckets: dict[str, list[str]]) -> list[str]:
    """Every case the gate ADMITTED and the reference refuses, sorted. Zero
    tolerance: any member is a release blocker, never a baselined allowance."""
    return sorted(buckets.get(ADMISSION, []))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", choices=sorted(ENGINES), default="selfhost")
    ap.add_argument("--all", action="store_true",
                    help="also sweep bench/registry/site (not baselined)")
    ap.add_argument("--fuzz", type=int, default=0,
                    help="add N mutated inputs (never baselined)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--check", action="store_true",
                    help="fail on any difference from the baseline")
    ap.add_argument("--record", action="store_true",
                    help="rewrite the baseline from this run")
    ap.add_argument("--json", type=Path, help="write the full census here")
    ap.add_argument("--examples", type=int, default=4)
    args = ap.parse_args(argv)

    if (args.check or args.record) and (args.all or args.fuzz):
        ap.error("--check/--record run over the baselined corpus only; "
                 "--all and --fuzz are for manual sweeps")

    reference, oracle = _reference()
    cases = load_corpus(oracle, everything=args.all)
    if args.fuzz:
        cases += load_fuzz(args.fuzz, args.seed, cases)
    engine = ENGINES[args.engine]()

    buckets, details = run(cases, engine, reference)
    print(report(buckets, examples=args.examples))

    if args.json:
        args.json.write_text(json.dumps(
            {"engine": engine.name, "buckets": buckets, "details": details},
            indent=1, sort_keys=True) + "\n")

    if args.record:
        BASELINE.write_text(json.dumps(
            {"note": ("Recorded by `python3 tools/gate_reference_census.py "
                      "--record`. Every entry is a KNOWN gate/reference "
                      "divergence over the census corpus; `--check` fails on "
                      "one that is not here, and on one here that no longer "
                      "diverges, so the allowance can only shrink in a diff "
                      "somebody reads. A `false-admit` entry is an OPEN GATE "
                      "BYPASS, not an accepted state: the list is capped by "
                      "name in tests/test_gate_reference_census.py so it "
                      "cannot grow quietly while it is worked down."),
             "corpus_dirs": list(CORPUS_DIRS),
             "buckets": {k: sorted(v) for k, v in sorted(buckets.items())
                         if k.split("/", 1)[0] in TRACKED
                         and k.split("/", 1)[0] not in NEVER_BASELINED},
             "details": {cid: d for cid, d in details.items()
                         if d["bucket"].split("/", 1)[0] not in NEVER_BASELINED}},
            indent=1, sort_keys=True) + "\n")
        print(f"\nrecorded {BASELINE.relative_to(ROOT)}")
        return 0

    if args.check:
        baseline = json.loads(BASELINE.read_text())
        problems = compare(buckets, baseline)
        if problems:
            print("\ngate/reference census FAILED:")
            for line in problems:
                print(f"  {line}")
            return 1
        print("\ngate/reference census: no change from the baseline.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
