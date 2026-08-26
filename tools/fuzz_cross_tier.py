#!/usr/bin/env python3
"""Cross-tier differential fuzzer (roadmap item 292) — the empirical counterpart
to item 133 (the cross-tier agreement theorem).

revl's central claim is not "we have six backends" but "one composition has the
same meaning across six runtimes". This harness turns that claim into evidence:

  1. GENERATE a valid revl program from a bounded grammar of pure functions and
     expressions over the types the language ships (Int/Float/Bool/Str, List,
     Opt, Result, records, ADTs + match). Generation is type-directed, so most
     programs are admissible; any the frontend rejects is DISCARDED, never
     counted as a divergence.
  2. COMPILE it to every backend (compilation is pure Python, always available).
  3. EXECUTE it on the tiers whose runtime is present on this box. The reference
     is the py tier (compilation + the py runtime need no external toolchain);
     the py value of the probe function is embedded as an assertion, and each
     other available tier runs that assertion. A tier whose toolchain is absent
     is SKIPPED with the reason its runner reports — never reported as a
     divergence.
  4. COMPARE: a divergence is a tier that RAN (did not skip) and disagreed with
     the py reference — either it failed to build/emit the admitted program
     (a compiles-implies-runs violation) or it computed a different value.
  5. SHRINK the divergence to a minimal program (delta-debug the declarations,
     then simplify the probe expression) that still diverges on the same tier.
  6. EMIT the minimized divergence as a regression fixture under
     examples/regressions/ with a note: source, seed, the property, per-tier
     outputs, and whether it maps to a known open item (278 rust / 279 ts /
     280 go) or is NEW.

This is a FIRST CUT: a bounded generator, a simple two-phase shrinker, and
py-plus-whatever-runs execution, all deterministic under `--seed`. It is not a
property-testing framework. Honesty over coverage: the report states which
tiers it could execute and how many programs it actually ran.

    python3 tools/fuzz_cross_tier.py --seed 1 --count 200
    python3 tools/fuzz_cross_tier.py --seed 1 --count 40 --slow   # + rust
    python3 tools/fuzz_cross_tier.py --tiers py,go --count 100 --no-fixtures
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import random
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.test import RUNNERS  # noqa: E402

REGRESSIONS = ROOT / "examples" / "regressions"

# The py tier is the reference oracle: compilation is pure Python and the py
# runtime needs no external toolchain, so it is always the ground truth a value
# divergence is measured against. Every other tier is a candidate to disagree.
REFERENCE = "py"

# A tier's divergence maps to a known open roadmap item (this session's wave) or
# is flagged NEW for the human to file.
KNOWN_ITEMS = {
    "rust": "278 (rust build gaps)",
    "ts": "279 (ts reserved-word / dynamic-access)",
    "go": "280 (go Opt / empty-list / wildcard-match gaps)",
}


# ==========================================================================
# the py emitter, loaded once, for extracting the reference value of `probe`
# ==========================================================================

def _load_py_emitter() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "revl_python_emit_fuzz", ROOT / "backends" / "python" / "emit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PY_EMIT = _load_py_emitter()
if str(ROOT / "backends" / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "backends" / "python"))


class ReferenceFault(Exception):
    """The py reference could not produce a clean value for `probe` (a runtime
    fault, or a value the literal renderer cannot represent). The program is
    discarded, never counted as a divergence."""


def reference_value(source: str):
    """Emit the py module, exec it, and return probe()'s concrete value."""
    ir = compile_source(source)
    code = _PY_EMIT.emit(ir)
    module = types.ModuleType("revl_fuzz_ref")
    module.__dict__["__name__"] = "revl_fuzz_ref"
    sys.modules["revl_fuzz_ref"] = module
    try:
        exec(compile(code, "<revl-fuzz-ref>", "exec"), module.__dict__)
        return module.probe()
    except Exception as error:  # noqa: BLE001 — any reference fault -> discard
        raise ReferenceFault(f"{type(error).__name__}: {error}") from error
    finally:
        sys.modules.pop("revl_fuzz_ref", None)


# ==========================================================================
# types — a small closed vocabulary, represented as tuples
# ==========================================================================

BASE = ("Int", "Float", "Bool", "Str")


def type_src(t) -> str:
    if isinstance(t, str):
        return t
    head = t[0]
    if head == "List":
        return f"List[{type_src(t[1])}]"
    if head == "Opt":
        return f"Opt[{type_src(t[1])}]"
    if head == "Result":
        return f"Result[{type_src(t[1])}, {type_src(t[2])}]"
    if head == "Rec":
        return t[1]  # the declared record type name
    raise AssertionError(t)


# ==========================================================================
# expressions — a small typed AST that renders to source and shrinks uniformly
# ==========================================================================

@dataclass
class Expr:
    type: object                 # the type tuple/str this expression has
    op: str                      # discriminant (see render())
    kids: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def render(self) -> str:
        op, m, k = self.op, self.meta, self.kids
        if op == "lit":
            return m["src"]
        if op == "var":
            return m["name"]
        if op == "bin":
            return f"({k[0].render()} {m['sym']} {k[1].render()})"
        if op == "cmp":
            return f"({k[0].render()} {m['sym']} {k[1].render()})"
        if op == "logic":
            return f"({k[0].render()} {m['sym']} {k[1].render()})"
        if op == "not":
            return f"(!{k[0].render()})"
        if op == "concat":
            return f"({k[0].render()} + {k[1].render()})"
        if op == "len":
            return f"{k[0].render()}.length()"
        if op == "list":
            return "[" + ", ".join(x.render() for x in k) + "]"
        if op == "some":
            return f"Some({k[0].render()})"
        if op == "none":
            return "None"
        if op == "ok":
            return f"Ok({k[0].render()})"
        if op == "err":
            return f"Err({k[0].render()})"
        if op == "rec":
            fields = m["fields"]  # list of field names, aligned with kids
            inner = ", ".join(f"{name}: {kid.render()}"
                              for name, kid in zip(fields, k))
            return "{ " + inner + " }"
        if op == "call":
            return f"{m['fn']}(" + ", ".join(x.render() for x in k) + ")"
        if op == "match_opt":
            # k = [scrut, some_body, none_body]; binds m['var']
            return (f"match {k[0].render()} {{ "
                    f"Some({m['var']}) => {k[1].render()}, "
                    f"None => {k[2].render()} }}")
        if op == "match_res":
            return (f"match {k[0].render()} {{ "
                    f"Ok({m['ok']}) => {k[1].render()}, "
                    f"Err({m['err']}) => {k[2].render()} }}")
        raise AssertionError(op)

    def clone(self) -> "Expr":
        return Expr(self.type, self.op,
                    [kid.clone() for kid in self.kids], dict(self.meta))


# ==========================================================================
# literal rendering — turn a py reference value into a typed revl literal
# ==========================================================================

_SAFE_STR = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "


def render_literal(t, value) -> str:
    """Render *value* (a py value produced by the reference) as a revl literal
    of static type *t*. Raises ReferenceFault for anything unrepresentable, so
    the program is discarded rather than mis-asserted."""
    if t == "Int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReferenceFault(f"Int expected, got {value!r}")
        return str(value)
    if t == "Float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ReferenceFault(f"Float expected, got {value!r}")
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            raise ReferenceFault(f"non-finite float {f!r}")
        text = repr(f)
        if "." not in text and "e" not in text and "E" not in text:
            text += ".0"
        return text
    if t == "Bool":
        return "true" if value else "false"
    if t == "Str":
        if not isinstance(value, str) or any(c not in _SAFE_STR for c in value):
            raise ReferenceFault(f"unsafe Str {value!r}")
        return f'"{value}"'
    head = t[0]
    if head == "List":
        if not isinstance(value, list):
            raise ReferenceFault(f"List expected, got {value!r}")
        return "[" + ", ".join(render_literal(t[1], v) for v in value) + "]"
    if head == "Opt":
        if value is None:
            return "None"
        return f"Some({render_literal(t[1], value)})"
    if head == "Result":
        name = type(value).__name__
        payload = getattr(value, "value", None)
        if name == "Ok":
            return f"Ok({render_literal(t[1], payload)})"
        if name == "Err":
            return f"Err({render_literal(t[2], payload)})"
        raise ReferenceFault(f"Result expected, got {value!r}")
    if head == "Rec":
        # value is a dict; render fields in declared order
        fields = t[2]  # list of (field, ftype)
        if not isinstance(value, dict):
            raise ReferenceFault(f"record expected, got {value!r}")
        inner = ", ".join(f"{fn}: {render_literal(ft, value[fn])}"
                          for fn, ft in fields)
        return "{ " + inner + " }"
    raise ReferenceFault(f"unrenderable type {t!r}")


# ==========================================================================
# the generator
# ==========================================================================

@dataclass
class FuncSig:
    name: str
    params: list          # list of (name, type)
    ret: object


class Generator:
    """A bounded, type-directed generator. Every expression is built to have a
    known static type, so the program type-checks by construction most of the
    time; the frontend is still the final admissibility judge."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.records: list[tuple[str, list]] = []   # (name, [(field, base)])
        self.adts: list[tuple[str, list]] = []       # (name, [(ctor, base|None)])
        self.helpers: list[FuncSig] = []

    # -- small helpers ----------------------------------------------------
    def _int_lit(self) -> str:
        return str(self.rng.randint(-6, 9))

    def _float_lit(self) -> str:
        return f"{self.rng.randint(-4, 6)}.{self.rng.randint(0, 9)}"

    def _str_lit(self) -> str:
        n = self.rng.randint(0, 4)
        alpha = "abcdefghijklmnopqrstuvwxyz"
        return '"' + "".join(self.rng.choice(alpha) for _ in range(n)) + '"'

    def _nonzero_int_lit(self) -> str:
        return str(self.rng.choice([1, 2, 3, 4, 5, 6, 7, 8, 9]))

    def _nonzero_float_lit(self) -> str:
        return f"{self.rng.randint(1, 6)}.{self.rng.randint(1, 9)}"

    # -- renderable-type picker ------------------------------------------
    def pick_type(self, allow_containers=True, allow_rec=True):
        pool = list(BASE)
        if allow_containers:
            pool += ["ListT", "OptT", "ResultT"]
        if allow_rec and self.records:
            pool.append("RecT")
        choice = self.rng.choice(pool)
        if choice == "ListT":
            return ("List", self.rng.choice(BASE))
        if choice == "OptT":
            return ("Opt", self.rng.choice(BASE))
        if choice == "ResultT":
            return ("Result", self.rng.choice(BASE), self.rng.choice(BASE))
        if choice == "RecT":
            name, fields = self.rng.choice(self.records)
            return ("Rec", name, fields)
        return choice

    # -- expression generation -------------------------------------------
    def gen(self, t, env: dict, depth: int) -> Expr:
        """Generate an Expr of type *t* using variables in *env* (name->type)."""
        if isinstance(t, str):
            return self._gen_base(t, env, depth)
        head = t[0]
        if head == "List":
            return self._gen_list(t, env, depth)
        if head == "Opt":
            return self._gen_opt(t, env, depth)
        if head == "Result":
            return self._gen_result(t, env, depth)
        if head == "Rec":
            return self._gen_rec(t, env, depth)
        raise AssertionError(t)

    def _vars_of(self, t, env):
        return [n for n, vt in env.items() if vt == t]

    def _call_returning(self, t, env, depth):
        """Maybe build a call to a helper whose return type is *t*."""
        candidates = [h for h in self.helpers if h.ret == t]
        if not candidates:
            return None
        h = self.rng.choice(candidates)
        args = [self.gen(pt, env, depth - 1) for _, pt in h.params]
        return Expr(t, "call", args, {"fn": h.name})

    def _gen_base(self, t, env, depth) -> Expr:
        rng = self.rng
        vars_t = self._vars_of(t, env)
        atoms = []
        if t == "Int":
            atoms.append(lambda: Expr("Int", "lit", meta={"src": self._int_lit()}))
        elif t == "Float":
            atoms.append(lambda: Expr("Float", "lit", meta={"src": self._float_lit()}))
        elif t == "Bool":
            atoms.append(lambda: Expr("Bool", "lit",
                                      meta={"src": rng.choice(["true", "false"])}))
        elif t == "Str":
            atoms.append(lambda: Expr("Str", "lit", meta={"src": self._str_lit()}))
        for v in vars_t:
            atoms.append(lambda v=v: Expr(t, "var", meta={"name": v}))

        if depth <= 0:
            return rng.choice(atoms)()

        forms = list(atoms)
        call = self._call_returning(t, env, depth)
        if call is not None:
            forms.append(lambda call=call: call)

        if t == "Int":
            forms.append(lambda: Expr("Int", "bin",
                [self.gen("Int", env, depth - 1), self.gen("Int", env, depth - 1)],
                {"sym": rng.choice(["+", "-", "*"])}))
            forms.append(lambda: Expr("Int", "bin",
                [self.gen("Int", env, depth - 1),
                 Expr("Int", "lit", meta={"src": self._nonzero_int_lit()})],
                {"sym": "%"}))
            forms.append(lambda: Expr("Int", "len",
                [self.gen(("List", rng.choice(BASE)), env, depth - 1)]))
            forms.append(lambda: Expr("Int", "len",
                [self.gen("Str", env, depth - 1)]))
            forms.append(lambda: self._gen_match(t, env, depth))
        elif t == "Float":
            forms.append(lambda: Expr("Float", "bin",
                [self.gen("Float", env, depth - 1), self.gen("Float", env, depth - 1)],
                {"sym": rng.choice(["+", "-", "*"])}))
            forms.append(lambda: Expr("Float", "bin",
                [self.gen("Float", env, depth - 1),
                 Expr("Float", "lit", meta={"src": self._nonzero_float_lit()})],
                {"sym": "/"}))
        elif t == "Bool":
            forms.append(lambda: Expr("Bool", "cmp",
                [self.gen("Int", env, depth - 1), self.gen("Int", env, depth - 1)],
                {"sym": rng.choice(["<", ">", "<=", ">=", "==", "!="])}))
            forms.append(lambda: Expr("Bool", "cmp",
                [self.gen("Str", env, depth - 1), self.gen("Str", env, depth - 1)],
                {"sym": rng.choice(["==", "!="])}))
            forms.append(lambda: Expr("Bool", "logic",
                [self.gen("Bool", env, depth - 1), self.gen("Bool", env, depth - 1)],
                {"sym": rng.choice(["&&", "||"])}))
            forms.append(lambda: Expr("Bool", "not",
                [self.gen("Bool", env, depth - 1)]))
            forms.append(lambda: self._gen_match(t, env, depth))
        elif t == "Str":
            forms.append(lambda: Expr("Str", "concat",
                [self.gen("Str", env, depth - 1), self.gen("Str", env, depth - 1)]))
            forms.append(lambda: self._gen_match(t, env, depth))
        return rng.choice(forms)()

    def _gen_match(self, t, env, depth) -> Expr:
        """A match expression yielding type *t*, over an Opt or Result."""
        rng = self.rng
        if rng.random() < 0.5:
            elem = rng.choice(BASE)
            scrut = self.gen(("Opt", elem), env, depth - 1)
            var = f"m{rng.randint(0, 999)}"
            some_env = dict(env, **{var: elem})
            return Expr(t, "match_opt",
                        [scrut, self.gen(t, some_env, depth - 1),
                         self.gen(t, env, depth - 1)],
                        {"var": var})
        ok_t = rng.choice(BASE)
        err_t = rng.choice(BASE)
        scrut = self.gen(("Result", ok_t, err_t), env, depth - 1)
        okv, errv = f"o{rng.randint(0, 999)}", f"e{rng.randint(0, 999)}"
        return Expr(t, "match_res",
                    [scrut,
                     self.gen(t, dict(env, **{okv: ok_t}), depth - 1),
                     self.gen(t, dict(env, **{errv: err_t}), depth - 1)],
                    {"ok": okv, "err": errv})

    def _gen_list(self, t, env, depth) -> Expr:
        rng = self.rng
        if depth > 0 and rng.random() < 0.3:
            call = self._call_returning(t, env, depth)
            if call is not None:
                return call
        n = rng.randint(0, 3)
        elem = t[1]
        return Expr(t, "list",
                    [self.gen(elem, env, max(0, depth - 1)) for _ in range(n)])

    def _gen_opt(self, t, env, depth) -> Expr:
        rng = self.rng
        if depth > 0 and rng.random() < 0.25:
            call = self._call_returning(t, env, depth)
            if call is not None:
                return call
        if rng.random() < 0.4:
            return Expr(t, "none")
        return Expr(t, "some", [self.gen(t[1], env, max(0, depth - 1))])

    def _gen_result(self, t, env, depth) -> Expr:
        rng = self.rng
        if depth > 0 and rng.random() < 0.25:
            call = self._call_returning(t, env, depth)
            if call is not None:
                return call
        if rng.random() < 0.5:
            return Expr(t, "ok", [self.gen(t[1], env, max(0, depth - 1))])
        return Expr(t, "err", [self.gen(t[2], env, max(0, depth - 1))])

    def _gen_rec(self, t, env, depth) -> Expr:
        _, name, fields = t
        return Expr(t, "rec",
                    [self.gen(ft, env, max(0, depth - 1)) for _, ft in fields],
                    {"fields": [fn for fn, _ in fields]})

    # -- top-level program ------------------------------------------------
    def program(self) -> "Program":
        rng = self.rng
        # 0..1 record types
        n_rec = rng.randint(0, 1)
        for i in range(n_rec):
            nf = rng.randint(1, 3)
            fields = [(f"f{j}", rng.choice(BASE)) for j in range(nf)]
            self.records.append((f"Rec{i}", fields))
        # 0..2 ADTs (exercised via a dedicated match helper)
        n_adt = rng.randint(0, 2)
        for i in range(n_adt):
            nc = rng.randint(1, 3)
            ctors = []
            for j in range(nc):
                payload = rng.choice([None] + list(BASE))
                ctors.append((f"C{i}_{j}", payload))
            self.adts.append((f"Adt{i}", ctors))

        # helper functions
        n_help = rng.randint(1, 4)
        helper_srcs = []
        for i in range(n_help):
            sig, src = self._gen_helper(f"h{i}")
            self.helpers.append(sig)
            helper_srcs.append(src)

        # a helper per ADT that constructs and matches it (Bool -> Int),
        # so the ADT/match emitter paths are exercised even though ADTs are
        # not probe-return types.
        adt_srcs = [self._gen_adt_helper(name, ctors)
                    for name, ctors in self.adts]

        # the probe: a pub fn over a renderable type
        ret = self.pick_type()
        body = self.gen(ret, {}, depth=3)
        probe_src = (f"pub fn probe() -> {type_src(ret)} "
                     f"{{ return {body.render()} }}")

        return Program(records=list(self.records), adts=list(self.adts),
                       helper_srcs=helper_srcs, adt_srcs=adt_srcs,
                       probe_ret=ret, probe_body=body, probe_src=probe_src)

    def _gen_helper(self, name) -> tuple[FuncSig, str]:
        rng = self.rng
        n_params = rng.randint(0, 2)
        params = [(f"p{j}", rng.choice(BASE + (("List", rng.choice(BASE)),
                                               ("Opt", rng.choice(BASE)))))
                  for j in range(n_params)]
        ret = self.pick_type(allow_rec=bool(self.records))
        env = {pn: pt for pn, pt in params}
        body = self.gen(ret, env, depth=2)
        sig = FuncSig(name, params, ret)
        params_src = ", ".join(f"{pn}: {type_src(pt)}" for pn, pt in params)
        src = (f"fn {name}({params_src}) -> {type_src(ret)} "
               f"{{ return {body.render()} }}")
        return sig, src

    def _gen_adt_helper(self, name, ctors) -> str:
        """A Bool->Int fn that builds one ADT value and matches all variants —
        pins the ADT construction + exhaustive-match emitter paths."""
        rng = self.rng
        arms = []
        for ci, (ctor, payload) in enumerate(ctors):
            if payload is None:
                arms.append(f"{ctor} => {ci}")
            else:
                arms.append(f"{ctor}(_v) => {ci}")
        # build the first ctor as the scrutinee
        ctor0, payload0 = ctors[0]
        if payload0 is None:
            build = ctor0
        elif payload0 == "Int":
            build = f"{ctor0}({rng.randint(0, 5)})"
        elif payload0 == "Float":
            build = f"{ctor0}({rng.randint(0, 5)}.0)"
        elif payload0 == "Bool":
            build = f"{ctor0}(true)"
        else:
            build = f'{ctor0}("x")'
        return (f"fn use_{name}() -> Int {{ let s = {build}\n"
                f"  return match s {{ " + ", ".join(arms) + " } }")


# ==========================================================================
# a generated program, and its rendering / shrinking surface
# ==========================================================================

@dataclass
class Program:
    records: list
    adts: list
    helper_srcs: list
    adt_srcs: list
    probe_ret: object
    probe_body: Expr
    probe_src: str = ""

    def decls(self) -> list[str]:
        """Every top-level declaration except the probe, in source form —
        the unit the shrinker's phase 1 prunes over."""
        out = []
        for name, fields in self.records:
            inner = ", ".join(f"{fn}: {type_src(ft)}" for fn, ft in fields)
            out.append(f"type {name} = {{ {inner} }}")
        for name, ctors in self.adts:
            variants = " | ".join(
                ctor if payload is None else f"{ctor}({type_src(payload)})"
                for ctor, payload in ctors)
            out.append(f"type {name} = {variants}")
        out.extend(self.helper_srcs)
        out.extend(self.adt_srcs)
        return out

    def render(self, extra_decls: list[str] | None = None) -> str:
        decls = extra_decls if extra_decls is not None else self.decls()
        probe = (f"pub fn probe() -> {type_src(self.probe_ret)} "
                 f"{{ return {self.probe_body.render()} }}")
        return "\n".join([*decls, probe]) + "\n"


# ==========================================================================
# the oracle
# ==========================================================================

@dataclass
class Divergence:
    tier: str
    kind: str          # 'build' (compiles-implies-runs violation) or 'value'
    signature: str     # normalized root-cause fingerprint
    py_msg: str
    tier_msg: str
    tier_stdout: str
    source: str        # the assertion-augmented program that diverges


def _run_tier(tier: str, ir: dict) -> tuple[str, str, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            outcome, message = RUNNERS[tier](ir)
        except Exception as error:  # noqa: BLE001 — a crash is a tier failure
            outcome, message = "fail", f"{type(error).__name__}: {error}"
    return outcome, message, buf.getvalue().strip()


def assertion_source(program_src: str, ret, value) -> str | None:
    """Augment *program_src* with `test "probe" { assert probe() == <lit> }`.
    Returns None if the value cannot be rendered as a literal."""
    try:
        literal = render_literal(ret, value)
    except ReferenceFault:
        return None
    return program_src + f'\ntest "cross_tier_probe" {{ assert probe() == {literal} }}\n'


# The three outcomes that are NOT a divergence:
#   pass    — the tier agreed with the reference.
#   skip    — the tier's toolchain is absent (honest unavailability).
#   refusal — the tier's EMITTER refused the construct at compile time. This is
#             a DECLARED, LOUD capability boundary (e.g. wasm has no Float value
#             representation — docs/wasm-capabilities.md), tracked by the
#             conformance matrix, not a silent wrong answer. A differential
#             fuzzer must not cry "divergence" on a tier honestly saying "I do
#             not lower this construct"; it is closer to a skip than to a bug.
#
# A divergence is only:
#   build   — the emitter PRODUCED code, but the tier's toolchain could not
#             build/run it (a compiles-implies-runs violation — the 278/280
#             class), OR
#   value   — the tier built and ran, but computed a DIFFERENT value than the
#             reference (the worst kind: a silent wrong answer — the 279 class).

# Signatures that mean the emitted code did not BUILD/VALIDATE (as opposed to
# building, running, and returning a wrong value). Every runner prints "FAIL"
# on a build failure too (`go test` prints `FAIL ... [build failed]`), so the
# bare presence of "FAIL" cannot separate the two — these signatures do.
_BUILD_ERROR_SIGNS = (
    "build failed", "undefined:", "cannot use", "cannot find", "cannot resolve",
    "syntax error", "type mismatch", "mismatched types", "invalid input webassembly",
    "does not compile", "error[e", "not a type", "no field", "expected i32",
    "expected i64", "expected f64", "compile error", "javac failed", "cannot be",
    "declared and not used", "is not lowerable",
)


def classify(message: str, stdout: str) -> str:
    """Map a runner's ("fail", message) into 'refusal' / 'build' / 'value'.

    refusal — the emitter itself declined the construct (a declared boundary).
    build   — the emitter produced code the toolchain could not build/validate
              (a compiles-implies-runs violation).
    value   — the code built and ran, but an assertion disagreed on the value.
    """
    if message.startswith("emitter refused:"):
        return "refusal"
    blob = (message + "\n" + stdout).lower()
    if any(sign in blob for sign in _BUILD_ERROR_SIGNS):
        return "build"
    return "value"


def divergence_signature(message: str, stdout: str) -> str:
    """A stable fingerprint of a divergence's root cause, so distinct bugs get
    distinct fixtures instead of all collapsing onto a generic runner message
    (`go test exited 1`). Picks the first error-bearing line from the tier's
    own output and normalizes away file:line and numeric offsets."""
    import re  # noqa: PLC0415 — local to keep the module top lean
    # `--- FAIL` / bare `FAIL` header lines carry no root cause, so they are NOT
    # keys: the informative line (a `path:line:` compiler/assert message) is
    # preferred, and only if none is found does the runner message stand in.
    keys = ("undefined:", "cannot", "error", "mismatch", "invalid input",
            "expected", "assertion", "no field", "not lowerable",
            "panic", "exception", "is not", "not used", "not an", "no such")
    # a compiler-error line typically looks like `path:line:col: message` — pick
    # the first such line, or the first line hitting a keyword.
    loc = re.compile(r'\S+:\d+(:\d+)?:\s*(?P<msg>.+)')
    for line in stdout.splitlines():
        m = loc.search(line)
        text = m.group("msg") if m else (line if any(
            k in line.lower() for k in keys) else None)
        if text:
            s = re.sub(r'^FAIL\s+\S+:\s*\d*:?\s*', '', text)  # runner prefix
            s = re.sub(r'offset \d+', 'offset N', s)
            s = re.sub(r'\b\d+\b', 'N', s)
            return s.strip()[:90]
    return message.splitlines()[0][:90]


def check_tier(tier: str, source: str) -> tuple[str, str, str]:
    """Run *tier* on the assertion-augmented *source* once and classify the
    outcome into one of: 'pass', 'skip', 'refusal', 'build', 'value'. Returns
    (outcome, message, stdout). Only 'build' and 'value' are divergences."""
    try:
        ir = compile_source(source)
    except RevlError:
        # the augmented program is no longer admissible (rare) -> nothing to say
        return "skip", "augmented program no longer admissible", ""
    outcome, message, out = _run_tier(tier, ir)
    if outcome != "fail":
        return outcome, message, out          # 'pass' or 'skip'
    return classify(message, out), message, out  # 'refusal' / 'build' / 'value'


def diverges_on(tier: str, source: str) -> tuple[str, str, str] | None:
    """Return (kind, msg, stdout) for a genuine divergence (kind in
    {'build','value'}) on *tier*, else None. Used by the shrinker, where only
    the divergence predicate matters."""
    outcome, message, out = check_tier(tier, source)
    if outcome in ("build", "value"):
        return outcome, message, out
    return None


# ==========================================================================
# the shrinker — phase 1: prune declarations; phase 2: simplify the probe
# ==========================================================================

def _minimal_expr(t) -> Expr:
    """The simplest expression of type *t*: a literal (or None / empty)."""
    if t == "Int":
        return Expr("Int", "lit", meta={"src": "0"})
    if t == "Float":
        return Expr("Float", "lit", meta={"src": "0.0"})
    if t == "Bool":
        return Expr("Bool", "lit", meta={"src": "false"})
    if t == "Str":
        return Expr("Str", "lit", meta={"src": '""'})
    head = t[0]
    if head == "List":
        return Expr(t, "list", [])
    if head == "Opt":
        return Expr(t, "none")
    if head == "Result":
        return Expr(t, "err", [_minimal_expr(t[2])])
    if head == "Rec":
        _, _name, fields = t
        return Expr(t, "rec", [_minimal_expr(ft) for _, ft in fields],
                    {"fields": [fn for fn, _ in fields]})
    raise AssertionError(t)


def _walk(e: Expr, path=()):
    yield path, e
    for i, kid in enumerate(e.kids):
        yield from _walk(kid, path + (i,))


def _replace_at(root: Expr, path, new: Expr) -> Expr:
    if not path:
        return new
    root = root.clone()
    node = root
    for idx in path[:-1]:
        node = node.kids[idx]
    node.kids[path[-1]] = new
    return root


def _candidates(node: Expr):
    """Simpler same-typed expressions that could replace *node*: a same-typed
    child lifted up (dropping the surrounding operation), then the minimal
    literal for the node's type."""
    cands = []
    for kid in node.kids:
        if kid.type == node.type:
            cands.append(kid.clone())
    try:
        minimal = _minimal_expr(node.type)
        if minimal.render() != node.render():
            cands.append(minimal)
    except AssertionError:
        pass
    return cands


def _render_prog(decls: list[str], body: Expr, ret) -> str:
    probe = f"pub fn probe() -> {type_src(ret)} {{ return {body.render()} }}"
    return "\n".join([*decls, probe]) + "\n"


def shrink(program: Program, tier: str, ret, value, max_steps: int
           ) -> tuple[list[str], Expr, str]:
    """Delta-debug the program while it still diverges on *tier*, in two
    phases: (1) drop whole declarations; (2) simplify the probe expression
    toward its minimal form. Returns (surviving_decls, reduced_body,
    diverging_assertion_source).

    Every candidate is re-checked from scratch: the reference must still
    produce a value, the augmented program must still compile, and the tier
    must still diverge (build or value, never a refusal). A candidate that
    changes the reference value is fine — its literal is re-derived — so the
    reduced fixture is always self-consistent."""
    budget = [max_steps]

    def diverging_source(decls: list[str], body: Expr) -> str | None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        src = _render_prog(decls, body, ret)
        try:
            v = reference_value(src)
        except (ReferenceFault, RevlError):
            return None
        aug = assertion_source(src, ret, v)
        if aug is None:
            return None
        return aug if diverges_on(tier, aug) is not None else None

    decls = list(program.decls())
    body = program.probe_body
    current_aug = assertion_source(program.render(), ret, value) or program.render()

    # phase 1: drop one declaration at a time (a fixpoint pass). Simple and
    # robust — a divergence usually needs only the probe plus a couple of decls.
    progress = True
    while progress and budget[0] > 0:
        progress = False
        for i in range(len(decls)):
            trial = decls[:i] + decls[i + 1:]
            aug = diverging_source(trial, body)
            if aug is not None:
                decls, current_aug, progress = trial, aug, True
                break

    # phase 2: simplify the probe expression, outermost first (a fixpoint).
    progress = True
    while progress and budget[0] > 0:
        progress = False
        for path, node in list(_walk(body)):
            for candidate in _candidates(node):
                if budget[0] <= 0:
                    break
                trial_body = _replace_at(body, path, candidate)
                aug = diverging_source(decls, trial_body)
                if aug is not None:
                    body, current_aug, progress = trial_body, aug, True
                    break
            if progress:
                break

    return decls, body, current_aug


# ==========================================================================
# fixture emission
# ==========================================================================

def emit_fixture(div: Divergence, seed: int, index: int) -> Path:
    REGRESSIONS.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(div.source.encode()).hexdigest()[:8]
    stem = f"fuzz_{div.tier}_{digest}"
    rvl = REGRESSIONS / f"{stem}.rvl"
    note = REGRESSIONS / f"{stem}.md"
    rvl.write_text(div.source)
    mapping = KNOWN_ITEMS.get(div.tier, "NEW — not one of 278/279/280; please file")
    happened = ("built and ran the admitted program but computed a DIFFERENT "
                "value than the reference (a silent cross-tier value divergence)"
                if div.kind == "value" else
                "could not build/validate the code its own emitter produced for "
                "the admitted program (a compiles-implies-runs violation)")
    note.write_text(
        f"# Cross-tier divergence: `{div.tier}` disagrees with the `{REFERENCE}` reference\n\n"
        f"- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the "
        f"empirical counterpart to item 133), seed {seed}, program #{index}.\n"
        f"- Property under test: one composition has the same meaning across "
        f"tiers. The shared frontend ADMITTED this program (it compiles), the "
        f"`{REFERENCE}` reference tier ran it, and the `{div.tier}` tier {happened}.\n"
        f"- Divergence kind: **{div.kind}**\n"
        f"- Root-cause fingerprint: `{div.signature}`\n"
        f"- Maps to roadmap item: {mapping}\n\n"
        f"## Per-tier outcome\n\n"
        f"- `{REFERENCE}` (reference): pass — {div.py_msg}\n"
        f"- `{div.tier}`: fail — {div.tier_msg}\n\n"
        f"### `{div.tier}` output\n\n```\n{div.tier_stdout[:1500]}\n```\n\n"
        f"## Minimized program (`{stem}.rvl`)\n\n```revl\n{div.source}```\n")
    return rvl


# ==========================================================================
# driver
# ==========================================================================

def detect_tiers(requested: list[str] | None, slow: bool) -> tuple[list[str], dict]:
    """Probe each runner once with a trivial program. A tier that skips is
    honestly recorded as unavailable with its reason; a tier that passes is
    available. The py reference is required."""
    probe_ir = compile_source(
        'pub fn probe() -> Int { return 1 }\n'
        'test "t" { assert probe() == 1 }\n')
    default_order = ["py", "wasm", "go", "rust"]  # ts/java probed too, will skip
    order = requested or default_order + ["ts", "java"]
    available, reasons = [], {}
    for tier in order:
        if tier not in RUNNERS:
            reasons[tier] = "unknown tier"
            continue
        if tier == "rust" and not slow and (requested is None):
            reasons[tier] = "skipped by default (slow; pass --slow to include)"
            continue
        outcome, message, _ = _run_tier(tier, probe_ir)
        if outcome == "pass":
            available.append(tier)
        else:
            reasons[tier] = message
    if REFERENCE not in available:
        raise SystemExit(f"the {REFERENCE} reference tier is not runnable on "
                         f"this box: {reasons.get(REFERENCE, 'unknown')}")
    return available, reasons


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--count", type=int, default=100,
                    help="number of programs to generate")
    ap.add_argument("--tiers", type=str, default=None,
                    help="comma-separated tiers to execute (default: auto-detect)")
    ap.add_argument("--slow", action="store_true",
                    help="include the rust tier (a cargo build per program)")
    ap.add_argument("--max-shrink", type=int, default=120,
                    help="max shrink candidate evaluations per divergence")
    ap.add_argument("--no-fixtures", action="store_true",
                    help="do not write fixtures under examples/regressions/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    requested = ([t.strip() for t in args.tiers.split(",")]
                 if args.tiers else None)
    available, reasons = detect_tiers(requested, args.slow)
    others = [t for t in available if t != REFERENCE]

    def say(*a):
        if not args.quiet:
            print(*a)

    say(f"cross-tier differential fuzzer (item 292) — seed {args.seed}, "
        f"{args.count} programs")
    say(f"reference tier: {REFERENCE}")
    say(f"comparison tiers executed: {', '.join(others) if others else '(none)'}")
    for tier, why in reasons.items():
        say(f"  tier {tier}: not executed — {why}")
    say("")

    rng = random.Random(args.seed)
    generated = admitted = ran = discarded_reject = discarded_ref = 0
    # dedup divergences by (tier, kind, first line of message) so the same bug
    # is recorded once, not once per random program that hits it.
    seen: dict[tuple[str, str, str], Divergence] = {}
    # per-tier refusal accounting: a tier's emitter declining a construct is a
    # DECLARED capability boundary, not a divergence — count and summarize it
    # honestly, but do not treat it as a finding.
    refusals: dict[str, dict[str, int]] = {t: {} for t in others}

    for i in range(args.count):
        generated += 1
        gen = Generator(random.Random(rng.random()))
        try:
            prog = gen.program()
            src = prog.render()
        except Exception as error:  # noqa: BLE001 — a generator bug is not a finding
            if not args.quiet:
                print(f"  [gen error #{i}] {type(error).__name__}: {error}")
            continue
        # admissibility: the frontend is the judge.
        try:
            compile_source(src)
        except RevlError:
            discarded_reject += 1
            continue
        admitted += 1
        # reference value
        try:
            value = reference_value(src)
        except ReferenceFault:
            discarded_ref += 1
            continue
        aug = assertion_source(src, prog.probe_ret, value)
        if aug is None:
            discarded_ref += 1
            continue
        # the reference must actually pass its own assertion (else the literal
        # renderer is wrong — a harness bug, not a divergence).
        oc, _msg, _out = _run_tier(REFERENCE, compile_source(aug))
        if oc != "pass":
            discarded_ref += 1
            continue
        ran += 1
        for tier in others:
            outcome, tmsg, tout = check_tier(tier, aug)
            if outcome in ("pass", "skip"):
                continue
            if outcome == "refusal":
                # a declared capability boundary — count it, not a divergence.
                sig = tmsg.split(":", 2)[-1].strip()[:60]
                refusals[tier][sig] = refusals[tier].get(sig, 0) + 1
                continue
            kind = outcome  # 'build' or 'value'
            sig = divergence_signature(tmsg, tout)
            key = (tier, kind, sig)
            if key in seen:
                continue
            say(f"  [divergence] program #{i}: {tier} disagrees ({kind}) — {sig}")
            decls, body, reduced_aug = shrink(
                prog, tier, prog.probe_ret, value, args.max_shrink)
            # re-read the tier's output on the SHRUNK program, so the fixture
            # note reflects exactly what the minimized case does.
            _oc, red_msg, red_out = check_tier(tier, reduced_aug)
            div = Divergence(tier=tier, kind=kind,
                             signature=divergence_signature(red_msg, red_out),
                             py_msg="reference passed",
                             tier_msg=red_msg, tier_stdout=red_out,
                             source=reduced_aug)
            seen[key] = div
            say("      shrunk to:\n"
                + "\n".join("        " + ln
                            for ln in reduced_aug.strip().splitlines()))

    say("")
    say("=" * 68)
    say(f"generated:             {generated}")
    say(f"admitted (compiled):   {admitted}")
    say(f"discarded (rejected):  {discarded_reject}   (frontend refused — a language limit, not a divergence)")
    say(f"discarded (ref fault): {discarded_ref}   (py reference could not produce a clean value)")
    say(f"ran differential on:   {ran} program(s)  over tiers: {', '.join(others) or '(none)'}")
    say(f"distinct divergences:  {len(seen)}")

    # honest per-tier capability-boundary summary (refusals are not divergences)
    for tier in others:
        total = sum(refusals[tier].values())
        if total:
            say(f"  {tier}: declined {total} program(s) at a documented "
                f"capability boundary (emitter refusal, not a divergence):")
            for sig, n in sorted(refusals[tier].items(), key=lambda kv: -kv[1]):
                say(f"      {n:4}x  {sig}")

    if not seen:
        say("no cross-tier VALUE/BUILD divergence found in this batch.")
    else:
        say("")
        say("DIVERGENCES (a tier that ran but disagreed with the reference):")
        for idx, (_key, div) in enumerate(seen.items()):
            mapping = KNOWN_ITEMS.get(div.tier, "NEW — please file")
            say(f"  * {div.tier}: {div.kind} divergence  [maps to item {mapping}]")
            if not args.no_fixtures:
                path = emit_fixture(div, args.seed, idx)
                say(f"      fixture: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
