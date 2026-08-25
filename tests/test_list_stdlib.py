"""The stdlib LIST module (roadmap item 194, docs/stdlib-list.md,
stdlib/list.rvl).

The set-shaped list utilities a self-hosted emitter needs and the base
List/Str stdlib does not ship: `list_contains` (the `str_in` replacement),
`list_sort` (the `sorted(uses)` replacement — ascending lexicographic by
Unicode code point), and `list_dedup` (set-from-list). A Path B enabler kin to
stdlib/value.rvl (180/188) and stdlib/str.rvl (193).

Unlike value.rvl these are PURE revl (built on the base List/Str surface, no
`@py`), so there is nothing to defer per tier — the module runs on every
backend. The py tier is executed here; the crux is `list_sort` matching
Python's `sorted()` BYTE-FOR-BYTE (codepoint order, not alphabetical), pinned
by a fuzz over mixed-case / prefix / empty / punctuation inputs.

Checked here:
  * the module imports through `use` and the functions reach the IR;
  * the module file is the documented surface;
  * the py tier EXECUTES a pure-revl program using ONLY the kit;
  * `list_sort` == `sorted()` byte-for-byte (fixed cases + fuzz);
  * `list_contains` present/absent/empty; `list_dedup` order + edges.
"""

import importlib.util
import random
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

STDLIB = ROOT / "stdlib" / "list.rvl"

#: a self-hosted-emitter-shaped consumer: the two set operations the reference
#: emitters reach for — `x in set` (list_contains) and `sorted(set)`
#: (list_sort(list_dedup(...))) — in PURE revl, using ONLY the kit.
CONSUMER = """\
use "stdlib/list.rvl" { str_lt, list_contains, list_sort, list_dedup }

// membership — the `str_in` replacement
fn has(xs: List[Str], x: Str) -> Bool { return list_contains(xs, x) }

// `sorted(set(xs))` — the emitter's `sorted(uses)` shape, in pure revl
fn sorted_set(xs: List[Str]) -> List[Str] { return list_sort(list_dedup(xs)) }

// join so the test can compare rendered order as one string
fn sort_join(xs: List[Str]) -> Str { return list_sort(xs).join("|") }
fn dedup_join(xs: List[Str]) -> Str { return list_dedup(xs).join("|") }
fn sorted_set_join(xs: List[Str]) -> Str { return sorted_set(xs).join("|") }

// the codepoint comparator, exposed for a direct pin
fn lt(a: Str, b: Str) -> Bool { return str_lt(a, b) }
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    # the module resolves relative to the importing file, so the stdlib file
    # sits beside the consumer fixture (its repo content is pinned by
    # test_module_file_is_the_documented_surface)
    d = tmp_path_factory.mktemp("list_consumer")
    (d / "stdlib").mkdir()
    (d / "stdlib" / "list.rvl").write_text(STDLIB.read_text(encoding="utf-8"),
                                           encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_functions_reach_the_ir(consumer_ir):
    names = {f["name"] for f in consumer_ir["functions"]}
    assert {"str_lt", "list_contains", "list_sort", "list_dedup"} <= names
    # PURE revl: no @py externs are introduced by the kit
    assert consumer_ir.get("externs", []) == []


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub fn list_contains(xs: List[Str], x: Str) -> Bool" in text
    assert "pub fn list_sort(xs: List[Str]) -> List[Str]" in text
    assert "pub fn list_dedup(xs: List[Str]) -> List[Str]" in text
    assert "pub fn str_lt(a: Str, b: Str) -> Bool" in text


# ---------------------------------------------------------------- py tier

def _exec_python(ir: dict):
    spec = importlib.util.spec_from_file_location(
        "pyemit_list", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "list.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns(consumer_ir):
    return _exec_python(consumer_ir)


# ---- list_contains: present / absent / empty --------------------------------

def test_contains_present(ns):
    assert ns["has"](["a", "b", "c"], "b") is True
    assert ns["has"](["a", "b", "c"], "a") is True  # first
    assert ns["has"](["a", "b", "c"], "c") is True  # last


def test_contains_absent(ns):
    assert ns["has"](["a", "b", "c"], "z") is False
    assert ns["has"](["ab", "cd"], "a") is False  # not a substring match


def test_contains_empty(ns):
    assert ns["has"]([], "a") is False


# ---- list_sort: BYTE-FOR-BYTE vs Python sorted() ----------------------------

# fixed cases, each pinning a property; the crux is codepoint (not alphabetical)
# order — `sorted()` puts every capital before every lower-case letter.
SORT_CASES = [
    [],                                      # empty
    ["x"],                                   # single
    ["b", "a"],                              # basic
    ["b", "A", "a", "B"],                    # MIXED CASE -> A,B,a,b not a,A,b,B
    ["Frame", "Job", "Map", "Pool",
     "schedule_after", "schedule_every"],    # the emit_py `uses` vocabulary
    ["schedule_every", "Pool", "Map",
     "Job", "schedule_after", "Frame"],      # …shuffled -> same canonical order
    ["a", "a", "b", "a"],                    # duplicates preserved
    ["", "a", "", "b"],                      # empty strings sort first
    ["aa", "a", "ab", "a"],                  # shared prefix: shorter is less
    ["Z", "a", "9", "_", "1", "~", "!"],     # punctuation/digit codepoints
]


@pytest.mark.parametrize("case", SORT_CASES, ids=lambda c: repr(c)[:40])
def test_sort_matches_python_sorted_byte_for_byte(ns, case):
    assert ns["sort_join"](case) == "|".join(sorted(case))
    # and as a real list, not just the joined string
    # (join is only the transport; the list order is what matters)


def test_sort_mixed_case_is_codepoint_not_alphabetical(ns):
    # the one that separates codepoint order (A<a, i.e. 65<97) from a
    # case-insensitive/alphabetical sort
    assert ns["sort_join"](["b", "A", "a", "B"]) == "A|B|a|b"


def test_sort_fuzz_matches_sorted(ns):
    alphabet = ["a", "A", "b", "B", "z", "Z", "1", "9", "_", "!", "~",
                "", "aa", "ab", "Ab", "aB", "ba", "A1", "a1"]
    rnd = random.Random(1994)
    ls = ns["sort_join"]
    for _ in range(3000):
        xs = [rnd.choice(alphabet) for _ in range(rnd.randint(0, 8))]
        assert ls(xs) == "|".join(sorted(xs)), xs


def test_str_lt_is_codepoint_lexicographic(ns):
    lt = ns["lt"]
    # matches Python's str `<` exactly
    pairs = [("A", "a"), ("a", "b"), ("a", "aa"), ("aa", "ab"),
             ("", "a"), ("Z", "a"), ("Job", "Map"), ("Pool", "schedule_after")]
    for a, b in pairs:
        assert lt(a, b) == (a < b), (a, b)
        assert lt(b, a) == (b < a), (b, a)
        assert lt(a, a) is False


# ---- list_dedup: order + edges ----------------------------------------------

def test_dedup_keeps_first_occurrence_order(ns):
    assert ns["dedup_join"](["b", "a", "b", "c", "a"]) == "b|a|c"


def test_dedup_edges(ns):
    assert ns["dedup_join"]([]) == ""
    assert ns["dedup_join"](["x"]) == "x"
    assert ns["dedup_join"](["x", "x", "x"]) == "x"


def test_sorted_set_is_sorted_unique(ns):
    # the emitter's `sorted(set(uses))` shape: dedup then codepoint sort
    xs = ["Pool", "Map", "Pool", "Frame", "Map", "Job"]
    assert ns["sorted_set_join"](xs) == "|".join(sorted(set(xs)))
