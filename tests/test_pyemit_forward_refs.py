"""Targeted forward-reference quoting in the python backend's emitted types.

revl types may be mutually recursive (a record referencing an ADT declared
later, or vice versa), but Python evaluates class-body annotations at
class-definition time, so a bare forward name would raise NameError. The
emitter therefore quotes exactly those annotations that reference a
not-yet-emitted type.

The rejected alternative — `from __future__ import annotations` (PEP 563) in
the emitted header — breaks consumers that exec() the module without
registering it in sys.modules: @dataclass's InitVar/ClassVar detection calls
sys.modules.get(cls.__module__).__dict__ on every string annotation and
crashes with AttributeError when the module is anonymous. These tests exec
the emitted module in a bare namespace with no sys.modules registration,
which is precisely the case PEP 563 cannot support.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_emitter():
    spec = importlib.util.spec_from_file_location(
        "pyemit_under_test", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile_and_exec(src: str):
    from revl.compiler import compile_source

    emit = _load_emitter()
    code = emit.emit(compile_source(src))
    # the rejected PEP 563 approach must stay out of the emitted header
    assert "from __future__ import annotations" not in code
    # bare namespace, never registered in sys.modules — anonymous module
    namespace = {}
    exec(compile(code, "<emitted>", "exec"), namespace)
    return code, namespace


def test_record_to_adt_forward_ref_is_quoted_and_constructs():
    # `Tree` references `Forest`, which is declared later: the annotation must
    # be quoted, and the module must still exec and construct without
    # sys.modules registration.
    code, ns = _compile_and_exec(
        "type Tree = { kids: List[Forest] }\n"
        "type Forest = Grove(Tree) | Empty\n"
    )
    assert "kids: 'list[Forest]'" in code

    tree = ns["Tree"](kids=[ns["Grove"](ns["Tree"](kids=[]))])
    assert isinstance(tree.kids[0], ns["Grove"])
    assert isinstance(tree.kids[0].value, ns["Tree"])
    assert isinstance(ns["Empty"](), ns["Forest"])


def test_adt_to_record_backward_ref_stays_bare():
    # `Leaf`'s payload references `LeafData`, declared earlier: no forward
    # ref, so the annotation stays an unquoted expression.
    code, ns = _compile_and_exec(
        "type LeafData = { n: Int }\n"
        "type Thing = Leaf(LeafData) | Hole\n"
    )
    leaf = ns["Leaf"](ns["LeafData"](n=7))
    assert leaf.value.n == 7
    assert isinstance(ns["Hole"](), ns["Thing"])
    # backward refs must NOT be quoted
    assert "'LeafData'" not in code


def test_self_recursive_record_is_quoted():
    code, ns = _compile_and_exec(
        "type Node = { val: Int, next: Opt[Node] }\n"
    )
    assert "next: 'Optional[Node]'" in code
    assert ns["Node"](val=1, next=None).next is None
    assert ns["Node"](val=2, next=ns["Node"](val=1, next=None)).next.val == 1


def test_generic_containing_forward_ref_is_quoted_whole():
    # Map/List wrappers around a forward ref: quoting the whole rendered
    # string is fine — dataclasses treat any string annotation as lazy.
    code, ns = _compile_and_exec(
        "type Env = { scopes: Map[Str, Vals] }\n"
        "type Vals = { items: List[Int] }\n"
    )
    assert "scopes: 'dict[str, Vals]'" in code
    env = ns["Env"](scopes={"x": ns["Vals"](items=[1])})
    assert env.scopes["x"].items == [1]
