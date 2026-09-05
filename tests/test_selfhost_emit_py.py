"""The self-hosted cordis-py EMITTER (selfhost/emit_py.rvl, roadmap items
146/174/185 — Path B slices 1+2): compiled by revl, emitted through the python
backend, executed, and cross-checked BYTE-FOR-BYTE against the reference emitter
(backends/python/emit.py's ``emit``) over a corpus of interchange-IR documents.

This is the first proof that revl can emit ITSELF. It has the exact shape of
tests/test_selfhost_{lexer,parser,checker,lower}.py: two independent
implementations of one lowering — the reference backend and its revl port — are
forced to agree. Here the agreement is the strongest kind an emitter can be held
to: the emitted Python source must be identical to the last byte. The reference
is ground truth; any divergence is a defect in the slice.

Slice 2 (item 185) also rewrote the IR-navigation bridge: where slice 1 read the
IR through a bespoke ``@py`` accessor set (``g``/``gs``/``alist``/``at``/…),
navigation is now PURE revl through stdlib/value.rvl's ``value_*`` (item 180) —
a refactor of HOW the IR is read, proven by the function corpus staying
byte-identical. Only host formatting stays ``@py`` (``py_repr``/``newline``/
``mangle``/``snake``/``pascal``), plus one flagged gap: value.rvl ships no
record-key enumerator, so ``record_keys`` is bridged locally (see the file).

Covered subset (what emits byte-identical):
  * the FUNCTION-ONLY document — module scaffold, gated arithmetic preludes
    (i64/i32 traps, IEEE ``_revl_div``), and ``_emit_functions`` ->
    ``_fn_stmt`` -> ``_expr`` for the base surface: let/assign, return,
    if/while/for, expr, assert; lit, var, bin (incl ``??``, bounded ``+ - *``,
    ``/``, truncated ``%``), un, call, field, index, ternary-if, record, list,
    len, stdlib builtins, maplit, sync arrow, match, record-update, string
    interpolation, opt field/call;
  * COMPONENTS/SERVICES (item 185, the ``_ComponentEmitter``) — the populated
    SERVICES table; the conditional ``from runtime import`` line; per component
    the ``_<snake>_apply`` closure + plugin dict with the ``inject`` list; the
    effect accumulator (``effect``/``let-effect``/``fail``/``if``-guard/``emit``
    with saga ``compensate``); timers (``every``/``after`` ->
    ``schedule_*``/``.cancel()``); ``provide`` classes with sync methods; and
    the component-body expression dispatcher ``cexpr`` (``req``/``name``/method
    ``call`` and the un-specialized component arithmetic);
  * the MODULE DECLARATION surface (item 192, slice 3): type declarations
    (``_emit_types`` — record shape classes + sealed-variant classes, the
    forward-reference annotation quoting, and the ``_py_type`` surface->python
    map incl function types -> ``Callable``); the built-in Result (``Ok``/``Err``)
    classes with user-case shadowing; the canonical Float->Str (``_revl_ftoa``)
    helper gated by a float ``${…}`` interpolation; and host roots
    (``Map``/``Pool``/``Job``) in fn/test bodies pulled into the sorted
    ``from runtime import``.

Slice 4 (item 206) adds three more byte-identical forms:
  * externs (``_emit_externs``) — ``def <name>(...)`` plus the verbatim ``@py``
    body run through stdlib/str.rvl's ``dedent`` (item 193's verified
    ``textwrap.dedent`` port) and a faithful ``str.splitlines()`` (the local
    ``splitlines`` drops the trailing empty a trailing ``\\n`` yields). This is
    also the item-193 duplication cull: the four string helpers slices 1-3
    hand-rolled (``trim``/``lstrip``/``last_index_of``/``ident_tokens``) are now
    ``use``d from str.rvl, and the slice-1-3 corpus staying byte-identical is the
    refactor's proof;
  * component ``config`` (``ConfigSchema``) — the ``_<SNAKE>_CONFIG =
    ConfigSchema([...])`` schema block, the ``ConfigSchema``-first sorted
    ``from runtime import``, the plugin dict ``'Config'`` key, and a
    ``config.<field>`` read lowering to ``_revl_config['field']``;
  * method-body ``effect`` / saga ``emit ... compensate`` — the per-request
    accumulator join ``_revl_frame.adopt(_revl_ctx.effect(<fn>, <label>))`` with
    the shared ``_counter`` threaded through the whole component (the
    ``_effect_N`` / ``_emit_N`` closure names and the ``{comp}.{provide}.{method}
    #{n}`` ``_label``).

Deliberately OUT (excluded from the corpus, deferred to Path B slice 5+):
in-file ``test``/``fault_test`` and ``lifecycle test`` emission, async coloring
(async methods / async externs' await-seed / ``await`` bodies), realm placements
(``isolate``/``intercept``/``routes``), spawn/instances, ``adt`` construction, and
the canonical ABI. Method-body ``let-effect`` is emitted (the
``_revl_frame.acquire`` form) but NOT cross-checked this slice: the surface admits
only a ``spawn`` acquire there, whose ``cexpr`` lands in slice 5. ``let_pattern``
(destructuring) is still unimplemented here but is no longer a *permanent*
exclusion: item 179 made the reference's destructure temp deterministic (a
per-``_Lines`` counter, not ``id(node)``), so a future slice can port it.

Restored by item 317 (was OUT as of item 247, docs/design/teardown-contract.md):
``services_body.rvl`` exercises the ACTIVATION-BODY ``emit ... compensate ...``
saga. The py runtime flip (``backends/python/emit.py``'s ``_body_step``) emits
``yield _revl_frame.compensation(lambda: ...)`` — a first-class COMPENSATION
entry on ``Frame``'s shared LIFO stack, two-phase-abort aware — instead of the
old bare ``yield lambda: ...`` disposer. Item 317 ported the SAME change into
``selfhost/emit_py.rvl``'s ``body_step`` (the compensated-``emit`` branch), so
the two are byte-identical again and the fixture is back in the corpus.
``services_method_effects.rvl`` is unaffected and stays IN:
its PROVIDE-METHOD ``emit ... compensate ...`` (``_method_step``) is a
different, request-scoped feature (registered post-commit, governed by
``backends/python/replay.py``'s ``:back`` stepping) that this slice does not
touch.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
CORPUS = [
    # function-only documents (slice 1); still byte-exact after the value_*
    # navigation rewrite (slice 2, item 185) — the refactor's own proof
    "arith.rvl",       # bounded int/int32, division/modulo, comparisons, unary
    "bitwise.rvl",  # Int32 bitwise & | ^ << >> and unary ~ (item 366, item 391 self-host port)
    "strings.rvl",     # the stdlib string builtins and `${…}` interpolation
    "control.rvl",     # while/for/if, match (Some/None/wildcard), sync arrow
    "records.rvl",     # record literal, functional record update, list literal
    "optionals.rvl",   # optional-call chaining (opt receiver)
    "mixed.rvl",       # a cross-section of the above in three functions
    # component/service documents (slice 2, item 185)
    "services_basic.rvl",    # SERVICES table, req/provide, effect accumulator, a provide method
    "services_timers.rvl",   # every/after timers -> schedule_* import + cancel inverses
    "services_methods.rvl",  # provide methods: params, un-specialized bin, builtin, ternary
    "services_body.rvl",     # let-effect, if-guard + fail, activation-body saga `emit ... compensate` (Frame.compensation, item 317)
    # item 436 F3 / item 429 exit (3): the corpus reached the FN-body match
    # (control/mixed/result) but never the COMPONENT one, so `cmatch_branch`'s
    # binder decision had no oracle over it. Added FAILING FIRST, and the
    # component half of F3's binder-free arms was the divergence it caught.
    "services_match.rvl",    # a match in a provide-method body: the body-IS-the-bind, unread-bind and read-bind arms, over an ADT and an Opt
    # module-level declaration surface (slice 3, item 192)
    "types.rvl",       # `_emit_types`: record shape + variant classes, forward-ref quoting, gated `typing` import, `_py_type` (incl fn types)
    "result.rvl",      # built-in Result (Ok/Err) classes, gated by a match on Ok/Err
    "floats.rvl",      # `_revl_ftoa` canonical Float->Str, gated by a float `${…}` interpolation
    "hostroots.rvl",   # host roots (Map/Pool/Job) in a fn body -> the sorted `from runtime import`
    # externs / config / method-body effects (slice 4, item 206)
    "externs.rvl",              # `_emit_externs`: verbatim `@py` body via stdlib/str.rvl::dedent (item 193) + splitlines
    "services_config.rvl",      # component `config`/`ConfigSchema`: schema block, ConfigSchema-first import, `Config` key, `config.<field>` read
    "services_method_effects.rvl",  # method-body `effect` + saga `emit ... compensate`: the `_revl_frame.adopt` accumulator + `_label`/`_effect_N`/`_emit_N` counter
    # item 383 / 391 (self-host port) — the `.map`/`.filter`/`.reduce` transforms
    # desugar (frontend) to `list_map`/`list_filter`/`list_reduce` free calls; the
    # py tier lowers the function-value params, arrow args, and `f(x)` calls.
    "transforms.rvl",
    # item 421 F6 / item 429(d): the declared-`Secret[T]` markings the EMITTED
    # program carries at both ends: `@_revl_secret_result` on a `Secret[T]`-
    # returning extern (the origin, where a confidential value enters the value
    # world) and `_revl_mark_secret(...)` at the head of a provide method whose
    # service declares that param `Secret[T]` (the receiver), plus both names in
    # the sorted `from runtime import`. Added as a family that FAILED FIRST
    # (item 429 exit (3)/(5)): the corpus carried no `Secret[` at all, so the
    # oracle was green while the self-host emitted a program that DOES NOT
    # REDACT, a security divergence and not a parity nicety.
    "secrets.rvl",
    # item 421 F6 / item 256 §7: the declaration positions `secrets.rvl` does not
    # reach — a nested `Result[Secret[Str], Str]` extern return, a `Secret[T]`
    # module-fn parameter, an `Opt[Secret[Str]]` service parameter and, the one
    # this emitter had no site for at all, a `Secret[T]` COMPONENT CONFIG FIELD
    # (`_revl_ConfigSchema([...], secret=[...])`). Added to the corpus FIRST and
    # red on the missing `secret=[...]` argument, per item 429's standing rule:
    # the frontend (`selfhost/lower.rvl`) stamps a config field's `secret`, and an
    # emitter with no site for the stamp emits a schema the runtime cannot redact
    # from.
    "secrets_nested.rvl",
    # item 233 / 276 (self-host port per item 429(d)): the ASCII classification
    # builtins and `codepoint_at`. Added FIRST and red — `selfhost/emit_py.rvl`
    # rendered `<<UNSUPPORTED-BUILTIN:is_digit>>` for every one of them. No
    # corpus document called them, so the oracle was green while the self-host
    # could not emit a program that classifies a character, `selfhost/lexer.rvl`
    # (which calls all five) included.
    "classify.rvl",
    # docs/arithmetic.md's total division forms: the `_revl_checked_*` preamble
    # helpers and the `Ok`/`Err` gate they need. `selfhost/lower.rvl` already
    # lowered these as `builtin` nodes — the gap was emission-only, and invisible
    # for the same reason: nothing in the corpus divided totally.
    "checked_div.rvl",
    # item 189: the Value dot-accessors. Byte-identical BY CONSTRUCTION on this
    # tier (the redirect happens in the frontend, so the emitter sees a plain
    # call) — the document is here so the emit oracle pins that property, while
    # tests/test_selfhost_lower_ir.py, which globs this directory, is the oracle
    # that actually sees the frontend gap.
    "value_accessors.rvl",
    # TAGGED ADT construction (`Ok`/`Err` and user variant cases). `result.rvl`
    # only MATCHES on Ok/Err and nothing in the corpus built a variant, so both
    # oracles agreed trivially while the self-host emitted
    # `<<UNSUPPORTED-EXPR:adt>>` and lowered every constructor as a call of a
    # function that does not exist — the self-host could not compile a program
    # that BUILDS a Result. Added red on both stages.
    "adt.rvl",
    "cache_pure.rvl",
    "witnessed.rvl",
]


def _load_reference_emit():
    """The reference emitter, loaded by path so we compare against the exact
    file this slice mirrors (not whatever `revl` re-exports)."""
    spec = importlib.util.spec_from_file_location(
        "pyemit_reference", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exec_emitted() -> dict:
    """Compile selfhost/emit_py.rvl, emit python, exec it. The file's component
    wrapper makes the emitted module `from runtime import …`; the pure emitter
    functions under test never touch it, so a lazy stub suffices (as in the
    other self-host stage tests)."""
    ir = compile_files([str(ROOT / "selfhost" / "emit_py.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_backend", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had_runtime = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_emit_py.py", "exec"), namespace)
    finally:
        if had_runtime:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def emitted():
    return _exec_emitted()


@pytest.fixture(scope="module")
def reference():
    return _load_reference_emit()


@pytest.mark.parametrize("rel", CORPUS)
def test_selfhosted_emitter_is_byte_identical(emitted, reference, rel):
    """The self-hosted emitter's Python output == the reference's, byte-for-byte,
    for every interchange-IR document in the covered subset."""
    ir = compile_files([str(CORPUS_DIR / rel)])
    want = reference.emit(ir)
    got = emitted["emit_py_src"](ir)
    assert got == want, (
        f"self-hosted emitter diverged from the reference on {rel}\n"
        f"--- lengths ref={len(want)} got={len(got)} ---"
    )


def test_selfhosted_emitter_output_is_executable_python(emitted, reference):
    """A byte-identical output is trivially valid, but pin it: the emitted
    module for a corpus program compiles and its functions run."""
    ir = compile_files([str(CORPUS_DIR / "arith.rvl")])
    src = emitted["emit_py_src"](ir)
    ns: dict = {}
    exec(compile(src, "arith_emitted.py", "exec"), ns)
    assert ns["i64ops"](3, 4) == 3 + 4 - 3 * 4
    assert ns["divmod"](7, 2) == (
        7 // 2 + 7 // 2 + 7 // 2 + 7 % 2 + 7 % 2  # all same sign here
    )


def test_selfhosted_emitter_optional_chains_run(emitted):
    """Item 436's category-(i) hole, closed: `optionals.rvl` used to prove
    byte-agreement on `s?.length()` while NOTHING executed it, and both sides
    were emitting `(s).length()`, which raises `AttributeError` on a str. The
    corpus case now RUNS. It also pins the single-evaluation contract: a `??`
    left operand and a `?.` receiver are evaluated once, and the temp a nested
    chain in an ARGUMENT binds does not clobber the one above it."""
    ir = compile_files([str(CORPUS_DIR / "optionals.rvl")])
    src = emitted["emit_py_src"](ir)
    ns: dict = {}
    exec(compile(src, "optionals_emitted.py", "exec"), ns)
    assert ns["call_chain"]("abc") == 3
    assert ns["call_chain"](None) is None
    assert ns["call_chain2"]("ab") == "ab!"
    assert ns["call_chain2"](None) is None
    assert ns["defaulted"]({"k": "hit"}, "k", "fallback") == "hit"
    assert ns["defaulted"]({}, "k", "fallback") == "fallback"
    # `join` renders its argument before the receiver, so a shared temp here
    # would read the argument's value as the receiver
    assert ns["joined"]({"k": ["a", "b"]}, "k", "+") == "a+b"
    assert ns["joined"]({"k": ["a", "b"]}, "k", None) == "a-b"
    assert ns["joined"]({}, "k", "+") is None


def test_selfhosted_cache_pure_functions_execute_through_memo_wrapper(emitted):
    ir = compile_files([str(CORPUS_DIR / "cache_pure.rvl")])
    ns: dict = {}
    exec(compile(emitted["emit_py_src"](ir), "cache_pure_emitted.py", "exec"), ns)
    assert ns["twice"](4) == 8
    assert ns["cached_record"]({"x": 2, "y": 5}) == 7
    assert "_revl_uncached_twice" in ns
    assert ns["twice"] is not ns["_revl_uncached_twice"]


def test_witnessed_effects_register_each_success_once(emitted, monkeypatch):
    ir = compile_files([str(CORPUS_DIR / "witnessed.rvl")])
    source = emitted["emit_py_src"](ir)
    frames = []

    class Frame:
        begin = drain = None

        def __init__(self, ctx, name):
            self.name = name
            self.entries = []
            frames.append(self)

        def install(self, body):
            list(body())

        def transactional(self, inverse, witness, **kwargs):
            self.entries.append(("activation", inverse, witness, kwargs))

        def transactional_method(self, inverse, witness):
            self.entries.append(("method", inverse, witness, {}))

    runtime = types.ModuleType("runtime")
    runtime.Frame = Frame
    monkeypatch.setitem(sys.modules, "runtime", runtime)
    ns = {}
    exec(compile(source, "witnessed_emitted.py", "exec"), ns)
    acquired, restored = [], []

    def stash(path):
        acquired.append(path)
        if path == "fail":
            return ns["Err"]({"code": "missing"})
        return ns["Ok"]({"path": path, "ordinal": len(acquired)})

    ns.update(stash=stash, unstash=restored.append)
    services = {}
    ctx = types.SimpleNamespace(provide=lambda key: None, set=services.__setitem__)
    ns["Stasher"]["apply"](ctx, {})
    ns["MethodStasher"]["apply"](ctx, {})
    services["stashing"].save("success")
    services["stashing"].save("fail")
    assert acquired == ["payload", "second", "activation", "success", "success", "fail", "fail"]
    assert [len(frame.entries) for frame in frames] == [2, 3]
    assert [entry[0] for entry in frames[1].entries] == ["activation", "method", "method"]
    for frame in frames:
        for mode, inverse, witness, kwargs in frame.entries:
            assert kwargs == ({"undo_idempotent": True, "register": "declared"}
                              if mode == "activation" else {})
            inverse(witness)
    assert [witness["ordinal"] for witness in restored] == [1, 2, 3, 4, 5]
    assert "saved = _revl_wit2" in source
    assert "_revl_wit3 = stash(path)" in source


@pytest.mark.parametrize("body, message", [
    ('component C { if (true) { effect stash("x") } }',
     "only `fail` .* may appear in a component guard"),
    ('service S { emission fn save() }\n'
     'component C provides s: S { provide s { fn save() {'
     ' let saved = effect stash("x") } } }',
     "only `spawn`"),
])
def test_witnessed_source_boundaries_are_frontend_rejections(tmp_path, body, message):
    from revl.lower import RevlError

    source = (CORPUS_DIR / "witnessed.rvl").read_text().split("component Stasher")[0]
    path = tmp_path / "witnessed_boundary.rvl"
    path.write_text(source + body)
    with pytest.raises(RevlError, match=message):
        compile_files([str(path)])


def test_selfhosted_emitter_lowers_components_and_services(emitted):
    """Beyond byte-identity: a component/service document actually drives the
    slice-2 path — the emitted module populates SERVICES and COMPONENTS and its
    plugin dict / apply closure exec cleanly (with the runtime stubbed)."""
    ir = compile_files([str(CORPUS_DIR / "services_basic.rvl")])
    src = emitted["emit_py_src"](ir)
    assert "def _backing_apply(_revl_ctx, _revl_config):" in src
    assert "yield _revl_ctx.provide('health')" in src
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    prev = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(src, "services_basic_emitted.py", "exec"), ns)
    finally:
        if had:
            sys.modules["runtime"] = prev
        else:
            del sys.modules["runtime"]
    assert set(ns["SERVICES"]) == {"Store", "Health"}
    assert set(ns["COMPONENTS"]) == {"Backing", "Reader"}
    assert ns["Backing"]["inject"] == ["store"]
    assert ns["Reader"]["inject"] == ["store"]


def test_selfhosted_emitter_in_file_tests_pass(emitted):
    """The .rvl file's own `test` blocks run under the python backend."""
    tests = emitted.get("REVL_TESTS")
    assert tests and len(tests) >= 4, "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
