#!/usr/bin/env python3
"""bench_agentic3.py -- bench_agentic2_cap2.py for external OpenAI-compatible endpoints.

Changes against bench_agentic2_cap2.py (everything else is unchanged):
  * endpoint/model/key from BENCH_BASE_URL, BENCH_API_KEY, BENCH_MODEL; the key is
    only ever read from the environment and scrubbed from anything printed/written;
  * chat_template_kwargs is sent only when BENCH_OMLX=1; BENCH_TEMPERATURE (0.0);
  * the model call runs in a thread with a wall timeout (BENCH_CALL_TIMEOUT, 600 s),
    retries 429/5xx with backoff, and a failed call is recorded, not fatal;
  * hatch_calls (revl_verbs with `name`) and lookup_calls (revl_verbs with `names`)
    are separate; export_calls counts revl_export direct or through the hatch;
  * every row records model, base-URL host, workdir, sha256 of the exact system
    prompt, tool cap, per-call out_len/cut, provider usage and per-turn latency.
Single-run mode: --workdir DIR --out FILE --arm-src FILE [--arm-var NAME].

Original docstring follows.

bench_agentic2.py -- the two experiments the revl developer assigned.

    EXP A  scaffold-first  vs  free generation
    EXP B  preflight-mandatory  vs  free order

Why these two
-------------
`bench_agentic.py` (R10) measured the CHANGE task on a realm-scoped composition
and produced a result that is NOT a language result:

    revl-mcp       8 calls, all READ-ONLY, file never touched, break_set False
    revl-mcp-sees  9 revl_edit calls, every one refused, break_set False
    revl-nomcp     read/grep/write, break_set True
    ts-tools       read/grep/write, break_set True

The MCP arm lost because it has NO mutation path to a file-backed composition:

    revl_edit on a files-loaded composition  -> refused (P0/R13a; PR #1705)
    revl_swap(source=<realm source>)         -> refused (G9, untrusted profile
                                                forbids naming a realm)
    no file-write verb in the MCP surface

So every arm in this bench carries the SAME I/O tools (read_file, write_file,
grep) PLUS the MCP verbs, and the arms differ ONLY in the guidance under test.
That isolates the loop from the tool inventory -- which is the whole point of
the two experiments.

EXP A -- scaffold-first vs free generation
------------------------------------------
Task: author a NEW component. Both arms can act; only the loop differs.

    gen-free      write the component yourself, then load/swap it in
    gen-scaffold  call revl_scaffold FIRST, get the typed skeleton, load the
                  base inline, edit the skeleton in, fill each hole by line

The scaffold path is reachable only because the base composition names no
realm: inline `revl_load` is refused by G9 for any source that names one, and
`revl_edit` only patches an inline-loaded buffer. Both facts are measured in
`probe_doors.py`; the loop is closed end-to-end in `probe_loop4.py`, where an
inserted hole comes back as `{"line": 12, "expected": "Unit", "code": "T3"}`.

The predicted win is on WRITE tokens: the scaffold arm sends hole fills
(small) where the free arm sends a whole component (large), and output is
decode-bound.

EXP B -- preflight-mandatory
----------------------------
Task: the R10 withdrawal task. `revl_query_withdraw` is an EXACT proof over the
linked graph and no other language has it. Addendum 12 measured the agent using
it unprompted on turns 0-2; EXP B asks what changes when it is mandatory BEFORE
any edit, and whether the mandate converts a wrong answer into a right one.

Metrics that only this bench can produce
----------------------------------------
    preflight_before_edit   did a read-only proof run before the first mutation
    write_tokens            output tokens spent on mutating calls
    admitted                did the change actually enter a running composition
    break_set_exact         the R10 score

Run
---
    python3 bench_agentic2.py --exp scaffold
    python3 bench_agentic2.py --exp preflight
    python3 bench_agentic2.py --exp both --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import socket
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_URL = os.environ.get("BENCH_BASE_URL", "")
MODEL = os.environ.get("BENCH_MODEL", "")
TEMPERATURE = float(os.environ.get("BENCH_TEMPERATURE", "0.0"))
SEND_TEMPLATE_KWARGS = os.environ.get("BENCH_OMLX", "") == "1"
# Wall bound for ONE model call (including its retries).
MODEL_CALL_TIMEOUT = float(os.environ.get("BENCH_CALL_TIMEOUT", "600"))
MODEL_RETRIES = int(os.environ.get("BENCH_RETRIES", "6"))
# Env vars that carry secrets: never forwarded to children, never printed/written.
SECRET_VARS = ("BENCH_API_KEY", "KIRA_API_KEY", "OPENROUTER_API_KEY")


def _scrub(text):
    """Replace every secret value present in the environment. Applied to anything
    that is printed or written."""
    text = str(text)
    for v in SECRET_VARS:
        val = os.environ.get(v)
        if val and len(val) >= 4:
            text = text.replace(val, "***")
    return text


def _child_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_VARS}
    env.update(extra)
    return env


def chat_url():
    u = BASE_URL.rstrip("/")
    if not u.endswith("/chat/completions"):
        u += "/chat/completions"
    return u


def base_host():
    from urllib.parse import urlparse
    return urlparse(BASE_URL).netloc.split("@")[-1]


class ModelCallError(RuntimeError):
    def __init__(self, msg, status=None, kind="error"):
        super().__init__(msg)
        self.status = status
        self.kind = kind  # "timeout" | "http" | "rate_limit" | "error"
REVL_REPO = Path(os.environ.get("REVL_REPO", str(HERE.parents[2]))).resolve()
# Absolute on purpose: _compile runs `sys.executable -m revl` with cwd=REVL_REPO
# and PYTHONPATH=MCP_PATH; a relative REVL_REPO silently breaks module
# resolution and zeroes every compiles/done metric (see SPEC §16.13).
MCP_PYTHON = os.environ.get(
    "REVL_MCP_PYTHON",
    str(REVL_REPO / "backends" / "python" / ".venv" / "bin" / "python"),
)
# The interpreter that carries `cordis` need not carry `mcp` (the server venv is
# rebuilt by setup.sh, which drops it). Extra import dirs for the server process.
_extra = os.environ.get("REVL_EXTRA_PATH", str(HERE / ".mcpdeps"))
MCP_PATH = os.pathsep.join(
    [str(REVL_REPO / "src")] + ([_extra] if _extra and Path(_extra).is_dir() else [])
)
# A single MCP verb that never returns means the stdio server wedged (seen when
# the box is oversubscribed). Bound it so the run reports instead of hanging.
CALL_TIMEOUT = int(os.environ.get("REVL_CALL_TIMEOUT", "600"))
# An arm that wedges must become a recorded error, never an infinite hang: the
# event loop's own teardown waits forever on a leaked stdio task, so a stall
# used to swallow the exception that caused it.
ARM_TIMEOUT = int(os.environ.get("REVL_ARM_TIMEOUT", "2700"))
# The tool-result cap. 6000 cut the surface's export note (SPEC 16.22).
TOOL_CAP = int(os.environ.get("BENCH_CAP", "24000"))

# Calls that change the world, for the preflight-order metric.
MUTATING = {"write_file", "revl_edit", "revl_swap", "revl_load", "revl_undo",
            "revl_rollback", "revl_step_back", "revl_commit_confirm", "revl_fork_confirm"}
# Calls that are proofs/reads, for the same metric.
PREFLIGHT = {"revl_query_withdraw", "revl_query_dependents", "revl_query_reach",
             "revl_query_emitters", "revl_query_drift", "revl_check", "revl_audit",
             "revl_plan", "read_file", "grep"}


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
def _usage_extract(u):
    """Provider usage, normalised. Cached tokens are reported under different
    names by different providers; take the first one present."""
    u = u or {}
    ptd = u.get("prompt_tokens_details") or {}
    cached = None
    for v in (ptd.get("cached_tokens"), u.get("cached_tokens"),
              u.get("prompt_cache_hit_tokens"), u.get("cache_read_input_tokens")):
        if isinstance(v, (int, float)):
            cached = int(v)
            break
    return cached


def _model_call_sync(messages, tools, max_tokens, deadline):
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "stream": False,
    }
    if SEND_TEMPLATE_KWARGS:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("BENCH_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode()
    last = None
    t_all = time.time()
    for attempt in range(MODEL_RETRIES + 1):
        remaining = deadline - time.time()
        if remaining <= 1:
            raise ModelCallError(f"call deadline reached after {attempt} attempts; last: {last}",
                                 kind="timeout")
        req = urllib.request.Request(chat_url(), data=data, headers=headers)
        t0 = time.time()
        retry_after = None
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                raw = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                pass
            last = _scrub(f"HTTP {e.code}: {txt}")
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    retry_after = min(float(ra), 60.0) if ra else None
                except ValueError:
                    retry_after = None
                if attempt >= MODEL_RETRIES:
                    raise ModelCallError(f"gave up after {attempt + 1} attempts: {last}",
                                         status=e.code,
                                         kind="rate_limit" if e.code == 429 else "http")
            else:
                raise ModelCallError(last, status=e.code, kind="http")
        except (TimeoutError, socket.timeout) as e:
            raise ModelCallError(f"timeout: {e}", kind="timeout")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                raise ModelCallError(f"timeout: {e.reason}", kind="timeout")
            last = _scrub(f"URLError: {e.reason}")
            if attempt >= MODEL_RETRIES:
                raise ModelCallError(last, kind="error")
        else:
            if "choices" in raw and raw["choices"]:
                wall = time.time() - t0
                u = raw.get("usage") or {}
                cached = _usage_extract(u)
                pt = u.get("prompt_tokens", 0) or 0
                return {
                    "message": raw["choices"][0]["message"],
                    "finish_reason": raw["choices"][0].get("finish_reason"),
                    "wall": wall,
                    "wall_with_retries": time.time() - t_all,
                    "attempts": attempt + 1,
                    "prompt_tokens": pt,
                    "completion_tokens": u.get("completion_tokens", 0) or 0,
                    "cached_tokens": cached,
                    "new_prefill_tokens": max(0, pt - (cached or 0)),
                    "usage_raw": u,
                }
            # HTTP 200 with {"error": ...} and no choices (oMLX, some gateways)
            err = raw.get("error", raw)
            last = _scrub(err.get("message") if isinstance(err, dict) else str(err))
            code = err.get("code") if isinstance(err, dict) else None
            if attempt >= MODEL_RETRIES:
                raise ModelCallError(f"no choices after {attempt + 1} attempts: {last}",
                                     status=code if isinstance(code, int) else None,
                                     kind="rate_limit" if code == 429 else "error")
        sleep = retry_after if retry_after is not None else min(2 ** (attempt + 1), 30)
        time.sleep(min(sleep, max(0.0, deadline - time.time() - 1)))
    raise ModelCallError(f"exhausted retries: {last}")


async def model_call(messages, tools=None, max_tokens=1400):
    """One model call in a worker thread under a wall timeout. A stalled request
    becomes a ModelCallError, not a hang."""
    deadline = time.time() + MODEL_CALL_TIMEOUT
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_model_call_sync, messages, tools, max_tokens, deadline),
            timeout=MODEL_CALL_TIMEOUT + 5)
    except asyncio.TimeoutError:
        raise ModelCallError(f"no response in {MODEL_CALL_TIMEOUT:.0f}s", kind="timeout")


# --------------------------------------------------------------------------
# MCP bridge
# --------------------------------------------------------------------------
class MCPBridge:
    def __init__(self, root: Path, extra_args=None):
        self.root = root
        self.extra_args = extra_args or []
        self.session = None
        self._stack = None
        self.tools = []

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=MCP_PYTHON,
            args=["-m", "revl", "mcp", "serve", "--root", str(self.root),
                  *self.extra_args],
            env=_child_env(PYTHONPATH=MCP_PATH),
        )
        self._stack = stdio_client(params)
        read, write = await self._stack.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()
        self.tools = list((await self.session.list_tools()).tools)
        return self

    async def __aexit__(self, *exc):
        # Teardown can deadlock if the child never sees EOF; bound it so a
        # failure upstream is reported rather than swallowed by the exit path.
        async def _close():
            try:
                if self.session is not None:
                    await self.session.__aexit__(*exc)
            finally:
                if self._stack is not None:
                    await self._stack.__aexit__(*exc)
        try:
            await asyncio.wait_for(_close(), timeout=30)
        except (asyncio.TimeoutError, Exception):
            if self._stack is not None:
                try:
                    await asyncio.wait_for(self._stack.__aexit__(*exc), timeout=10)
                except Exception:
                    pass

    def openai_schemas(self):
        # `mcp` renamed Tool.inputSchema -> input_schema; accept either so a
        # newer SDK does not turn every arm into an AttributeError.
        out = []
        for t in self.tools:
            schema = (getattr(t, "input_schema", None)
                      or getattr(t, "inputSchema", None)
                      or {"type": "object", "properties": {}})
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "")[:1024],
                    "parameters": schema,
                },
            })
        return out

    async def call(self, name, args):
        # A wedged stdio server must name itself, not hang the whole bench.
        try:
            res = await asyncio.wait_for(
                self.session.call_tool(name, args or {}), timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{name}: no response in {CALL_TIMEOUT}s (server wedged)")
        return "\n".join(getattr(c, "text", None) or str(c)
                         for c in (res.content or []))


# --------------------------------------------------------------------------
# file tools -- every arm gets these, so the arms differ only in guidance
# --------------------------------------------------------------------------
FILE_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file in the working directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Overwrite a file with new content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Regex search across files in the working directory.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
]


def run_file_tool(root: Path, name, args):
    if name == "read_file":
        p = (root / args.get("path", "")).resolve()
        if not str(p).startswith(str(root.resolve())):
            return "refused: outside root"
        return p.read_text(encoding="utf-8") if p.exists() else f"not found: {args.get('path')}"
    if name == "write_file":
        p = (root / args.get("path", "")).resolve()
        if not str(p).startswith(str(root.resolve())):
            return "refused: outside root"
        p.write_text(args.get("content", ""), encoding="utf-8")
        return f"wrote {len(args.get('content',''))} bytes to {args.get('path')}"
    if name == "grep":
        pat = re.compile(args.get("pattern", ""), re.I)
        # The schema declares `path`; honour it instead of silently searching
        # the whole root. Relative paths resolve against root; an absolute
        # path must still land inside root (same contract as read_file).
        root_r = root.resolve()
        base = root_r
        if args.get("path"):
            base = (root_r / args["path"]).resolve()
            if not str(base).startswith(str(root_r)):
                return "refused: outside root"
        hits = []
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for f in files:
            if f.is_file():
                try:
                    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if pat.search(line):
                            hits.append(f"{f.relative_to(root_r)}:{i}: {line.strip()[:120]}")
                except Exception:
                    pass
        return "\n".join(hits[:80]) or "no matches"
    return f"unknown tool {name}"


# --------------------------------------------------------------------------
# EXP B -- the withdrawal task (identical to R10, so scores are comparable)
# --------------------------------------------------------------------------
TENANTS = REVL_REPO / "examples" / "tenants.rvl"
TRUTH = {"breaks": ["TenantAApp"], "lost_keys": ["kv"], "orphaned": ["kv@tenant_a"],
         "survivors": ["TenantBApp", "TenantBStore"]}
SURVIVOR_MARKERS = ["TenantBApp", "TenantBStore"]

TASK_WITHDRAW = (
    "The working directory contains tenants.rvl. Withdraw the component "
    "`TenantAStore` from it and fix everything that breaks as a result. "
    "When you are done, state exactly which components broke and why, and "
    "confirm which components were unaffected."
)

SYS_BASE = (
    "The composition under change is at {path} . "
    "revl_check and the revl_query_* tools require it explicitly as "
    'files: ["{path}"] . '
    "You are a coding agent working on a revl composition. revl is a language "
    "where provision is realm-scoped: two components may both provide `kv` as "
    "long as they are isolated in different realms. Use the provided tools to "
    "inspect the composition before you change it, and to verify your change "
    "after. Prefer asking the compiler over guessing."
)

SYS_PREFLIGHT = SYS_BASE + (
    "\n\nMANDATORY PREFLIGHT: before you make ANY edit or write, you MUST first "
    "call revl_query_withdraw to obtain the exact, compiler-derived blast radius "
    "of the withdrawal, and state the complete break set in your reply. Do not "
    "modify anything until that proof has run. The proof is exact; a guess is not."
)


def score_withdraw(root: Path, final_text, usage, order):
    final = (root / "tenants.rvl").read_text(encoding="utf-8") if (root / "tenants.rvl").exists() else ""
    named_breaks = all(b in final_text for b in TRUTH["breaks"])
    named_cause = (any(k in final_text for k in TRUTH["lost_keys"])
                   or any(o in final_text for o in TRUTH["orphaned"]))
    code = re.sub(r"//[^\n]*", "", final)
    survivors_present = all(s in code for s in SURVIVOR_MARKERS)
    survivors_intact = ("quota: 9" in code) and ("tenant_b" in code)
    # word-boundary, not substring: `component TenantAStoreV2` contains
    # `component TenantAStore` and must not read as "not withdrawn".
    withdrew = re.search(r"\bcomponent\s+TenantAStore\b", code) is None
    compiles, compile_err = _compile(root / "tenants.rvl")
    exact = bool(withdrew and named_breaks and named_cause
                 and survivors_present and survivors_intact and compiles)
    return {
        "break_set_exact": exact,
        "withdrew": withdrew,
        "named_breaks": named_breaks,
        "named_cause": named_cause,
        "survivors_present": survivors_present,
        "survivors_intact": survivors_intact,
        "compiles": compiles,
        "compile_err": "" if compiles else compile_err,
        **order,
        **_usage_fields(usage),
    }


# --------------------------------------------------------------------------
# EXP A -- the authoring task
# --------------------------------------------------------------------------
BASE_PLAIN = """service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
}

component Store provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
  }
}
"""

TASK_AUTHOR = (
    "The working directory contains plain.rvl, a revl composition. Add a NEW "
    "component `AuditLog` that provides a new service `Audit` with one method "
    "`fn record(msg: Str) -> Str` (it stores the message and returns the key it "
    "stored it under). `AuditLog` must follow the same acquire/undo "
    "discipline as `Store` in that file (an `effect ... undo ...` resource). "
    "Leave `Store` exactly as it is. When you are done, the composition must "
    "compile and the new component must be present in plain.rvl ON DISK — "
    "check the file itself, not just the running session."
)

SYS_AUTHOR = (
    "The composition is at {path} . "
    "You are a coding agent working on a revl composition. You may edit files "
    "directly, or drive the revl MCP tools. The revl tools that matter here:\n"
    "  revl_check  -- compiles a source/files and lists typed-hole obligations\n"
    "  revl_load   -- boots a composition IN MEMORY (source= inline, or files=)\n"
    "  revl_edit   -- patches the SERVER-SIDE source of a loaded composition;\n"
    "                 fill a hole with {{\"hole\": <line>, \"expr\": \"<fill>\"}}\n"
    "  revl_swap   -- admits and hot-swaps a candidate in\n"
    "  revl_verbs  -- with {{\"name\": ..., \"args\": {{...}}}} it CALLS that verb.\n"
    "                 Use it for any verb not listed here, e.g.\n"
    "                 {{\"name\": \"revl_state\", \"args\": {{}}}} to see what is loaded,\n"
    "                 or {{\"name\": \"revl_export\", \"args\": {{}}}} to write the held\n"
    "                 source out.\n"
    "  revl_edit and revl_load work on the SERVER'S HELD source, not the file. "
    "The held source is the source of truth and disk is an export: until you "
    "call revl_export, {path} on disk still holds the OLD bytes. The task is "
    "not done until {path} on disk contains your change.\n"
    "How to VERIFY the composition you are holding:\n"
    "  * `revl_swap` with no arguments admits the HELD candidate against the "
    "running composition and reports `admitted`. This is the check that works "
    "on a held composition.\n"
    "  * `revl_check` with NO `source` does NOT check the held composition: it "
    "returns `ok: false` with \"ValueError: provide `source` or `files`\". That "
    "`ok: false` means you forgot an argument -- it does NOT mean your code is "
    "broken. Do not change correct code because of it.\n"
    "  * `revl_check` WITH `source` replaces the held composition with what you "
    "sent, so only send a source you mean to become the held one.\n"
    "Prefer asking the compiler over guessing."
)

SYS_SCAFFOLD = SYS_AUTHOR + (
    "\n\nMANDATORY SCAFFOLD-FIRST: do NOT write the component yourself. First "
    "call revl_scaffold with the service, component name, provision key, "
    "resource type and methods for `AuditLog`. It returns a well-typed skeleton "
    "whose unknown expressions are already typed holes, each with a fill spec. "
    "Then put that skeleton into the composition and fill each hole one at a "
    "time, sending only the fill expression. Do not re-send the whole file.\n"
    "Four things about revl_scaffold's arguments and reply:\n"
    "  * `methods` entries are `name(param: Type, ...) -> Return` -- the "
    "language's `fn` keyword is NOT part of this argument.\n"
    "  * `resource` is the TYPE of the resource binding, i.e. the type of the "
    "service this component manages -- give the bare type name (the service "
    "type declared in the file, e.g. `Kv`). The host family that actually "
    "satisfies it is chosen later, by the fill. This revision refuses an "
    "applied type here: `Map[Str, Str]` -> \"a resource type must be an "
    "identifier\". If you see that, drop to the bare type name.\n"
    "  * `unfillable` in the reply can be a FALSE NEGATIVE on this revision. A "
    "resource hole reported `unfillable` is normally still fillable with the "
    "host family's constructor (`Map.new()`). Do not stop, and do not ask a "
    "human for host code: try the fill first.\n"
    "  * For each hole, take the fill from `fillSpec.fillable.producers[].write` "
    "when it is present -- those are expressions valid in YOUR component. "
    "`fillSpec.idiom.fill` is the EXAMPLE component's own expression and names "
    "its bindings (`store`, `db`, `key`), which do not exist in your component; "
    "submitting it is refused.\n"
    "  * The scaffold names the resource binding `resource`. If a hole asks for "
    "`Unit` and has no producer write (that is the release hole), the fill is "
    "`resource.drop()` -- NOT the `idiom.fill` value, which names the example "
    "component's own binding."
)

AUTHOR_MARKERS = ["component AuditLog", "Audit", "record", "effect", "undo"]


def _extract_component(code, name):
    """The full text of `component <name> ... { ... }`, brace-matched."""
    m = re.search(rf"\bcomponent\s+{re.escape(name)}\b[^{{]*\{{", code)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(code):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"\s+", " ", code[m.start():i + 1]).strip()
        i += 1
    return None


def _provenance(arm_prompt: str | None = None):
    """A result that does not name the revision it measured is not reproducible.

    The trees this bench runs against are extracted copies (no .git), so `git
    rev-parse` is not always available. Fall back to a content hash of
    `src/revl`: that is the identity that actually determines behaviour, and it
    distinguishes two extractions of the same revision only if they differ.
    """
    import hashlib, subprocess
    rev, dirty = None, False
    try:
        rev = subprocess.run(["git", "-C", str(REVL_REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "-C", str(REVL_REPO), "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        pass
    src_sha = "?"
    pkg = REVL_REPO / "src" / "revl"
    if pkg.is_dir():
        h = hashlib.sha256()
        for p in sorted(pkg.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(str(p.relative_to(pkg)).encode())
            h.update(p.read_bytes())
        src_sha = h.hexdigest()[:12]
    return {
        "revl_repo": str(REVL_REPO),
        "revl_tree": REVL_REPO.name,
        "revl_rev": (rev + ("+dirty" if dirty else "")) if rev else f"src-sha:{src_sha}",
        "revl_src_sha": src_sha,
        "model": MODEL,
        # Hash the prompt the arm ACTUALLY ran with. Hashing SYS_AUTHOR alone
        # let two arms with different instructions carry the same prompt_sha,
        # so a result could not name the instructions it measured.
        "prompt_sha": hashlib.sha256(
            ((arm_prompt or SYS_AUTHOR) + TASK_AUTHOR).encode()).hexdigest()[:12],
        "arm_prompt_sha": hashlib.sha256(
            (arm_prompt or "").encode()).hexdigest()[:12] if arm_prompt else None,
    }


def score_author(root: Path, final_text, usage, order, mcp_state, arm_prompt=None):
    final = (root / "plain.rvl").read_text(encoding="utf-8") if (root / "plain.rvl").exists() else ""
    code = re.sub(r"//[^\n]*", "", final)
    has_component = "component AuditLog" in code
    has_service = bool(re.search(r"service\s+Audit\b", code))
    has_record = bool(re.search(r"fn\s+record\b", code))
    has_effect = ("effect" in code and "undo" in code)
    # `Store` must be untouched: compare its actual block against the base,
    # not merely check that the words appear somewhere in the file.
    store_intact = _extract_component(code, "Store") == _extract_component(BASE_PLAIN, "Store")
    compiles, compile_err = _compile(root / "plain.rvl")
    admitted = bool(mcp_state.get("loaded")) and any(
        c.get("name") == "AuditLog" for c in (mcp_state.get("components") or []))
    complete = bool(has_component and has_service and has_record and has_effect
                    and store_intact and compiles)
    return {
        "task_complete": complete,
        "admitted": admitted,
        "has_component": has_component,
        "has_service": has_service,
        "has_record": has_record,
        "has_effect_undo": has_effect,
        "store_intact": store_intact,
        "compiles": compiles,
        "compile_err": "" if compiles else compile_err,
        # §10's metric contract: the outcome is the artifact on disk, compared
        # byte-for-byte against the base. An arm that "looks cheap" (few tokens,
        # few calls) while leaving the file untouched is a FAILURE, not a win.
        "artifact_bytes": len(final.encode()),
        "artifact_changed": final != BASE_PLAIN,
        "artifact_ok": bool(final != BASE_PLAIN and has_component and compiles),
        **_provenance(arm_prompt),
        **order,
        **_usage_fields(usage),
    }


# --------------------------------------------------------------------------
def _compile(path: Path):
    if not path.exists():
        return None, "missing file"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "revl", "compile", str(path)],
            capture_output=True, text=True, timeout=180,
            env=_child_env(PYTHONPATH=MCP_PATH),
            cwd=str(REVL_REPO),
        )
        return r.returncode == 0, (r.stderr or r.stdout or "")[:400]
    except Exception as e:
        return None, str(e)


def _usage_fields(usage):
    return {
        "tool_calls": usage["tool_calls"],
        "turns": usage["turns"],
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
        "tokens_new_prefill": usage["tokens_new_prefill"],
        "write_tokens": usage["write_tokens"],
        "hatch_calls": usage.get("hatch_calls", 0),
        "lookup_calls": usage.get("lookup_calls", 0),
        "tokens_cached": usage.get("tokens_cached"),
        "wall_s": round(usage["wall"], 1),
    }


def _is_committed(out):
    """revl_change / revl_commit report `committed: true` when the change landed
    in the session (not on disk). Parsed from the FULL result, before the cap."""
    try:
        obj = json.loads(str(out))
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("committed") is True


def _is_refusal(out):
    """Did the surface refuse this call? A spiral is a run of refusals, and a
    run of identical calls that all SUCCEEDED is a different thing (the agent
    re-reading, not the surface stonewalling). Without this the two are the
    same row in the call log."""
    s = str(out)
    if s.startswith("ERROR:"):
        return True
    try:
        obj = json.loads(s)
    except Exception:
        return False
    if isinstance(obj, dict) and obj.get("ok") is False:
        return True
    if isinstance(obj, dict) and obj.get("diagnostics"):
        return any(d.get("severity") in ("error", "refused")
                   for d in obj["diagnostics"] if isinstance(d, dict))
    return False


def _refusal_reason(out):
    """Why the surface refused. A retry spiral is only diagnosable if the log
    carries the reason: three identical `revl_edit` calls tell you the agent
    repeated itself, but not whether the surface said the same thing three
    times (a dead end) or three different things (a moving target)."""
    s = str(out)
    if s.startswith("ERROR:"):
        return s[:300]
    try:
        obj = json.loads(s)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    diags = obj.get("diagnostics") or []
    msgs = [d.get("message", "") for d in diags if isinstance(d, dict)]
    msgs = [m for m in msgs if m]
    if msgs:
        return msgs[0][:300]
    for key in ("error", "message", "hint"):
        if obj.get(key):
            return str(obj[key])[:300]
    return ""


def _order_metrics(call_log):
    """Did a proof/read run before the first mutation? How many turns to it?"""
    first_mut = None
    first_pre = None
    preflight_before_edit = False
    for i, (name, _) in enumerate(call_log):
        if name in PREFLIGHT and first_pre is None:
            first_pre = i
        if name in MUTATING and first_mut is None:
            first_mut = i
            preflight_before_edit = first_pre is not None
            break
    if first_mut is None:
        preflight_before_edit = first_pre is not None
    return {
        "preflight_before_edit": preflight_before_edit,
        "first_preflight_idx": first_pre,
        "first_mutation_idx": first_mut,
        "mutating_calls": sum(1 for n, _ in call_log if n in MUTATING),
        "query_withdraw_calls": sum(1 for n, _ in call_log if n == "revl_query_withdraw"),
        "scaffold_calls": sum(1 for n, _ in call_log if n == "revl_scaffold"),
        "edit_calls": sum(1 for n, _ in call_log if n == "revl_edit"),
        "load_calls": sum(1 for n, _ in call_log if n == "revl_load"),
        # direct revl_export, or through the revl_verbs hatch (name=revl_export)
        "export_calls": sum(1 for n, _ in call_log
                            if n in ("revl_export", "revl_verbs->revl_export")),
        "export_direct_calls": sum(1 for n, _ in call_log if n == "revl_export"),
        "export_via_hatch_calls": sum(1 for n, _ in call_log if n == "revl_verbs->revl_export"),
        "write_file_calls": sum(1 for n, _ in call_log if n == "write_file"),
    }


# --------------------------------------------------------------------------
async def run_arm(exp, arm, workdir, max_turns, verbose, sys_template_override=None):
    root = Path(workdir)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    if exp == "preflight":
        shutil.copy(TENANTS, root / "tenants.rvl")
        target = root / "tenants.rvl"
        if sys_template_override is not None:
            system = sys_template_override.format(path=str(target))
        elif arm == "preflight-on":
            system = SYS_PREFLIGHT.format(path=str(target))
        else:
            system = SYS_BASE.format(path=str(target))
        task = TASK_WITHDRAW
    else:
        (root / "plain.rvl").write_text(BASE_PLAIN, encoding="utf-8")
        target = root / "plain.rvl"
        sys_template = SYS_SCAFFOLD if arm == "gen-scaffold" else SYS_AUTHOR
        system = sys_template.format(path=str(target))
        task = TASK_AUTHOR

    bridge = await MCPBridge(root).__aenter__()
    print(f"    [bridge] {len(bridge.tools)} tools up", flush=True)
    tools = []
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task}]

    usage = {"tokens_in": 0, "tokens_out": 0, "tokens_new_prefill": 0,
             "wall": 0.0, "tool_calls": 0, "turns": 0, "write_tokens": 0,
             "tokens_cached": None, "hatch_calls": 0, "lookup_calls": 0}
    turn_log = []        # per-turn latency, attempts, finish_reason, provider usage
    model_error = None   # a failed model call ends the run but is recorded
    call_log = []
    call_detail = []
    transcript = []
    final_text = ""
    mcp_state = {}

    try:
        tools = bridge.openai_schemas() + FILE_TOOLS
        for turn in range(max_turns):
            print(f"    [t{turn}] calling model...", flush=True)
            t_turn = time.time()
            try:
                r = await model_call(messages, tools=tools, max_tokens=1400)
            except Exception as e:
                msg_e = _scrub(f"{type(e).__name__}: {e}")
                print(f"    [t{turn}] MODEL FAILED: {msg_e}", flush=True)
                model_error = {"turn": turn, "kind": getattr(e, "kind", "error"),
                               "status": getattr(e, "status", None), "message": msg_e[:400]}
                turn_log.append({"turn": turn, "latency_s": round(time.time() - t_turn, 2),
                                 "error": model_error["kind"]})
                break
            turn_log.append({"turn": turn, "latency_s": round(r["wall_with_retries"], 2),
                             "call_s": round(r["wall"], 2), "attempts": r["attempts"],
                             "finish_reason": r.get("finish_reason"),
                             "prompt_tokens": r["prompt_tokens"],
                             "completion_tokens": r["completion_tokens"],
                             "cached_tokens": r["cached_tokens"],
                             "usage": r["usage_raw"]})
            if r["cached_tokens"] is not None:
                usage["tokens_cached"] = (usage["tokens_cached"] or 0) + r["cached_tokens"]
            usage["tokens_in"] += r["prompt_tokens"]
            usage["tokens_out"] += r["completion_tokens"]
            usage["tokens_new_prefill"] += r["new_prefill_tokens"]
            usage["wall"] += r["wall"]
            usage["turns"] = turn + 1

            msg = r["message"]
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": calls} if calls else {})})
            names = [c["function"]["name"] for c in calls]
            transcript.append({"turn": turn, "calls": names,
                               "text": (msg.get("content") or "")[:200]})
            if verbose:
                print(f"    t{turn}: {names} | {r['new_prefill_tokens']} new | "
                      f"{r['wall']:.1f}s", flush=True)
            else:
                print(f"    t{turn}: {names or '(done)'} | {r['wall']:.1f}s", flush=True)

            if not calls:
                final_text = msg.get("content") or ""
                break

            for c in calls:
                usage["tool_calls"] += 1
                name = c["function"]["name"]
                if name in MUTATING:
                    usage["write_tokens"] += r["completion_tokens"] // max(1, len(calls))
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                # The #2073 hatch: a listed verb standing in for an unlisted one.
                # Counting it separately is the measurement of whether the fix is
                # actually *used*: the tiered surface hides revl_export, so the
                # artifact can only reach disk through here (or write_file).
                # The schema key is `names` (an array); SYS_AUTHOR advertises a
                # singular `name` the server ignores. Count either form, so a
                # hatch is never recorded as 0 because of the key it used.
                # hatch_calls = revl_verbs with `name` (CALLS a verb);
                # lookup_calls = revl_verbs with `names` (asks for a description).
                if name == "revl_verbs" and isinstance(args, dict) and "name" in args:
                    usage["hatch_calls"] += 1
                    if "names" in args:
                        usage["lookup_calls"] += 1
                    call_log.append((f"revl_verbs->{args.get('name')}", turn))
                elif name == "revl_verbs" and isinstance(args, dict) and "names" in args:
                    usage["lookup_calls"] += 1
                    call_log.append((f"revl_verbs?{args.get('names')}", turn))
                else:
                    call_log.append((name, turn))
                try:
                    if name in ("read_file", "write_file", "grep"):
                        out = run_file_tool(root, name, args)
                    else:
                        out = await bridge.call(name, args)
                except Exception as e:
                    out = f"ERROR: {type(e).__name__}: {e}"
                # A call log that names the tool but not its arguments cannot
                # answer the question a spiral analysis asks: the agent called
                # `revl_source` three times, but three times for WHAT? Record
                # the arguments and whether the surface refused them, so a run
                # of identical calls is distinguishable from a run of retries.
                call_detail.append({
                    "turn": turn,
                    "name": name,
                    "args": args,
                    "refused": _is_refusal(out),
                    "reason": _refusal_reason(out),
                    # observability only: the size the surface emitted, and
                    # whether the delivered result was truncated
                    "out_len": len(str(out)),
                    "cut": len(str(out)) > TOOL_CAP,
                    "committed": _is_committed(out),
                })
                messages.append({"role": "tool",
                                 "tool_call_id": c.get("id", f"call_{usage['tool_calls']}"),
                                 "content": str(out)[:TOOL_CAP]})

        try:
            mcp_state = json.loads(await bridge.call("revl_state", {}))
        except Exception:
            mcp_state = {}
    finally:
        await bridge.__aexit__(None, None, None)

    order = _order_metrics(call_log)
    if exp == "preflight":
        res = score_withdraw(root, final_text, usage, order)
    else:
        res = score_author(root, final_text, usage, order, mcp_state,
                           arm_prompt=sys_template)
    res["exhausted"] = not final_text.strip()
    res["transcript"] = transcript
    res["call_log"] = [n for n, _ in call_log]
    res["call_detail"] = call_detail
    res["refusals"] = sum(1 for c in call_detail if c["refused"])
    # The spiral metric: the longest run of consecutive refusals. A refusal
    # count alone cannot separate six refusals spread over a productive run
    # from three in a row that ended it.
    _run = _best = 0
    for c in call_detail:
        _run = _run + 1 if c["refused"] else 0
        _best = max(_best, _run)
    res["max_refusal_run"] = _best
    res["tool_cap"] = TOOL_CAP
    res["model"] = MODEL
    res["base_url_host"] = base_host()
    res["workdir"] = str(root)
    res["system_prompt_sha256"] = hashlib.sha256(system.encode()).hexdigest()
    res["task_sha256"] = hashlib.sha256(task.encode()).hexdigest()
    res["temperature"] = TEMPERATURE
    res["sent_template_kwargs"] = SEND_TEMPLATE_KWARGS
    res["revl_repo"] = str(REVL_REPO)
    res["turn_log"] = turn_log
    res["mean_turn_latency_s"] = (round(sum(t["latency_s"] for t in turn_log) / len(turn_log), 2)
                                  if turn_log else None)
    res["model_error"] = model_error
    res["call_names"] = [n for n, _ in call_log]
    res["committed"] = any(c.get("committed") for c in call_detail)
    if exp == "preflight":
        res["disk_changed"] = (root / "tenants.rvl").read_bytes() != TENANTS.read_bytes() \
            if (root / "tenants.rvl").exists() else None
    res["wd_final_sha256"] = hashlib.sha256(
        (root / "tenants.rvl").read_bytes()).hexdigest() if (root / "tenants.rvl").exists() else None
    res["n_cut"] = sum(1 for c in call_detail if c.get("cut"))
    res["max_out_len"] = max((c.get("out_len", 0) for c in call_detail), default=0)
    res["final_text"] = final_text
    return res


# --------------------------------------------------------------------------
EXPS = {
    "scaffold": ["gen-free", "gen-scaffold"],
    "preflight": ["preflight-off", "preflight-on"],
}


def _agg(rs, key):
    """Mean and range of a numeric field across reps, or the True-count of a bool."""
    vals = [r.get(key) for r in rs if isinstance(r.get(key), (int, float, bool))]
    if not vals:
        return "—"
    if all(isinstance(v, bool) for v in vals):
        return f"{sum(vals)}/{len(vals)}"
    m = sum(vals) / len(vals)
    if len(vals) == 1:
        return f"{m:.0f}"
    return f"{m:.0f} ({min(vals):.0f}-{max(vals):.0f})"


def load_arm_prompt(path, var):
    """Read a module-level string assignment from a driver file WITHOUT importing
    it (those files run asyncio.run(main()) at import). The prompt is evaluated
    from the original source, so it stays byte-identical to the original."""
    import ast
    src = Path(path).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == var for t in node.targets):
            return eval(compile(ast.Expression(node.value), path, "eval"), {})
    raise KeyError(f"{var} not found in {path}")


def _single(args):
    template = load_arm_prompt(args.arm_src, args.arm_var)
    wd = Path(args.workdir)
    t0 = time.time()
    try:
        res = asyncio.run(asyncio.wait_for(
            run_arm("preflight", "preflight-off", wd, args.max_turns, True,
                    sys_template_override=template),
            timeout=ARM_TIMEOUT))
    except Exception as e:
        traceback.print_exc()
        res = {"error": _scrub(f"{type(e).__name__}: {e}"), "model": MODEL,
               "base_url_host": base_host(), "workdir": str(wd),
               "revl_repo": str(REVL_REPO)}
    res["arm_variant"] = args.arm_var
    res["arm_src"] = args.arm_src
    res["arm_src_sha256"] = hashlib.sha256(Path(args.arm_src).read_bytes()).hexdigest()
    res["arm_template_sha256"] = hashlib.sha256(template.encode()).hexdigest()
    res["rep"] = args.rep
    res["label"] = args.label
    res["wall_total_s"] = round(time.time() - t0, 1)
    line = _scrub(json.dumps(res, default=str))
    with open(args.out, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    keys = ("break_set_exact", "withdrew", "committed", "disk_changed", "export_calls",
            "write_file_calls", "hatch_calls", "lookup_calls", "exhausted", "refusals",
            "turns", "tokens_in", "tokens_out", "wall_total_s", "error")
    print("    -> " + json.dumps({k: res.get(k) for k in keys}), flush=True)
    me = res.get("model_error")
    if me:
        print("    model_error: " + json.dumps(me), flush=True)
    sys.stdout.flush()
    # a timed-out HTTP worker thread must not hold the process open
    os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="both",
                    choices=["scaffold", "preflight", "both"])
    ap.add_argument("--arms", nargs="+", default=None)
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--reps", type=int, default=1,
                    help="independent repetitions per arm; >1 makes the ratios quotable")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default=str(HERE / "results_agentic2.jsonl"))
    ap.add_argument("--workdir", default=None,
                    help="single-run mode: one withdraw run in this workdir, row appended to --out")
    ap.add_argument("--arm-src", default=None,
                    help="file holding the arm system prompt (read with ast, never imported); required with --workdir")
    ap.add_argument("--arm-var", default="SYS_WARM")
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    if args.workdir:
        if not args.arm_src:
            ap.error("--arm-src is required with --workdir (the file holding the arm system prompt)")
        _single(args)
        return

    exps = ["scaffold", "preflight"] if args.exp == "both" else [args.exp]
    results = []
    for exp in exps:
        arms = args.arms or EXPS[exp]
        for arm in arms:
            for rep in range(args.reps):
                tag = f"{exp} / {arm}" + (f" / rep {rep + 1}/{args.reps}" if args.reps > 1 else "")
                print(f"\n=== {tag} ===")
                # rep-isolated: a stale artifact from one rep must not seed the next
                wd = HERE / (f"a2_{exp}_{arm}" + (f"_r{rep}" if args.reps > 1 else ""))
                try:
                    res = asyncio.run(asyncio.wait_for(
                        run_arm(exp, arm, wd, args.max_turns, args.verbose),
                        timeout=ARM_TIMEOUT))
                except Exception as e:
                    traceback.print_exc()
                    res = {"error": f"{type(e).__name__}: {e}"}
                res["exp"] = exp
                res["arm"] = arm
                res["rep"] = rep
                results.append(res)
                print(_scrub("    -> " + json.dumps({k: v for k, v in res.items()
                                              if k not in ("final_text", "transcript", "call_log", "call_detail", "turn_log")}, default=str)))

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(_scrub(json.dumps(r, default=str)) + "\n")
    print(f"\nwrote {args.out}")

    for exp in exps:
        rs = [r for r in results if r.get("exp") == exp]
        if not rs:
            continue
        by_arm = {}
        for r in rs:
            by_arm.setdefault(r["arm"], []).append(r)
        print(f"\n=== scoreboard: {exp} (n={args.reps}, max-turns={args.max_turns}) ===")
        if exp == "preflight":
            print(f"{'arm':<16}{'exact':<8}{'prefl<edit':<12}{'q.wdr':<7}"
                  f"{'muts':<7}{'tools':<12}{'out':<12}{'wr':<12}{'wall':<14}")
            for arm, g in by_arm.items():
                errs = [r for r in g if "error" in r]
                if errs:
                    print(f"{arm:<16}ERROR {errs[0]['error'][:60]}"); continue
                print(f"{arm:<16}{_agg(g,'break_set_exact'):<8}"
                      f"{_agg(g,'preflight_before_edit'):<12}"
                      f"{_agg(g,'query_withdraw_calls'):<7}{_agg(g,'mutating_calls'):<7}"
                      f"{_agg(g,'tool_calls'):<12}{_agg(g,'tokens_out'):<12}"
                      f"{_agg(g,'write_tokens'):<12}{_agg(g,'wall_s'):<14}")
        else:
            print(f"{'arm':<16}{'done':<8}{'admitted':<10}{'scaff':<7}"
                  f"{'edits':<7}{'artifact':<10}{'hatch':<7}{'tools':<8}"
                  f"{'in':<10}{'out':<10}{'wall':<10}")
            for arm, g in by_arm.items():
                errs = [r for r in g if "error" in r]
                if errs:
                    print(f"{arm:<16}ERROR {errs[0]['error'][:60]}"); continue
                print(f"{arm:<16}{_agg(g,'task_complete'):<8}{_agg(g,'admitted'):<10}"
                      f"{_agg(g,'scaffold_calls'):<7}{_agg(g,'edit_calls'):<7}"
                      f"{_agg(g,'artifact_ok'):<10}{_agg(g,'hatch_calls'):<7}"
                      f"{_agg(g,'tool_calls'):<8}"
                      f"{_agg(g,'tokens_in'):<10}{_agg(g,'tokens_out'):<10}"
                      f"{_agg(g,'wall_s'):<10}")
        print(f"{'exhausted':<16}" + "".join(
            f"{arm}={_agg(g,'exhausted')} " for arm, g in by_arm.items()
        ))


if __name__ == "__main__":
    main()
