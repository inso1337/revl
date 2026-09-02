"""async externs on the TypeScript tier — roadmap item 80, slice 3.

The original bug (harness milestone 2, finding #15): the harness's single G8
crossing `http_post` has a `@ts` body returning a `Promise<string>`, but the
extern was typed `string`, so `tsc` rejected `Type 'Promise<string>' is not
assignable to type 'string'`. The fix emits an async extern as
`async function …: Promise<T>` and `await`s it at every admitted call site.

The durable exit test is the golden `golden/async_http.ts`, which
`npm run typecheck` compiles (tsconfig includes golden/**) — a regression fails
`tsc`. These toolchain-free checks fold the same invariant into the Python
suite. Regenerate the golden with backends/typescript/scripts/regen-golden.py.
"""

import importlib.util
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_async_extern", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ir():
    return json.loads((BACKEND / "tests" / "fixtures" / "async_http.ir.json")
                      .read_text(encoding="utf-8"))


def test_async_extern_emits_promise_signature_and_awaited_call():
    m = _load_ts_emit()
    out = m.emit(_ir())
    # the extern signature is an async function returning a Promise (was the
    # bare `string` that tsc rejected)
    assert ("export async function http_post(url: string, body: string): "
            "Promise<string> {") in out
    # the author never spells await; the emitter inserts it at the admitted
    # call site inside the async provide method
    assert "return (await http_post(url, body))" in out
    # a sync `function http_post` must NOT survive
    assert "export function http_post(" not in out


def test_async_http_golden_is_current():
    """The checked-in golden matches the emitter, so a stale golden (or an
    emitter change that drops the await) fails here as well as under `tsc`."""
    m = _load_ts_emit()
    golden = (BACKEND / "golden" / "async_http.ts").read_text(encoding="utf-8")
    assert m.emit(_ir()) == golden


def _agent_loop_ir():
    return json.loads((BACKEND / "tests" / "fixtures" / "async_agent_loop.ir.json")
                      .read_text(encoding="utf-8"))


def test_agent_loop_colored_fn_emits_awaited_async_ts():
    """Item 90 exit test — the harness agent-loop shape: a module `fn` that
    funnels an async extern each turn of a bounded recursion, colored async by
    the frontend fixed point. The emitter renders it `async function …:
    Promise<T>` and awaits every async call site — the extern, the recursive
    self-call, and the match-arm arrows. tsc-validated by `npm run typecheck`;
    this folds the same invariant into the Python suite."""
    m = _load_ts_emit()
    out = m.emit(_agent_loop_ir())
    # the colored fn is async and returns a Promise
    assert ("export async function agent_loop(prompt: string, "
            "decode: ((a0: string) => Step), n: bigint): Promise<Step> {") in out
    # the direct async-extern call is awaited
    assert "decode((await model_complete(prompt)))" in out
    # the recursive self-call (a colored callee) is awaited too
    assert "await agent_loop(req.name, decode, revlI64(n - 1n))" in out
    # the match IIFE is async and awaited, because one arm awaits the recursive
    # self-call (else that await would land in a sync arrow, a tsc error).
    # Item 435(a): the colour is decided per rendered body, so the awaiting arm
    # keeps `async` and the sync arm sheds it.
    assert "return (await (async ($revl_match_1) => {" in out
    assert ("return (await (async (req) => ((await agent_loop(req.name, decode, "
            "revlI64(n - 1n)))))($revl_match_1.value))") in out
    assert ('return ((answer) => ({ kind: "Final", value: answer }))'
            "($revl_match_1.value)") in out
    # the sync decoder stays a plain function — not every fn is colored
    assert "export function decode_response(resp: string): Step {" in out


def test_agent_loop_golden_is_current():
    m = _load_ts_emit()
    golden = (BACKEND / "golden" / "async_agent_loop.ts").read_text(encoding="utf-8")
    assert m.emit(_agent_loop_ir()) == golden


def _fn_values_ir():
    return json.loads((BACKEND / "tests" / "fixtures" / "async_fn_values.ir.json")
                      .read_text(encoding="utf-8"))


def test_async_fn_value_callback_emits_awaited_promise_ts():
    """Item 92 exit test (finding #21) — the callback-arrow shape. The async
    color rides the declared function type `(Str) -> Async[Str]`: `agent_loop`
    colors async, its `complete` parameter types as `Promise<T>`, and the call
    site awaits it.

    Item 435(b) narrowed the arrow's own colour. The arrow body here is the
    un-awaited emission Promise `ctx.model.complete(msgs)`, so `async` only
    added a resolution hop over a Promise the body already returns (2 excess
    microtask turns and 2 excess Promise allocations per operation call). The
    arrow now renders plain, which has the identical TS type `(p) =>
    Promise<T>` and so stays assignable to `complete`. tsc-validated by
    `npm run typecheck`."""
    m = _load_ts_emit()
    out = m.emit(_fn_values_ir())
    assert ("export async function agent_loop(current: string, "
            "complete: ((a0: string) => Promise<string>)): Promise<string> {") in out
    assert "const resp = (await complete(current))" in out
    # the callback arrow forwards the emission Promise, so it is NOT async
    assert "((msgs: any) => (ctx.model.complete(msgs)))" in out
    assert "(async (msgs: any) =>" not in out
    assert "(await agent_loop(prompt," in out


def test_async_fn_values_golden_is_current():
    m = _load_ts_emit()
    golden = (BACKEND / "golden" / "async_fn_values.ts").read_text(encoding="utf-8")
    assert m.emit(_fn_values_ir()) == golden


def _match_sync_arms_ir():
    return json.loads((BACKEND / "tests" / "fixtures" / "match_sync_arms.ir.json")
                      .read_text(encoding="utf-8"))


def test_sync_armed_match_in_async_fn_emits_no_async_and_no_await():
    """Item 435(a) exit test: the async colour follows the rendered body.

    `classify` is coloured async (it calls an async extern once, at the top)
    but its match has no suspending arm: every arm body is a bound variable or
    a literal. Colouring the match by `ctx.in_async`, a property of the
    ENCLOSING function, wrapped each arm's already-computed value in a Promise
    and awaited it: 2 excess microtask turns and 4 excess Promise allocations
    per evaluation, measured by
    `bench/codegen/typescript/cases/match_sync_arms.ts`. The IIFE and the arm
    arrows are unchanged in structure; only the keyword and its matching
    `await` are gone.
    """
    m = _load_ts_emit()
    out = m.emit(_match_sync_arms_ir())
    # the enclosing fn is still coloured: the extern call is still awaited
    assert "export async function classify(p: string): Promise<string> {" in out
    assert "const resp = (await fetch_one(p))" in out
    # the match IIFE and every arm arrow are plain
    assert "return (($revl_match_1) => {" in out
    assert "return ((a) => (a))($revl_match_1.value)" in out
    assert "return ((t) => (t))($revl_match_1.value)" in out
    assert "})(decode(resp))" in out
    # nothing inside the match is coloured or awaited
    match_text = out[out.index("return (($revl_match_1) => {"):]
    assert "async " not in match_text
    assert "await " not in match_text
