"""In-memory compilation and the live MCP session.

Two properties are under test here. First, that a draft component never
touches the filesystem: an agent can check, admit, load, call and tear down
code that has no file. Second — the load-bearing one — that a *rejected*
candidate cannot deploy, and the composition it failed to replace keeps
answering.

Only the second half needs a runtime. The in-memory compilation tests below
are pure frontend and must run everywhere, so the cordis-py gate is a marker
on the live-session tests (`@needs_runtime`) rather than a module-level
`importorskip` — that form aborts *collection* of the whole file and took the
runtime-free tests down with it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

READONLY = """
service Cache { fn get(key: Str) -> Opt[Str] }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key) }
}
"""

CACHE = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
}
"""

# The same service, with a component whose activation body cannot complete.
# Loading it is allowed and observable (item 372); making it the rollback
# target is what the activation health gate refuses. It carries a real
# `provide cache` block: a declared key with no block is refused at the
# checker (A9, issue #1172), and this fixture must fail at ACTIVATION.
BROKEN_ACTIVATION = """
service Cache { fn get(key: Str) -> Opt[Str]
                fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn get(key) = store.get(key)
                  fn size() = 0 }
  fail "activation cannot complete"
}
"""

USING_MODULE = 'use "./lib.rvl" { double }\n' + """
service S { fn f(a: Int) -> Int }
component C provides s: S { provide s { fn f(a) = double(a) } }
"""


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


# ------------------------------------------------------ in-memory compilation

def test_inline_source_never_touches_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = _call("revl_check", {"source": READONLY})
    assert payload["ok"] is True
    assert list(tmp_path.iterdir()) == []


def test_use_imports_resolve_from_memory():
    payload = _call("revl_check", {
        "source": USING_MODULE,
        "modules": {"./lib.rvl": "pub fn double(n: Int) -> Int { return n * 2 }"},
    })
    assert payload["ok"] is True, payload
    assert payload["loadOrder"] == ["C"]


def test_no_modules_supplied_says_so():
    # an empty `modules` is the no-imports fast path: the diagnostic names
    # the fix rather than complaining about a missing file
    payload = _call("revl_check", {"source": USING_MODULE, "modules": {}})
    assert payload["ok"] is False
    assert "`modules=`" in payload["diagnostics"][0]["message"]


def test_a_module_missing_from_the_supplied_set_is_a_normal_rejection():
    payload = _call("revl_check", {
        "source": USING_MODULE,
        "modules": {"./other.rvl": "pub fn unrelated() -> Int { return 1 }"},
    })
    assert payload["ok"] is False
    assert "cannot find imported module `./lib.rvl`" in payload["diagnostics"][0]["message"]


def test_compile_source_carries_the_ambient_manifest():
    running = compile_source(READONLY)
    admitted = compile_source(
        """service Cache { fn get(key: Str) -> Opt[Str] }
service Log { fn note(m: Str) -> Int }
component Watcher requires cache: Cache provides log: Log {
  provide log { fn note(m) = 1 }
}""",
        manifest=running,
    )
    assert [c["name"] for c in admitted["components"]] == ["Watcher"]
    assert set(admitted["manifest"]["loadOrder"]) == {"C", "Watcher"}


# ---------------- step 5 reads what steps 1-4 mutated (issue #2117)
#
# `revl_check` is the loop's verification step, but the loop mutates the
# composition the SESSION holds: `revl_scaffold` -> `revl_load` (which opens a
# draft for a holed scaffold, #1727) -> `revl_edit` fills -> `revl_swap`. A
# check that could only compile a SUPPLIED source made step 5 unreachable for
# the loop's own artifact — the agent had to re-send the whole file, which the
# scaffold tells it not to do. So the bare `revl_check {}` (and the explicit
# `{"session": true}`) checks the held working set: the same set `revl_swap {}`
# re-admits and `revl_source {}` reads, and the same substance comes back
# (`selfCheck`, `holes`, `boundary`, `admissible`), so it is a check.
#
# The second half is the shape of the refusal. `revl_check {}` on a session
# holding nothing used to answer `ok: false` with a raw
# `ValueError: provide \`source\` or \`files\`` under `category: "internal"` —
# an argument error wearing a composition verdict's clothes. An agent that asks
# "is what I just built correct?" and reads `ok: false` starts changing code
# that is right. So a usage error is now `category: "usage"`, with a `fix`
# naming the missing argument and a `next` carrying the call to make.
#
# Both halves need a session that HOLDS a composition, and only `revl_load`
# opens one — a runtime verb (`runtime_gate.RUNTIME_VERBS`). The held form
# reads `loaded`, `origin`, `draft` and `pending_draft` and never touches the
# driver, so these tests stub the boot, not the read.

# a composition whose only fault is a reach the component never required: a
# verdict about the code, which `ok: false` is for
UNDECLARED = """
service S { fn f(a: Int) -> Int }
component C provides s: S { provide s { fn f(a) = nope(a) } }
"""

# what a scaffold returns before its holes are filled: a draft that compiles
HOLED = """
service S { fn f(a: Int) -> Str }
component C provides s: S {
  provide s { fn f(a) { return hole } }
}
"""


class _HeldSession:
    """A session holding a composition, with no runtime booted.

    `Session.loaded` is `_driver is not None`, and the held form of
    `revl_check` reads only `loaded`, `origin`, `draft` and `pending_draft` —
    never the driver — so what is stubbed here is the boot, not the read."""

    def __init__(self, source=None, loaded=True):
        self.ir = None
        self.origin = ({"source": source, "modules": {}}
                       if source is not None else None)
        self.draft = None
        self.pending_draft = None
        self.loaded = loaded

    def unload(self):
        self.loaded = False


def _held(monkeypatch, session):
    from revl.mcp import server as server_mod

    monkeypatch.setattr(server_mod, "SESSION", session)
    return session


def test_a_held_composition_is_checkable_without_resending_it(monkeypatch):
    """The bare form names no candidate, so it checks what the session holds —
    and answers with the supplied form's substance, so step 5 verifies the
    artifact steps 1-4 built (issue #2117)."""
    held = _held(monkeypatch, _HeldSession(READONLY))

    payload = _call("revl_check", {})

    assert payload["ok"] is True, payload
    assert payload["checked"] == "session"
    assert payload["components"] == [{"name": "C", "requires": [],
                                      "provides": ["cache"]}]
    assert payload["selfCheck"]["admissible"] is True
    assert [g["code"] for g in payload["selfCheck"]["guarantees"]] == [
        f"G{n}" for n in range(1, 10)]
    assert payload["selfCheck"]["summary"] == {"pass": 9, "fail": 0,
                                               "unchecked": 0}
    assert payload["holes"] == []
    assert payload["boundary"]["C"] == {"emissions": [], "capabilities": {},
                                        "compensated": 0, "awaits": 0,
                                        "externs": []}
    # nothing was sent, so there is no canonical form to report — and the
    # session is byte-identical afterwards: verifying does not disturb the work
    assert "canonicalSource" not in payload
    assert held.origin == {"source": READONLY, "modules": {}}
    assert held.draft is None


def test_the_explicit_session_form_is_the_bare_form(monkeypatch):
    """`{"session": true}` and `{}` are one door: the spelling an agent reaches
    for when it wants to be explicit costs it nothing (issue #2117)."""
    _held(monkeypatch, _HeldSession(READONLY))

    assert _call("revl_check", {"session": True}) == _call("revl_check", {})


def test_a_held_draft_is_checkable_before_it_boots(monkeypatch):
    """The state `revl_load` of a scaffold opens (#1727): a draft with open
    holes, held but not booted. Step 5 reads it — the holes with their
    fillSpecs, the self-check, the boundary — so the loop can verify the work
    before the swap that admits it (issue #2117)."""
    held = _held(monkeypatch, _HeldSession(loaded=False))
    held.pending_draft = {"vs": {"source": HOLED, "modules": {}},
                          "config": None, "record": False}

    payload = _call("revl_check", {})

    assert payload["ok"] is True, payload
    assert payload["checked"] == "session"
    assert [h["expected"] for h in payload["holes"]] == ["Str"]
    assert payload["holes"][0]["fillSpec"]["expected"] == "Str"
    # a draft is checkable but not admissible: that is what step 5 reports
    assert payload["selfCheck"]["admissible"] is False
    assert payload["selfCheck"]["summary"]["fail"] == 0
    assert held.pending_draft["vs"]["source"] == HOLED


def test_a_held_composition_that_does_not_compile_is_a_verdict(monkeypatch):
    """The held form is a real check: a composition that does not compile comes
    back as a guarantee diagnostic with its `selfCheck`, exactly as the
    supplied form answers it (issue #2117)."""
    _held(monkeypatch, _HeldSession(UNDECLARED))

    payload = _call("revl_check", {})

    assert payload["ok"] is False
    assert payload["checked"] == "session"
    assert payload["diagnostics"][0]["code"] == "G1"
    assert payload["diagnostics"][0]["category"] != "usage"
    assert payload["selfCheck"]["admissible"] is False


def test_an_argument_error_is_not_a_composition_verdict(monkeypatch):
    """`revl_check {}` on a session holding nothing is a USAGE error: the
    `category` says so, the `fix` names the missing argument, the `next` is the
    call to make — and no `selfCheck` is attached, because there is no verdict
    to give (issue #2117)."""
    from revl.mcp import server as server_mod

    _held(monkeypatch, server_mod.Session())

    payload = _call("revl_check", {})

    assert payload["ok"] is False
    (diagnostic,) = payload["diagnostics"]
    assert diagnostic["category"] == "usage"
    assert "source" in diagnostic["message"]
    assert "ValueError" not in diagnostic["message"]
    assert diagnostic["fix"] == "pass `source` or `files`"
    assert payload["next"]["tool"] == "revl_load"
    assert "selfCheck" not in payload


def test_a_candidate_that_names_no_candidate_is_a_usage_error(monkeypatch):
    """`modules` are a candidate's `use` imports: a call carrying them and no
    `source`/`files` names no candidate to compile, so it is an argument error
    with the missing argument spelled out — not `ok: false` read as a verdict
    (issue #2117)."""
    _held(monkeypatch, _HeldSession(READONLY))

    payload = _call("revl_check", {
        "modules": {"./lib.rvl": "pub fn d(n: Int) -> Int { return n }"}})

    assert payload["ok"] is False
    assert payload["diagnostics"][0]["category"] == "usage"
    assert payload["diagnostics"][0]["fix"] == (
        "pass `source` or `files` beside `modules`")
    assert "selfCheck" not in payload


def test_an_empty_files_list_names_no_candidate_either(monkeypatch):
    """`files: []` carries no files, and this tree reads `files` by truth, not
    by presence (`query_tools`, `quarantine`, `edit.virtual_source` all test
    `not files`), so it is the bare form: the held composition is checked. The
    alternative reading — an empty candidate — is the shape that has no
    meaning, which is why the sibling verbs do not take it either
    (issue #2117)."""
    _held(monkeypatch, _HeldSession(READONLY))

    payload = _call("revl_check", {"files": []})

    assert payload["ok"] is True, payload
    assert payload["checked"] == "session"
    assert payload["selfCheck"]["admissible"] is True


def test_session_true_refuses_a_candidate_of_its_own(monkeypatch):
    """`{"session": true}` chooses which artifact to check, so a call that also
    carries one asks for two different checks: refused as an argument error,
    naming both ways out, rather than silently checking one of them
    (issue #2117)."""
    _held(monkeypatch, _HeldSession(READONLY))

    payload = _call("revl_check", {"session": True, "source": READONLY})

    assert payload["ok"] is False
    assert payload["diagnostics"][0]["category"] == "usage"
    assert "takes no candidate of its own" in payload["diagnostics"][0]["message"]
    assert "selfCheck" not in payload


# ------------------------------------------------- a load over a held draft
#
# A draft is the session's only copy of the work in progress: nothing on disk
# has it. So `files`/`source` on the edit and check verbs — which loads the
# candidate first — is not a step in the loop while a draft is held, it is the
# loss: the load replaces the draft, and the refusal that follows describes an
# edit of a composition that no longer exists (issue #2111). Below: the guard,
# and the negative controls that show a load with no draft held is untouched.

@pytest.fixture
def _no_runtime_gate(monkeypatch):
    """Lift the runtime gate. `revl_edit` and `revl_change` are runtime verbs
    (`runtime_gate.RUNTIME_VERBS`) and this interpreter has no cordis, so the
    gate would answer first — with a refusal that is not the one under test.
    The draft guard runs before anything is loaded, so the gate is lifted and
    the load half stubbed where a test reaches it."""
    from revl.mcp import server as server_mod

    monkeypatch.setattr(server_mod, "_RUNTIME_AVAILABLE", True)


def _draft_held(monkeypatch, source=HOLED):
    """A session holding a draft and nothing loaded — the state `revl_load` of
    a scaffold opens (#1727)."""
    held = _held(monkeypatch, _HeldSession(loaded=False))
    held.pending_draft = {"vs": {"source": source, "modules": {}},
                          "config": None, "record": False}
    return held


def test_a_candidate_while_a_draft_is_held_is_refused_not_checked(monkeypatch):
    """Step 5 of the loop, spelled the cold way. `revl_check {source: ...}` on
    a session holding a draft used to answer about a composition the loop was
    not building, and read as though the draft were gone (issue #2111). It is
    now a usage refusal that names the draft it would have passed over, its
    open holes, and the form that checks the held draft instead."""
    held = _draft_held(monkeypatch)

    payload = _call("revl_check", {"source": READONLY})

    assert payload["ok"] is False
    (diagnostic,) = payload["diagnostics"]
    # a usage error, not a verdict: nothing about the caller's composition was
    # judged, so `ok: false` keeps meaning "your composition has a problem"
    assert diagnostic["category"] == "usage"
    assert "selfCheck" not in payload
    assert ("refused: revl_check with the inline `source` you sent checks that "
            "candidate, not the held draft (C, 1 open hole(s))") in diagnostic["message"]
    assert payload["draft"] is True
    # the draft the message names is the one the session still holds
    assert held.pending_draft["vs"]["source"] == HOLED
    # and the remedy is the call that verifies it without re-sending it
    assert payload["next"] == {"tool": "revl_check",
                               "arguments": {"session": True}, "ready": True}
    assert _call("revl_check", {"session": True})["checked"] == "session"


def test_files_while_a_draft_is_held_is_refused_and_names_the_file(
        monkeypatch, tmp_path, _no_runtime_gate):
    """The edit half. `revl_edit {files: [...]}` on a draft session loads the
    file and replaces the draft, so the edit that follows patches a
    composition the caller never meant to build (issue #2111). The refusal
    says which file would have been loaded, and carries the same edit pointed
    at the held draft."""
    monkeypatch.chdir(tmp_path)
    plain = tmp_path / "plain.rvl"
    plain.write_text(READONLY, encoding="utf-8")
    held = _draft_held(monkeypatch)

    payload = _call("revl_edit", {"files": [str(plain)],
                                  "edits": [{"target": "C", "append": ""}]})

    assert payload["ok"] is False
    (diagnostic,) = payload["diagnostics"]
    assert diagnostic["category"] == "usage"
    assert "refused: revl_edit with `files` (plain.rvl) loads it first" \
        in diagnostic["message"]
    assert "replacing the held draft (C, 1 open hole(s))" in diagnostic["message"]
    assert payload["loaded"] is False
    assert payload["edited"] is False
    assert payload["draft"] is True
    assert held.pending_draft["vs"]["source"] == HOLED
    # the next call is the edit the caller wrote, with the load dropped
    assert payload["next"]["tool"] == "revl_edit"
    assert payload["next"]["arguments"] == {"edits": [{"target": "C", "append": ""}]}
    assert payload["next"]["ready"] is True


def test_an_edit_with_nothing_to_apply_offers_a_next_that_is_not_ready(
        monkeypatch, _no_runtime_gate):
    """A `next` is a call to send as-is, so it must not refuse again. A
    draft-destroying `revl_edit` that carries no `edits` of its own has
    nothing left to point at the draft once the load is dropped, and says so
    rather than handing back a call that fails (issue #2111)."""
    held = _draft_held(monkeypatch)

    payload = _call("revl_edit", {"source": READONLY})

    assert payload["ok"] is False
    assert payload["diagnostics"][0]["category"] == "usage"
    assert payload["next"]["tool"] == "revl_edit"
    assert payload["next"]["ready"] is False
    assert "`edits`" in payload["next"]["needs"]
    assert held.pending_draft["vs"]["source"] == HOLED


def test_the_intent_shaped_change_is_the_same_refusal(monkeypatch,
                                                      _no_runtime_gate):
    """`revl_change` reaches the edit door — `change.edit_arguments` hands
    `revl_edit` the load set beside the intent it built — so the
    intent-shaped spelling of the destroying call is refused too, naming the
    verb the caller called and carrying the edit the intent amounts to
    (issue #2111)."""
    held = _draft_held(monkeypatch)

    payload = _call("revl_change", {
        "add": {"source": "component Extra provides extra: Extra { }"},
        "source": READONLY})

    assert payload["ok"] is False
    assert "refused: revl_change with the inline `source` you sent" \
        in payload["diagnostics"][0]["message"]
    assert payload["diagnostics"][0]["category"] == "usage"
    assert payload["committed"] is False
    assert held.pending_draft["vs"]["source"] == HOLED
    assert payload["next"]["tool"] == "revl_edit"
    assert payload["next"]["ready"] is True


def test_a_candidate_with_no_draft_held_is_still_checked(monkeypatch):
    """NEGATIVE CONTROL, the cold half: the refusal is scoped to a held draft.
    With nothing held at all, `source` is the ordinary candidate it always was
    — checked as sent, with its canonical form, exactly as before (issue
    #2111)."""
    _held(monkeypatch, _HeldSession(loaded=False))

    payload = _call("revl_check", {"source": READONLY})

    assert payload["ok"] is True
    assert "checked" not in payload        # a candidate of its own, not the session's
    assert "canonicalSource" in payload
    assert payload["selfCheck"]["admissible"] is True


def test_a_candidate_with_a_composition_loaded_is_still_checked(
        monkeypatch, tmp_path):
    """NEGATIVE CONTROL, the warm half: `loaded` and `draft` are exclusive
    states (a draft opens only on a load where nothing runs), so a session
    running a composition holds no draft and the same call is the candidate's
    own check — the answer describes the file, not the composition (issue
    #2111)."""
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other.rvl"
    other.write_text(CACHE, encoding="utf-8")
    _held(monkeypatch, _HeldSession(READONLY))   # loaded, and no draft

    payload = _call("revl_check", {"files": [str(other)]})

    assert payload["ok"] is True
    assert "checked" not in payload
    assert payload["components"] == [{"name": "MemCache", "requires": [],
                                      "provides": ["cache"]}]


def test_a_cold_edit_that_carries_source_still_runs_its_load(monkeypatch,
                                                            _no_runtime_gate):
    """NEGATIVE CONTROL, the edit half: with nothing held, `revl_edit` still
    loads the `source` it was handed and then edits it — the one-call form
    #1690 documented, and the path #2138 deliberately preserved (issue
    #2111). The load half is stubbed because booting needs the runtime; what
    is asserted is that it is still reached, with the source."""
    from revl.mcp import server as server_mod

    _held(monkeypatch, _HeldSession(loaded=False))
    loaded_with = []

    def _record(arguments):
        loaded_with.append(arguments)
        return {"ok": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "compile",
            "message": "the load half ran"}]}

    monkeypatch.setattr(server_mod, "_tool_load", _record)

    payload = _call("revl_edit", {"source": READONLY,
                                  "edits": [{"target": "C", "append": ""}]})

    assert loaded_with == [{"source": READONLY}]
    assert payload["diagnostics"][0]["message"] == "the load half ran"
    assert payload["loaded"] is False
    assert payload["edited"] is False


# --------------------------------------------------------------- live session
#
# Everything below actually loads code into a cordis-py Context. Gate it per
# test, not per module: `pytest.importorskip` at module scope skips the file
# during collection, so the runtime-free tests above never even get counted.

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)


@pytest.fixture(autouse=True)
def _fresh_session():
    from revl.mcp import server as server_mod

    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()


@needs_runtime
def test_load_call_and_unload_with_no_residue():
    loaded = _call("revl_load", {"source": CACHE})
    assert loaded["ok"] is True
    assert loaded["components"] == [{"name": "MemCache", "state": "ACTIVE"}]
    assert loaded["providedKeys"] == ["cache"]

    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0

    unloaded = _call("revl_unload", {})
    assert unloaded["noResidue"] is True
    assert all(unloaded["checks"].values())


@needs_runtime
def test_swap_replaces_the_running_generation():
    _call("revl_load", {"source": CACHE})
    swapped = _call("revl_swap", {"source": CACHE.replace("fn size() = 0",
                                                          "fn size() = 42")})
    assert swapped["admitted"] is True and swapped["swapped"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42


@needs_runtime
def test_a_rejected_swap_leaves_the_running_system_serving():
    _call("revl_load", {"source": CACHE})
    rejected = _call("revl_swap", {"source": CACHE.replace("fn size() = 0",
                                                           'fn size() = "nope"')})
    assert rejected["ok"] is False
    assert rejected["swapped"] is False
    assert rejected["diagnostics"][0]["code"] == "T1"
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


@needs_runtime
def test_rollback_restores_the_previous_generation():
    _call("revl_load", {"source": CACHE})
    _call("revl_swap", {"source": CACHE.replace("fn size() = 0", "fn size() = 42")})
    assert _call("revl_rollback", {})["ok"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0


@needs_runtime
def test_a_refused_rollback_keeps_the_generation_it_would_have_restored():
    """A rollback the gate refuses must not consume the target it restores.

    `rollback` used to clear `previous`/`previous_origin` before delegating to
    `swap`. `swap` saves those pointers for its own abort path, so it saved the
    `None` `rollback` had just written and put it back: a refused rollback
    restored `previous` to `None` and the rollback target was gone. The retry
    an operator makes once the cause is fixed then failed with "no previous
    generation to roll back to" instead of reaching the real reason.
    """
    from revl.mcp import server as server_mod

    # Gen 1 does not activate; gen 2 does. Rolling back to gen 1 is refused by
    # the activation health gate — precisely the operator-recovery case.
    _call("revl_load", {"source": BROKEN_ACTIVATION})
    _call("revl_swap", {"source": CACHE})
    target = server_mod.SESSION.previous
    assert target is not None

    refused = _call("revl_rollback", {})
    assert refused["ok"] is False
    assert "swap rejected" in refused["diagnostics"][0]["message"]

    # The running generation keeps serving, and the target survives the refusal.
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0
    assert server_mod.SESSION.previous is target

    # So a retry reaches the real reason, not a lost target.
    retry = _call("revl_rollback", {})
    assert retry["ok"] is False
    assert "no previous generation" not in retry["diagnostics"][0]["message"]


@needs_runtime
def test_rollback_rolls_forward_again_after_rolling_back():
    """The success path is unchanged: the generation just left becomes the
    rollback target, so rollback toggles between two generations."""
    from revl.mcp import server as server_mod

    _call("revl_load", {"source": CACHE})
    _call("revl_swap", {"source": CACHE.replace("fn size() = 0", "fn size() = 42")})
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42

    assert _call("revl_rollback", {})["ok"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 0
    assert server_mod.SESSION.previous is not None

    assert _call("revl_rollback", {})["ok"] is True
    assert _call("revl_call", {"key": "cache", "method": "size"})["result"] == 42


@needs_runtime
def test_state_reports_what_is_running():
    assert _call("revl_state", {})["loaded"] is False
    _call("revl_load", {"source": CACHE})
    state = _call("revl_state", {})
    assert state["loaded"] is True
    assert state["loadOrder"] == ["MemCache"]


@needs_runtime
def test_calling_before_loading_is_a_clean_error():
    payload = _call("revl_call", {"key": "cache", "method": "size"})
    assert payload["ok"] is False
    assert "nothing is loaded" in payload["diagnostics"][0]["message"]


# ------------------------------------------------- a chain, built in memory

CHAIN_SERVICES = """
service Db { fn get(k: Str) -> Opt[Str] }
service Cache { fn get(k: Str) -> Opt[Str] }
service Api { fn lookup(k: Str) -> Str }
"""
CHAIN_DB = """
component MemDb provides db: Db {
  let store = effect Map.new() undo store.drop()
  provide db { fn get(k) = store.get(k) }
}
"""
CHAIN_CACHE = """
component L1 requires db: Db provides cache: Cache {
  provide cache { fn get(k) = db.get(k) }
}
"""
CHAIN_CACHE_V2 = """
component L1 requires db: Db provides cache: Cache {
  provide cache { fn get(k) = db.get(k) ?? Some("default") }
}
"""
CHAIN_API = """
component Front requires cache: Cache provides api: Api {
  provide api { fn lookup(k) = "v1:" + (cache.get(k) ?? "miss") }
}
"""
CHAIN_LOG = """
service Log { fn note(m: Str) -> Int }
component Auditor requires api: Api provides log: Log {
  provide log { fn note(m) = 1 }
}
"""


def _lookup() -> object:
    return _call("revl_call", {"key": "api", "method": "lookup",
                               "args": ["k"]})["result"]


@needs_runtime
def test_a_three_link_chain_loads_in_dependency_order_and_answers():
    loaded = _call("revl_load",
                   {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE + CHAIN_API})
    assert loaded["loadOrder"] == ["MemDb", "L1", "Front"]
    assert [c["state"] for c in loaded["components"]] == ["ACTIVE"] * 3
    assert _lookup() == "v1:miss"


@needs_runtime
def test_swapping_the_middle_link_is_visible_through_the_chain():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE + CHAIN_API})
    swapped = _call("revl_swap",
                    {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    assert swapped["swapped"] is True
    # the dependent at the end of the chain sees the new middle behaviour
    assert _lookup() == "v1:default"


@needs_runtime
def test_a_running_chain_can_grow_a_new_component():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    grown = _call("revl_swap", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2
                                + CHAIN_API + CHAIN_LOG})
    assert grown["swapped"] is True
    assert grown["loadOrder"] == ["MemDb", "L1", "Front", "Auditor"]
    assert grown["providedKeys"] == ["api", "cache", "db", "log"]


@needs_runtime
def test_a_broken_middle_link_cannot_deploy_into_a_chain():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2 + CHAIN_API})
    broken = CHAIN_CACHE_V2.replace('fn get(k) = db.get(k) ?? Some("default")',
                                    "fn get(k) = 42")
    rejected = _call("revl_swap",
                     {"source": CHAIN_SERVICES + CHAIN_DB + broken + CHAIN_API})
    assert rejected["swapped"] is False
    assert "expects `Opt[Str]`, got `Int`" in rejected["diagnostics"][0]["message"]
    assert _lookup() == "v1:default"  # the whole chain keeps serving


@needs_runtime
def test_the_whole_chain_unloads_without_residue():
    _call("revl_load", {"source": CHAIN_SERVICES + CHAIN_DB + CHAIN_CACHE_V2
                        + CHAIN_API + CHAIN_LOG})
    unloaded = _call("revl_unload", {})
    assert unloaded["noResidue"] is True
