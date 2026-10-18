"""Issue #2536: a refusal with a known remedy carries its `next` (#1691), and
a census over the source says so for every site, so a new one cannot be missed.

`tests/test_mcp_refusal_next_1691.py` lists its sites by hand, which is how the
cascade refusal slipped through with a prose hint and no `next`. Two things
are pinned here:

* the cascade refusal, driven through the real dispatch (`handle()`), warm and
  cold: `next` is the same `revl_change` with `cascade: true` and without
  `commit` (the cascade withdraws more than the caller named, so a client that
  follows `next` as-is gets the plan, and committing is a separate call). Sent
  unchanged it answers `committed: false` with the cascade; its prose is
  rendered from the same data (`remedy.describe`); a cold refusal keeps
  `loaded: false`;
* a census built from the source of `src/revl/mcp/*.py` (not from a list of
  sites). It finds two kinds of refusal that name their remedy in prose, and
  requires each to carry `next`:
    A. a refusal payload (a dict with `diagnostics` or `ok: False`) that holds
       a prose `hint`;
    B. a refusal built by `_session_error(...)`, or a raised error, whose
       message says to "call revl_x" / "send revl_x" / "run revl_x".
  A site with no single mechanical remedy, or one whose remedy the caller has
  to choose, is listed in an exemption table with the reason. An exemption
  that no longer matches a site fails, so the table cannot go stale, and a
  site that has gained `next` must leave it.

Limits. The census walks only `src/revl/mcp/*.py`. It does not see a hint
passed as a `hint=` constructor argument (identity.py:166, session.py:8087), a
hint assigned after the dict is built, or an error built in a variable and
raised later. It is a ratchet that stops the shapes it knows from growing, not
a proof that every refusal with a remedy carries `next`.
"""

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import remedy  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

MCP = ROOT / "src" / "revl" / "mcp"

# ------------------------------------------------------------ the census

#: Prose that tells the caller to make a call: "call revl_load first", ...
_NAMES_A_CALL = re.compile(r"\b(?:call|send|run) `?revl_\w+")

#: Sites that name a remedy in prose and carry no `next`, as
#: (file, enclosing function) -> why none. Each one is a known unconverted site,
#: or one whose remedy the caller has to choose; none is a licence for a new
#: one. The census sees only the shapes in the module docstring's Limits.
EXEMPT = {
    # A: the path jail. The caller rewrites its own paths, or an operator
    # widens `--root`; no single call is the remedy. Unconverted.
    ("server.py", "_jail_refusal"):
        "rewrite paths or widen --root; no single call",
    # B: `revl_edit` raised with nothing loaded. The server's load gate answers
    # first (with a `next`), so this is the library-level backstop.
    ("edit.py", "apply_edit"):
        "backstop behind the server's own load gate, which carries `next`",
    # B: asking a recording verb of a composition loaded without `record`.
    # The remedy is a single reload with `record: true`, but that call is
    # destructive (it drops the running state), so the caller has to choose it.
    ("session.py", "_timeline"):
        "one reload with record: true, but destructive: the caller chooses",
    ("session.py", "timeline"):
        "one reload with record: true, but destructive: the caller chooses",
}


def _text_of(node) -> str:
    return " ".join(n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _keys(node: ast.Dict) -> set:
    return {k.value for k in node.keys if isinstance(k, ast.Constant)}


def _is_refusal(node: ast.Dict) -> bool:
    if "diagnostics" in _keys(node):
        return True
    return any(isinstance(k, ast.Constant) and k.value == "ok"
               and isinstance(v, ast.Constant) and v.value is False
               for k, v in zip(node.keys, node.values))


def _holds_hint(node: ast.Dict) -> bool:
    """`hint` as a key of this payload, or of any dict nested in it (the
    diagnostics carry theirs)."""
    return any(isinstance(n, ast.Dict) and "hint" in _keys(n)
               for n in ast.walk(node))


class _Sites(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module, self.scope, self.raising, self.in_refusal = module, [], 0, False
        self.sites: list[tuple] = []   # (kind, module, function, line, has_next)

    def _function(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Raise(self, node):
        self.raising += 1
        self.generic_visit(node)
        self.raising -= 1

    def visit_Dict(self, node):
        outermost = not self.in_refusal and _is_refusal(node)
        if outermost and _holds_hint(node):
            self.sites.append(("A", self.module, self._where(), node.lineno,
                               "next" in _keys(node)))
        was, self.in_refusal = self.in_refusal, self.in_refusal or outermost
        self.generic_visit(node)
        self.in_refusal = was

    def visit_Call(self, node):
        name = (node.func.id if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", ""))
        built = name == "_session_error" or (self.raising and name.endswith("Error"))
        if built and node.args and _NAMES_A_CALL.search(_text_of(node.args[0])):
            # `NothingLoaded` carries `next` by type (`remedy.for_error`)
            first = node.args[0]
            typed = name == "NothingLoaded" or (
                isinstance(first, ast.Call)
                and getattr(first.func, "id", "") == "NothingLoaded")
            explicit = any(k.arg == "next" for k in node.keywords)
            self.sites.append(("B", self.module, self._where(), node.lineno,
                               typed or explicit))
        self.generic_visit(node)

    def _where(self) -> str:
        return self.scope[-1] if self.scope else "<module>"


def census() -> list[tuple]:
    sites = []
    for path in sorted(MCP.glob("*.py")):
        walker = _Sites(path.name)
        walker.visit(ast.parse(path.read_text()))
        sites += walker.sites
    return sites


def test_the_census_finds_the_refusal_sites_it_is_built_on():
    """A positive control: a census that found nothing would pass silently."""
    sites = census()
    assert {kind for kind, *_ in sites} == {"A", "B"}
    assert ("A", "change.py", "cascade_refusal") in {s[:3] for s in sites}
    assert ("B", "server.py", "_unload_stale_refusal") in {s[:3] for s in sites}
    assert len(sites) >= 8, sites


def test_every_refusal_that_names_its_remedy_carries_a_next():
    missing = [f"{kind} {module}:{line} in {where}"
               for kind, module, where, line, has_next in census()
               if not has_next and (module, where) not in EXEMPT]
    assert not missing, (
        "a refusal names a remedy in prose and has no `next` (issue #1691): "
        "add `next=remedy.call(...)`, or exempt the site in EXEMPT with the "
        "reason no single call is its remedy:\n" + "\n".join(missing))


def test_the_exemptions_are_live():
    """An exemption must match a site that still lacks `next`."""
    lacking = {(module, where) for _, module, where, _, has_next in census()
               if not has_next}
    stale = sorted(set(EXEMPT) - lacking)
    assert not stale, f"stale exemptions (gone, or converted): {stale}"


# ------------------------------------------- the cascade refusal, in use

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime: "
           "install it with `sh backends/python/setup.sh`")

SOURCE = (
    "service Store { fn get() -> Str }\n"
    "service Api { fn read() -> Str }\n"
    "service Clock { fn now() -> Int }\n"
    "\n"
    "component MemStore provides store: Store {\n"
    '  provide store { fn get() = "stored" }\n'
    "}\n"
    "\n"
    "component ApiImpl requires store: Store provides api: Api {\n"
    "  provide api { fn read() = store.get() }\n"
    "}\n"
    "\n"
    "component FixedClock provides clock: Clock {\n"
    "  provide clock { fn now() = 7 }\n"
    "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _components() -> set[str]:
    return {c["name"] for c in server_mod.SESSION.ir["manifest"]["components"]}


def _reset() -> None:
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    _call("revl_change", {"discard": True})


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    remedy.forget()
    _reset()
    yield tmp_path
    _reset()
    remedy.forget()
    server_mod.AUTHORING = old


def _carried(kind: str, tmp_path: Path) -> dict:
    if kind == "files":
        path = tmp_path / "app.rvl"
        path.write_text(SOURCE)
        return {"files": [str(path)]}
    return {"source": SOURCE}


def _assert_the_cascade_next(refusal: dict, sent: dict) -> dict:
    """`next` is one ready `revl_change` call: the call that was refused, with
    the cascade asked for and never `commit`."""
    nxt = refusal["next"]
    carried = {k: v for k, v in sent.items() if k != "commit"}
    assert nxt == remedy.call("revl_change", {
        **carried, "withdraw": {"component": "MemStore", "cascade": True}}), nxt
    assert "commit" not in nxt["arguments"]
    assert nxt["ready"] is True and "needs" not in nxt
    assert nxt["tool"] in {tool["name"] for tool in server_mod._ADVERTISED}
    # the prose is rendered from the same data, so the two cannot disagree
    assert refusal["diagnostics"][0]["message"].endswith(remedy.describe(nxt))
    assert "cascade: true" in refusal["hint"], "the prose hint stays"
    return nxt


def _replay_then_commit(nxt: dict) -> None:
    """`next` exactly as handed over: it plans the whole cascade and commits
    nothing. Committing it is a separate call."""
    planned = _call(nxt["tool"], nxt["arguments"])
    assert planned["ok"] is True and planned["committed"] is False, planned
    assert [e["component"] for e in planned["plan"]["cascade"]] == ["ApiImpl"]
    assert planned["proposal"]["replacing"] == ["MemStore", "ApiImpl"]
    done = _call("revl_change", {"commit": True})
    assert done["ok"] is True and done["committed"] is True, done
    assert _components() == {"FixedClock"}


@needs_runtime
@pytest.mark.parametrize("kind", ["files", "source"])
def test_a_cold_stranding_withdrawal_names_the_cascade_call(kind, clean_session):
    carried = _carried(kind, clean_session)
    sent = {"withdraw": "MemStore", **carried}
    refusal = _call("revl_change", sent)
    assert refusal["ok"] is False and refusal["committed"] is False, refusal
    assert refusal["loaded"] is False and server_mod.SESSION.loaded is False
    nxt = _assert_the_cascade_next(refusal, sent)
    _replay_then_commit(nxt)


@needs_runtime
def test_a_warm_stranding_withdrawal_names_the_cascade_call():
    assert _call("revl_load", {"source": SOURCE})["ok"] is True
    sent = {"withdraw": "MemStore"}
    refusal = _call("revl_change", sent)
    assert refusal["ok"] is False and "loaded" not in refusal, refusal
    nxt = _assert_the_cascade_next(refusal, sent)
    _replay_then_commit(nxt)


@needs_runtime
def test_a_refused_commit_names_a_next_that_does_not_commit():
    """A refusal of `commit: true`: the cascade withdraws more than the caller
    named, so `next` leaves `commit` out and, replayed unchanged, plans."""
    sent = {"withdraw": {"component": "MemStore"}, "commit": True,
            "source": SOURCE}
    refusal = _call("revl_change", sent)
    assert refusal["ok"] is False and refusal["committed"] is False
    assert refusal["next"]["arguments"] == {
        "withdraw": {"component": "MemStore", "cascade": True},
        "source": SOURCE}
    assert refusal["next"]["ready"] is True
    assert "leaves out `commit`" in refusal["hint"]
    assert "cascade: true" in refusal["hint"]
    planned = _call(refusal["next"]["tool"], refusal["next"]["arguments"])
    assert planned["ok"] is True and planned["committed"] is False, planned
    assert [e["component"] for e in planned["plan"]["cascade"]] == ["ApiImpl"]
    assert _components() == {"MemStore", "ApiImpl", "FixedClock"}, "nothing committed"


@needs_runtime
def test_a_withdrawal_with_nothing_stranded_offers_no_cascade_call():
    refusal = _call("revl_change", {"withdraw": "FixedClock", "source": SOURCE})
    assert refusal["ok"] is True and "next" not in refusal, refusal
