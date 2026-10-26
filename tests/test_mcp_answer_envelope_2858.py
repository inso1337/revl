"""The answer envelope and the refusal-code registry (issue #2858).

`revl_check` and `revl_test_run` are the first verbs converted to one shared
answer envelope:

  ok          did the call answer at all
  verdict     passed | failed | refused
  payloadKey  the key in the same answer that carries the verb's substance
  refusal     present when verdict is refused: code, category, message,
              location, repair

The envelope is additive: every key the two verbs answered with before is
still there, at the same place. The stable contract is the refusal `code`;
`message` stays free to improve. The registry is `revl.answer.REFUSALS`, a
module-level mapping code -> category and meaning, and the test below proves
the converted handlers can only emit a code the registry holds.

The last test reads the tests' own module list from the server source, so a
new refusal added to a converted handler with a code no one registered fails
here rather than in a script that parses prose.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import answer  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

SERVER = ROOT / "src" / "revl" / "mcp" / "server.py"

CLEAN = ("service S { fn f() -> Int }\n"
         "component C provides s: S { provide s { fn f() = 1 } }\n")

#: The converted handlers and the helpers they refuse through. A code literal
#: passed by any of them must be registered.
CONVERTED = ("_tool_check", "_check_held", "_tool_test_run",
             "_test_run_refusal")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


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


def _suite(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _assert_refusal_is_registered(payload: dict, code: str) -> dict:
    assert payload["ok"] is False, payload
    assert payload["verdict"] == "refused", payload
    refusal = payload["refusal"]
    entry = answer.REFUSALS[code]
    assert refusal["code"] == code
    assert refusal["category"] == entry.category
    assert payload["diagnostics"][0]["category"] == entry.category
    assert refusal["message"]
    assert set(refusal) == {"code", "category", "message", "location", "repair"}
    return refusal


# ---------------------------------------------------------- a passing answer

def test_a_passing_check_matches_the_envelope():
    payload = _call("revl_check", {"source": CLEAN})

    assert payload["ok"] is True
    assert payload["verdict"] == "passed"
    assert payload["payloadKey"] == "selfCheck"
    assert "refusal" not in payload
    # the keys this verb answered with before the envelope are unchanged
    for key in ("selfCheck", "boundary", "holes", "components", "irVersion",
                "loadOrder", "services"):
        assert key in payload, key
    assert payload["selfCheck"]["admissible"] is True


def test_a_passing_test_run_matches_the_envelope(clean_session):
    suite = _suite(clean_session, "test_tiny_pass.py",
                   "def test_ok():\n    assert 1 + 1 == 2\n")
    payload = _call("revl_test_run", {"selection": [suite],
                                      "cwd": str(clean_session)})

    assert payload["ok"] is True
    assert payload["verdict"] == "passed"
    assert payload["payloadKey"] == "tests"
    assert "refusal" not in payload
    # the keys this verb answered with before the envelope are unchanged
    for key in ("tests", "counts", "receipt", "selection"):
        assert key in payload, key


# --------------------------------------------------------- a refused answer

def test_a_check_with_no_candidate_refuses_with_a_registered_code():
    payload = _call("revl_check", {})
    refusal = _assert_refusal_is_registered(payload, "NO_CANDIDATE")
    assert refusal["location"] == "source"
    assert payload["payloadKey"] == "selfCheck"


def test_a_check_with_session_and_a_candidate_refuses_by_code():
    payload = _call("revl_check", {"session": True, "source": CLEAN})
    refusal = _assert_refusal_is_registered(
        payload, "SESSION_TAKES_NO_CANDIDATE")
    assert refusal["location"] == "session"
    assert "candidate" in refusal["message"]


def test_modules_without_a_candidate_refuse_by_code():
    payload = _call("revl_check", {"modules": {"a.rvl": CLEAN}})
    refusal = _assert_refusal_is_registered(
        payload, "MODULES_WITHOUT_CANDIDATE")
    assert refusal["location"] == "modules"


def test_a_gate_refusal_before_the_handler_derives_a_registered_code(
        clean_session):
    # the path jail runs before the handler and names a category, not a
    # verb-specific code; the envelope maps it to the registered fallback
    payload = _call("revl_check", {"files": [str(ROOT / "examples" / "pulse.rvl")]})
    _assert_refusal_is_registered(payload, "ADMISSION_REFUSED")

    # an argument the schema does not declare is the same shape of refusal
    payload = _call("revl_check", {"source": CLEAN, "notAnArgument": True})
    _assert_refusal_is_registered(payload, "USAGE")


def test_the_hatch_answers_the_converted_verb_in_the_same_envelope():
    payload = _call("revl_verbs", {"name": "revl_check", "args": {"source": CLEAN}})

    assert payload["ok"] is True
    assert payload["verdict"] == "passed"
    assert payload["payloadKey"] == "selfCheck"


def test_test_run_refusals_carry_the_handlers_own_codes(clean_session):
    cases = [
        ("EMPTY_SELECTION", {"selection": [], "cwd": str(clean_session)}),
        ("BAD_SELECTION", {"selection": [""], "cwd": str(clean_session)}),
        ("BAD_CWD", {"selection": ["tests/test_x.py"]}),
        ("BAD_TIMEOUT", {"selection": ["tests/test_x.py"],
                         "cwd": str(clean_session), "timeout": 0}),
    ]
    for code, arguments in cases:
        payload = _call("revl_test_run", arguments)
        refusal = _assert_refusal_is_registered(payload, code)
        assert refusal["location"], code
        assert payload["code"] == code
        assert payload["payloadKey"] == "tests"


def test_a_selection_outside_the_roots_refuses_by_code(clean_session):
    outside = str(clean_session.parent / "test_outside_2858.py")
    payload = _call("revl_test_run", {"selection": [outside],
                                      "cwd": str(clean_session)})
    _assert_refusal_is_registered(payload, "OUTSIDE_ROOT")


def test_a_timed_out_run_carries_the_runtime_category(clean_session):
    suite = _suite(clean_session, "test_tiny_slow.py",
                   "import time\n\ndef test_slow():\n    time.sleep(10)\n")
    payload = _call("revl_test_run", {"selection": [suite],
                                      "cwd": str(clean_session), "timeout": 1})
    refusal = _assert_refusal_is_registered(payload, "TIMEOUT")
    assert refusal["category"] == "runtime"


def test_a_repeated_refusal_keeps_the_code_of_its_first_attempt(clean_session):
    """The repeat layer prepends a REPEATED_REFUSAL diagnostic from attempt 3.
    The envelope is stamped before that rewrite, so the derived code is read
    from the verb's own diagnostic and cannot flip to the repeat's category."""
    server_mod._repeat.forget()
    arguments = {"source": CLEAN, "notAnArgument": True}
    codes = []
    for _ in range(3):
        payload = _call("revl_check", arguments)
        codes.append(payload["refusal"]["code"])
        assert payload["refusal"]["category"] == "usage"
    assert codes == ["USAGE", "USAGE", "USAGE"]
    assert payload["repeat"]["attempt"] == 3
    assert payload["diagnostics"][0]["code"] == "REPEATED_REFUSAL"


# -------------------------------------------------------------- the registry

def test_the_registry_is_a_closed_mapping():
    assert answer.REFUSALS, "the registry is empty"
    categories = set()
    for code, entry in answer.REFUSALS.items():
        assert code == code.upper(), code
        assert entry.category, code
        assert entry.meaning, code
        assert not set(entry.meaning) & set("\u2014"), code
        categories.add(entry.category)
    # every category a shared refusal can be derived from has a code
    assert set(answer.CATEGORY_CODES) == categories
    assert set(answer.CATEGORY_CODES.values()) <= set(answer.REFUSALS)


def test_an_unknown_code_is_impossible_by_construction():
    with pytest.raises(answer.UnknownRefusalCode):
        answer.refusal("NOT_A_REGISTERED_CODE", "some message")
    # an explicit refusal object must name a registered code
    with pytest.raises(answer.UnknownRefusalCode):
        answer.envelope("revl_test_run", {
            "ok": False, "verdict": "refused",
            "refusal": {"code": "NOT_REGISTERED", "message": "m"}})


def test_a_carried_code_or_unmapped_category_still_answers_a_registered_refusal():
    # a SessionError's carried code is not a refusal code: derive from the
    # diagnostic category instead of raising inside the transport
    payload = answer.envelope("revl_check", {
        "ok": False, "code": "STATE_UNDISCLOSED",
        "diagnostics": [{"category": "session", "message": "m"}]})
    assert payload["refusal"]["code"] == "SESSION_REFUSED"

    # a compiler category this module does not map is the internal fallback,
    # not an exception in `handle`
    payload = answer.envelope("revl_check", {
        "ok": False,
        "diagnostics": [{"category": "blast-radius", "message": "m"}]})
    assert payload["refusal"]["code"] == "INTERNAL_REFUSED"
    assert payload["refusal"]["category"] == "internal"


def test_the_converted_handlers_only_name_registered_codes():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in CONVERTED:
            continue
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            name = getattr(call.func, "id", None)
            if name == "_session_error":
                for keyword in call.keywords:
                    if keyword.arg == "refusal_code" and isinstance(
                            keyword.value, ast.Constant):
                        used.add(keyword.value.value)
            elif name == "_test_run_refusal" and len(call.args) >= 2 \
                    and isinstance(call.args[1], ast.Constant):
                used.add(call.args[1].value)
    assert used, "the scan found no code in the converted handlers; it is stale"
    assert used <= set(answer.REFUSALS), used - set(answer.REFUSALS)


def test_a_message_change_does_not_change_the_code():
    first = answer.refusal("EMPTY_SELECTION", "the first wording")
    second = answer.refusal("EMPTY_SELECTION", "a completely different wording")

    assert first["code"] == second["code"] == "EMPTY_SELECTION"
    assert first["category"] == second["category"]
    entry = answer.REFUSALS["EMPTY_SELECTION"]
    assert first["message"] != entry.meaning


def _section(text: str, name: str) -> str:
    start = text.index(f"### `{name}`")
    end = text.find("\n### `", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_the_documented_registry_lists_every_code():
    text = (ROOT / "docs" / "mcp-reference.md").read_text(encoding="utf-8")
    for code in answer.REFUSALS:
        assert f"`{code}`" in text, code


def test_each_converted_verb_section_names_the_codes_it_can_emit():
    text = (ROOT / "docs" / "mcp-reference.md").read_text(encoding="utf-8")
    check = _section(text, "revl_check")
    for code in ("SESSION_TAKES_NO_CANDIDATE", "DRAFT_HELD",
                 "MODULES_WITHOUT_CANDIDATE", "NO_CANDIDATE",
                 "NOTHING_TO_CHECK", "USAGE", "ADMISSION_REFUSED",
                 "AUTHORING_REFUSED", "SESSION_REFUSED",
                 "INTERNAL_REFUSED"):
        assert f"`{code}`" in check, code
    run = _section(text, "revl_test_run")
    for code in ("EMPTY_SELECTION", "BAD_SELECTION", "BAD_CWD", "BAD_TIMEOUT",
                 "OUTSIDE_ROOT", "TIMEOUT", "TEST_RUN_FAILED",
                 "NO_TESTS_COLLECTED", "ALL_SKIPPED", "USAGE",
                 "SESSION_REFUSED", "INTERNAL_REFUSED"):
        assert f"`{code}`" in run, code


def test_the_converted_verbs_are_the_only_ones_converted():
    assert set(answer.PAYLOAD_KEYS) == {"revl_check", "revl_test_run"}
