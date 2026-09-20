#!/usr/bin/env python3
"""Which agent frameworks publish an unload path: falsifiable claims, checked.

`bench/hosts.json` left the second host unnamed on purpose, under a stated
criterion: a framework with no unload path cannot fail the residue column
honestly, because it would score maximally badly by construction, for a reason
that says nothing about the runtime comparison. Naming one without checking that
criterion would have produced a dishonest column. This module does the check.

## Why this is a claim checker and not a symbol search

The first version of this file searched a vocabulary (`remove`, `dispose`,
`unregister`, ...) across each package and reported a boolean. That version was
deleted because it does not work, and the way it failed is worth recording:
across six frameworks it returned true for all six, on hits like a graph builder
calling `list.remove()` and an HTTP client's `aclose()`. A boolean that is true
for everything discriminates nothing, and publishing it as "these frameworks
have an unload path" would have been false.

So the data below is a set of **named, falsifiable claims** about each
framework's published API, and this module's job is to try to falsify them. A
claim is one of:

  * `present`: this exact symbol is in this exact published file. Checked by
    finding it. A miss fails the check.
  * `absent`: none of these names appears anywhere under this path. Checked by
    searching for them. A hit fails the check.

An `absent` claim is scoped to the paths it names and is written that way in the
report: "the published surface under X exposes no Y", not "the framework cannot
do Y". A caller can always reach into a framework's internals, and several of
these keep their registry in a plain mutable dict.

## What a `present` claim still does not tell you

That a symbol exists, not what it does. `remove()` can delete a registry entry
and release nothing at all, and this module cannot tell those apart. The residue
probe can, and that is the measurement the benchmark's residue column carries.
Nothing here may be quoted as a residue result.

## Usage

  python3 bench/framework_unload_survey.py              # the committed survey
  python3 bench/framework_unload_survey.py --fetch      # download and re-check
  python3 bench/framework_unload_survey.py --fetch --write
  python3 bench/framework_unload_survey.py --check      # validate the committed survey

`--fetch` is the only mode that reaches the network, it reaches only the two
public package indexes, and it downloads exactly the versions named below.
Nothing here uploads anything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
SURVEY = BENCH / "results" / "framework-bench" / "unload-survey.json"

SURVEY_SCHEMA = "UNLOAD-SURVEY-2"

# The de-registration names this file treats as the ones a framework would use
# if it had the API. An `absent` claim asserts that none of these occurs under
# the named paths. They are spelled out once rather than per-framework so the
# same net is cast over every candidate.
DEREGISTRATION_NAMES = [
    "unregister", "deregister", "removeTool", "remove_tool",
    "removePlugin", "remove_plugin", "removeFunction", "remove_function",
]

CANDIDATES = [
    {
        "id": "modelcontextprotocol-sdk",
        "name": "@modelcontextprotocol/sdk",
        "ecosystem": "npm",
        "version": "1.30.0",
        "role": "candidate",
        "registration_api": "McpServer.registerTool(name, config, handler)",
        "claims": [
            {"kind": "present", "symbol": "remove(): void",
             "path": "package/dist/esm/server/mcp.d.ts",
             "proves": "unload", "granularity": "per-registration",
             "means": "a registration hands back a handle that can retire itself"},
            {"kind": "present", "symbol": "sendToolListChanged",
             "path": "package/dist/esm/server/mcp.js",
             "proves": "unload", "granularity": "per-registration",
             "means": "retiring a tool is announced to the peer, so the surface a "
                      "caller sees actually narrows"},
            {"kind": "present", "symbol": "close(): Promise<void>",
             "path": "package/dist/esm/server/mcp.d.ts",
             "proves": "unload", "granularity": "per-host",
             "means": "the host itself has a teardown"},
        ],
        "note": [
            "remove() is implemented as update({ name: null }), which deletes the",
            "registry entry. It calls nothing on the handler, so a tool that acquired",
            "something at registration has no callback in which to give it back. That",
            "is the residue shape, and it is why this framework is worth measuring",
            "rather than a reason to disqualify it.",
        ],
    },
    {
        "id": "pydantic-ai-slim",
        "name": "pydantic-ai-slim",
        "ecosystem": "pypi",
        "version": "2.46.0",
        "role": "candidate",
        "registration_api": "Agent(toolsets=[...]) with AbstractToolset",
        "claims": [
            {"kind": "present", "symbol": "async def __aexit__",
             "path": "pydantic_ai/toolsets/abstract.py",
             "proves": "unload", "granularity": "per-toolset",
             "means": "a toolset is an async context manager, so there is a scope "
                      "exit where it can release what it took"},
            {"kind": "absent", "names": DEREGISTRATION_NAMES,
             "path": "pydantic_ai/toolsets",
             "means": "teardown is at toolset scope only; an individual registered "
                      "tool has no retirement path"},
        ],
        "note": [
            "The runner-up, and the closest thing to a real teardown contract among",
            "the python frameworks here: __aexit__ is documented as where a toolset",
            "tears down connections. It is scoped to the toolset, not to a tool, and",
            "for_run_step hands lifecycle transitions back to the caller in so many",
            "words.",
        ],
    },
    {
        "id": "semantic-kernel",
        "name": "semantic-kernel",
        "ecosystem": "pypi",
        "version": "1.44.1",
        "role": "candidate",
        "registration_api": "Kernel.add_plugin(...) / add_function(...)",
        "claims": [
            {"kind": "present", "symbol": "def add_plugin",
             "path": "semantic_kernel/functions/kernel_function_extension.py",
             "proves": "registration",
             "means": "registration exists and is named a plugin. This is NOT unload "
                      "evidence and the verdict must not read it as any"},
            {"kind": "absent",
             "names": ["remove_plugin", "remove_function", "unregister", "deregister"],
             "path": "semantic_kernel/functions",
             "means": "nothing published retires a registered plugin"},
            {"kind": "absent",
             "names": ["remove_plugin", "remove_function"],
             "path": "semantic_kernel/kernel.py",
             "means": "and the kernel itself has no counterpart to add_plugin"},
        ],
        "note": [
            "The one mainstream framework that calls its unit a plugin, which made it",
            "the expected pick. It keeps them in a plain dict field and publishes",
            "add_plugin with no counterpart, so a caller who wants one mutates the",
            "dict, and no plugin is ever told it was removed.",
            "The claim was first written against the whole package and the checker",
            "falsified it on the word 'unregistered' in a comment about azure agent",
            "threads. It is scoped to the plugin registry's own modules now, which is",
            "the scope it should have had.",
        ],
    },
    {
        "id": "langchain-js",
        "name": "langchain",
        "ecosystem": "npm",
        "version": "1.5.11",
        "role": "candidate",
        "registration_api": "createAgent({ tools: [...] })",
        "claims": [
            {"kind": "absent", "names": DEREGISTRATION_NAMES,
             "path": "package/dist",
             "means": "tools are a constructor argument; the published surface "
                      "exposes no retirement path"},
        ],
        "note": ["The framework most readers mean by 'agent framework'."],
    },
    {
        "id": "langchain-core-py",
        "name": "langchain-core",
        "ecosystem": "pypi",
        "version": "1.6.3",
        "role": "candidate",
        "registration_api": "model.bind_tools([...])",
        "claims": [
            {"kind": "absent", "names": DEREGISTRATION_NAMES,
             "path": "langchain_core/tools",
             "means": "bind_tools returns a new runnable rather than mutating a "
                      "registry, so there is nothing to retire"},
        ],
        "note": ["The python side of the same framework."],
    },
    {
        "id": "openai-agents",
        "name": "@openai/agents",
        "ecosystem": "npm",
        "version": "0.18.0",
        "role": "candidate",
        "registration_api": "new Agent({ tools: [...] })",
        "claims": [
            {"kind": "absent", "names": DEREGISTRATION_NAMES,
             "path": "package/dist",
             "means": "same shape: tools are constructor state"},
        ],
        "note": ["First-party agent SDK."],
    },
    {
        "id": "crewai",
        "name": "crewai",
        "ecosystem": "pypi",
        "version": "1.15.22",
        "role": "candidate",
        "registration_api": "Agent(tools=[...]) / Crew(...)",
        "claims": [
            {"kind": "absent", "names": ["remove_tool", "unregister", "deregister"],
             "path": "crewai/agent",
             "means": "an agent's tools are constructor state with no retirement path"},
        ],
        "note": [
            "crewai does publish an event_bus unregister for event handlers, which is",
            "a different registry and does not retire a tool. The claim is scoped to",
            "the agent package for that reason.",
        ],
    },
    {
        "id": "cordis",
        "name": "cordis",
        "ecosystem": "npm",
        "version": "4.0.0-rc.10",
        "role": "control",
        "registration_api": "ctx.plugin(...) returning a Fiber",
        "claims": [
            {"kind": "present", "symbol": "dispose",
             "path": "package/lib/fiber.d.ts",
             "proves": "unload", "granularity": "per-registration",
             "means": "a loaded plugin is a fiber and the fiber disposes"},
        ],
        "note": [
            "The control. The raw-ts host already runs on a fork of this, pinned by",
            "commit, and its unload path is known to exist, so a survey that failed to",
            "find one here would be reporting on its own regexes rather than on the",
            "ecosystem. The published 4.0.0-rc.10 is scanned rather than the fork,",
            "because a reader can fetch the published one.",
        ],
    },
]

PYPI_API = "https://pypi.org/pypi/{name}/{version}/json"

# Excluded from every search: a de-registration name inside a vendored copy of
# another package is not this framework's API.
EXCLUDED_DIRS = {"node_modules", "tests", "test", "__pycache__"}

SOURCE_SUFFIXES = {".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".py", ".pyi"}


class SurveyError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise SurveyError(f"{' '.join(cmd[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def fetch_npm(spec: dict, into: Path) -> tuple:
    out = _run(["npm", "pack", f"{spec['name']}@{spec['version']}", "--silent"], into)
    names = [line.strip() for line in out.splitlines() if line.strip().endswith(".tgz")]
    tarball = (into / names[0]) if names else None
    if tarball is None or not tarball.exists():
        found = sorted(into.glob("*.tgz"))
        if not found:
            raise SurveyError(f"npm pack produced no tarball for {spec['name']}")
        tarball = found[0]
    dest = into / spec["id"]
    dest.mkdir(exist_ok=True)
    with tarfile.open(tarball) as tar:
        _safe_extract_tar(tar, dest)
    return dest, {"filename": tarball.name}


def fetch_pypi(spec: dict, into: Path) -> tuple:
    """The index's JSON API rather than `pip download`, so the row can name the
    artifact and repeat the index's digest: that is what a reader needs in order
    to check they scanned the same bytes."""
    url = PYPI_API.format(name=spec["name"], version=spec["version"])
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SurveyError(f"pypi: cannot read {url}: {exc}") from exc
    wheels = [u for u in meta.get("urls", [])
              if u.get("packagetype") == "bdist_wheel"
              and str(u.get("filename", "")).endswith("-none-any.whl")]
    if not wheels:
        raise SurveyError(f"pypi: {spec['name']}=={spec['version']} publishes no "
                          f"pure-python wheel")
    chosen = wheels[0]
    target = into / chosen["filename"]
    try:
        with urllib.request.urlopen(chosen["url"], timeout=300) as resp:
            target.write_bytes(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SurveyError(f"pypi: cannot download {chosen['filename']}: {exc}") from exc
    dest = into / spec["id"]
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(target) as zf:
        _safe_extract_zip(zf, dest)
    return dest, {"filename": chosen["filename"],
                  "sha256": (chosen.get("digests") or {}).get("sha256")}


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract without letting a member escape `dest`.

    These archives come from a public index and were not built here, so every
    member is checked rather than trusted: tarfile will otherwise write
    `../../anything` if a member says so.
    """
    root = dest.resolve()
    for member in tar.getmembers():
        if not str((root / member.name).resolve()).startswith(str(root)):
            raise SurveyError(f"archive member escapes the root: {member.name}")
        if member.issym() or member.islnk():
            raise SurveyError(f"archive member is a link: {member.name}")
    tar.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for name in zf.namelist():
        if not str((root / name).resolve()).startswith(str(root)):
            raise SurveyError(f"archive member escapes the root: {name}")
    zf.extractall(dest)


def _sources_under(root: Path, rel: str) -> list:
    base = root / rel
    if base.is_file():
        return [base]
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        parts = path.relative_to(root).parts
        if any(p in EXCLUDED_DIRS or p.endswith(".dist-info") for p in parts):
            continue
        out.append(path)
    return out


def _hits(paths: list, root: Path, needles: list, limit: int) -> list:
    found = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in needles:
            for m in re.finditer(re.escape(needle), text):
                found.append({
                    "file": str(path.relative_to(root)),
                    "line": text.count("\n", 0, m.start()) + 1,
                    "symbol": needle,
                })
                if len(found) >= limit:
                    return found
    return found


def check_claim(root: Path, claim: dict) -> dict:
    paths = _sources_under(root, claim["path"])
    result = dict(claim)
    result["files_searched"] = len(paths)
    if not paths:
        result["verdict"] = "not-measured"
        result["reason"] = f"no source files under {claim['path']!r} in this artifact"
        return result
    if claim["kind"] == "present":
        hits = _hits(paths, root, [claim["symbol"]], limit=5)
        result["hits"] = hits
        result["verdict"] = "confirmed" if hits else "falsified"
        return result
    hits = _hits(paths, root, claim["names"], limit=20)
    result["hits"] = hits
    result["verdict"] = "falsified" if hits else "confirmed"
    return result


def survey(fetch: bool) -> dict:
    if not fetch:
        if not SURVEY.exists():
            raise SurveyError(f"no committed survey at {SURVEY.relative_to(ROOT)}; "
                              f"run with --fetch")
        return json.loads(SURVEY.read_text())

    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="unload-survey-"))
    try:
        for spec in CANDIDATES:
            row = {k: spec[k] for k in
                   ("id", "name", "ecosystem", "version", "role",
                    "registration_api", "note")}
            try:
                fetcher = fetch_npm if spec["ecosystem"] == "npm" else fetch_pypi
                root, artifact = fetcher(spec, tmp)
                row["fetched"] = True
                row["artifact"] = artifact
                row["claims"] = [check_claim(root, c) for c in spec["claims"]]
            except (SurveyError, subprocess.SubprocessError, OSError) as exc:
                # Recorded as not-measured, never as "no unload path": a failed
                # download says nothing about the framework.
                row["fetched"] = False
                row["not_measured_reason"] = f"{type(exc).__name__}: {exc}"[:300]
                row["claims"] = []
            rows.append(row)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "schema": SURVEY_SCHEMA,
        "what_this_measures": (
            "named claims about a published package's surface, at a pinned version, "
            "each confirmed or falsified by searching the artifact the index served. "
            "A confirmed 'present' claim says a symbol exists, not what it does; a "
            "confirmed 'absent' claim is scoped to the paths it names."),
        "deregistration_names": DEREGISTRATION_NAMES,
        "frameworks": rows,
    }


def verdict_for(row: dict) -> str:
    """One phrase for the table: does this framework publish an unload path?

    Only a confirmed `present` claim tagged `proves: unload` counts. The
    distinction is load-bearing: semantic-kernel's confirmed `add_plugin` proves
    a registration API and nothing about unloading, and an earlier version of
    this function read it as an unload path and printed the wrong verdict.

    The granularity of the coarsest confirmed unload claim is carried into the
    verdict, because "there is a teardown when the whole toolset exits" and
    "this registration can retire itself" are different guarantees and the
    residue column means different things under each.
    """
    if not row.get("fetched"):
        return "not-measured"
    claims = row.get("claims") or []
    if any(c.get("verdict") == "falsified" for c in claims):
        return "claim-falsified"
    unload = [c for c in claims
              if c.get("kind") == "present" and c.get("verdict") == "confirmed"
              and c.get("proves") == "unload"]
    if not unload:
        return "none published"
    order = ["per-registration", "per-toolset", "per-host"]
    best = min(unload,
               key=lambda c: order.index(c.get("granularity", "per-host"))
               if c.get("granularity") in order else len(order))
    return f"publishes one ({best.get('granularity', 'unspecified')})"


def render(doc: dict) -> str:
    lines = ["| framework | version | registration | unload path | evidence |",
             "|---|---|---|---|---|"]
    for row in doc["frameworks"]:
        if not row.get("fetched"):
            lines.append(f"| `{row['name']}` | {row['version']} | "
                         f"{row['registration_api']} | not-measured "
                         f"| {row.get('not_measured_reason', '')[:70]} |")
            continue
        evidence = []
        for claim in row["claims"]:
            if claim["kind"] == "present" and claim["verdict"] == "confirmed":
                hit = claim["hits"][0]
                evidence.append(f"`{claim['symbol']}` at `{hit['file']}:{hit['line']}`")
            elif claim["kind"] == "absent" and claim["verdict"] == "confirmed":
                evidence.append(f"none of {len(claim['names'])} names under "
                                f"`{claim['path']}` ({claim['files_searched']} files)")
            else:
                evidence.append(f"{claim['verdict']}: {claim.get('reason', '')}")
        lines.append(f"| `{row['name']}` | {row['version']} | "
                     f"{row['registration_api']} | {verdict_for(row)} "
                     f"| {'; '.join(evidence)} |")
    lines += ["",
              "A confirmed `present` claim says a symbol exists in a published file. "
              "It does not say what calling it releases, which is what the residue "
              "probe measures and what nothing in this table may be quoted as."]
    return "\n".join(lines)


def check(doc: dict) -> list:
    problems = []
    if doc.get("schema") != SURVEY_SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {SURVEY_SCHEMA!r}")
    rows = doc.get("frameworks") or []
    if not rows:
        problems.append("survey holds no frameworks")
    controls = [r for r in rows if r.get("role") == "control"]
    if not controls:
        problems.append("survey holds no control row")
    for row in controls:
        if row.get("fetched") and not verdict_for(row).startswith("publishes one"):
            problems.append(
                f"control {row['name']} shows no unload path; the search is finding "
                f"nothing rather than the ecosystem having nothing")
    for row in rows:
        if row.get("fetched") is False and "not_measured_reason" not in row:
            problems.append(f"{row['id']}: unfetched row with no reason")
        for claim in row.get("claims") or []:
            if claim.get("verdict") == "falsified":
                problems.append(
                    f"{row['id']}: claim {claim.get('symbol') or claim.get('names')} "
                    f"is falsified against {row['version']}; the claim in this file "
                    f"is wrong, or the version moved under it")
    return problems


def main(argv: list = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true",
                    help="download the pinned versions and re-check every claim")
    ap.add_argument("--write", action="store_true",
                    help=f"write the survey to {SURVEY.relative_to(ROOT)}")
    ap.add_argument("--check", action="store_true",
                    help="validate the survey and exit non-zero on a problem")
    ap.add_argument("--json", action="store_true", help="print the raw document")
    args = ap.parse_args(argv)

    try:
        doc = survey(args.fetch)
    except SurveyError as exc:
        print(f"unload survey: {exc}")
        return 1

    if args.write:
        SURVEY.parent.mkdir(parents=True, exist_ok=True)
        SURVEY.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"wrote {SURVEY.relative_to(ROOT)}")

    if args.check:
        problems = check(doc)
        for problem in problems:
            print(f"unload survey: {problem}")
        if problems:
            return 1
        print(f"unload survey: {len(doc['frameworks'])} frameworks, "
              f"{sum(len(r.get('claims') or []) for r in doc['frameworks'])} claims, "
              f"none falsified")
        return 0

    print(json.dumps(doc, indent=2) if args.json else render(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
