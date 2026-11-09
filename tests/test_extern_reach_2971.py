"""Issue #2971: an extern declaration states what its host body may reach.

The clause is item 519's `reaches [...]`, read after the return type on every
extern class and canonicalized through the same `_capability_params` point every
other capability token uses. A declared reach lowers to
`reach: {"caps": [...], "basis": "declared"}`; an OMITTED clause writes no key,
so a consumer reads UNKNOWN, never empty. The compiler refuses a claim that
omits the extern's own `emission[X]`/`witnessed[X]` scope, the one fact it can
check about an opaque host body. `basis` stays `declared`: a future `verified`
value is reserved for the enforcement #2708 designs
(docs/design/1729-extern-reach-declaration.md).
"""

import pytest

from revl import compile_source
from revl.errors import RevlError


def _externs(source: str) -> list[dict]:
    return compile_source(source)["externs"]


def test_declared_reach_is_carried_and_undeclared_is_absent():
    declared, silent = _externs(
        "extern pure fn reads(path: Str) -> Str\n"
        "reaches [fs.read]\n"
        "= @py { return path }\n"
        "extern pure fn silent(path: Str) -> Str\n"
        "= @py { return path }\n")
    assert declared["reach"] == {"caps": ["fs.read"], "basis": "declared"}
    assert "reach" not in silent


def test_an_undeclared_reach_is_unknown_not_empty():
    """Absence and `reaches []` are DIFFERENT IR facts: absent = unknown.

    Reading silence as empty is the fail-open shape the issue names, so the two
    documents must be distinguishable by a consumer holding only the IR.
    """
    declared, absent = _externs(
        "extern pure fn empty(n: Int) -> Int\n"
        "reaches []\n"
        "= @py { return n }\n"
        "extern pure fn absent(n: Int) -> Int\n"
        "= @py { return n }\n")
    assert declared["reach"] == {"caps": [], "basis": "declared"}
    assert absent.get("reach") is None
    assert declared.get("reach", {}).get("caps") == []


def test_reaches_star_is_the_unbounded_spelling():
    (ext,) = _externs(
        "extern pure fn anything(p: Str) -> Str\n"
        "reaches [*]\n"
        "= @py { return p }\n")
    assert ext["reach"] == {"caps": ["*"], "basis": "declared"}


def test_tokens_are_canonicalized_through_the_capability_registry():
    (ext,) = _externs(
        "extern pure fn write(p: Str) -> Int\n"
        'reaches [fs.write(path="/data/")]\n'
        "= @py { return 1 }\n")
    # `_capability_params` drops the trailing slash, exactly as it does in an
    # `emission[...]` scope; the IR stores the canonical spelling.
    assert ext["reach"]["caps"] == ['fs.write(path="/data")']


def test_a_reach_must_cover_the_externs_own_scope():
    with pytest.raises(RevlError) as caught:
        compile_source(
            "extern emission[fs] fn write(p: Str) -> Int\n"
            "reaches [net]\n"
            "= @py { return 1 }\n")
    assert "omits" in caught.value.message
    assert "fs" in caught.value.message


def test_a_bare_reach_token_covers_its_parameterized_scope():
    (ext,) = _externs(
        'extern emission[fs.write(path="/tmp")] fn write(p: Str) -> Int\n'
        "reaches [fs.write]\n"
        "= @py { return 1 }\n")
    assert ext["reach"]["caps"] == ["fs.write"]


def test_a_narrower_parameter_does_not_cover_the_scope():
    with pytest.raises(RevlError) as caught:
        compile_source(
            'extern emission[fs.write(path="/tmp")] fn write(p: Str) -> Int\n'
            'reaches [fs.write(path="/data")]\n'
            "= @py { return 1 }\n")
    assert "omits" in caught.value.message


def test_unbounded_reach_covers_a_scoped_extern():
    (ext,) = _externs(
        "extern emission[fs] fn write(p: Str) -> Int\n"
        "reaches [*]\n"
        "= @py { return 1 }\n")
    assert ext["reach"] == {"caps": ["*"], "basis": "declared"}


def test_duplicate_capability_is_refused():
    with pytest.raises(RevlError) as caught:
        compile_source(
            "extern pure fn twice(n: Int) -> Int\n"
            "reaches [fs, fs]\n"
            "= @py { return n }\n")
    assert "duplicate capability" in caught.value.message


def test_confinement_only_reach_is_unchanged():
    (ext,) = _externs(
        "extern emission[fs](confined: cwd) fn write(cwd: Str, p: Str) -> Int\n"
        "= @py { return 1 }\n")
    assert ext["reach"] == {"kind": "confined", "target": "cwd"}


def test_a_local_extern_may_declare_a_reach():
    (ext,) = _externs(
        "extern local fn put(k: Str, v: Str) -> Unit\n"
        "reaches [db]\n"
        "= @py { return }\n")
    assert ext["reach"] == {"caps": ["db"], "basis": "declared"}


def test_witnessed_scope_is_covered_the_same_way():
    externs = {e["name"]: e for e in _externs(
        "type Stash = { path: Str }\n"
        "type FsError = { code: Str }\n"
        "extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
        "extern witnessed[fs] fn stash(path: Str) -> Result[Stash, FsError]\n"
        "reaches [fs]\n"
        "undo unstash(result)\n"
        "= @py { return }\n")}
    assert externs["stash"]["reach"] == {"caps": ["fs"], "basis": "declared"}
    assert "reach" not in externs["unstash"]


def test_confinement_and_declared_reach_share_the_field():
    (ext,) = _externs(
        "extern emission[fs](confined: cwd) fn write(cwd: Str, p: Str) -> Int\n"
        "reaches [fs]\n"
        "= @py { return 1 }\n")
    assert ext["reach"] == {
        "kind": "confined",
        "target": "cwd",
        "caps": ["fs"],
        "basis": "declared",
    }
