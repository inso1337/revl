"""`revl_change {add}` and `verified.guarantees` (issue #1695, the slice after
#1741).

Adding a declaration was not an intent: an agent had to read the buffer with
`revl_source` to learn its length, then send a raw range edit at that offset.
And `verified` reported admission as one verdict, not which guarantee held.

* `{add: {source, target?}}` appends new declarations in one call, through
  revl_edit's own path (jail, trust rule, gates, drafts, speculation);
* a name already declared is refused before anything applies, pointing at
  `{replace}`; several buffers and no `target` is refused with the list;
* `verified.guarantees` is the G1-G9 self-check: all pass when admitted, the
  failing guarantee with its fix when refused.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime "
           "- install it with `sh backends/python/setup.sh`")

SOURCE = ("service Store { fn get() -> Str }\n"
          "service Clock { fn now() -> Int }\n"
          "\n"
          "component MemStore provides store: Store {\n"
          '  provide store { fn get() = "stored" }\n'
          "}\n")

CLOCK = ("component FixedClock provides clock: Clock {\n"
         "  provide clock { fn now() = 7 }\n"
         "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _components() -> set[str]:
    return {c["name"] for c in server_mod.SESSION.ir["manifest"]["components"]}


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION.pending_draft = None
    server_mod.SESSION.draft = None
    server_mod.AUTHORING = old
    # a speculative case leaves a proposal behind
    _call("revl_change", {"discard": True})


@pytest.fixture
def loaded():
    assert _call("revl_load", {"source": SOURCE})["ok"] is True
    assert _components() == {"MemStore"}


def _rows(verified: dict) -> list:
    g = verified["guarantees"]
    return g.get("guarantees") or g.get("rows")


# ------------------------------------------------ exit 1 and 6


def test_add_commits_a_new_component_in_one_call(loaded):
    result = _call("revl_change", {"commit": True, "add": {"source": CLOCK}})
    assert result["ok"] is True and result["committed"] is True, result
    assert result["intent"] == "add"
    assert result["components"] == [{"component": "FixedClock", "change": "added"}]
    assert _components() == {"MemStore", "FixedClock"}
    assert _call("revl_call", {"key": "clock", "method": "now"})["result"] == 7
    rows = _rows(result["verified"])
    assert [r["code"] for r in rows] == [f"G{n}" for n in range(1, 10)]
    assert all(r["status"] == "pass" for r in rows)


# ------------------------------------------------ exit 2


def test_a_speculative_add_proposes_and_changes_nothing(loaded):
    result = _call("revl_change", {"add": {"source": CLOCK}})
    assert result["ok"] is True and result["committed"] is False, result
    assert result["verified"]["admission"] == "passed"
    assert all(r["status"] == "pass" for r in _rows(result["verified"]))
    assert _components() == {"MemStore"}
    committed = _call("revl_change", {"commit": True})
    assert committed["committed"] is True, committed
    assert _components() == {"MemStore", "FixedClock"}


# ------------------------------------------------ exit 3


def test_adding_a_name_that_exists_is_refused_and_names_replace(loaded):
    result = _call("revl_change", {"commit": True, "add": {
        "source": "component MemStore provides store: Store {\n"
                  '  provide store { fn get() = "other" }\n}\n'}})
    assert result["ok"] is False and result["committed"] is False
    message = result["diagnostics"][0]["message"]
    assert "MemStore is already declared" in message and "replace" in message
    assert "guarantees" not in result["verified"]   # never reached a compile
    assert _call("revl_call", {"key": "store", "method": "get"})["result"] == "stored"


def test_adding_nothing_is_refused(loaded):
    result = _call("revl_change", {"commit": True, "add": {"source": "// hi\n"}})
    assert result["ok"] is False
    assert "declares nothing" in result["diagnostics"][0]["message"]


def test_add_without_source_is_a_malformed_intent(loaded):
    result = _call("revl_change", {"add": {"target": "x"}})
    assert result["ok"] is False
    assert "`add` is {source, target?}" in result["diagnostics"][0]["message"]


# ------------------------------------------------ exit 4


def test_an_add_that_breaks_a_guarantee_commits_nothing_and_names_it(loaded):
    # reads `store` without declaring it: G1, declared access
    broken = ("component Sneaky provides clock: Clock {\n"
              "  provide clock { fn now() = store.get().length() }\n}\n")
    result = _call("revl_change", {"commit": True, "add": {"source": broken}})
    assert result["ok"] is False and result["committed"] is False, result
    assert result["verified"]["admission"] == "refused"
    failing = [r for r in _rows(result["verified"]) if r["status"] == "fail"]
    assert [r["code"] for r in failing] == ["G1"], result["verified"]["guarantees"]
    assert failing[0]["fix"]
    assert "`store` is not a declared requirement" in \
        failing[0]["failures"][0]["message"]
    assert _components() == {"MemStore"}


# ------------------------------------------------ exit 5


def test_several_buffers_need_a_target_and_the_target_is_honoured(clean_session):
    a = clean_session / "a.rvl"
    b = clean_session / "b.rvl"
    a.write_text("service Store { fn get() -> Str }\nservice Clock { fn now() -> Int }\n"
                 'component MemStore provides store: Store {\n'
                 '  provide store { fn get() = "stored" }\n}\n', encoding="utf-8")
    b.write_text("service Bell { fn ring() -> Int }\n"
                 "component Ringer provides bell: Bell {\n"
                 "  provide bell { fn ring() = 1 }\n}\n", encoding="utf-8")
    assert _call("revl_load", {"files": [str(a), str(b)]})["ok"] is True

    refused = _call("revl_change", {"commit": True, "add": {"source": CLOCK}})
    assert refused["ok"] is False
    message = refused["diagnostics"][0]["message"]
    assert "name the one to edit in `target`" in message
    assert str(a) in message and str(b) in message

    result = _call("revl_change", {"commit": True,
                                   "add": {"source": CLOCK, "target": str(a)}})
    assert result["committed"] is True, result
    assert _components() == {"MemStore", "Ringer", "FixedClock"}
    held = server_mod.SESSION.origin["files_content"]
    assert "FixedClock" in held[str(a)] and "FixedClock" not in held[str(b)]
    assert "FixedClock" not in a.read_text(encoding="utf-8")   # disk untouched


# ------------------------------------------------ the other intents gain it too


def test_a_withdrawal_reports_the_guarantees_too(loaded):
    assert _call("revl_change", {"commit": True, "add": {"source": CLOCK}})["committed"]
    result = _call("revl_change", {"commit": True, "withdraw": "FixedClock"})
    assert result["committed"] is True
    assert all(r["status"] == "pass" for r in _rows(result["verified"]))
