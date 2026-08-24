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
