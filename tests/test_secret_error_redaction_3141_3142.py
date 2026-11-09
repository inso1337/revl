"""An activation error's TEXT must not carry a `Secret[T]` value (issues
#3141 and #3142).

#3141. A `fail` step whose message is a `Secret[Str]` config field (or a value
derived from one) put the credential into the exception text the runtime kept
on the FAILED fiber, and `revl_load` / `revl_state` returned that text in
`components[].error.message`. The same text reached the generated lifecycle
driver's AssertionError and a failed swap's rollback reason, both of which read
the same fiber record. The message is now funnelled through
`confidential.redact_text` at capture (`runtime.fail_error`), and the checker
refuses a `fail` of a `confidential` value at compile time, so a program that
discloses its own config credential no longer compiles.

#3142. A host extern that receives a `Secret[Str]` at a declared receiver and
raises an exception quoting it (an HTTP or database client that puts the URL,
header or credential into its message) wrote the text verbatim into the durable
WAL row `emission-failed`, and the MCP session returned it from the fiber as
the load / state error. `_record_emission_outcome` applied the exact-value
funnel only, which cannot see the secret inside `"upstream rejected
credential <v>"`; both that writer and `_fiber_activation_error` now use the
substring scrubber, the same funnel the bridge applies to a seam failure.

Every assertion is PAIRED, the canary absent and the redaction marker present,
so no test can pass because nothing was recorded at all. The controls pin the
other half: an error that quotes no registered secret is byte-identical.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import confidential  # noqa: E402
import replay  # noqa: E402
import runtime  # noqa: E402
from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import session as session_mod  # noqa: E402
from revl.wal import read_wal  # noqa: E402

#: A value that looks like the credential the issues use, and is long enough
#: for the registry's minimum markable length.
CANARY = "sk-live-SECRETVALUE-1234"
REDACTED = "<redacted:secret>"

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="`revl_load` needs the cordis-py runtime; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)


@pytest.fixture(autouse=True)
def _empty_registry():
    """The registry is process-global, so leave no marking behind."""
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


# ---------------------------------------------------------------------------
# #3141: a fail step is a disclosure sink
# ---------------------------------------------------------------------------


def _fail_component(message: str) -> str:
    return ("component C {\n"
            "  config {\n"
            "    token: Secret[Str],\n"
            "  }\n"
            f"  fail {message}\n"
            "}\n")


@pytest.mark.parametrize("message", ["config.token",
                                     '"refused: " + config.token'])
def test_a_fail_of_a_secret_config_field_is_refused_at_compile_time(message):
    """The compile-time half: `fail` of a `confidential` value is a disclosure
    sink, so the program is refused by name instead of leaking at run time."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_fail_component(message), "t.rvl")
    text = str(excinfo.value)
    assert "G-SECRET-FLOW" in text, text
    assert "Secret[T]" in text, text


def test_a_fail_of_an_ordinary_value_still_compiles():
    """The control: the new sink check must not refuse an ordinary message."""
    ir = compile_source(
        "component C {\n"
        "  config {\n"
        "    level: Int = 2,\n"
        "  }\n"
        '  fail "bad level " + level_to_str(config.level)\n'
        "}\n"
        "fn level_to_str(n: Int) -> Str { return \"2\" }\n",
        "t.rvl")
    assert ir["components"][0]["body"][0]["step"] == "fail"


def test_fail_error_scrubs_a_registered_secret_out_of_its_sentence():
    """The runtime half: even if an IR reaches the runtime without the
    checker, the error text is scrubbed before the exception is built."""
    confidential.register_secret_value(CANARY)
    err = runtime.fail_error("activation refused: token=" + CANARY)
    assert CANARY not in str(err)
    assert REDACTED in str(err), str(err)
    assert "activation refused" in str(err), str(err)
    assert getattr(err, "_revl_activation_fail", False) is True


def test_fail_error_leaves_an_ordinary_message_verbatim():
    """The control: no registered secret means the message is untouched."""
    err = runtime.fail_error("activation refused: token=opaque")
    assert str(err) == "activation refused: token=opaque"


# ---------------------------------------------------------------------------
# #3142: a host error that QUOTES a secret argument
# ---------------------------------------------------------------------------


def test_an_emission_failure_quoting_a_secret_is_redacted_in_the_wal(tmp_path):
    """The durable half: the `emission-failed` row keeps the sentence's shape
    and loses the credential it quoted."""
    path = tmp_path / "run.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    timeline = replay.Timeline("C")
    timeline.attach_wal(wal, {})
    confidential.register_secret_value(CANARY)
    step = timeline.record_emission("send", "send", (), None, ("t", 1))

    def host():
        raise ValueError("upstream rejected credential " + CANARY)

    with pytest.raises(ValueError):
        replay._call_within(replay._crossing_ref(timeline, step), host, (), {})
    wal.close()

    failed = [r for r in read_wal(str(path))["records"]
              if r.get("record") == "emission-failed"]
    assert len(failed) == 1, failed
    assert failed[0]["error"] == {
        "type": "ValueError",
        "message": "upstream rejected credential " + REDACTED,
    }


def test_an_emission_failure_without_a_secret_is_recorded_verbatim(tmp_path):
    """The control: an ordinary host failure is unchanged, marker and all."""
    path = tmp_path / "run.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    timeline = replay.Timeline("C")
    timeline.attach_wal(wal, {})
    step = timeline.record_emission("send", "send", (), None, ("t", 1))

    def host():
        raise ValueError("upstream rejected credential opaque")

    with pytest.raises(ValueError):
        replay._call_within(replay._crossing_ref(timeline, step), host, (), {})
    wal.close()

    failed = [r for r in read_wal(str(path))["records"]
              if r.get("record") == "emission-failed"]
    assert len(failed) == 1, failed
    assert failed[0]["error"] == {
        "type": "ValueError",
        "message": "upstream rejected credential opaque",
    }


class _Fiber:
    """A cordis fiber reduced to the one field `_fiber_activation_error` reads."""

    def __init__(self, error: BaseException) -> None:
        self._error = error


def test_the_fiber_error_redacts_a_secret_a_host_body_quoted():
    """The response half: `_fiber_activation_error` is the single reading of a
    FAILED fiber that `state()` and the swap rollback reason share, so the
    scrub there covers both surfaces."""
    confidential.register_secret_value(CANARY)
    error = session_mod._fiber_activation_error(
        _Fiber(ValueError("upstream rejected credential " + CANARY)))
    assert error == {"type": "ValueError",
                     "message": "upstream rejected credential " + REDACTED}


def test_the_fiber_error_of_an_unmarked_failure_is_unchanged():
    error = session_mod._fiber_activation_error(
        _Fiber(RuntimeError("plain activation fault")))
    assert error == {"type": "RuntimeError",
                     "message": "plain activation fault"}


#: The IR `compile_source` produced for the #3141 probe before this change made
#: that program a compile-time refusal. The runtime still has to redact an IR
#: that reaches it from an embedding or from a checker gap, so `load` and
#: `state` are exercised from this lowercase IR rather than from the source.
_FAIL_IR = {
    "ir_version": 3,
    "services": {},
    "components": [{
        "name": "C",
        "source": "t.rvl",
        "config": [{"name": "token", "type": "Str",
                    "default": None, "secret": True}],
        "requires": {},
        "provides": {},
        "body": [{"step": "fail",
                  "message": {"kind": "config", "field": "token"}}],
    }],
    "manifest": {
        "components": [{"name": "C", "file": "t.rvl",
                        "inject": [], "provides": []}],
        "loadOrder": ["C"],
    },
}


@needs_cordis
def test_load_and_state_return_a_redacted_fail_message():
    """The end-to-end half of #3141: the surfaces the issue names."""
    session = session_mod.Session()
    try:
        loaded = session.load(_FAIL_IR, {"C": {"token": CANARY}})
        state = session.state()
    finally:
        session.unload()
    for out in (loaded, state):
        error = out["components"][0]["error"]
        assert error == {"type": "RuntimeError", "message": REDACTED}, out
