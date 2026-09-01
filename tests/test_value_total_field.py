"""The TOTAL (never-raising) FIELD read on a parsed value (roadmap item 368,
stdlib/value.rvl `value_field_or`).

Item 362 gave a total PARSE (`json_try_parse`). This is its missing sibling: a
total FIELD read. On a value parsed off the wire, `o.url` where the body lacks
`url` RAISES on the py tier — `_revl_field` does `dict[name]`, a `KeyError` (the
real HTTP 500 in the harness migration) — while the ts tier silently reads
`undefined` for the same access. The two tiers DISAGREE, and neither is the
`dict.get(k, default)` ("read this field if present, else a default") the
deleted host Python expressed trivially.

`value_field_or(v, name, default) -> Value` closes that. It is the caller-chosen
default read, pure revl over the existing `value_opt` primitive
(`value_opt(v, name) ?? default`): absent (or present-null, or non-record) reads
back as `default`, NEVER a raise, byte-identically on py and ts. A present field
reads back its exact value — the same value a raising `o.url` yields today.

Checked here:
  * the py tier reads an absent field and gets the default (not a raise), and a
    present field byte-identically to the raising `o.url`;
  * the SAME pure-revl source, emitted to the ts tier and run under node, gives
    the byte-identical result — value.rvl is now two-tier (item 368 added its
    @ts bodies), so the read cannot diverge between the tiers;
  * a tolerant "read this field if present" settings/schedule rewrite — the
    three latent raises found on the same wire — as PURE revl over
    `value_field_or`, demonstrating the tolerant-read externs are now deletable
    in-language.
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
            f"emit_field_{backend}", ROOT / "backends" / backend / "emit.py")
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


# ----------------------------------------------- absent field returns default

# The raising typed read (`o.url`) and the total read (`value_field_or`), side by
# side, over the same parsed value.
_READ = r'''
use "stdlib/json.rvl"  { json_parse }
use "stdlib/value.rvl" { value_field_or, value_str }

type Settings = { url: Str }

// today's raising read: a typed field access. `o.url` on a body lacking `url`
// is `_revl_field(o, 'url')` -> `dict['url']` -> KeyError on py (the HTTP 500).
fn raising_url(body: Str) -> Str {
  let o: Settings = json_parse(body)
  return o.url
}

// the total read: absent (or present-null, or non-record) -> the caller's
// default, never a raise. Byte-identical on py + ts.
fn total_url(body: Str, dflt: Str) -> Str {
  let o = json_parse(body)
  return value_str(value_field_or(o, "url", dflt))
}
'''


@pytest.fixture(scope="module")
def read_ir() -> dict:
    return _compile(_READ)


def test_py_absent_field_returns_default_not_raise(read_ir):
    ns = _exec_py(read_ir)
    # present: the total read equals the raising read exactly (byte-compat)
    present = '{"url": "https://example.com"}'
    assert ns["total_url"](present, "<none>") == "https://example.com"
    assert ns["raising_url"](present) == "https://example.com"
    # absent: the total read yields the default, the raising read RAISES
    absent = '{"other": 1}'
    assert ns["total_url"](absent, "<none>") == "<none>"
    with pytest.raises(Exception):
        ns["raising_url"](absent)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_total_read_matches_py_tier(read_ir):
    """The SAME pure-revl source, emitted to ts and run under node, produces the
    byte-identical default-on-absent / value-on-present result. value.rvl is now
    two-tier, so this read cannot diverge between the tiers."""
    ns = _exec_py(read_ir)
    present = '{"url": "https://example.com"}'
    absent = '{"other": 1}'
    for body in (present, absent):
        py_out = ns["total_url"](body, "<none>")
        ts_out = _run_ts(
            read_ir,
            "console.log(JSON.stringify("
            f"total_url({json.dumps(body)}, {json.dumps('<none>')})));")
        assert ts_out == py_out
    assert ns["total_url"](present, "<none>") == "https://example.com"
    assert ns["total_url"](absent, "<none>") == "<none>"


# ------------------------------------ the payoff: tolerant settings/schedule read

# The three latent raises the migration found on the same wire: a partial
# settings POST (a body that carries only the keys the client chose to change)
# and two optional schedule fields (`cron`, `timezone`) that may be absent. The
# deleted host Python read each as `body.get(k, <default>)`; in revl the typed
# `s.retries` / `s.cron` would RAISE on the partial body. Here it is a PURE revl
# fn over `value_field_or` — every read carries its own default, so no key is
# ever mandatory and no read can fault.
_TOLERANT = r'''
use "stdlib/json.rvl"  { json_parse }
use "stdlib/value.rvl" { value_field_or, value_str }

// partial settings POST: url and retries default when the client omits them
fn apply_settings(body: Str) -> Str {
  let o = json_parse(body)
  let url     = value_str(value_field_or(o, "url", "http://localhost"))
  let retries = value_str(value_field_or(o, "retries", "3"))
  return `${url}#${retries}`
}

// optional schedule fields: cron + timezone both tolerate absence
fn schedule_line(body: Str) -> Str {
  let o = json_parse(body)
  let cron = value_str(value_field_or(o, "cron", "@daily"))
  let tz   = value_str(value_field_or(o, "timezone", "UTC"))
  return `${cron} ${tz}`
}
'''


@pytest.fixture(scope="module")
def tolerant_ir() -> dict:
    return _compile(_TOLERANT)


def test_py_tolerant_reads_are_pure_revl(tolerant_ir):
    ns = _exec_py(tolerant_ir)
    # full body: every field present, read straight through
    assert ns["apply_settings"](
        '{"url": "http://x", "retries": "7"}') == "http://x#7"
    # partial POST: retries omitted -> default; no raise
    assert ns["apply_settings"]('{"url": "http://x"}') == "http://x#3"
    # empty body: both default
    assert ns["apply_settings"]("{}") == "http://localhost#3"
    # schedule: both optional fields absent -> both default
    assert ns["schedule_line"]("{}") == "@daily UTC"
    assert ns["schedule_line"](
        '{"cron": "0 0 * * *"}') == "0 0 * * * UTC"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_tolerant_reads_match_py(tolerant_ir):
    ns = _exec_py(tolerant_ir)
    for fn, body in (
        ("apply_settings", '{"url": "http://x"}'),
        ("apply_settings", "{}"),
        ("schedule_line", "{}"),
        ("schedule_line", '{"cron": "0 0 * * *"}'),
    ):
        py_out = ns[fn](body)
        ts_out = _run_ts(
            tolerant_ir,
            f"console.log(JSON.stringify({fn}({json.dumps(body)})));")
        assert ts_out == py_out, (fn, body)
