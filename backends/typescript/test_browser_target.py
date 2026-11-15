"""The ts tier's browser target, gated in a real browser engine (issue #3294).

`docs/ts-runtime-contract.md` used to define the ts tier's environment as plain
node. The runtime is portable (it names no node builtin at module scope) and
cordis is portable, but nothing said so and nothing tested it, so "an emitted
module runs in a page" was an assumption nobody could rely on.

This file is the gate for the part of the contract that is written down in
"The browser target" of that document:

  * `test_the_runtime_imports_no_node_builtin` is the toolchain-free half: the
    runtime an emitted module imports is ESM with no node builtin imports. The
    single node lookup, `_uiStorage`'s optional
    `process.getBuiltinModule('node:async_hooks')`, is a call, not an import,
    and falls back to the stack in a page.
  * `test_the_strip_step_rewrites_the_import_graph_only` owns the strip step:
    it edits module-specifier nodes, not text that looks like one, and it
    resolves each relative specifier against its inputs (the emitted default
    `../runtime.ts` becomes the flat `./runtime.js`) or fails naming the file.
  * `test_an_emitted_module_runs_in_a_browser_engine` is the bite: it emits a
    document with a top-level `fn` and a component that provides a service,
    strips the emitted module and `runtime.ts` to JavaScript with
    `scripts/strip-types.mjs`, serves them over HTTP with the import map a page
    needs for `cordis`, and runs it in headless Chrome. The page calls the
    function, applies the component and calls the service method, and reports
    the portable `host` members it sees. The engine's verdict is the assertion.

WHY THE ENGINE IS CHROME: the repository has no browser dependency and this
change does not add one. `google-chrome` is on the `ubuntu-latest` image the
`backend-typescript` job runs on, and on a developer machine it is found in the
usual places; `REVL_CHROME` names another binary. The job sets
`REVL_BROWSER_REQUIRED=1`, which turns "no engine" into a failure with the
command to install one, because a gate that skips in CI proves nothing. A local
run without an engine skips with that reason.

WHAT THIS DOES NOT PROVE: a page still needs the strip step (a browser has no
type stripper) and an import map for cordis (the tier ships no browser bundle).
Documents that use `fs` or `shell` import host modules that name node builtins;
they are outside the target, and the contract says so.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source  # noqa: E402

_STRIP = _HERE / "scripts" / "strip-types.mjs"
_TYPESCRIPT = _HERE / "node_modules" / "typescript" / "lib" / "typescript.js"

# The portable half of `host` (runtime.ts). A member that reaches for the
# filesystem, a child process, a socket or another node builtin does not run in
# a page and does not belong here. The gate asserts this is the exact set, so
# adding a host member is an edit to this list and to the browser table in
# docs/ts-runtime-contract.md, in the same change.
_BROWSER_HOST = (
    "Stream",
    "Pool",
    "Map",
    "Job",
    "scheduleEvery",
    "scheduleAfter",
    "clockAdvance",
    "clockReset",
    "applyConfigDefaults",
    "markSecret",
    "secretResult",
)

_DOC = """
service Calc {
  fn twice(n: Int) -> Int
}
component Doubler provides calc: Calc {
  provide calc {
    fn twice(n) = n * 2
  }
}
pub fn add(a: Int, b: Int) -> Int { return a + b }
"""

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>PENDING</title>
<script type="importmap">
{importmap}
</script>
</head>
<body>
<pre id="out">pending</pre>
<script type="module">
import {{ Context }} from 'cordis'
import {{ plug }} from './runtime.js'
import * as runtime from './runtime.js'
import * as mod from './module.js'
try {{
  const fn = mod.add(2n, 3n)
  const ctx = new Context()
  await plug(ctx, mod.Doubler)
  const service = ctx.calc.twice(21n)
  const host = Object.keys(runtime.host).join(',')
  document.title = 'REVL-BROWSER-OK fn=' + fn.toString() +
    ' service=' + service.toString() + ' host=' + host
}} catch (error) {{
  document.title = 'REVL-BROWSER-ERROR ' + String(error && (error.stack || error))
}}
</script>
</body>
</html>
"""


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_3294", _HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chrome():
    """The browser engine, or None.

    When `REVL_CHROME` is set it names the engine and discovery is skipped, so
    a bad path fails the gate instead of silently running another engine.
    """
    named = os.environ.get("REVL_CHROME")
    if named:
        return named if Path(named).exists() else None
    candidates = []
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _node_typescript_reason():
    if shutil.which("node") is None:
        return "needs node on PATH"
    if not _TYPESCRIPT.exists():
        return "needs backends/typescript/node_modules (`cd backends/typescript && npm ci`)"
    return None


def _browser_reason():
    reason = _node_typescript_reason()
    if reason is not None:
        return reason
    if _chrome() is None:
        return ("needs a browser engine: set REVL_CHROME, or install Google Chrome "
                "or Chromium")
    return None


def _require(reason):
    if reason is None:
        return
    if os.environ.get("REVL_BROWSER_REQUIRED") == "1":
        pytest.fail(
            f"the ts browser gate cannot run: {reason}. "
            "REVL_BROWSER_REQUIRED=1 makes a missing prerequisite a failure, "
            "not a hollow green")
    pytest.skip(reason)


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_HERE), **kwargs)

    def log_message(self, *args):  # keep pytest output about the gate
        pass


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _dump_dom(chrome, url, profile, cwd):
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=10000",
        "--dump-dom",
        url,
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # The runner is normally not root; a container may run as root, where
        # Chrome's own sandbox cannot start.
        args.append("--no-sandbox")
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180)


def test_the_runtime_imports_no_node_builtin():
    """The runtime an emitted module imports is loadable in a page.

    Type-level references are fine and are erased; a specifier is not. The one
    node lookup in `runtime.ts` is a call guarded by `typeof getBuiltin ===
    'function'`, so a page skips it and takes the stack fallback.
    """
    text = (_HERE / "runtime.ts").read_text(encoding="utf-8")
    found = re.findall(
        r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)(['"])node:[^'"]*\1""",
        text)
    assert not found, (
        "runtime.ts names a node builtin in an import, so an emitted module "
        "cannot load in a browser (docs/ts-runtime-contract.md): " + repr(found))
    assert re.search(r"process\s*\?\.\s*getBuiltinModule", text), (
        "the one optional node lookup changed; update the browser target "
        "section of docs/ts-runtime-contract.md with what replaced it")
    assert re.search(r"""getBuiltin\(\s*['"]node:async_hooks""", text), (
        "the optional lookup no longer asks for node:async_hooks; update the "
        "browser target section of docs/ts-runtime-contract.md")


def test_the_strip_step_rewrites_the_import_graph_only(tmp_path):
    """The step edits module specifiers, not text that looks like one.

    A regex over the emitted JavaScript also rewrote `from './dep.ts'` inside a
    string literal, which is a program's data. It also has to resolve each
    relative specifier against `inputs`, because outputs are flat in
    `<out-dir>`: the default emitted module imports `../runtime.ts`, and the
    page can only load `./runtime.js` beside the module.
    """
    _require(_node_typescript_reason())
    (tmp_path / "dep.ts").write_text("export const x = 1\n", encoding="utf-8")
    (tmp_path / "module.ts").write_text(
        "import { x } from './dep.ts'\n"
        "export const text = \"from './dep.ts' and import('./dep.ts')\"\n"
        "export const value = x\n", encoding="utf-8")
    out = tmp_path / "out"
    run = subprocess.run(
        ["node", str(_STRIP), str(out), str(tmp_path / "module.ts"), str(tmp_path / "dep.ts")],
        cwd=_HERE, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert (out / "dep.js").exists(), "the step did not write the second input"
    module_js = (out / "module.js").read_text(encoding="utf-8")
    assert "./dep.js" in module_js, module_js
    assert module_js.count("./dep.ts") == 2, (
        "the strip step rewrote text inside a string literal, so a program's "
        "data changed:\n" + module_js)

    # A relative .ts specifier the step was not given cannot be resolved by the
    # page either, so it fails and names the file instead of writing a module
    # that cannot load.
    (tmp_path / "broken.ts").write_text(
        "import { x } from './missing.ts'\nexport const y = x\n", encoding="utf-8")
    run = subprocess.run(
        ["node", str(_STRIP), str(tmp_path / "out2"), str(tmp_path / "broken.ts")],
        cwd=_HERE, capture_output=True, text=True, timeout=120)
    assert run.returncode != 0, "a specifier naming no input was accepted"
    assert "missing.ts" in run.stderr, run.stderr


def test_an_emitted_module_runs_in_a_browser_engine():
    """The emitted module, the runtime and cordis all run in headless Chrome."""
    _require(_browser_reason())
    generated = _HERE / "tests" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    page_dir = Path(tempfile.mkdtemp(prefix="browser-target-", dir=generated))
    server = None
    try:
        # A relative runtime import that does not land beside the module:
        # `tests/generated/<dir>/module.ts` reaches the backend root with
        # `../../../runtime.ts` (the emitted default `../runtime.ts` from
        # `tests/generated/` is the same shape one level up). The strip step
        # has to resolve it against the inputs and rewrite it to the flat
        # `./runtime.js`.
        source = _emitter().emit(
            compile_source(_DOC, "browser_target.rvl"),
            runtime_import="../../../runtime.ts")
        module_ts = page_dir / "module.ts"
        module_ts.write_text(source, encoding="utf-8")

        strip = subprocess.run(
            ["node", str(_STRIP), str(page_dir), str(module_ts), str(_HERE / "runtime.ts")],
            cwd=_HERE, capture_output=True, text=True, timeout=120)
        assert strip.returncode == 0, (
            "scripts/strip-types.mjs refused the emitted module:\n"
            + strip.stdout + strip.stderr)

        module_js = (page_dir / "module.js").read_text(encoding="utf-8")
        assert (page_dir / "runtime.js").exists(), "the step did not write the runtime"
        assert "./runtime.js" in module_js, (
            "the strip step did not rewrite the emitted module's runtime import "
            "to the flat ./runtime.js; a page cannot follow ../../runtime.ts:\n"
            + module_js[:400])
        for specifier in re.findall(r"""from\s*['"]([^'"]+)['"]""", module_js):
            if specifier.startswith("."):
                assert (page_dir / specifier).exists(), (
                    f"the stripped module names {specifier!r}, which is not in "
                    "the served directory")

        importmap = json.dumps({"imports": {
            "cordis": "/node_modules/cordis/lib/index.js",
            "cosmokit": "/node_modules/cosmokit/lib/index.mjs",
            "@standard-schema/spec": "/node_modules/@standard-schema/spec/dist/index.js",
        }})
        (page_dir / "index.html").write_text(_PAGE.format(importmap=importmap), encoding="utf-8")

        server, _thread = _serve()
        url = f"http://127.0.0.1:{server.server_address[1]}/" \
              f"tests/generated/{page_dir.name}/index.html"
        result = _dump_dom(_chrome(), url, page_dir / "profile", _HERE)
        match = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
        assert match, (
            f"chrome produced no page title (exit {result.returncode}):\n"
            + result.stdout[-2000:] + result.stderr[-2000:])
        title = match.group(1).strip()
        assert not title.startswith("REVL-BROWSER-ERROR"), (
            "the page failed in the browser:\n" + title)
        assert title.startswith("REVL-BROWSER-OK"), (
            f"unexpected browser verdict: {title!r}\n" + result.stderr[-2000:])

        fields = dict(part.split("=", 1) for part in title.split()[1:])
        assert fields["fn"] == "5", f"the emitted fn returned {fields['fn']}"
        assert fields["service"] == "42", (
            f"the component's service method returned {fields['service']}")
        assert sorted(fields["host"].split(",")) == sorted(_BROWSER_HOST), (
            "runtime.ts's host surface changed; the browser target in "
            "docs/ts-runtime-contract.md and _BROWSER_HOST must change with it:\n"
            f"page saw {fields['host']}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(page_dir, ignore_errors=True)
