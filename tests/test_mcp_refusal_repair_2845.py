"""Issue #2845: a refusal carries the repair, not only the diagnosis.

Two refusal classes the authoring loop hits most carry a structured `repair`
beside their diagnostics:

* the typecheck/lower type-mismatch refusals. A record literal that does not
  match the record type it is checked against is the shape both check
  positions construct (`typecheck.check_ast` for a checked body, and
  `typecheck.check_ir`, called from `lower`, for a lowered one), and the
  compiler knows the exact field edit that closes it. A plain type mismatch
  knows the required and the actual type but not the minimal edit, so it says
  so with `known: false` instead of omitting the field;
* the admission refusal `STATE_UNDISCLOSED`
  (`session._enforce_declared_disclosure`), which knows the template name or
  the handoff key and the edit that retires it.

The old fields are pinned unchanged: the diagnostics are byte for byte what
they were, and `repair` is additive beside them. The repair's shape is
`target` (a `file:line` locator plus what sits there, or a template name or a
handoff key), `edit` (the minimal edit, no `command` shape lands in this
slice), and `reason` (the rule that refused, in one sentence). A refusal with
no known minimal edit carries no `edit` and says `known: false` in words.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.typecheck import check_ir  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")

#: a record literal missing one declared field: the checker knows the edit
MISSING_FIELD = ("type Point = { x: Int, y: Int }\n"
                 "fn p() -> Point { return { x: 1 } }\n")

#: a record literal carrying a field the declared record does not declare
EXTRA_FIELD = ("type Point = { x: Int, y: Int }\n"
               "fn p() -> Point { return { x: 1, y: 2, z: 3 } }\n")

#: a definite mismatch with no minimal edit to name: the position requires
#: `Int` and the value is `Str`, and the compiler cannot tell which side the
#: author meant to change
PLAIN_MISMATCH = "fn g(x: Str) -> Int { return x }\n"

#: an `Opt[...]` where the plain type is required: the checker's known shape,
#: whose repair is the unwrap
OPT_MISMATCH = "fn f(x: Opt[Str]) -> Str { return x }\n"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session, undo stack and authoring
    trust. Start each test from the trust it found and put it back, so a loaded
    composition cannot leak into the next refusal test."""
    from revl.mcp import server as server_mod

    saved = server_mod.AUTHORING
    server_mod.UNDO_STACK.clear()
    try:
        yield server_mod
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.AUTHORING = saved
        server_mod.UNDO_STACK.clear()
        server_mod._remedy.forget()


def _stateful_instance_source(version: int) -> str:
    """`Worker` is a template (a `spawn` target) holding a `Map`; `Supervisor`
    spawns one and exposes seed/read/ver through the handle. A successor that
    changes `Worker` cannot inherit the live instance's operator state."""
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
service Admin {{
  fn seed(k: Str, v: Str)
  fn read(k: Str) -> Opt[Str]
  fn ver() -> Int
}}
component Worker provides store: Store {{
  let m = effect Map.new() undo m.drop()
  provide store {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{
    fn seed(k, v) = w.store.put(k, v)
    fn read(k) = w.store.get(k)
    fn ver() = w.store.version()
  }}
}}
"""


def test_a_record_literal_refusal_carries_the_edit_that_closes_it(
        _fresh_server_session):
    """The checker's check position knows the missing field, so the refusal
    names the field to add and where to add it."""
    refused = _call("revl_check", {"source": MISSING_FIELD})

    assert refused["ok"] is False
    repair = refused["repair"]
    assert repair["target"] == "<candidate>.rvl:2 record literal for `Point`"
    assert "add the field `y`" in repair["edit"]
    assert "record literal" in repair["reason"]
    assert repair.get("known", True) is True


def test_a_known_repair_drops_an_unknown_record_field(_fresh_server_session):
    """The same check the other way round: the literal has a field the record
    does not declare, and the edit drops it."""
    refused = _call("revl_check", {"source": EXTRA_FIELD})

    assert refused["ok"] is False
    assert "drop the unknown field `z`" in refused["repair"]["edit"]


def test_the_lowering_ir_check_constructs_the_same_repair():
    """`lower` checks a lowered body through `typecheck.check_ir`, so the
    repair is constructed there too, not only in the checker's AST position."""
    types = {"Point": {"kind": "record",
                       "fields": {"x": "Int", "y": "Int"}}}
    node = {"kind": "record",
            "fields": [["x", {"kind": "int", "value": 1}]]}

    with pytest.raises(RevlError) as caught:
        check_ir(node, "Point", {}, types, {}, "app.rvl", 7, "")

    repair = caught.value.repair
    assert repair["target"] == "app.rvl:7 record literal for `Point`"
    assert "add the field `y`" in repair["edit"]
    assert "record literal" in repair["reason"]


def test_an_optional_mismatch_carries_the_unwrap(_fresh_server_session):
    """The mismatch constructor's other known shape: an `Opt[...]` where the
    plain type is required, and the repair is the unwrap."""
    refused = _call("revl_check", {"source": OPT_MISMATCH})

    assert refused["ok"] is False
    repair = refused["repair"]
    assert repair["target"] == "<candidate>.rvl:1 this function's return"
    assert "unwrap the optional" in repair["edit"]
    assert "`Str`" in repair["reason"] and "Opt" in repair["reason"]
    assert repair.get("known", True) is True


def test_the_principal_mismatch_names_its_producer():
    """A `Principal` position reached with a non-Principal value is refused by
    the same constructor, and its repair names the sole producer."""
    from revl.typecheck import mismatch

    error = mismatch("app.rvl", 3, "this argument", "Principal", "Str")

    assert error.repair["target"] == "app.rvl:3 this argument"
    assert "Auth.validate" in error.repair["edit"]
    assert "sole producer" in error.repair["reason"]


def test_a_mismatch_with_no_known_repair_says_so(_fresh_server_session):
    """A definite mismatch whose minimal edit the compiler does not know still
    carries `repair`, and says in words that there is none. Omitting the field
    would read as "not checked" rather than "checked, nothing known"."""
    refused = _call("revl_check", {"source": PLAIN_MISMATCH})

    assert refused["ok"] is False
    repair = refused["repair"]
    assert repair["known"] is False
    assert "edit" not in repair and "command" not in repair
    assert repair["target"] == "<candidate>.rvl:1 this function's return"
    assert "`Int`" in repair["reason"] and "`Str`" in repair["reason"]


@needs_runtime
def test_state_undisclosed_carries_the_edit_that_retires_it(
        _fresh_server_session, tmp_path):
    """The disclosure gate knows which template crossed, so the refusal names
    the template and the edit that keeps it from crossing: re-declare it
    unchanged, or quiesce the live instances first."""
    server = _fresh_server_session
    operator_file = tmp_path / "instances.rvl"
    operator_file.write_text(_stateful_instance_source(1))
    server.set_authoring_trust(host_code=False, roots=[str(tmp_path)])
    assert _call("revl_load", {"files": [str(operator_file)]})["ok"] is True
    assert _call("revl_call", {"key": "admin", "method": "seed",
                               "args": ["alice", "42"]})["ok"] is True

    swapped = _call("revl_swap", {"source": _stateful_instance_source(2)})

    assert swapped["ok"] is False
    assert swapped["code"] == "STATE_UNDISCLOSED"
    repair = swapped["repair"]
    assert repair["target"] == "template `Worker`"
    assert "re-declare `Worker`" in repair["edit"]
    assert "quiesce" in repair["edit"]
    assert "operator state" in repair["reason"]
    assert repair.get("known", True) is True
    # gen N is intact and still serving its own code and state
    assert _call("revl_call", {"key": "admin", "method": "ver"})["result"] == 1
    assert _call("revl_call", {"key": "admin", "method": "read",
                               "args": ["alice"]})["result"] == "42"


def test_the_handoff_shape_carries_its_repair():
    """The other STATE_UNDISCLOSED shape: an accept type that cannot hold the
    running export. Its repair names the handoff key and both types, so the
    gate's second construction site is pinned too."""
    from types import SimpleNamespace

    from revl.mcp import session as session_mod

    session = session_mod.Session()
    session._driver = SimpleNamespace(
        runtime=SimpleNamespace(live_instances=lambda name: []))
    old_ir = {"manifest": {"templates": []},
              "components": [{"name": "Db",
                              "handoff": {"key": "db",
                                          "type": "Map[Str, Str]"}}]}
    new_ir = {"manifest": {"templates": []},
              "components": [{"name": "Db",
                              "handoff": {"key": "db",
                                          "type": "Map[Str, Int]"}}]}

    with pytest.raises(session_mod.SessionError) as caught:
        session._enforce_declared_disclosure(old_ir, new_ir)

    error = caught.value
    assert error.code == "STATE_UNDISCLOSED"
    repair = error.repair
    assert repair["target"] == "handoff `db`"
    assert "handoff accept db" in repair["edit"]
    assert "Map[Str, Str]" in repair["edit"]
    assert "Map[Str, Int]" in repair["reason"]
    assert "Map[Str, Str]" in repair["reason"]


def test_the_old_fields_are_unchanged(_fresh_server_session):
    """`repair` is additive: the diagnostic beside it is the same record the
    refusal carried before (severity, code, category, file, line, message,
    guarantee and fix), and the verdict is still `ok: false`."""
    refused = _call("revl_check", {"source": MISSING_FIELD})

    assert refused["ok"] is False
    assert refused["diagnostics"] == [{
        "severity": "error",
        "code": "T1",
        "category": "type-mismatch",
        "file": "<candidate>.rvl",
        "line": 2,
        "message": "record literal for `Point` has missing `y`",
        "guarantee": "declared types are checked",
        "fix": "make the types agree at the call site, or change the declaration",
    }]
    assert set(refused) >= {"ok", "diagnostics", "selfCheck", "repair"}
