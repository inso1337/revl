"""Values must not leak out of the seam or onto the operator console
(roadmap item 421, findings F5 and F6).

Both findings are the same mistake made twice: a value the program was trusted
with is rendered into text that then crosses a boundary revl never analysed.

**F5, the seam ERROR reply.** `bridge._invoke` marshalled a provider-side
failure back as ``f"{type(exc).__name__}: {exc}"``, so a plain ``self.data[token]``
lookup handed the CONSUMER back the very token it called with. No author
interpolation is involved: `KeyError` quotes its key. The checker admits the
forward crossing into a declared `Secret[T]` receiver and refuses the reverse
one; the error channel performed the refused reverse crossing unanalysed. The
same text is re-raised by `_Client.call` as a `RuntimeError` and logged by
`_process_runner`, so all three sinks inherit whatever the reply says.

**F6, the host trace.** `runtime._record` events interpolate a `Map` key, a
`pool.query` sql, a `stream.emit` item. `_process_runner` installs that trace as
the operator console sink and the conductor forwards it to its own stdout, so a
`Secret[Str]` used as a `Map` key (the shipped `demo/components/user_cache.rvl`
idiom) printed verbatim.

Both fixes reuse the item-256/416c redaction path rather than inventing a second
one. `confidential.py` gains two text funnels (`redact_text`, `redact_call_text`)
next to the existing value funnel; the marking that drives them is the SAME
declared marking the recorder reads (`params[i]["secret"]`), now also registered
by the emitted program itself so it fires without a recorder attached.

Every assertion below is paired: the canary must be ABSENT **and** the redaction
marker PRESENT, so a test cannot pass because nothing was emitted at all. The
false-positive tests are the other half of the claim: an ordinary argument is
still quoted verbatim, so a diagnostic stays worth reading.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import bridge  # noqa: E402
import confidential  # noqa: E402

# Spelled out rather than imported, so these tests can RUN against a tree with
# no redaction in it and fail on the leak instead of on an import.
CANARY = "SEKRIT-CANARY-421-F5F6"
REDACTED_SECRET = "<redacted:secret>"
REDACTED_ARG = "<redacted:arg>"


def _cordis_python() -> str | None:
    """The interpreter that can BOOT a composition, or None.

    `REVL_PY` first (ci/placement_smoke.sh's own override), then this
    interpreter when the suite is already running under the runtime venv (the
    case in a WORKTREE, where the repo-root `.venv` path does not exist), and
    only then the repo-root venv."""
    override = os.environ.get("REVL_PY")
    if override and Path(override).exists():
        return override
    if importlib.util.find_spec("cordis") is not None:
        return sys.executable
    local = ROOT / "backends" / "python" / ".venv" / "bin" / "python"
    return str(local) if local.exists() else None


CORDIS_PY = _cordis_python()
needs_cordis = pytest.mark.skipif(
    CORDIS_PY is None,
    reason="needs the cordis-py runtime (backends/python/.venv/bin/python)")


@pytest.fixture(autouse=True)
def _fresh_marking():
    """The value marking is process-wide by design, so each test starts and ends
    with an empty set; otherwise one test's canary would redact another's."""
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


# ---------------------------------------------------------------------------
# F5, unit: what a failure is allowed to carry back
# ---------------------------------------------------------------------------


def test_seam_failure_scrubs_the_caller_s_own_argument():
    """The finding's exact trigger: a dict lookup, no author interpolation."""
    text = bridge.seam_failure(KeyError(CANARY), [CANARY])
    assert CANARY not in text
    assert REDACTED_ARG in text


def test_seam_failure_keeps_the_failure_worth_reading():
    """Redact, do not delete: the exception type and the sentence around the
    value survive, so the consumer still learns what went wrong."""
    text = bridge.seam_failure(ValueError(f"no row for {CANARY} in ledger"),
                               [CANARY])
    assert text.startswith("ValueError: ")
    assert "no row for" in text and "in ledger" in text
    assert CANARY not in text


def test_seam_failure_finds_an_argument_nested_in_a_record():
    """A record argument crosses as a dict; its VALUES are the caller's data."""
    text = bridge.seam_failure(KeyError(CANARY), [{"token": CANARY, "n": 1}])
    assert CANARY not in text
    assert REDACTED_ARG in text


def test_seam_failure_finds_an_argument_nested_in_a_list():
    text = bridge.seam_failure(KeyError(CANARY), [[["a", CANARY]]])
    assert CANARY not in text and REDACTED_ARG in text


def test_seam_failure_scrubs_a_numeric_argument():
    text = bridge.seam_failure(ValueError("account 8675309 is closed"), [8675309])
    assert "8675309" not in text and REDACTED_ARG in text


def test_seam_failure_leaves_an_unrelated_diagnostic_alone():
    """The false-positive half: a failure that quotes none of the arguments is
    byte-identical to what the seam sent before the fix."""
    text = bridge.seam_failure(RuntimeError("pool exhausted"), [CANARY])
    assert text == "RuntimeError: pool exhausted"


def test_seam_failure_does_not_erase_a_records_field_names():
    """A record's KEYS are field names the author wrote, not the caller's data;
    erasing them would destroy the diagnostic's shape."""
    text = bridge.seam_failure(ValueError("field token is malformed"),
                               [{"token": CANARY}])
    assert "field token is malformed" in text


def test_seam_failure_also_scrubs_a_registered_secret_it_was_not_called_with():
    """A credential threaded in from a `Secret[Str]` CONFIG field is not an
    argument of this call, so the argument scrub alone would miss it."""
    confidential.register_secret_value(CANARY)
    text = bridge.seam_failure(RuntimeError(f"upstream rejected {CANARY}"), ["u1"])
    assert CANARY not in text
    assert REDACTED_SECRET in text


# ---------------------------------------------------------------------------
# F6, unit: the trace text funnel
# ---------------------------------------------------------------------------


def test_redact_text_is_identity_until_something_is_marked():
    assert confidential.redact_text(f"map#1.insert {CANARY}") == \
        f"map#1.insert {CANARY}"


def test_redact_text_scrubs_a_marked_value_and_keeps_the_event_shape():
    confidential.register_secret_value(CANARY)
    out = confidential.redact_text(f"map#1.insert {CANARY}")
    assert CANARY not in out
    assert out == f"map#1.insert {REDACTED_SECRET}"


def test_the_record_choke_point_scrubs_every_trace_site():
    """The fix is at `_record`, not at each printer, so a site F6 never named,
    and a site added tomorrow, is covered by the same funnel."""
    import runtime  # noqa: PLC0415 (backend module, path set above)

    confidential.register_secret_value(CANARY)
    seen: list[str] = []
    unsubscribe = runtime.add_trace(seen.append)
    try:
        for event in (f"map#1.insert {CANARY}",
                      f"pool#1.query SELECT * FROM t WHERE k = '{CANARY}'",
                      f"stream.emit {CANARY}",
                      f"pool#1.open postgres://u:{CANARY}@db/app",
                      f"stream.source fault {CANARY}",
                      f"job.run {CANARY} start"):
            runtime._record(event)
    finally:
        unsubscribe()

    assert seen, "the trace produced nothing: this test would pass vacuously"
    for event in seen:
        assert CANARY not in event, event
        assert REDACTED_SECRET in event, event
    # ...and the shape of each event is otherwise intact
    assert seen[0] == f"map#1.insert {REDACTED_SECRET}"
    assert seen[1].startswith("pool#1.query SELECT * FROM t WHERE k = ")


def test_an_ordinary_trace_line_is_untouched():
    confidential.register_secret_value(CANARY)
    assert confidential.redact_text("map#1.insert alice") == "map#1.insert alice"


# ---------------------------------------------------------------------------
# F5, live: a real seam, a real provider failure, a real consumer
# ---------------------------------------------------------------------------


class _Store:
    """A provider whose failure quotes its argument with no interpolation of the
    author's own: `self.data[token]` raises `KeyError('<token>')`."""

    def __init__(self) -> None:
        self.data: dict = {}

    def lookup(self, token):
        return self.data[token]


class _Ctx:
    def get(self, key):
        return _Store()


def _round_trip(args: list) -> str:
    """Serve one provider over a UDS, make one failing call, return exactly what
    the CONSUMER saw: the `RuntimeError` `_Client.call` re-raises."""
    async def go() -> str:
        directory = tempfile.mkdtemp()
        socket_path = os.path.join(directory, "provider.sock")
        server = await bridge.serve(_Ctx(), {"store": ["lookup"]}, socket_path)
        loop = asyncio.get_running_loop()

        def call() -> str:
            client = bridge._Client(socket_path)
            try:
                client.call("store", "lookup", args)
            except Exception as exc:  # noqa: BLE001 (the reply IS the subject)
                return f"{type(exc).__name__}: {exc}"
            finally:
                client.close()
            return "NO ERROR"

        try:
            return await loop.run_in_executor(None, call)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(go())


def test_a_failing_seam_call_does_not_return_the_argument_to_the_consumer():
    seen = _round_trip([CANARY])
    assert "RuntimeError" in seen, seen
    assert CANARY not in seen, seen
    assert REDACTED_ARG in seen, seen


def test_a_failing_seam_call_still_names_the_failure():
    seen = _round_trip(["alice"])
    assert "KeyError" in seen, seen


# ---------------------------------------------------------------------------
# F5 + F6, end to end: two real processes, one real seam, one real console
# ---------------------------------------------------------------------------
#
# The composition below is the audit's shape exactly: `mint_token` is the
# `Secret[Str]` ORIGIN (item 256 §7a), the token is used as a `Map` key (the
# shipped user_cache idiom, F6) and then crosses the seam into a declared
# `Secret[Str]` receiver whose provider fails with a plain `KeyError` (F5). One
# run therefore exercises all three sinks the findings name: the seam reply, the
# runner log, and the operator console trace.

LEAKY = """
extern emission[vault.mint] fn mint_token(u: Str) -> Secret[Str]
  = @py { return "SEKRIT-CANARY-421-F5F6" }

extern emission[db.write] fn strict_write(sql: Secret[Str]) -> Int
  = @py { raise KeyError(sql) }

service Database { emission fn execute(sql: Secret[Str]) -> Int }
service Cache { emission fn put(u: Str) -> Int }

component PgDatabase provides db: Database {
  provide db {
    fn execute(sql) {
      emit strict_write(sql)
      return 1
    }
  }
}

component UserCache requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn put(u) {
      let t = emit mint_token(u)
      effect store.insert(t, "PUBLIC-VALUE-421")
      undo   store.remove(t)
      emit db.execute(t)
      return 1
    }
  }
}
"""

PLACEMENT = """
[processes.provider]
components = ["PgDatabase"]

[processes.consumer]
components = ["UserCache"]
probe = ["cache.put('alice')"]
"""


@pytest.fixture
def placed(tmp_path):
    source = tmp_path / "leaky.rvl"
    source.write_text(LEAKY, encoding="utf-8")
    toml = tmp_path / "leaky.toml"
    toml.write_text(PLACEMENT, encoding="utf-8")
    return source, toml


@needs_cordis
def test_no_sink_of_a_real_placement_run_carries_the_secret(placed):
    source, toml = placed
    result = subprocess.run(
        [CORDIS_PY, "-m", "revl", "run", str(source),
         "--placement", str(toml), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(source.parent),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    trace = result.stdout + result.stderr
    assert result.returncode == 0, trace

    # the run really did the things whose output is under test; without these
    # the absence assertions below would hold vacuously
    assert "map#1.insert" in trace, trace          # F6's sink fired
    assert "probe | cache.put" in trace, trace     # F5's sink fired
    assert "ERROR RuntimeError" in trace, trace    # the provider really failed

    # F6: the operator console trace
    assert CANARY not in trace, trace
    assert f"map#1.insert    | {REDACTED_SECRET}" in trace, trace
    # ...including the inverse replayed during teardown
    assert f"map#1.remove    | {REDACTED_SECRET}" in trace, trace

    # F5: the seam reply, as the consumer's runner logged it
    assert f"KeyError: '{REDACTED_ARG}'" in trace, trace

    # the control: an ordinary trace line is still verbatim, so the redaction
    # did not simply blank the console
    assert "map#1.new" in trace, trace
    assert "cache.put('alice')" in trace, trace


@needs_cordis
def test_the_run_still_reverts_cleanly_with_the_redaction_in_place(placed):
    """Redaction touches only what the trace SAYS. The failed emission still
    unwinds LIFO and both processes still prove no residue."""
    source, toml = placed
    result = subprocess.run(
        [CORDIS_PY, "-m", "revl", "run", str(source),
         "--placement", str(toml), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(source.parent),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    trace = result.stdout + result.stderr
    assert trace.count("residue no residue") == 2, trace


# ---------------------------------------------------------------------------
# the marking that drives F6, read off the IR
# ---------------------------------------------------------------------------


def test_a_secret_returning_extern_is_stamped_into_the_ir():
    """`taint.py` strips the `Secret[...]` qualifier before lowering, so this
    flag is the only surviving record that the RETURN position is confidential.
    Without it the emitter cannot mark the value at its origin."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(LEAKY)
    externs = {ext["name"]: ext for ext in ir["externs"]}
    assert externs["mint_token"]["secret_return"] is True
    assert externs["mint_token"]["returns"] == "Str"  # qualifier still stripped
    # ...and a `Secret[T]` PARAM keeps its own existing stamp
    assert externs["strict_write"]["params"][0]["secret"] is True


def test_an_ordinary_extern_ir_is_unchanged():
    """Additive: the flag is absent unless the author wrote `Secret[...]`."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(
        'extern pure fn plain(a: Str) -> Str = @py { return a }\n'
        'component C { }\n')
    assert "secret_return" not in ir["externs"][0]


def test_the_emitter_marks_both_ends_of_the_declared_marking():
    """The origin (a `Secret[T]` return) and the receiver (a `Secret[T]` provide
    param) are both marked in the emitted module, so the marking fires with no
    recorder attached: a plain `revl run` prints the same trace."""
    import emit as pyemit  # noqa: PLC0415 (backend module, path set above)

    from revl import compile_source  # noqa: PLC0415

    code = pyemit.emit(compile_source(LEAKY))
    assert "@_revl_secret_result" in code
    assert "_revl_mark_secret(sql)" in code
