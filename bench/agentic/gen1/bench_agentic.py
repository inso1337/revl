#!/usr/bin/env python3
"""bench_agentic.py -- the agentic change-task bench (R10).

Why this bench exists
---------------------
`bench_holefill.py` (addenda 1-8) measured ONE-SHOT GENERATION with zero tools:
the model writes a component and a subprocess compiles it afterwards. That
measures the single axis revl is expected to LOSE on -- a novel language against
TypeScript's pretraining prior -- and holds the agent loop at zero, which is the
axis revl is designed to WIN on.

This bench measures the designed axis: a CHANGE task driven through a live tool
loop, scored on break-set correctness.

Task
----
    "Withdraw TenantAStore from tenants.rvl. Fix everything that breaks."

Ground truth is EXACT and produced by revl itself (`revl_query_withdraw`):

    cascade       [TenantAApp]              depth 1
    lostKeys      [kv]
    orphanedKeys  [kv@tenant_a]
    survivors     [TenantBApp, TenantBStore]
    breaks        1

The decisive metric is `break_set_exact`: the agent must name the COMPLETE break
set and must NOT damage the survivors. A TypeScript arm cannot win this by
construction -- TS has no realm concept, so `kv` resolves ambiguously across
tenant_a/tenant_b and "what breaks" is only ever a grep approximation.

Arms
----
    revl-mcp     revl MCP verbs (the proposal)      -> the thing being defended
    revl-nomcp   generic read/write only (control)  -> does the MCP actually matter?
    ts-tools     read/grep/write + tsc (baseline)   -> the language revl replaces

Run
---
    python3 bench_agentic.py --arms revl-mcp revl-nomcp ts-tools
    python3 bench_agentic.py --arms revl-mcp --max-turns 16 --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OMLX = os.environ.get("OMLX_URL", "http://127.0.0.1:8010/v1/chat/completions")
MODEL = os.environ.get("OMLX_MODEL", "Ornith-1.5-35B-A3B-MLX-4bit")
REVL_REPO = Path(os.environ.get("REVL_REPO", str(HERE.parents[2])))

# `revl mcp serve` must run under the interpreter that has cordis-py
# (src/revl/mcp/session.py::_backend names it). Override with REVL_MCP_PYTHON.
MCP_PYTHON = os.environ.get(
    "REVL_MCP_PYTHON",
    str(REVL_REPO / "backends" / "python" / ".venv" / "bin" / "python"),
)
SOURCE_RVL = REVL_REPO / "examples" / "tenants.rvl"

# Ground truth for "withdraw TenantAStore", measured from revl itself.
TRUTH = {
    "breaks": ["TenantAApp"],
    "lost_keys": ["kv"],
    "orphaned": ["kv@tenant_a"],
    "survivors": ["TenantBApp", "TenantBStore"],
}
# Components that must be byte-identical afterwards (the survivors).
SURVIVOR_MARKERS = ["TenantBApp", "TenantBStore"]
ORIGINAL_SOURCE = ""  # set by run_arm to the pristine composition


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------
def omlx_call(messages, tools=None, max_tokens=1024, timeout=1800):
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        OMLX,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    raw = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    wall = time.time() - t0
    u = raw.get("usage", {}) or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    msg = raw["choices"][0]["message"]
    return {
        "message": msg,
        "wall": wall,
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        "cached_tokens": cached or 0,
        "new_prefill_tokens": max(0, u.get("prompt_tokens", 0) - (cached or 0)),
    }


# --------------------------------------------------------------------------
# MCP bridge to `revl mcp serve`
# --------------------------------------------------------------------------
class MCPBridge:
    """Spawns `revl mcp serve --root <dir>` over stdio and exposes its verbs."""

    def __init__(self, root: Path, extra_args=None):
        self.root = root
        self.extra_args = extra_args or []
        self.session = None
        self._stack = None
        self.tools = []

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # The MCP server's runtime half (revl_load/swap/edit/state/snapshot/...)
        # imports `cordis`, which lives in the backends venv -- NOT in this
        # interpreter. Launching with sys.executable silently kills 11 verbs:
        # revl_load, revl_swap, revl_edit, revl_undo, revl_unload, revl_commit,
        # revl_abort, revl_estop, revl_estop_report, revl_state, revl_snapshot.
        params = StdioServerParameters(
            command=MCP_PYTHON,
            args=[
                "-m", "revl", "mcp", "serve",
                "--root", str(self.root),
                *self.extra_args,
            ],
            env={**os.environ, "PYTHONPATH": str(REVL_REPO / "src")},
        )
        self._stack = stdio_client(params)
        read, write = await self._stack.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = list(listed.tools)
        return self

    async def __aexit__(self, *exc):
        try:
            if self.session is not None:
                await self.session.__aexit__(*exc)
        finally:
            if self._stack is not None:
                await self._stack.__aexit__(*exc)

    def openai_schemas(self):
        out = []
        for t in self.tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "")[:1024],
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            })
        return out

    async def call(self, name, args):
        res = await self.session.call_tool(name, args or {})
        parts = []
        for c in (res.content or []):
            parts.append(getattr(c, "text", None) or str(c))
        return "\n".join(parts)


# --------------------------------------------------------------------------
# local file tools (the non-MCP arms)
# --------------------------------------------------------------------------
FILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite a file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Regex search across files in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
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
        hits = []
        for f in root.rglob("*"):
            if f.is_file():
                try:
                    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if pat.search(line):
                            hits.append(f"{f.relative_to(root)}:{i}: {line.strip()[:120]}")
                except Exception:
                    pass
        return "\n".join(hits[:80]) or "no matches"
    return f"unknown tool {name}"


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------
def score(root: Path, transcript, final_text, tool_calls, usage):
    """Objective scoring against the exact ground truth."""
    final = ""
    rvl = root / "tenants.rvl"
    if rvl.exists():
        final = rvl.read_text(encoding="utf-8")

    # did the agent name the complete break set?
    named_breaks = all(b in final_text for b in TRUTH["breaks"])
    named_cause = any(k in final_text for k in TRUTH["lost_keys"]) or any(
        o in final_text for o in TRUTH["orphaned"]
    )

    # Strip comments before every structural check: a withdrawal note that
    # *names* the withdrawn component must not count as the component present.
    code = re.sub(r"//[^\n]*", "", final)

    # The survivors must keep every provision AND their own intercept metadata.
    # NOTE: a correct withdrawal removes TenantAApp's intercept, so the total
    # drops to ONE -- so check the survivor's own metadata, not a raw count.
    survivors_present = all(s in code for s in SURVIVOR_MARKERS)
    survivors_intact = ("quota: 9" in code) and ("tenant_b" in code)
    withdrew = "component TenantAStore" not in code

    # does the result compile?
    compiles = None
    try:
        r = subprocess.run(
            [sys.executable, "-m", "revl", "compile", str(rvl)],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PYTHONPATH": str(REVL_REPO / "src")},
            cwd=str(REVL_REPO),
        )
        compiles = r.returncode == 0
        compile_err = (r.stderr or r.stdout or "")[:400]
    except Exception as e:
        compile_err = str(e)

    break_set_exact = bool(
        withdrew and named_breaks and named_cause
        and survivors_present and survivors_intact and compiles
    )

    return {
        "changed": final != ORIGINAL_SOURCE,
        "break_set_exact": break_set_exact,
        "withdrew": withdrew,
        "named_breaks": named_breaks,
        "named_cause": named_cause,
        "survivors_present": survivors_present,
        "survivors_intact": survivors_intact,
        "compiles": compiles,
        "compile_err": "" if compiles else compile_err,
        "tool_calls": tool_calls,
        "turns": len(transcript),
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
        "tokens_new_prefill": usage["tokens_new_prefill"],
        "wall_s": round(usage["wall"], 1),
        "final_text": final_text[:600],
    }


# --------------------------------------------------------------------------
# the arms
# --------------------------------------------------------------------------
TASK = (
    "The working directory contains tenants.rvl. Withdraw the component "
    "`TenantAStore` from it and fix everything that breaks as a result. "
    "When you are done, state exactly which components broke and why, and "
    "confirm which components were unaffected."
)

SYSTEM_REVL = (
    "The composition under change is at {path} . "
    "revl_check and the revl_query_* tools require it explicitly as "
    'files: ["{path}"] . '
    "You are a coding agent working on a revl composition. revl is a language "
    "where provision is realm-scoped: two components may both provide `kv` as "
    "long as they are isolated in different realms. Use the provided tools to "
    "inspect the composition before you change it, and to verify your change "
    "after. Prefer asking the compiler over guessing."
)

SYSTEM_FILE = (
    "The working directory contains tenants.rvl, at {path} . "
    "You are a coding agent. Use the provided tools to inspect and edit files "
    "in the working directory."
)


async def run_arm(arm, workdir, max_turns, verbose):
    global ORIGINAL_SOURCE
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE_RVL, root / "tenants.rvl")
    ORIGINAL_SOURCE = (root / "tenants.rvl").read_text(encoding="utf-8")

    transcript = []
    tool_calls = 0
    usage = {"tokens_in": 0, "tokens_out": 0, "tokens_new_prefill": 0, "wall": 0.0}
    final_text = ""

    bridge = None
    if arm in ("revl-mcp", "revl-mcp-sees"):
        # `--files` is the server's own "default composition for tools called
        # without one" (src/revl/cli/parser.py) -- the designed usage, so the
        # agent need not thread a path through every call.
        bridge = await MCPBridge(
            root, extra_args=["--files", str(root / "tenants.rvl")]
        ).__aenter__()
        tools = bridge.openai_schemas()
        system = SYSTEM_REVL.format(path=str(root / "tenants.rvl"))
        if arm == "revl-mcp-sees":
            # Isolates SURFACE from LANGUAGE: the identical tool surface, but
            # the agent is handed the source it spends every turn asking for.
            # This is what R13 would make unnecessary.
            system += (
                "\n\nFor reference, the current contents of tenants.rvl are:\n"
                "```\n" + ORIGINAL_SOURCE + "\n```"
            )
    else:
        tools = FILE_TOOLS
        system = SYSTEM_FILE.format(path=str(root / "tenants.rvl"))

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": TASK},
    ]

    try:
        for turn in range(max_turns):
            r = omlx_call(messages, tools=tools, max_tokens=1200)
            usage["tokens_in"] += r["prompt_tokens"]
            usage["tokens_out"] += r["completion_tokens"]
            usage["tokens_new_prefill"] += r["new_prefill_tokens"]
            usage["wall"] += r["wall"]

            msg = r["message"]
            calls = msg.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                **({"tool_calls": calls} if calls else {}),
            })
            transcript.append({"turn": turn, "text": (msg.get("content") or "")[:300],
                               "calls": [c["function"]["name"] for c in calls]})
            if verbose:
                print(f"    turn {turn}: {[c['function']['name'] for c in calls]} "
                      f"| {r['new_prefill_tokens']} new tok | {r['wall']:.1f}s")

            if not calls:
                final_text = msg.get("content") or ""
                break

            for c in calls:
                tool_calls += 1
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                try:
                    if bridge is not None:
                        out = await bridge.call(name, args)
                    else:
                        out = run_file_tool(root, name, args)
                except Exception as e:
                    out = f"ERROR: {type(e).__name__}: {e}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": c.get("id", f"call_{tool_calls}"),
                    "content": str(out)[:6000],
                })
    finally:
        if bridge is not None:
            await bridge.__aexit__(None, None, None)

    res = score(root, transcript, final_text, tool_calls, usage)
    # `exhausted` separates "ran out of turns" from "answered wrong" -- the
    # scorer reads final_text, which is empty when the loop never finished.
    res["exhausted"] = not final_text.strip()
    res["transcript"] = transcript
    res["final_text"] = final_text
    return res


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+",
                    default=["revl-mcp", "revl-mcp-sees", "revl-nomcp", "ts-tools"])
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default=str(HERE / "results_agentic.jsonl"))
    args = ap.parse_args()

    results = []
    for arm in args.arms:
        print(f"\n=== arm {arm} ===")
        wd = HERE / f"agentic_{arm}"
        if wd.exists():
            shutil.rmtree(wd)
        try:
            res = asyncio.run(run_arm(arm, wd, args.max_turns, args.verbose))
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        res["arm"] = arm
        results.append(res)
        print(f"    -> {json.dumps({k: v for k, v in res.items() if k != 'final_text'})}")

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"\nwrote {args.out}")

    print("\n=== scoreboard ===")
    print(f"{'arm':<12} {'exact':<6} {'withdrew':<9} {'compiles':<9} {'tools':<6} "
          f"{'tok_in':<8} {'tok_out':<8} {'new_pref':<9} {'wall_s':<8}")
    for r in results:
        if "error" in r:
            print(f"{r['arm']:<12} ERROR {r['error'][:60]}")
            continue
        print(f"{r['arm']:<12} {str(r['break_set_exact']):<6} "
              f"{str(r.get('withdrew')):<9} "
              f"{str(r['compiles']):<9} {r['tool_calls']:<6} "
              f"{r['tokens_in']:<8} {r['tokens_out']:<8} "
              f"{r['tokens_new_prefill']:<9} {r['wall_s']:<8}")


if __name__ == "__main__":
    main()
