#!/usr/bin/env python3
"""Per-stage overhead benchmark for the self-hosted revl compiler (roadmap 229).

The self-host stages are *written* in revl (selfhost/*.rvl) but *run* by being
compiled to Python through the reference python backend (backends/python/emit.py)
and executed on CPython. This tool measures the **known overhead** of that
CPython-emitted self-host LOGIC against the hand-written reference stage, per
stage, over the same corpora the differential stage tests already use.

What "overhead" means here: the self-host stages are pure, functional revl that
navigates the IR / AST through value-copying and `value_*` / `@py` accessor
indirection. Emitted to CPython, that indirection is a constant tax on top of
the reference's direct Python. This is NOT a verdict on revl — it is the price
of the *CPython tier*. The meaningful future comparison is the self-host
compiler emitted to a fast NATIVE tier (rust/go) vs CPython; this file pins the
py-tier baseline so that comparison has a before.

Methodology (fair + reproducible):
  * Each self-host stage is compiled to Python exactly ONCE, in setup; only the
    RUN is timed. This isolates the self-hosted logic's overhead from the
    one-time revl->py compile cost (reported separately as a note).
  * Identical input feeds both sides; outputs are asserted equal (a correctness
    gate) before any timing, so both sides do equivalent work.
  * Warm + repeated: warmup passes are discarded, then the median of many
    whole-corpus passes is reported, plus the overhead FACTOR (self-host / ref).

Run:  python3 tools/bench_selfhost.py
"""

import importlib.util
import platform
import statistics
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.lexer import lex as reference_lex  # noqa: E402
import revl.parser as refparser  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.typecheck import infer_ast  # noqa: E402


# ----------------------------------------------------------- backend loader

def _load_module(relpath: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The exact reference python emitter the self-host mirrors (loaded by path, as
# the stage tests do, so we time the file under comparison — not a re-export).
_PYEMIT = _load_module("backends/python/emit.py", "pyemit_reference_bench")
reference_emit = _PYEMIT.emit


def compile_selfhost_stage(rvl_relpath: str):
    """Compile selfhost/<file>.rvl with revl, emit Python via the reference
    backend, exec it, and return (namespace, compile_seconds). The stage's
    component wrapper makes the emitted module ``from runtime import ...``; the
    pure functions under test never touch it, so a lazy stub suffices (exactly
    the machinery in tests/test_selfhost_*.py)."""
    t0 = time.perf_counter()
    ir = compile_files([str(ROOT / rvl_relpath)])
    assert ir["ir_version"] == 3, f"{rvl_relpath}: unexpected ir_version"
    src = _PYEMIT.emit(ir)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    prev = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(src, f"selfhost_{Path(rvl_relpath).stem}.py", "exec"), ns)
    finally:
        if had:
            sys.modules["runtime"] = prev
        else:
            del sys.modules["runtime"]
    return ns, time.perf_counter() - t0


# ----------------------------------------------------------- timing harness

def time_pass(fn, items, warmup: int, repeats: int) -> float:
    """Median wall time (seconds) of one whole-corpus pass: apply ``fn`` to
    every item once per pass, discard ``warmup`` passes, take the median of
    ``repeats`` measured passes."""
    for _ in range(warmup):
        for it in items:
            fn(it)
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        for it in items:
            fn(it)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


# =========================================================================
# stage 1 — LEXER   selfhost/lexer.rvl::lex_src  vs  src/revl/lexer.py::lex
# =========================================================================

# The lexer differential's own file corpus (proven token-identical): real
# example programs, a triple-string fixture, and — the money shot — the lexer's
# own 416-LOC source (self-application), the largest representative program.
LEXER_FILES = [
    "examples/migrator.rvl",
    "examples/pulse.rvl",
    "examples/user_cache.rvl",
    "examples/beacon.rvl",
    "examples/tenants.rvl",
    "backends/rust/scenarios/probe.rvl",
    "tests/fixtures/triple_string.rvl",
    "selfhost/lexer.rvl",
]


def _esc(s: str) -> str:
    return s.replace("%", "%%").replace("|", "%p")


def _canon_reference_tokens(tokens):
    out = []
    for t in tokens:
        if t.kind == "eof":
            out.append(("eof", "", t.line))
        elif t.kind == "int":
            out.append(("int", str(t.value), t.line))
        elif t.kind == "float":
            out.append(("float", repr(float(t.value)), t.line))
        elif t.kind == "template":
            parts = "|".join(
                ("t:" if k == "text" else "v:") + _esc(s) for k, s in t.value)
            out.append(("template", parts, t.line))
        elif t.kind == "hostbody":
            return None  # corpus file carries a host body: skip (as the test does)
        else:
            out.append((t.kind, str(t.value), t.line))
    return out


def _canon_emitted_tokens(tokens):
    return [(t["kind"],
             repr(float(t["text"])) if t["kind"] == "float" else t["text"],
             t["line"]) for t in tokens]


# =========================================================================
# stage 2 — PARSER  selfhost/parser.rvl::parse_render vs src/revl/parser.py
# =========================================================================

# The parser differential's accepted-expression corpus (proven S-expression
# identical): the full precedence / associativity / call / field / match /
# arrow / template / optional-chaining surface.
PARSER_EXPRS = [
    "2.5", "0.5", "3.0", "2.5 + 1", "2.5 * 0.5 < 2.0",
    "1", '"hi"', "true", "false", "null", "x", "config.retries",
    "1 + 2 * 3", "1 * 2 + 3", "(1 + 2) * 3", "a - b - c", "a - (b - c)",
    "a / b % c", "-x", "!ok", "!!ok", "- - 1",
    "a < b == c > d", "a <= b != c >= d", "a === b", "a !== b",
    "a && b || c", "a || b && c", "a ?? b ?? c", "(a ?? b) || c",
    "a || (b ?? c)", "a ? b : c", "a ? b : c ? d : e", "a ? b ? c : d : e",
    "f()", "f(1)", "f(1, 2)", "f(1,)", "f(g(h(1)))",
    "a.b.c", "a.b(1).c", "xs[0]", "xs[i + 1]", "f(1)[2].g",
    "a?.b", "a?.b?.c", "a?.b(1)", "a?.b(1)?.c", "a?.b()",
    "match e { }",
    "{}", "{ a: 1 }", "{ a: 1, b: 2 }", "{ a: 1, b: { c: 2 } }",
    "[]", "[1]", "[1, 2, 3]", "[[1], [2]]", "[{ a: 1 }]",
    "x => x", "x => x + 1", "() => 1", "(a) => a", "(a, b) => a + b",
    "(a: Int) => a", "(a: Int, b) => a + b", "(a: List[Str]) => a",
    "(f: (Int) -> Bool) => f", "(a: Str?) => a",
    "emit db.write(1)", "emit f(1) + 2",
    "match e { Ok(v) => v, _ => 0, }",
    "match e { Ok(v) => v, _ => 0 }",
    "match e { None => 1, Some(x) => x, }",
    "match f(1) { A(x) => x + 1, B(y) => y * 2, _ => 0, }",
    "`plain`", "`hi ${name}`", "`n=${r.count}!`", "`${a + b}`",
    "`${f(1, 2)}`", "`a${x}b${y}c`",
    "`${a || b}`", "`a|b`", "`100%`", "`%p`", "`${a || b}|${c}`",
    "`p${`inner ${x}`}q`",
    "hole", 'hole "why"', "hole[Int]", 'hole[List[Str]] "todo"',
    "a.b ?? c.d", "xs[0] ?? 1", "f(a ?? b)", "[a ?? b]", "{ k: a ?? b }",
    "(a && b) ?? c", "a ?? (b && c)",
    "x => y => x + y", "f(x => x + 1)", "[x => x, y => y]",
]


def _ref_parse(src: str):
    parser = refparser.Parser(src, "diff.rvl")
    node = parser.pure_expr()
    if not parser.at("eof"):
        raise RevlError("diff.rvl", 1, "trailing tokens")
    return node


def _seq(nodes) -> str:
    return " ".join(_render(n) for n in nodes)


def _render(n) -> str:
    P = refparser
    if isinstance(n, P.ExprLit):
        v = n.value
        if v is None:
            return "(null)"
        if v is True:
            return "(bool true)"
        if v is False:
            return "(bool false)"
        if isinstance(v, int):
            return f"(int {v})"
        if isinstance(v, float):
            return f"(float {v})"
        return f"(str {v})"
    if isinstance(n, P.ExprVar):
        return f"(var {n.name})"
    if isinstance(n, P.ExprBin):
        return f"(bin {n.op} {_render(n.left)} {_render(n.right)})"
    if isinstance(n, P.ExprUn):
        return f"(un {n.op} {_render(n.operand)})"
    if isinstance(n, P.EmitExpr):
        return f"(emit {_render(n.expr)})"
    if isinstance(n, P.ExprCall):
        return f"(call {_render(n.callee)} {_seq(n.args)})"
    if isinstance(n, P.ExprField):
        return f"(field {_render(n.target)} {n.name})"
    if isinstance(n, P.ExprOptField):
        return f"(optfield {_render(n.target)} {n.name})"
    if isinstance(n, P.ExprOptCall):
        return f"(optcall {_render(n.target)} {n.method} {_seq(n.args)})"
    if isinstance(n, P.ExprIndex):
        return f"(index {_render(n.target)} {_render(n.index)})"
    if isinstance(n, P.ExprIf):
        return f"(if {_render(n.cond)} {_render(n.then)} {_render(n.otherwise)})"
    if isinstance(n, P.ExprRecord):
        return "(rec " + " ".join(f"(f {k} {_render(v)})" for k, v in n.fields) + ")"
    if isinstance(n, P.ExprList):
        return f"(list {_seq(n.items)})"
    if isinstance(n, P.ExprArrow):
        params = " ".join(
            f"(p {name} {ty if ty else '_'})"
            for name, ty in zip(n.params, n.param_types))
        return f"(arrow {params} {_render(n.body)})"
    if isinstance(n, P.ExprMatch):
        arms = " ".join(
            f"(arm {pat} {bind if bind else '_'} {_render(body)})"
            for pat, bind, body in n.arms)
        return f"(match {_render(n.scrutinee)} {arms})"
    if isinstance(n, P.Interp):
        return "(templ " + " ".join(
            f"(t {v})" if k == "text" else f"(e {_render(v)})"
            for k, v in n.parts) + ")"
    if isinstance(n, P.ExprHole):
        return f"(hole {n.type or '_'} {n.message or '_'})"
    raise AssertionError(f"no renderer for {type(n).__name__}")


def _reference_parse_render(src: str) -> str:
    try:
        return _render(_ref_parse(src))
    except RevlError:
        return "(bad)"


# =========================================================================
# stage 3 — CHECKER  selfhost/checker.rvl::infer_expr_str vs typecheck.infer_ast
# =========================================================================

ENV = {"x": "Int", "y": "Int", "f": "Float", "s": "Str", "flag": "Bool"}

CHECKER_EXPRS = [
    "1", "0", "true", "false", "x", "y", "f", "s", "flag",
    "2.5", "0.5", "2.5 + 1", "1 + 2.5", "f / 2.5", "2.5 < f",
    "s + 2.5", "x / 0.5", "(2.5 + 0.5) * x", "q",
    "1 + 2", "x - y", "x * 2", "7 / 2", "x % 3",
    "f + 1", "1 + f", "f - f", "f * f", "f / f", "f % 2", "x / f", "x % f",
    "(1 + 2) * x", "x - y - z", "7 / 2 / 2",
    "s + s", "s + 1", "1 + s", "s + f", "s + q",
    "x < y", "x <= f", "f > 1", "f >= x", "s < s", "s <= q",
    "x == y", "x != y", "s == s", "flag == false", "flag != true",
    "q == x", "q != q", "x === y", "x !== y",
    "1 + 2 < 4", "x < y == true", "(x == y) == (flag == false)",
    "1 < 2 == s < s",
]


def _reference_infer(src: str) -> str:
    try:
        node = _ref_parse(src)
        t = infer_ast(node, dict(ENV), {}, filename="diff.rvl")
    except RevlError:
        return "refuse"
    return t if t else "?"


# =========================================================================
# stage 4 — LOWER / ADMIT  selfhost/lower.rvl::admit_src vs compile_source
# =========================================================================

# Whole programs the reference admits (from the lower differential's
# ACCEPTED_PROGRAMS): honest providers, declared emission, async parity, and
# per-realm composition. admit_src runs the full front-end gate in revl;
# compile_source (lex->parse->check->lower admission) is the reference.
LOWER_PROGRAMS = [
    """
service Cache { fn put(key: Str, value: Str) }
component HonestCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
""",
    """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}
""",
    """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) { emit db.execute(key) }
  }
}
""",
    """
service D { fn q(s: Str) -> Int }
service E { fn g(s: Str) -> Int }
component A provides db: D { provide db { fn q(s) { return 1 } } }
component B provides ev: E { provide ev { fn g(s) { return 1 } } }
""",
    """
extern emission async fn http_post(url: Str, body: Str) -> Str
  = @py { return url }
service Http { emission async fn post(url: Str, body: Str) -> Str }
component Poster provides http: Http {
  provide http { async fn post(url, body) = http_post(url, body) }
}
""",
    """
service Kv { fn get(k: Str) -> Opt[Str] }
component StoreOne provides kv: Kv {
  isolate kv in realm("tenant_a")
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(k) }
}
component StoreTwo provides kv: Kv {
  isolate kv in realm("tenant_b")
  let m = effect Map.new() undo m.drop()
  provide kv { fn get(k) = m.get(k) }
}
""",
]


def _reference_admit(src: str) -> str:
    try:
        compile_source(src, "diff.rvl")
        return ""
    except RevlError as e:
        return e.code or "REFUSE"


# =========================================================================
# stage 5 — EMIT_PY  selfhost/emit_py.rvl::emit_src(ir) vs backends/python/emit.py::emit(ir)
#   THE key number: item 195's state-threading (render-context) tax lives here.
# =========================================================================

EMIT_CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
EMIT_CORPUS = [
    "arith.rvl", "strings.rvl", "control.rvl", "records.rvl", "optionals.rvl",
    "mixed.rvl", "services_basic.rvl", "services_timers.rvl",
    "services_methods.rvl", "services_body.rvl", "types.rvl", "result.rvl",
    "floats.rvl", "hostroots.rvl", "externs.rvl", "services_config.rvl",
    "services_method_effects.rvl",
]


# ----------------------------------------------------------- driver

def _fmt(ms: float) -> str:
    return f"{ms:8.3f}"


def main() -> int:
    print("=" * 74)
    print("revl self-host compiler — per-stage overhead vs the reference (CPython)")
    print("=" * 74)
    print(f"machine : {platform.platform()}")
    print(f"python  : {platform.python_implementation()} "
          f"{platform.python_version()} ({platform.processor() or 'cpu'})")
    print(f"HEAD    : {_git_head()}")
    print()
    print("Each self-host stage is compiled to Python ONCE (setup); only the RUN")
    print("is timed. Reported: median whole-corpus pass (ms), over warmup-discarded")
    print("repeated passes, and the overhead FACTOR = self-host / reference.")
    print()

    # ---- compile every self-host stage once; record the one-time cost --------
    print("compiling self-host stages (one-time revl->py cost, reported as a note)...")
    lexer_ns, c_lex = compile_selfhost_stage("selfhost/lexer.rvl")
    parser_ns, c_par = compile_selfhost_stage("selfhost/parser.rvl")
    checker_ns, c_chk = compile_selfhost_stage("selfhost/checker.rvl")
    lower_ns, c_low = compile_selfhost_stage("selfhost/lower.rvl")
    emit_ns, c_emit = compile_selfhost_stage("selfhost/emit_py.rvl")
    print("  done.\n")

    sh_lex = lexer_ns["lex_src"]
    sh_parse = parser_ns["parse_render"]
    sh_infer = checker_ns["infer_expr_str"]
    sh_admit = lower_ns["admit_src"]
    sh_emit = emit_ns["emit_src"]

    # ---- load corpora -------------------------------------------------------
    lexer_items = [(rel, (ROOT / rel).read_text(encoding="utf-8"))
                   for rel in LEXER_FILES]
    lexer_srcs = [src for _, src in lexer_items]
    lexer_loc = sum(src.count("\n") + 1 for src in lexer_srcs)

    emit_irs = [compile_files([str(EMIT_CORPUS_DIR / rel)]) for rel in EMIT_CORPUS]
    emit_loc = sum((EMIT_CORPUS_DIR / rel).read_text().count("\n") + 1
                   for rel in EMIT_CORPUS)

    # ---- correctness gates (assert equal output BEFORE timing) --------------
    print("correctness gate: self-host output == reference output ...")
    _gate_lexer(sh_lex, lexer_items)
    _gate_parser(sh_parse)
    _gate_checker(sh_infer)
    _gate_lower(sh_admit)
    _gate_emit(sh_emit, emit_irs)
    print("  all stages agree with the reference on every corpus item.\n")

    # ---- benchmark ----------------------------------------------------------
    rows = []

    # lexer: heavy per-pass work (8 files incl. 416-LOC self-source) -> fewer reps
    ref = time_pass(lambda s: reference_lex(s, "b.rvl"), lexer_srcs, 3, 15) * 1e3
    sh = time_pass(sh_lex, lexer_srcs, 3, 15) * 1e3
    rows.append(("lexer", f"{len(lexer_items)} files / {lexer_loc} LOC", ref, sh))

    ref = time_pass(_reference_parse_render, PARSER_EXPRS, 5, 25) * 1e3
    sh = time_pass(sh_parse, PARSER_EXPRS, 5, 25) * 1e3
    rows.append(("parser", f"{len(PARSER_EXPRS)} exprs", ref, sh))

    ref = time_pass(_reference_infer, CHECKER_EXPRS, 5, 25) * 1e3
    sh = time_pass(sh_infer, CHECKER_EXPRS, 5, 25) * 1e3
    rows.append(("checker", f"{len(CHECKER_EXPRS)} exprs", ref, sh))

    ref = time_pass(_reference_admit, LOWER_PROGRAMS, 3, 20) * 1e3
    sh = time_pass(sh_admit, LOWER_PROGRAMS, 3, 20) * 1e3
    rows.append(("lower/admit", f"{len(LOWER_PROGRAMS)} programs", ref, sh))

    ref = time_pass(reference_emit, emit_irs, 5, 25) * 1e3
    sh = time_pass(sh_emit, emit_irs, 5, 25) * 1e3
    rows.append(("emit_py", f"{len(emit_irs)} IR docs / {emit_loc} LOC", ref, sh))

    # ---- table --------------------------------------------------------------
    print("=" * 74)
    print(f"{'stage':<12}{'corpus':<26}{'ref ms':>9}{'self ms':>10}{'  factor':>9}")
    print("-" * 74)
    tot_ref = tot_sh = 0.0
    for name, corpus, ref, sh in rows:
        factor = sh / ref if ref else float("inf")
        tot_ref += ref
        tot_sh += sh
        print(f"{name:<12}{corpus:<26}{_fmt(ref)}{_fmt(sh)}{factor:8.1f}x")
    print("-" * 74)
    tot_factor = tot_sh / tot_ref if tot_ref else float("inf")
    print(f"{'TOTAL':<12}{'(sum of stage medians)':<26}"
          f"{_fmt(tot_ref)}{_fmt(tot_sh)}{tot_factor:8.1f}x")
    print("=" * 74)

    heaviest = max(rows, key=lambda r: r[3] / r[2] if r[2] else 0)
    print(f"\nheaviest overhead : {heaviest[0]} "
          f"({(heaviest[3] / heaviest[2]):.1f}x)")
    emit_row = next(r for r in rows if r[0] == "emit_py")
    print(f"emit_py (pre-195) : {(emit_row[3] / emit_row[2]):.1f}x  "
          "<- the number item 195's render-context change targets")
    print()
    print("one-time revl->py compile cost per stage (setup only, NOT in the run):")
    for name, sec in [("lexer", c_lex), ("parser", c_par), ("checker", c_chk),
                      ("lower", c_low), ("emit_py", c_emit)]:
        print(f"  {name:<10}{sec * 1e3:8.1f} ms")
    print()
    print("Framing: this is the KNOWN OVERHEAD of the CPython-emitted self-host")
    print("(functional style + value-copying + accessor indirection), NOT a")
    print("verdict. The meaningful future comparison is the self-host compiler")
    print("emitted to a fast NATIVE tier (rust/go) vs this CPython baseline.")
    return 0


def _git_head() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            text=True).strip()
    except Exception:
        return "unknown"


# ----------------------------------------------------------- correctness gates

def _gate_lexer(sh_lex, lexer_items):
    for rel, src in lexer_items:
        want = _canon_reference_tokens(reference_lex(src, rel))
        if want is None:
            continue
        got = _canon_emitted_tokens(sh_lex(src))
        assert "error" not in {k for k, _, _ in got}, f"lexer error on {rel}"
        assert got == want, f"lexer diverged on {rel}"


def _gate_parser(sh_parse):
    for src in PARSER_EXPRS:
        want = _reference_parse_render(src)
        got = sh_parse(src)
        assert got == want, f"parser diverged on {src!r}: {got!r} != {want!r}"


def _gate_checker(sh_infer):
    for src in CHECKER_EXPRS:
        want = _reference_infer(src)
        got = sh_infer(src)
        assert got == want, f"checker diverged on {src!r}: {got!r} != {want!r}"


def _gate_lower(sh_admit):
    for src in LOWER_PROGRAMS:
        want = _reference_admit(src)
        got = sh_admit(src)
        got_tag = got.split("|", 1)[0] if got else ""
        assert (want == "") == (got == ""), \
            f"lower diverged (admit verdict): ref={want!r} self={got!r}"


def _gate_emit(sh_emit, emit_irs):
    for ir in emit_irs:
        want = reference_emit(ir)
        got = sh_emit(ir)
        assert got == want, "emit_py diverged: self-host output != reference (bytes)"


if __name__ == "__main__":
    raise SystemExit(main())
