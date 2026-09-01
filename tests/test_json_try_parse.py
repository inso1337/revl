"""The TOTAL (never-raising) JSON parse (roadmap item 362, stdlib/json.rvl).

`json_parse(s) -> Any` RAISES on invalid input, and revl has no try/catch
(docs/syntax-2.0.md §3.2: "failure is `Result` in pure code"). So before this
item a *tolerant* JSON walk — read a JSONL stream, skip the malformed lines,
keep going — could not be written IN revl: it had to be a classified host
extern whose per-engine extraction was forced into hand-written @py/@ts bodies,
duplicated and free to diverge between tiers (a py/ts divergence in exactly such
a body was caught in the dogfood session that filed this item).

`json_try_parse(s) -> Result[Any, Str]` closes that. It is the total wrapper:
its @py and @ts bodies CALL `json_parse` and catch the parse error into the Err
channel, so

  * the `Ok`-carrying success path is byte-identical to today's `json_parse`
    (same value — the body literally calls it); only the error channel is new;
  * `match json_try_parse(line) { Ok(v) => …, Err(_) => skip }` walks a JSONL
    stream tolerantly in PURE revl, with the classify-extern rule unchanged for
    genuine host power (subprocess stays an extern — this item is only the
    parse).

Checked here:
  * the py tier executes the tolerant walk, skipping interleaved malformed
    lines via `Err(_)`;
  * the SAME pure-revl walk, emitted to the ts tier and run under node, gives
    the byte-identical result — the tier-divergence kill (skipped where `node`
    is absent);
  * byte-compat: `json_try_parse(valid).value` equals `json_parse(valid)`;
  * the emitter defines `Ok`/`Err` for a program that only CALLS a
    Result-returning extern, with no surface `match`/`adt` naming them;
  * `parse_engine_output` — the harness's per-engine final-answer extraction
    (revl-harness src/components/engine_run.rvl) — rewritten as a PURE revl fn
    over `json_try_parse` + the stdlib/value.rvl total accessors, demonstrating
    the duplicated @py/@ts parser bodies are now deletable.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402

# stdlib modules the consumers import; copied beside each consumer fixture so
# the `use "stdlib/…"` path resolves (mirrors test_json_stdlib.py).
_STDLIB = ("json.rvl", "str.rvl", "value.rvl")


def _compile(consumer_src: str) -> dict:
    d = Path(tempfile.mkdtemp())
    (d / "stdlib").mkdir()
    for name in _STDLIB:
        (d / "stdlib" / name).write_text(
            (ROOT / "stdlib" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (d / "consumer.rvl").write_text(consumer_src, encoding="utf-8")
    return compile_files([str(d / "consumer.rvl")])


def _emit(backend: str, ir: dict) -> str:
    path = str(ROOT / "backends" / backend)
    sys.path.insert(0, path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_try_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.emit(ir))
    finally:
        sys.path.remove(path)


def _exec_py(ir: dict) -> dict:
    code = _emit("python", ir)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had, previous = "runtime" in sys.modules, sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(code, "emitted.py", "exec"), ns)  # noqa: S102 — our own emitter
        return ns
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]


def _run_ts(ir: dict, tail: str):
    """Emit the ts tier, append `tail` (which console.logs a JSON line), run it
    under node, and return the parsed last stdout line. node v>=23 strips the TS
    types natively; the emitted `import type { Context } from 'cordis'` is erased
    and the value `import { host } from '../runtime.ts'` is satisfied by a stub."""
    code = _emit("typescript", ir)
    d = Path(tempfile.mkdtemp())
    (d / "runtime.ts").write_text("export const host: any = {};\n", encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    (pkg / "mod.ts").write_text(code + "\n" + tail + "\n", encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        ["node", str(pkg / "mod.ts")], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------- the walk

# A pure-revl tolerant JSONL walk. Each valid record is `{kind, text}`; a line
# that is not JSON lands in `Err(_)` and is skipped. `kind` and `text` are NOT
# revl reserved words, so the kept records are read through typed field access —
# the two-tier path (a reserved-word discriminant like "type" needs the
# stdlib/value.rvl string-keyed accessors instead; see the demonstration below).
_WALK = r'''
use "stdlib/json.rvl" { json_try_parse }

type Msg = { kind: Str, text: Str }

fn kept_texts(raw: Str) -> List[Str] {
  var out: List[Str] = []
  for (line of raw.split(nl())) {
    out = match json_try_parse(line) {
      Ok(v)  => keep(out, v),
      Err(_) => out,
    }
  }
  return out
}

fn keep(out: List[Str], v: Msg) -> List[Str] {
  if (v.kind == "msg") { return out.push(v.text) }
  return out
}

fn nl() -> Str { return `
` }
'''

# A stream with malformed lines interleaved between the good ones: a non-JSON
# line, a blank line, a truncated object, and a well-formed record whose `kind`
# excludes it. The tolerant walk keeps exactly alpha/beta/gamma.
_STREAM = "\n".join([
    '{"kind":"msg","text":"alpha"}',
    "this is not json",
    "",
    '{"kind":"noise","text":"dropped-by-kind"}',
    '{"kind":"msg","text":"beta"}',
    "{ truncated",
    '{"kind":"msg","text":"gamma"}',
])
_EXPECT = ["alpha", "beta", "gamma"]


@pytest.fixture(scope="module")
def walk_ir() -> dict:
    return _compile(_WALK)


def test_py_walk_skips_malformed_lines(walk_ir):
    ns = _exec_py(walk_ir)
    assert ns["kept_texts"](_STREAM) == _EXPECT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_walk_matches_py_tier(walk_ir):
    """The SAME pure-revl source, emitted to ts and run under node, produces the
    byte-identical kept list. One revl source can no longer diverge between the
    tiers the way two hand-written extern bodies could."""
    ns = _exec_py(walk_ir)
    py_out = ns["kept_texts"](_STREAM)
    ts_out = _run_ts(walk_ir,
                     f"console.log(JSON.stringify(kept_texts({json.dumps(_STREAM)})));")
    assert ts_out == py_out == _EXPECT


# ------------------------------------------------------- byte-compat + Err

_PARSE_PAIR = r'''
use "stdlib/json.rvl" { json_parse, json_try_parse }

// the raising parse and the total parse, side by side, for the byte-compat proof
fn raising(s: Str) -> Any { return json_parse(s) }

fn is_ok(s: Str) -> Bool {
  return match json_try_parse(s) { Ok(_) => true, Err(_) => false }
}

// the Ok payload, recovered into a typed record — the same value the raising
// parse yields (this body calls json_parse), so the success path is byte-equal
fn ok_name(s: Str) -> Str {
  return match json_try_parse(s) {
    Ok(v) => name_of(v),
    Err(_) => "<err>",
  }
}

type Named = { name: Str }
fn name_of(v: Named) -> Str { return v.name }
'''


@pytest.fixture(scope="module")
def pair_ir() -> dict:
    return _compile(_PARSE_PAIR)


def test_py_ok_path_is_byte_identical_to_raising_parse(pair_ir):
    ns = _exec_py(pair_ir)
    doc = '{"name": "get_weather", "extra": [1, 2, 3]}'
    # the total parse's Ok payload equals the raising parse's value exactly
    assert ns["ok_name"](doc) == "get_weather"
    assert ns["raising"](doc) == json.loads(doc)
    assert ns["is_ok"](doc) is True


def test_py_invalid_input_is_err_not_raise(pair_ir):
    ns = _exec_py(pair_ir)
    for bad in ("", "not json", "{ truncated", "[1, 2", '{"a":}'):
        assert ns["is_ok"](bad) is False, bad
    # and the raising parse still raises on the same input — the contrast the
    # item turns into the Err channel (only the error path changed)
    with pytest.raises(Exception):
        ns["raising"]("not json")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_ok_err_channels_match_py(pair_ir):
    ns = _exec_py(pair_ir)
    doc = '{"name": "get_weather", "extra": [1, 2, 3]}'
    assert _run_ts(pair_ir, f"console.log(JSON.stringify(ok_name({json.dumps(doc)})));") \
        == ns["ok_name"](doc) == "get_weather"
    assert _run_ts(pair_ir, f"console.log(JSON.stringify(is_ok({json.dumps(doc)})));") is True
    assert _run_ts(pair_ir, 'console.log(JSON.stringify(is_ok("not json")));') is False


# ------------------------------------------------- the Ok/Err emit gate

def test_result_returning_extern_emits_ok_err_classes():
    """A program that only CALLS a Result-returning extern — with no surface
    `match` or `adt` naming Ok/Err — still gets the built-in Result classes
    emitted, so the extern body's `Ok(..)`/`Err(..)` resolve. Regression for the
    `_uses_builtin_result` gate extension (item 362)."""
    ir = _compile(
        'use "stdlib/json.rvl" { json_try_parse }\n'
        "fn passthrough(s: Str) -> Result[Any, Str] { return json_try_parse(s) }\n")
    py = _emit("python", ir)
    assert "class Ok:" in py
    assert "class Err:" in py


# ----------------------------------------- the payoff: parse_engine_output

# The harness's per-engine final-answer extraction (revl-harness
# src/components/engine_run.rvl `parse_engine_output`) is a classified extern
# with duplicated @py and @ts bodies, FORCED into host code purely because the
# raising json_parse cannot walk a JSONL stream tolerantly in revl. Here it is a
# PURE revl fn: the tolerant parse is `json_try_parse` (item 362), and the
# per-engine field reads are the stdlib/value.rvl total accessors (a missing key
# reads back as a null Value, never a fault) — keyed by the reserved-word
# discriminant "type" that a typed record field cannot spell. The @py/@ts parser
# bodies (and their tier-divergence surface) are no longer needed.
_ENGINE = r'''
use "stdlib/json.rvl"  { json_try_parse }
use "stdlib/str.rvl"   { trim }
use "stdlib/value.rvl" { value_field, value_str }

fn nl() -> Str { return `
` }

fn parse_engine_output(engine: Str, raw: Str) -> Str {
  if (engine == "plain") { return trim(raw) }
  var last = ""
  for (line of raw.split(nl())) {
    let t = trim(line)
    if (t.length() > 0) {
      last = match json_try_parse(t) {
        Ok(v)  => extract(engine, v, last),
        Err(_) => last,
      }
    }
  }
  return last
}

fn extract(engine: Str, v: Value, last: Str) -> Str {
  let ty = value_str(value_field(v, "type"))
  if (engine == "claude") {
    if (ty == "result") {
      let r = trim(value_str(value_field(v, "result")))
      if (r.length() > 0) { return r }
    }
    return last
  }
  if (engine == "opencode") {
    if (ty == "text") {
      let txt = trim(value_str(value_field(value_field(v, "part"), "text")))
      if (txt.length() > 0) { return txt }
    }
    return last
  }
  if (engine == "droid") {
    if (ty == "completion") {
      let ft = trim(value_str(value_field(v, "finalText")))
      if (ft.length() > 0) { return ft }
    }
    return last
  }
  return last
}
'''


@pytest.fixture(scope="module")
def engine_ir() -> dict:
    return _compile(_ENGINE)


def test_parse_engine_output_is_pure_revl(engine_ir):
    """Per-engine extraction with malformed lines interleaved, all skipped by
    `json_try_parse`'s Err channel — the classified @py/@ts extern is now a pure
    revl fn (stdlib/value.rvl executes on the py tier today; its ts bodies are
    the module's own deferred follow-up, at which point this same source is
    two-tier)."""
    ns = _exec_py(engine_ir)
    peo = ns["parse_engine_output"]

    # plain: passthrough floor (trimmed), never touches JSON
    assert peo("plain", "  the answer  ") == "the answer"

    # claude: the last non-empty `result` on a `type == "result"` event wins;
    # the leading `system` line and the GARBAGE line are both skipped
    claude = "\n".join([
        '{"type":"system","subtype":"init"}',
        "GARBAGE not json",
        '{"type":"result","result":"  final claude answer  "}',
    ])
    assert peo("claude", claude) == "final claude answer"

    # opencode: the LAST `text` event's part.text
    opencode = "\n".join([
        '{"type":"text","part":{"text":"draft"}}',
        '{"type":"text","part":{"text":"final opencode"}}',
    ])
    assert peo("opencode", opencode) == "final opencode"

    # droid: the completion event's finalText
    assert peo("droid", '{"type":"completion","finalText":"droid done"}') == "droid done"

    # an unknown engine, and a stream of only malformed lines, both yield ""
    assert peo("nope", claude) == ""
    assert peo("claude", "not json\n\n{ broken") == ""
