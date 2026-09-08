"""Issue #116 / roadmap item 437 — the two rust codegen-performance findings
that are CLOSED as accepted-with-measured-reason rather than fixed.

The audit at `bench/codegen/rust/` measured what `backends/rust/emit.py` emits
against the rust a developer writes by hand. Its wins landed as fixes:
(a) the `concat`/`+` self-append, (b) the borrowed-literal slots, (c) the
`indexOf` prelude, (d) the index-read clones (receiver/operand on main, the
field-off-an-index subset in #648, the `&str` argument slot in #739), and
(f) the dead-after-loop `for` iterable. Findings (e) and (g) are the remainder,
and they are DELIBERATELY not fixed. This file makes that decision executable:
each is asserted at its current, accepted value, load-independently, so a later
change that alters the shape flips THIS test on purpose rather than silently.

  * (e) THE CONSTANT-LIST REBUILD. A param-less fn whose body is a constant list
    of string literals (`selfhost/lexer.rvl`'s `fn keywords() -> List[Str]`,
    consulted per identifier token, and thirteen more across the self-host
    stages) emits a fresh `Vec` of 35 `String::from` on every call. Measured by
    `bench/codegen/rust/run.py const_list`: 720,000 heap allocations and
    20,360,000 bytes for 20,000 lookups, against ZERO and zero for the
    `static KEYWORDS: &[&str]` a rust developer writes. The win is real; the FIX
    is not safely available as a local emitter transform. revl `List` lowers to
    an OWNED `Vec<String>` (no Rc/persistent backing at the rust level), so no
    clone-based or memoized `Vec<String>` return recovers the allocations — the
    only zero-allocation form returns `&'static [&'static str]`, which changes
    the element type from `String` to `&'static str` and therefore the calling
    convention at every site the result flows to. The frontend assigns the
    result the type `List[Str]` (= `Vec<String>`), so any owned or unifying use
    (`let k = keywords()`, a `List[Str]` argument/return, a persistent `push`)
    stops compiling under the borrow representation. That is precisely the
    class of calling-convention change item 277 recorded REGRESSING the
    self-host lexer by 28%, and item 437 defers (e) on exactly that ground.
    Accepted, not fixed.

  * (g) `Str.length()` IN A `while` CONDITION. `while (i < s.revl_length())`
    recomputes the codepoint count (`chars().count()`, an O(n) walk) every
    iteration, so the loop is O(n^2) where a developer hoists the invariant
    once. ANALYTIC ONLY: `bench/codegen/rust/run.py loop_length` reports ZERO
    heap allocations on both the emitted and the hand-written side — there is no
    allocation evidence and no wall clock was taken, so it is a complexity
    argument, not a measurement, and item 437 labels it so and ranks it last.
    It is also small in practice (the census finds only 3 `Str`-receiver sites;
    the other `revl_length` calls are on `Vec`, where it is `self.len()`, O(1)).
    Closed as a no-op — the shape is on file here rather than fixed.

These assertions turn on generated TEXT and on allocation properties, both of
which read the same on an idle host and on a loaded one (item 437's own
discipline: no wall clock is an input to any check here).
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "codegen" / "rust"
PROGRAMS = BENCH / "programs"
HANDWRITTEN = BENCH / "handwritten"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _load_emitter():
    """Load `backends/rust/emit.py` by path, as the backend's own tests and the
    bench harness do."""
    path = ROOT / "backends" / "rust" / "emit.py"
    spec = importlib.util.spec_from_file_location("rustemit_116", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EMITTED: dict[str, str] = {}


def _emit(rel: str) -> str:
    if rel not in _EMITTED:
        from revl import compile_files

        emitter = _load_emitter()
        _EMITTED[rel] = emitter.emit(compile_files([str(PROGRAMS / f"{rel}.rvl")]))
    return _EMITTED[rel]


def _fn_body(src: str, name: str) -> str:
    """The emitted body of `fn <name>` (with or without `pub`), brace-matched."""
    m = re.search(rf"\bfn {re.escape(name)}\(", src)
    assert m, f"no `fn {name}(` in emitted source"
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces in `fn {name}`")


def _module_body(src: str) -> str:
    """Emitted module WITHOUT the fixed helper-trait prelude, so a shape is
    counted where the program put it, not once per module (mirrors census.py)."""
    marker = "trait RevlStrOps"
    return src[: src.index(marker)] if marker in src else src


# --------------------------------------------------------------------------
# The bench harness itself is the standing measurement. Guard that the two
# programs and their hand-written comparators — the zero-allocation targets the
# findings are measured against — stay present, so a delete reds here.

def test_bench_programs_and_comparators_present():
    for name in ("const_list", "loop_length"):
        assert (PROGRAMS / f"{name}.rvl").exists(), f"missing bench program {name}"
        assert (HANDWRITTEN / f"{name}.rs").exists(), f"missing comparator {name}"

    # The comparators encode the hand-written zero/linear targets: the static
    # keyword table for (e), the hoisted invariant length for (g).
    const_hand = (HANDWRITTEN / "const_list.rs").read_text(encoding="utf-8")
    assert "static KEYWORDS: &[&str]" in const_hand
    assert "&'static [&'static str]" in const_hand
    loop_hand = (HANDWRITTEN / "loop_length.rs").read_text(encoding="utf-8")
    assert "s.chars().count()" in loop_hand  # computed ONCE, above the loop


# --------------------------------------------------------------------------
# (e) — the constant-list rebuild is ACCEPTED, so the emitter still rebuilds it.

def test_toward_116_e_const_list_still_rebuilt_per_call():
    """`keywords()` emits a fresh `Vec` of 35 `String::from(..)` every call, and
    NOT the `static KEYWORDS: &[&str]` / `&'static [&'static str]` borrow that
    would zero the allocations. Accepted, not fixed — closing it is the item-277
    calling-convention change (see the module docstring). If a later change
    hoists the table, this assertion flips and the decision is revisited on
    purpose."""
    src = _emit("const_list")
    body = _fn_body(src, "keywords")

    assert "vec![String::from(" in body, body
    assert body.count("String::from(") == 35, body

    # The zero-allocation borrow form is deliberately ABSENT.
    assert "&'static str" not in body, body
    assert "static KEYWORDS" not in src, src
    # keywords still returns an owned Vec<String>, not a borrowed slice.
    assert re.search(r"\bfn keywords\(\) -> Vec<String>", src), src
    assert "-> &'static [&'static str]" not in src, src


def test_toward_116_e_rebuild_footprint_is_real_in_the_selfhost_stages():
    """The waste is not hypothetical: param-less fns whose body is a constant
    `Vec<String>` of literals appear across the self-host stages the rust tier
    builds. Pinned as > 0 so the accepted finding keeps a live footprint; a
    fix would drive it to zero and flip this test."""
    from revl import compile_files

    emitter = _load_emitter()
    stages = [
        "selfhost/lexer.rvl",
        "selfhost/parser.rvl",
        "selfhost/checker.rvl",
        "selfhost/lower.rvl",
    ]
    rebuild = re.compile(r"fn \w+\(\) -> Vec<String> \{\s*\n\s*return vec!\[String::from\(")
    total = 0
    for rel in stages:
        body = _module_body(emitter.emit(compile_files([str(ROOT / rel)])))
        total += len(rebuild.findall(body))
    assert total > 0, "expected the const-list rebuild shape in the self-host stages"


# --------------------------------------------------------------------------
# (g) — `Str.length()` in a `while` is recomputed per iteration. ANALYTIC ONLY.

def test_toward_116_g_loop_length_recomputed_per_iteration():
    """The `while` condition still calls `.revl_length()` (an O(n)
    `chars().count()` on a `Str`) every iteration, and the invariant is NOT
    hoisted to a `let` above the loop the way the hand-written comparator does.
    This is the O(n^2)-vs-O(n) complexity note, and it carries ZERO allocation
    evidence, so it is closed as a documented no-op rather than fixed."""
    src = _emit("loop_length")
    body = _fn_body(src, "loop_length")

    # the length is recomputed inside the `while` condition ...
    m = re.search(r"while \([^\n]*\.revl_length\(\)\)", body)
    assert m, body
    # ... and is NOT pre-hoisted into a loop-invariant `let` before the `while`.
    before = body[: body.index("while (")]
    assert "revl_length()" not in before, body
