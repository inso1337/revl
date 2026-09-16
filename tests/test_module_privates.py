"""Module-private namespacing (roadmap 228, CAPSTONE SEAM 2).

A `use`d module's PRIVATE (non-`pub`) top-level declarations do not enter the
importer's merged namespace: only `pub` names are visible and mergeable. Two
modules that each define a private `Ctx` (or `contains`, or `rstrip`) therefore
co-compile into one composition, while a genuine duplicate of a `pub` name is
still refused. This is what lets the self-host `lower`/`emit_py`/`emit_rust`
stages co-compile (item 224) and resolves the item-201/206 duplicate-name
friction.
"""

import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl.compiler import (  # noqa: E402
    _ModuleLoader,
    _reject_cross_module_case_collisions,
)


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


# ---------------------------------------------------------------------------
# Issue #1145: the name kind item 228's two tables never see — an ADT CASE.
#
# 228 builds a fn/extern table and a `type` DECLARATION table, and renames a
# private decl whose bare name is not unique. A case name is in neither, so two
# modules could spend one name on a case and on a type/fn and merge with no
# diagnostic: `Field(x)` then has two readings, each tier emits them as one
# symbol, and which one wins is decided by the order the merge flattened the
# modules in. Measured on the `types.rvl`+`parser.rvl` composition as 415 of 761
# census programs faulting with `Field() takes no arguments`, while every unit
# oracle — each compiling its file ALONE — stayed green.
# ---------------------------------------------------------------------------

SELFHOST = ROOT / "selfhost"


def test_case_name_vs_record_type_across_a_use_edge_refuses(tmp_path):
    """The measured shape, verbatim: a private record `Field` in one module and
    the `Field(FieldN)` expression case in another."""
    (tmp_path / "types.rvl").write_text(
        "type Field = { name: Str, ty: Str }\n"
        "pub fn field_name(f: Field) -> Str { return f.name }\n"
    )
    (tmp_path / "parser.rvl").write_text(
        "pub type FieldN = { name: Str }\n"
        "pub type Expr =\n"
        "    Var(Str)\n"
        "  | Field(FieldN)\n"
        "pub fn mk(n: Str) -> Expr { return Field({ name: n }) }\n"
    )
    (tmp_path / "root.rvl").write_text(
        'use "./types.rvl" { field_name }\n'
        'use "./parser.rvl" { Expr, mk }\n'
        "pub fn go(s: Str) -> Expr { return mk(s) }\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(tmp_path / "root.rvl")])
    message = str(excinfo.value)
    # refused BY NAME, with both declaration sites cited — the shape
    # `_reject_clashing_private_externs` already uses for a private-extern clash.
    assert "duplicate name `Field`" in message
    assert "a case of `Expr`" in message
    assert f"{tmp_path / 'parser.rvl'}:4" in message
    assert f"{tmp_path / 'types.rvl'}:1" in message


def test_case_name_vs_fn_across_a_use_edge_refuses(tmp_path):
    """The same collision against a `fn` rather than a `type`. Before the check
    this was SILENT and strictly worse than the record shape: the case won at
    the call site, so `Field("q")` built an ADT value and the module's own `fn
    Field` body was never called, with no diagnostic anywhere."""
    (tmp_path / "a.rvl").write_text(
        "pub type A =\n    Field(Str)\n  | Zed\n"
        'pub fn mk() -> A { return Field("a") }\n'
    )
    (tmp_path / "b.rvl").write_text(
        'use "./a.rvl" { A, mk }\n'
        "fn Field(s: Str) -> A { return Zed }\n"
        'pub fn go() -> A { return Field("q") }\n'
    )
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(tmp_path / "b.rvl")])
    message = str(excinfo.value)
    assert "duplicate name `Field`" in message
    assert "a fn `Field`" in message
    assert f"{tmp_path / 'b.rvl'}:2" in message


def test_renaming_one_side_admits_again(tmp_path):
    """Non-vacuity in the other direction: the refusal is about the NAME, not
    about `use` edges or ADTs in general. The #1144 workaround — rename the
    record to `TyField` — compiles."""
    (tmp_path / "types.rvl").write_text(
        "type TyField = { name: Str, ty: Str }\n"
        "pub fn field_name(f: TyField) -> Str { return f.name }\n"
    )
    (tmp_path / "parser.rvl").write_text(
        "pub type FieldN = { name: Str }\n"
        "pub type Expr =\n"
        "    Var(Str)\n"
        "  | Field(FieldN)\n"
        "pub fn mk(n: Str) -> Expr { return Field({ name: n }) }\n"
    )
    (tmp_path / "root.rvl").write_text(
        'use "./types.rvl" { field_name }\n'
        'use "./parser.rvl" { Expr, mk }\n'
        "pub fn go(s: Str) -> Expr { return mk(s) }\n"
    )
    ir = compile_files([str(tmp_path / "root.rvl")])
    assert ir["types"]["Expr"]["kind"] == "variant"
    assert ir["types"]["TyField"]["kind"] == "record"
    mk = next(fn for fn in ir["functions"] if fn["name"] == "mk")
    assert mk["body"][0]["expr"]["case"] == "Field"


def test_one_file_declaring_both_is_unchanged(tmp_path):
    """Scope: the MERGE SEAM. Inside one file both spellings are in front of
    their author and the single-module rule is untouched — otherwise
    `examples/rejections/t18_type_alias_cycle.rvl`, which declares exactly this
    shape, would start refusing for the wrong reason."""
    (tmp_path / "single.rvl").write_text(
        "type Field = { name: Str, ty: Str }\n"
        "pub type Node =\n    Field(Str)\n  | Other\n"
        'pub fn go() -> Node { return Field("a") }\n'
    )
    ir = compile_files([str(tmp_path / "single.rvl")])
    assert ir["types"]["Field"]["kind"] == "record"
    assert ir["types"]["Node"]["kind"] == "variant"


def test_two_modules_sharing_a_case_name_is_unchanged(tmp_path):
    """Also out of scope, and for a reason: two ADTs sharing a case name is
    already LOUD rather than silent. `lower.py::_case_table` drops an ambiguous
    case from the constructor table, so the use site refuses (G1) instead of
    building a wrong program — no new refusal is needed and none is added."""
    (tmp_path / "x.rvl").write_text(
        "pub type A =\n    Field(Str)\n  | Zed\n"
        'pub fn mk() -> A { return Field("a") }\n'
    )
    (tmp_path / "y.rvl").write_text(
        'use "./x.rvl" { A, mk }\n'
        "type B =\n    Field(Int)\n  | Wye\n"
        "pub fn go() -> B { return Field(1) }\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(tmp_path / "y.rvl")])
    message = str(excinfo.value)
    assert "duplicate name" not in message
    assert "`Field` is not declared in this function" in message


def test_the_historical_collision_on_the_real_selfhost_files(tmp_path):
    """Plant the #1144 collision on the SHIPPED declarations, not a synthetic
    copy of them: `selfhost/types.rvl` declares `type Field = { name, ty }` and
    `selfhost/parser.rvl` declares the `Field(FieldN)` expression case. They do
    not share a composition on this tree, so one two-line root is the whole
    plant — which is also how much it will take for the next `use` edge to
    reintroduce it.

    The record's name is asserted into the COPY rather than read out of the
    tree, so the plant keeps reproducing the historical collision once the
    #1144 rename to `TyField` lands and nobody has to notice that this test
    quietly stopped planting anything."""
    shutil.copytree(SELFHOST, tmp_path / "selfhost")
    shutil.copytree(ROOT / "stdlib", tmp_path / "stdlib")
    types_rvl = tmp_path / "selfhost" / "types.rvl"
    source = types_rvl.read_text()
    planted = re.sub(r"\bTyField\b", "Field", source)
    types_rvl.write_text(planted)
    assert "type Field = { name: Str, ty: Str }" in planted, (
        "the structural-record declaration this plant depends on has been "
        "reshaped in selfhost/types.rvl; re-derive the plant from it"
    )
    # the case half is the shipped one, unedited.
    assert "| Field(FieldN)" in (tmp_path / "selfhost" / "parser.rvl").read_text()
    probe = tmp_path / "selfhost" / "probe.rvl"
    probe.write_text(
        'use "./types.rvl" { structural_parse }\n'
        'use "./parser.rvl" { Expr }\n'
        "pub fn probe(s: Str) -> Bool { return structural_parse(s).is_rec }\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(probe)])
    message = str(excinfo.value)
    assert "duplicate name `Field`" in message
    assert "a case of `Expr`" in message
    assert str(tmp_path / "selfhost" / "parser.rvl") in message
    assert str(tmp_path / "selfhost" / "types.rvl") in message


def test_every_use_closure_in_the_tree_is_free_of_case_collisions():
    """The other half of non-vacuity: the tree as it stands passes, and the
    denominator is reported rather than assumed. Runs the check itself over
    every multi-module `use` closure the repository contains, so a future
    composition that reintroduces the collision fails here as well as at the
    compile that first merges the two files."""
    closures = 0
    edges: set[tuple[str, str]] = set()
    for path in sorted(p for p in ROOT.rglob("*.rvl")
                       if ".git" not in p.parts and "node_modules" not in p.parts):
        loader = _ModuleLoader()
        try:
            root_module = loader.load(str(path))
        except Exception:
            continue  # a deliberately-unparsable fixture is not this test's business
        by_id = {id(m): m for m in loader._cache.values()}
        included = [root_module]
        seen = {id(root_module)}
        queue = [root_module]
        while queue:
            current = queue.pop(0)
            for dep in sorted(current.pure_dependencies, key=lambda v: by_id[v].path):
                if dep not in seen:
                    seen.add(dep)
                    included.append(by_id[dep])
                    queue.append(by_id[dep])
        if len(included) < 2:
            continue
        closures += 1
        for module in included:
            for use in module.program.uses:
                edges.add((module.path, use.path))
        # the production check, on the real closure
        _reject_cross_module_case_collisions(included)
    # the denominator, asserted so that a selector or loader change which
    # quietly stops finding compositions fails here instead of passing vacuously.
    assert closures >= 20, f"only {closures} multi-module closures found"
    assert len(edges) >= 40, f"only {len(edges)} distinct `use` edges inside them"
