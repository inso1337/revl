"""The site-spelled inverse slot is argument-checked (`_check_inverse_args`).

An `acquire` extern's DECLARED `undo` was checked - callee declared, no
self-reference, arity and argument types against the declared signature, with
`result: T` in scope (`_check_extern_undo`). The SITE slot got none of it:
`effect secret_put("v") undo secret_release(42)` compiled, and so did a
three-argument call to a one-argument inverse.

That is the worse half of the pair. The declared `undo` of an `acquire` extern
is never replayed by either tier's emitter (`ext["undo"]` is read only by the
item-243 witnessed emitters); the teardown that actually runs on abort is the
SITE undo. So the unchecked expression was the one executing while the
activation was already unwinding, where a wrong-arity or wrong-type call fails
at the worst possible moment - and it laundered types at a provide-method seam,
which is the one position where the acquired handle cannot be named.

The judgment is the stratum's own (`infer_ir` for a lowered site slot,
`check_ast`/`infer_ast` for the extern's AST slot), reading one signature
table, one arity rule, one `unify`, one `mismatch`. What was missing was the
FILENAME: both checkers are documented to raise only when handed one, and the
component lowering calls `infer_ir` in its non-raising oracle mode. Item 404's
`_sweep` handed it over for a provide-method `let`; this is the same move for
the slot that runs during teardown.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError, RevlErrors


_VAULT = (
    "type SecretHandle = Opaque\n"
    "extern acquire fn secret_put(v: Str) -> SecretHandle"
    " undo secret_release(result) = @py { return 1 }\n"
    "extern pure fn secret_release(h: SecretHandle) -> Unit = @py { return }\n"
    "extern pure fn note(tag: Str) -> Unit = @py { return }\n"
)


def _compile(src: str) -> dict:
    return compile_source(src, "t.rvl")


def _refusal(src: str) -> str:
    with pytest.raises((RevlError, RevlErrors)) as ei:
        _compile(src)
    return str(ei.value)


# -- the three reproduced exploits, at a bound activation-body site ---------

def test_site_undo_refuses_a_wrong_typed_argument():
    msg = _refusal(_VAULT + 'component C {\n'
                   '  let s = effect secret_put("v") undo secret_release("a plain Str")\n'
                   '}\n')
    assert "argument 1 of `secret_release(...)` expects `SecretHandle`, got `Str`" in msg


def test_site_undo_refuses_a_wrong_typed_literal():
    msg = _refusal(_VAULT + 'component C {\n'
                   '  let s = effect secret_put("v") undo secret_release(42)\n'
                   '}\n')
    assert "argument 1 of `secret_release(...)` expects `SecretHandle`, got `Int`" in msg


def test_site_undo_refuses_a_wrong_arity_call():
    msg = _refusal(_VAULT + 'component C {\n'
                   '  let s = effect secret_put("v") undo secret_release(1, 2, 3)\n'
                   '}\n')
    assert "`secret_release` takes 1 argument(s), 3 given" in msg


def test_site_undo_refusal_names_the_slot_and_both_legal_spellings():
    msg = _refusal(_VAULT + 'component C {\n'
                   '  let s = effect secret_put("v") undo secret_release(42)\n'
                   '}\n')
    assert "(`undo` slot)" in msg
    assert "bind the acquisition and name the binding" in msg
    assert "declare the extern `witnessed`" in msg


def test_the_correct_site_undo_still_compiles():
    ir = _compile(_VAULT + 'component C {\n'
                  '  let s = effect secret_put("v") undo secret_release(s)\n'
                  '}\n')
    assert any(c["name"] == "C" for c in ir["components"])


# -- every other site-undo position ----------------------------------------

def test_unbound_activation_effect_undo_is_checked():
    msg = _refusal(_VAULT + 'component C {\n'
                   '  effect secret_put("v") undo note(42)\n'
                   '}\n')
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in msg


def test_provide_method_site_undo_is_checked():
    msg = _refusal(_VAULT + "service Vault { emission fn stash(v: Str) -> Str }\n"
                   "component C provides vault: Vault {\n"
                   "  provide vault {\n"
                   "    fn stash(v: Str) -> Str {\n"
                   "      effect secret_put(v) undo secret_release(v)\n"
                   "      return \"ok\"\n"
                   "    }\n"
                   "  }\n"
                   "}\n")
    # `v: Str` is not a `SecretHandle`; this is the type-laundering shape that
    # was the ONLY thing that compiled at a seam before this check.
    assert "argument 1 of `secret_release(...)` expects `SecretHandle`, got `Str`" in msg


def test_provide_method_spawn_undo_is_checked():
    src = (_VAULT
           + "component Child { effect secret_put(\"c\") undo note(\"c\") }\n"
             "service S { fn go() -> Str }\n"
             "component P provides s: S {\n"
             "  provide s {\n"
             "    fn go() -> Str {\n"
             "      let inst = effect spawn Child undo note(42)\n"
             "      return \"ok\"\n"
             "    }\n"
             "  }\n"
             "}\n")
    msg = _refusal(src)
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in msg


def test_subscribe_undo_slot_is_checked():
    src = ("extern pure fn tidy(n: Int) -> Unit = @py { return }\n"
           "component C {\n"
           "  let src = effect Stream.source() undo src.close()\n"
           "  let sub = subscribe src undo tidy(\"not an Int\")\n"
           "  await sub.next()\n"
           "}\n")
    msg = _refusal(src)
    assert "argument 1 of `tidy(...)` expects `Int`, got `Str`" in msg


def test_subscribe_undo_arity_is_checked():
    src = ("extern pure fn tidy(n: Int) -> Unit = @py { return }\n"
           "component C {\n"
           "  let src = effect Stream.source() undo src.close()\n"
           "  let sub = subscribe src undo tidy(1, 2, 3)\n"
           "  await sub.next()\n"
           "}\n")
    assert "`tidy` takes 1 argument(s), 3 given" in _refusal(src)


def test_the_corpus_subscribe_bracket_still_compiles():
    ir = _compile("component C {\n"
                  "  let src = effect Stream.source() undo src.close()\n"
                  "  let sub = subscribe src undo sub.close()\n"
                  "  await sub.next()\n"
                  "}\n")
    assert any(c["name"] == "C" for c in ir["components"])


def test_site_compensate_slot_is_checked():
    src = (_VAULT + "service Out { emission fn ping(v: Str) -> Unit }\n"
           "component C requires out: Out {\n"
           '  emit out.ping("x") compensate note(42)\n'
           "}\n")
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in _refusal(src)


def test_a_laundered_call_nested_under_an_opaque_host_call_is_checked():
    # `infer_ir` deliberately does not visit the arguments of a host-verb
    # `call` node (a host result is opaque), so a root-only sweep would miss
    # this. The slot check walks every declared-callable node in the tree.
    src = (_VAULT + "component C {\n"
           "  let store = effect Map.new() undo store.drop()\n"
           '  let s = effect secret_put("v") undo store.insert("k", note(42))\n'
           "}\n")
    assert "argument 1 of `note(...)` expects `Str`, got `Int`" in _refusal(src)


# -- leniency the slot must keep -------------------------------------------

def test_an_unknown_typed_argument_stays_admitted():
    # a host-verb result is opaque (`infer_ir` -> None); `unify` passes on an
    # unknown, so the slot check stays silent exactly where the type oracle is.
    ir = _compile(_VAULT + "component C {\n"
                  "  let store = effect Map.new() undo store.drop()\n"
                  '  let s = effect secret_put("v") undo note(store.get("k"))\n'
                  "}\n")
    assert any(c["name"] == "C" for c in ir["components"])


def test_a_host_verb_site_undo_is_untouched():
    ir = _compile("component C {\n"
                  "  let store = effect Map.new() undo store.drop()\n"
                  "}\n")
    assert any(c["name"] == "C" for c in ir["components"])


# -- the legal spelling at a provide-method seam (Part 2) -------------------

_WITNESSED_SEAM = (
    "type SecretHandle = Opaque\n"
    "type PutErr = { code: Str }\n"
    "extern witnessed fn secret_put(v: Str) -> Result[SecretHandle, PutErr]"
    " undo secret_release(result) = @py { return 1 }\n"
    "extern pure fn secret_release(h: SecretHandle) -> Unit = @py { return }\n"
    "service Vault { emission fn stash(v: Str) -> Str }\n"
    "component C provides vault: Vault {\n"
    "  provide vault {\n"
    "    fn stash(v: Str) -> Str {\n"
    "      effect secret_put(v)\n"
    '      return "ok"\n'
    "    }\n"
    "  }\n"
    "}\n"
)


def test_a_seam_can_express_a_correct_release_through_witnessed():
    # The decision behind Part 2. At a provide-method seam the acquired handle
    # is unnameable: only `spawn` may be bound there, `result` is not in scope
    # in a site `undo`, and re-minting a same-typed handle is refused by 308's
    # O1. Rather than inventing a fourth surface (a site `result`, a method-
    # scope acquire binding, a handle-carrying form), the language already has
    # the one classification whose DECLARED inverse actually replays:
    # `witnessed` (items 243/318). It carries no site `undo` at all, and its
    # declared `undo <inverse>(result)` auto-registers on the enclosing
    # activation's transactional accumulator, once per acquisition, with
    # `result` bound to the handle the acquisition returned.
    ir = _compile(_WITNESSED_SEAM)
    assert any(c["name"] == "C" for c in ir["components"])


def test_the_witnessed_seam_releases_exactly_the_acquired_handle():
    # the proof that the spelling is not merely admitted but correct: the
    # emitted teardown binds `result` to the acquisition's own value.
    import importlib.util
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    path = root / "backends" / "python" / "emit.py"
    spec = importlib.util.spec_from_file_location("_emit_py_siteundo", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    code = mod.emit(_compile(_WITNESSED_SEAM))
    assert "transactional_method" in code
    assert "lambda result: secret_release(result)" in code


def test_a_seam_still_cannot_re_mint_a_handle_to_release():
    # 308's O1, unchanged: the re-mint shape is a double-close.
    msg = _refusal(_VAULT + "service Vault { emission fn stash(v: Str) -> Str }\n"
                   "component C provides vault: Vault {\n"
                   "  provide vault {\n"
                   "    fn stash(v: Str) -> Str {\n"
                   "      effect secret_put(v) undo secret_release(secret_put(v))\n"
                   "      return \"ok\"\n"
                   "    }\n"
                   "  }\n"
                   "}\n")
    assert "item 308, O1" in msg
    # Part 3: the hint names a fix reachable at THIS position.
    assert "declare the extern `witnessed`" in msg
    assert "only `spawn` may be acquired inside a provide-method body" in msg


# -- the extern slot keeps its own judgment, through the same entry point ---

def test_the_extern_slot_is_still_argument_checked():
    msg = _refusal("type SecretHandle = Opaque\n"
                   "extern pure fn secret_release(h: SecretHandle) -> Unit"
                   " = @py { return }\n"
                   "extern acquire fn secret_put(v: Str) -> SecretHandle"
                   ' undo secret_release("nope") = @py { return 1 }\n')
    assert "argument 1 of `secret_release(...)` expects `SecretHandle`, got `Str`" in msg


def test_the_extern_slot_arity_is_still_checked():
    msg = _refusal("type SecretHandle = Opaque\n"
                   "extern pure fn secret_release(h: SecretHandle) -> Unit"
                   " = @py { return }\n"
                   "extern acquire fn secret_put(v: Str) -> SecretHandle"
                   " undo secret_release(result, result) = @py { return 1 }\n")
    assert "`secret_release` takes 1 argument(s), 2 given" in msg
