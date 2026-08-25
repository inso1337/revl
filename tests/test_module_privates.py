"""Module-private namespacing (roadmap 228, CAPSTONE SEAM 2).

A `use`d module's PRIVATE (non-`pub`) top-level declarations do not enter the
importer's merged namespace: only `pub` names are visible and mergeable. Two
modules that each define a private `Ctx` (or `contains`, or `rstrip`) therefore
co-compile into one composition, while a genuine duplicate of a `pub` name is
still refused. This is what lets the self-host `lower`/`emit_py`/`emit_rust`
stages co-compile (item 224) and resolves the item-201/206 duplicate-name
friction.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402


def _fn_names(ir):
    return sorted(fn["name"] for fn in ir["functions"])


def test_same_named_private_fns_across_modules_co_compile(tmp_path):
    """Each module keeps a private `contains`; the merged program renames the
    privates apart instead of false-colliding on the bare name (the
    `emit_py`+`emit_rust` `contains` interference, item 224)."""
    (tmp_path / "a.rvl").write_text(
        "fn contains(x: Int) -> Bool { return x > 0 }\n"
        "pub fn run_a(x: Int) -> Bool { return contains(x) }\n"
    )
    (tmp_path / "b.rvl").write_text(
        "fn contains(s: Str) -> Bool { return true }\n"
        "pub fn run_b(s: Str) -> Bool { return contains(s) }\n"
    )
    (tmp_path / "root.rvl").write_text(
        'use "./a.rvl" { run_a }\n'
        'use "./b.rvl" { run_b }\n'
        "fn contains(z: Bool) -> Bool { return z }\n"
        "pub fn go() -> Bool { return run_a(1) && run_b(\"x\") && contains(true) }\n"
    )
    ir = compile_files([str(tmp_path / "root.rvl")])
    names = _fn_names(ir)
    # the three `pub` names stay bare; the three private `contains` are
    # renamed apart, so nothing collides in the one merged program.
    assert {"run_a", "run_b", "go"} <= set(names)
    privates = [n for n in names if n.startswith("contains")]
    assert len(privates) == 3, names
    assert "contains" not in privates or privates.count("contains") <= 1
    assert len(set(privates)) == 3, "private `contains` names must be distinct"


def test_same_named_private_types_across_modules_co_compile(tmp_path):
    """The item-224 `lower`+`emit_rust` private-`Ctx` collision: two modules
    each declare a private `Ctx` record; both survive lowering under distinct
    internal names and each module's fns still resolve their own `Ctx`."""
    (tmp_path / "a.rvl").write_text(
        "type Ctx = { depth: Int }\n"
        "pub fn depth_of(c: Ctx) -> Int { return c.depth }\n"
        "pub fn mk_a() -> Int { return depth_of({depth: 3}) }\n"
    )
    (tmp_path / "b.rvl").write_text(
        "type Ctx = { tag: Str }\n"
        "pub fn tag_of(c: Ctx) -> Str { return c.tag }\n"
        "pub fn mk_b() -> Str { return tag_of({tag: \"x\"}) }\n"
    )
    (tmp_path / "root.rvl").write_text(
        'use "./a.rvl" { mk_a }\n'
        'use "./b.rvl" { mk_b }\n'
        "pub fn go() -> Int { return mk_a() }\n"
    )
    ir = compile_files([str(tmp_path / "root.rvl")])
    types = ir["types"]
    assert isinstance(types, dict)
    ctx_types = [t for t in types if t.startswith("Ctx")]
    assert len(ctx_types) == 2, types
    # one carries `depth`, the other `tag` — proof each fn kept its own Ctx.
    shapes = sorted(sorted(types[t]["fields"]) for t in ctx_types)
    assert shapes == [["depth"], ["tag"]], shapes


def test_pub_name_still_imports_and_stays_bare(tmp_path):
    """The whole point of `pub` is unchanged: a public fn keeps its bare name,
    is importable by `use { … }`, and is callable across modules."""
    (tmp_path / "lib.rvl").write_text(
        "pub fn add(a: Int, b: Int) -> Int { return a + b }\n"
        "fn helper() -> Int { return 1 }\n"
    )
    (tmp_path / "main.rvl").write_text(
        'use "./lib.rvl" { add }\n'
        "pub fn twice(x: Int) -> Int { return add(x, x) }\n"
    )
    ir = compile_files([str(tmp_path / "main.rvl")])
    names = _fn_names(ir)
    assert "add" in names            # pub name unchanged
    assert "helper" in names         # lone private stays bare (no collision)
    twice = next(fn for fn in ir["functions"] if fn["name"] == "twice")
    # the call resolves to the imported public `add`, spelled bare.
    assert twice["body"][0]["expr"]["callee"]["name"] == "add"


def test_pub_duplicate_across_modules_still_refuses(tmp_path):
    """A real clash of two `pub` names is a genuine composition error and must
    still be refused — the rule only hides *private* names."""
    (tmp_path / "a.rvl").write_text("pub fn foo() -> Int { return 1 }\n")
    (tmp_path / "b.rvl").write_text("pub fn foo() -> Int { return 2 }\n")
    (tmp_path / "root.rvl").write_text(
        'use "./a.rvl" { foo }\n'
        'use "./b.rvl" { foo }\n'
        "pub fn g() -> Int { return foo() }\n"
    )
    with pytest.raises(RevlError, match="duplicate function `foo`"):
        compile_files([str(tmp_path / "root.rvl")])


def test_item201_use_then_local_same_name_no_longer_collides(tmp_path):
    """Item 201/206: `use { dedent }` used to silently link str.rvl's PRIVATE
    helpers, so the importer could not define any name the module privately
    defined (`duplicate function 'rstrip'`). Now the importer's own private
    `rstrip` coexists with the module's private `rstrip`."""
    (tmp_path / "str.rvl").write_text(
        "pub fn dedent(s: Str) -> Str { return rstrip(s) }\n"
        "fn rstrip(s: Str) -> Str { return s }\n"
    )
    (tmp_path / "main.rvl").write_text(
        'use "./str.rvl" { dedent }\n'
        "fn rstrip(s: Str) -> Str { return s }\n"
        "pub fn go(s: Str) -> Str { return dedent(rstrip(s)) }\n"
    )
    ir = compile_files([str(tmp_path / "main.rvl")])
    names = _fn_names(ir)
    assert "dedent" in names and "go" in names
    rstrips = [n for n in names if n.startswith("rstrip")]
    assert len(rstrips) == 2 and len(set(rstrips)) == 2, names
    # `dedent` still calls the module's own private rstrip (some mangled name),
    # not the importer's — proof the two are kept apart, not conflated.
    dedent = next(fn for fn in ir["functions"] if fn["name"] == "dedent")
    callee = dedent["body"][0]["expr"]["callee"]["name"]
    assert callee.startswith("rstrip") and callee in rstrips
