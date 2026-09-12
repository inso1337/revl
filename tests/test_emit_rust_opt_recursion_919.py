"""Rust boxing of INLINE-payload recursive types (issue 919).

`type Node = { val: Str, next: Opt[Node] }` is an infinite-size Rust struct:
`Option<T>` stores its payload INLINE, so `Option<Node>` is exactly as infinite
as a bare `Node` field, and `cargo` rejects the emitted crate with E0072
("recursive type has infinite size") plus E0391 ("cycle detected when computing
type of `Node`").

The boxing pass (``_recursive_boxed_edges``) used to count a containment edge
only for a *bare* user-type name — a KEY of the IR's ``types`` map — on the
reasoning that any generic wrapper is indirection. That is true of `Vec` and
`HashMap`, whose payload lives on the heap, but NOT of `Option`/`Result`. So the
pass saw no cycle here, boxed nothing, and emitted an uncompilable crate.

Fixing the edge computation alone is not enough, and that is the half of this
issue worth a dedicated test. A recursive struct-field edge is declared
`Box<T>`, and Rust does NOT auto-deref a `Box` field in value position:
`fn tail(n: Node) -> Option<Node> { return n.next }` with
`next: Box<Option<Node>>` is an E0308 ("expected `Option<Node>`, found
`Box<Option<Node>>`"). Every read of a boxed field therefore has to unbox
(``_boxed_field_read``). A boxed ENUM payload needs no such read-site change —
the match arm already unboxes with `let {b} = *{b};` — which is why the pass
prefers an enum edge and why this latent read-site defect only surfaced once
`Opt`/`Result` fields started getting boxed.

These tests live here rather than in ``tests/fixtures/emit_rust_corpus/`` on
purpose: the corpus is cross-checked byte-for-byte against the self-hosted
emitter (``tests/test_selfhost_emit_rust.py``), and ``selfhost/emit_rust.rvl``
has no boxing pass at all, so a recursive fixture in the corpus would fail the
byte-compare for a reason unrelated to this fix.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402


def _load_reference_emit():
    spec = importlib.util.spec_from_file_location(
        "rsemit_opt919", ROOT / "backends" / "rust" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OPT_RECORD = """
type Node = { val: Str, next: Opt[Node] }

fn seed(v: Str) -> Node {
  return { val: v, next: None }
}

fn tail(n: Node) -> Opt[Node] {
  return n.next
}
"""

RESULT_RECORD = """
type RNode = { val: Str, r: Result[Str, RNode] }

fn wrap(v: Str) -> RNode {
  return { val: v, r: Ok("leaf") }
}

fn unwrap_(n: RNode) -> Result[Str, RNode] {
  return n.r
}
"""

BARE_CYCLE = """
type A = { b: B, tag: Str }
type B = { a: A, tag: Str }

fn down(x: A) -> B {
  return x.b
}

fn up(y: B) -> A {
  return y.a
}
"""

# A non-recursive type that mentions `Vec`/`HashMap`/`Option` of a USER type.
# `Vec`/`HashMap` are heap-backed, so nothing here may be boxed; this is the
# over-boxing guard for the widened edge computation.
HEAP_BACKED = """
type Point = { x: Int, y: Int }
type Cloud = {
  points: List[Point],
  named: Map[Int, Point],
  origin: Opt[Point],
  tag: Str,
}

fn origin(c: Cloud) -> Opt[Point] {
  return c.origin
}

fn points(c: Cloud) -> List[Point] {
  return c.points
}

fn tag(c: Cloud) -> Str {
  return c.tag
}
"""

# A recursive ENUM, the shape the pass already handled, kept as the
# enum-edge-preferred baseline.
RECURSIVE_ENUM = """
type Tree = Leaf | Branch(TreeN)
type TreeN = { left: Tree, right: Tree }

fn size(t: Tree) -> Int {
  return match t {
    Leaf => 0,
    Branch(b) => size(b.left) + size(b.right),
  }
}
"""


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "prog.rvl"
    path.write_text(source, encoding="utf-8")
    return path


def _emit(tmp_path: Path, source: str) -> str:
    return _load_reference_emit().emit(compile_files([str(_write(tmp_path, source))]))


def test_opt_field_cycle_is_boxed(tmp_path):
    """`Opt[Node]` stores its payload inline, so it is a real containment edge:
    the declaration must be `Box<Option<Node>>` or the crate is E0072."""
    src = _emit(tmp_path, OPT_RECORD)
    assert "next: Box<Option<Node>>" in src
    assert "next: Option<Node>" not in src


def test_opt_field_read_unboxes(tmp_path):
    """The read site must unbox: Rust does not auto-deref a `Box` field in
    value position, so a bare `n.next` is an E0308."""
    src = _emit(tmp_path, OPT_RECORD)
    assert "return (*n.next);" in src
    assert "return n.next;" not in src


def test_opt_field_construction_wraps(tmp_path):
    src = _emit(tmp_path, OPT_RECORD)
    assert "Box::new(None)" in src


def test_result_field_cycle_is_boxed(tmp_path):
    """`Result` is the other inline-payload wrapper; `Result<Str, RNode>` is
    likewise infinite without a box."""
    src = _emit(tmp_path, RESULT_RECORD)
    assert "r: Box<Result<String, RNode>>" in src
    assert "return (*n.r);" in src


def test_bare_cycle_field_is_boxed_and_derefed(tmp_path):
    """The pre-existing latent half: a BARE `A`/`B` cycle already boxed a field,
    but the read emitted `a.b` (a `Box<B>`) — an E0308 in value position."""
    src = _emit(tmp_path, BARE_CYCLE)
    assert re.search(r"b: Box<[AB]>", src), src
    assert re.search(r"return \(\*x\.\w+\)", src), src


def test_heap_backed_containers_are_not_boxed(tmp_path):
    """`Vec`/`HashMap` payloads live on the heap, so they already break a cycle
    and must stay unboxed — the widened edge computation must not over-box."""
    src = _emit(tmp_path, HEAP_BACKED)
    assert "points: Vec<Point>" in src
    assert "named: std::collections::HashMap<i64, Point>" in src
    assert "origin: Option<Point>" in src
    assert "Box<" not in src
    # and the reads are plain field accesses, with no deref
    assert "return c.points;" in src
    assert "return c.origin;" in src
    assert "return c.tag;" in src


def test_recursive_enum_still_prefers_the_payload_edge(tmp_path):
    """The enum payload is the cheaper edge (the match arm unboxes it), so the
    pass must keep choosing it over a struct field — the struct stays unboxed."""
    src = _emit(tmp_path, RECURSIVE_ENUM)
    assert "Branch(Box<TreeN>)" in src
    assert "left: Tree" in src
    assert "right: Tree" in src
    assert "let b = *b;" in src


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
@pytest.mark.parametrize("label,source", [
    ("opt", OPT_RECORD),
    ("result", RESULT_RECORD),
    ("bare", BARE_CYCLE),
    ("heap", HEAP_BACKED),
    ("enum", RECURSIVE_ENUM),
])
def test_recursive_types_compile(label, source, tmp_path):
    """End-to-end: the emitted crate must actually `cargo build`."""
    reference = _load_reference_emit()
    src = reference.emit(compile_files([str(_write(tmp_path, source))]))
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(reference.cargo_toml("revl_opt919"),
                                      encoding="utf-8")
    (crate / "src" / "lib.rs").write_text(src, encoding="utf-8")
    result = subprocess.run(["cargo", "build", "--quiet"], cwd=crate,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
