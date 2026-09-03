"""Targeted forward-reference quoting in the python backend's emitted types.

revl types may be mutually recursive (a record referencing an ADT declared
later, or vice versa), but Python evaluates class-body annotations at
class-definition time, so a bare forward name would raise NameError. The
emitter therefore quotes exactly those annotations that reference a
not-yet-emitted type.

The rejected alternative — `from __future__ import annotations` (PEP 563) in
the emitted header — makes EVERY annotation a string, which pushes resolution
onto whatever consumer asks for it and, for a module exec'd anonymously,
leaves it with nothing to resolve against. These tests exec the emitted module
in a bare namespace with no sys.modules registration, which is precisely that
case.

A record's class is a SHAPE, never a constructor: a record VALUE is a plain
dict (roadmap item 436 F9 dropped the `@dataclass` that made it look
otherwise), so what is asserted here is the annotation text, the class-body
evaluation succeeding, and the dict the emitted function actually returns. The
ADT cases ARE constructed, because those classes are real.
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


def test_record_to_adt_forward_ref_is_quoted_and_execs():
    # `Tree` references `Forest`, which is declared later: the annotation must
    # be quoted, and the module must still exec without sys.modules
    # registration. A bare `list[Forest]` here is a NameError at class-body
    # evaluation, which is what the quoting exists to prevent.
    code, ns = _compile_and_exec(
        "type Tree = { kids: List[Forest] }\n"
        "type Forest = Grove(Tree) | Empty\n"
        "fn mk() -> Tree { return { kids: [] } }\n"
    )
    assert "kids: 'list[Forest]'" in code
    assert ns["Tree"].__annotations__ == {"kids": "list[Forest]"}
    # the record VALUE is a dict; the ADT cases are real classes
    assert ns["mk"]() == {"kids": []}
    assert isinstance(ns["Grove"]({"kids": []}), ns["Forest"])
    assert isinstance(ns["Empty"](), ns["Forest"])


def test_adt_to_record_backward_ref_stays_bare():
    # `Leaf`'s payload references `LeafData`, declared earlier: no forward
    # ref, so the annotation stays an unquoted expression.
    code, ns = _compile_and_exec(
        "type LeafData = { n: Int }\n"
        "type Thing = Leaf(LeafData) | Hole\n"
    )
    leaf = ns["Leaf"]({"n": 7})
    assert leaf.value["n"] == 7
    assert isinstance(ns["Hole"](), ns["Thing"])
    # backward refs must NOT be quoted
    assert "'LeafData'" not in code


def test_self_recursive_record_is_quoted():
    code, ns = _compile_and_exec(
        "type Node = { val: Int, next: Opt[Node] }\n"
        "fn mk() -> Node { return { val: 1, next: None } }\n"
    )
    assert "next: 'Optional[Node]'" in code
    assert ns["Node"].__annotations__["next"] == "Optional[Node]"
    assert ns["mk"]() == {"val": 1, "next": None}


def test_generic_containing_forward_ref_is_quoted_whole():
    # Map/List wrappers around a forward ref: quoting the whole rendered
    # string is fine, because a string annotation is inert.
    code, ns = _compile_and_exec(
        "type Env = { scopes: Map[Str, Vals] }\n"
        "type Vals = { items: List[Int] }\n"
        "fn mk() -> Vals { return { items: [1] } }\n"
    )
    assert "scopes: 'dict[str, Vals]'" in code
    assert ns["Env"].__annotations__ == {"scopes": "dict[str, Vals]"}
    assert ns["mk"]() == {"items": [1]}


def test_no_record_type_imports_nothing():
    """item 436 F9: a module whose only declaration is a variant used to pay
    `from dataclasses import dataclass` and a four-name `typing` import for
    annotations it could not contain."""
    code, _ = _compile_and_exec("type Thing = Yes | No\n")
    assert "import dataclass" not in code
    assert "from typing import" not in code


def test_typing_import_is_only_what_the_annotations_mention():
    """`Union` was imported into every module with a type declaration and is
    not a name `_py_type` can render (item 436 F9)."""
    code, _ = _compile_and_exec("type Node = { val: Int, next: Opt[Node] }\n")
    assert "from typing import Optional\n" in code
