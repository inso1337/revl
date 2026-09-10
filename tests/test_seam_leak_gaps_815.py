"""Test-coverage gaps from the item-421 seam-leak audit (issue #815).

Each test here pins a scenario the two-stage scrub contract (421 F5/F6) could
silently regress on — the exact gaps the pre-V3 audit listed. They complement
`tests/test_seam_value_leaks_421_f5f6.py` (the F5/F6 contract itself) and
`tests/test_redaction_residuals_813.py` (the documented residuals):

* **cross-process origin**: every F5 runtime test made the *provider* fail;
  nothing probed a *consumer* holding a secret RETURNED from a remote
  `secret_return` extern — the value crossed a process boundary, was handed
  back to the consumer's own code, and only then reached a sink. The audit
  turned up a CLOSURE rather than a missed scrub (the marking is per-process,
  and the only legal reverse crossing is refused at compile time); the tests
  below pin both halves so widening either one fails here;
* **probe-result secret**: the py fixture's only probe returned ``None``
  (`test_seam_value_leaks_421_f5f6.py:316`), so the runner's own probe render —
  ``log("probe", expr, ...)`` — was never exercised while a registered secret
  was in scope. Driving that gap found a LIVE LEAK: the placement runner's
  console channels are not `_record`, so a probe whose extern failed locally
  printed a registered secret verbatim. The run test below is the regression
  test for the fix;
* **boundary floors**: `_MIN_MARKABLE=4` / `_MIN_MATCHABLE_ARG=3` had no test
  pinning the edge itself, and no reformat-bypass test (an exception message
  that case-folds its argument).

The seam tests below drive the REAL bridge over a UDS (two asyncio tasks in one
process stand for the two processes — the wire, the failure marshal and the
re-raise on the consumer are all real), because what is under test is what
crosses the boundary, not who spawned it. Where the *process* boundary is the
thing under test, a real `revl run --placement` subprocess is used instead.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import bridge  # noqa: E402
import confidential  # noqa: E402

def _py_emitter():
    """The py tier's emitter, loaded from its own file.

    A bare `import emit` is order-dependent: `backends/java/test_emit_java.py`
    puts `backends/java` on `sys.path` at import time and that tier ships an
    `emit.py` of its own, so whichever module pytest imported first decides which
    emitter the name resolves to. Loading by path makes the assertion below mean
    what it says no matter what else is in the run."""
    spec = importlib.util.spec_from_file_location(
        "_revl_py_emit_815", ROOT / "backends" / "python" / "emit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CANARY = "SEKRIT-CANARY-815-ORIGIN"
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
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


# ---------------------------------------------------------------------------
# cross-process origin: a secret RETURNED from a remote secret_return extern
# ---------------------------------------------------------------------------
#
# The audit's gap: every F5 runtime test made the PROVIDER fail, so the value
# under scrub was always an argument the caller had just sent. Here the secret
# crosses the seam the OTHER way — the provider's extern returns a `Secret[Str]`
# (the item-256 §7a origin), the consumer holds it, and its OWN failure message
# quotes it. The argument scrub cannot see it (it was never an argument of the
# failing call); only the value registration a `secret_return` marking performs
# closes the reverse crossing.

RETURNING = """
extern emission[store.mint] fn mint(u: Str) -> Secret[Str]
  = @py { return "SEKRIT-CANARY-815-ORIGIN" }

service Store { emission fn mint(u: Str) -> Secret[Str] }
"""

# The service above is deliberately left UNPLACED. Binding a provider to it
# would require a `provide` block whose `mint` returns the extern's `Secret[Str]`
# — and the taint checker refuses exactly that return (item 256 4a.2 kind 4: a
# provide-method return hands the bound key across the service / MCP bridge).
# The REFUSAL is the audit's context for this gap: a secret cannot legally
# arrive on a consumer by being returned from a provide method, so the shape
# the scrub contract actually defends is the one the runtime tests below
# drive — the provider's own failure quoting a value its extern minted.


def test_the_taint_refusal_is_why_this_scenario_has_no_component():
    """Pin the refusal itself (the negative space of the gap): attempting to
    place a provider whose provide method returns the minted secret is
    refused at compile time, so the only legal `secret_return` crossing is the
    extern's own body — the thing the origin marking decorates. If this test
    FAILS, the checker was widened and the seam tests below need revisiting."""
    from revl import compile_source  # noqa: PLC0415

    with pytest.raises(Exception) as caught:
        compile_source(RETURNING + """
component Minter provides store: Store {
  provide store {
    fn mint(u) {
      return emit mint(u)
    }
  }
}
""")
    assert "provide-method return" in str(caught.value)


def test_the_ir_stamps_the_secret_return():
    """The origin's declaration: a `Secret[Str]` return keeps its flag on the
    extern (the py fixture already pinned this for its own shape; this pins it
    for the returning scenario the seam tests below compile)."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(RETURNING)
    externs = {ext["name"]: ext for ext in ir["externs"]}
    assert externs["mint"]["secret_return"] is True


def test_the_emitted_origin_registers_the_returned_value():
    """The py emitter decorates the extern (runtime `secret_result`), so the
    value is registered at the ORIGIN on the provider tier — the marking the
    consumer's own message would need to have carried across."""
    from revl import compile_source  # noqa: PLC0415

    code = _py_emitter().emit(compile_source(RETURNING))
    assert "@_revl_secret_result" in code


def test_the_value_marking_does_not_cross_a_process_boundary():
    """The other half of the closure. `confidential`'s registry is a per-process
    global, so the PROVIDER registering the value it is about to return does not
    scrub anything on the consumer: a fresh interpreter that only renders the
    text still leaks it. This is why the refusal pinned above is the load-bearing
    invariant — and why a test may not claim the reverse crossing is scrubbed.

    Real subprocesses, not a simulated boundary: `forget_secret_values()` in the
    test process would not model anything."""
    source = (
        "import sys; sys.path.insert(0, {src!r}); sys.path.insert(0, {be!r});"
        "import confidential; print(confidential.redact_text({text!r}))"
    )

    def render(with_registration: bool) -> str:
        code = source.format(
            src=str(ROOT / "src"),
            be=str(ROOT / "backends" / "python"),
            text=f"KeyError: '{CANARY}'",
        )
        if with_registration:
            code = code.replace(
                "print(",
                f"confidential.register_secret_value({CANARY!r}); print(")
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=120, check=True)
        return done.stdout.strip()

    # the process that minted the value scrubs it...
    assert render(with_registration=True) == f"KeyError: '{REDACTED_SECRET}'"
    # ...the process that merely RECEIVED it does not
    assert CANARY in render(with_registration=False)


# ---------------------------------------------------------------------------
# probe-result secret: the runner's own probe render under a registered secret
# ---------------------------------------------------------------------------
#
# The audit's gap: the 421 fixture's only probe returned None, so the runner's
# probe render — `log("probe", expr, ...)`, one of the runner's OWN console
# channels rather than the `_record` trace — was never exercised with a
# registered value in scope. Driving it exposed a live leak: every channel the
# py and ts runners print for themselves (load / serve / proxy / fiber / probe)
# bypassed both funnels, so a probe whose extern failed locally printed a
# registered secret verbatim, in the same run whose `host` trace line redacted
# it. The go tier prints a local extern panic itself, before its `log` runs;
# java already funnelled its runner log through `redactSecrets`.

PROBED = """
extern emission[vault.connect] fn connect(key: Secret[Str], url: Str, user: Str) -> Unit
  = @py { raise KeyError(key + " @ " + url) }

service Vault { emission fn open(user: Str) -> Str }

component Keeper provides vault: Vault {
  config { url: Str = "pg://main", api_key: Secret[Str] = "SEKRIT-CANARY-815-PROBE" }
  provide vault {
    fn open(user) {
      emit connect(config.api_key, config.url, user)
      return "opened"
    }
  }
}
"""

PROBED_PLACEMENT = """
[processes.provider]
components = ["Keeper"]
probe = ["vault.open('alice')"]
"""


@pytest.fixture
def probed(tmp_path):
    source = tmp_path / "probed.rvl"
    source.write_text(PROBED, encoding="utf-8")
    toml = tmp_path / "probed.toml"
    toml.write_text(PROBED_PLACEMENT, encoding="utf-8")
    return source, toml


@needs_cordis
def test_a_probe_whose_extern_fails_locally_does_not_print_the_secret(probed):
    """The runner's probe channel is a sink in its own right.

    `Keeper.probe | vault.open('alice')| ERROR KeyError: '<canary> @ pg://main'`
    was what a real run printed before the fix — the extern raises locally, the
    failure never crosses the seam, so no `seam_failure` funnel runs; the runner
    builds the message and logs it. Assertions are paired: the canary is ABSENT
    **and** the marker PRESENT, plus an ordinary value from the same message and
    the ordinary channel lines, so a pass cannot mean "nothing was printed"."""
    source, toml = probed
    done = subprocess.run(
        [CORDIS_PY, "-m", "revl", "run", str(source),
         "--placement", str(toml), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(source.parent),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    trace = done.stdout + done.stderr
    assert done.returncode == 0, trace

    # the sink under test really fired (its own line, with the canary in the
    # message the runner built) and the ordinary channels are still printed
    assert "probe | vault.open('alice')| ERROR KeyError:" in trace, trace
    assert "host  | Keeper.config" in trace, trace
    assert "fiber | Keeper" in trace, trace

    assert "SEKRIT-CANARY-815-PROBE" not in trace, trace
    assert f"KeyError: '{REDACTED_SECRET} @ pg://main'" in trace, trace

    # the control: the non-secret half of the SAME message survives, so the
    # scrub replaced a value rather than blanking the diagnostic
    assert "pg://main" in trace, trace
    assert "vault.open('alice')" in trace, trace


# ---------------------------------------------------------------------------
# escaped canary: a value whose rendering is not its bytes (issue #815, item 3)
# ---------------------------------------------------------------------------
#
# The audit's gap: the canary in every other test is a plain identifier, so no
# sink was ever driven with a value that a RENDERER has to escape. That matters
# because redaction is an exact replace on RENDERED text: a sink that renders a
# value into JSON (or into a repr) first, and scrubs the rendered text after,
# sees `ROOT\"KEY` where the registry holds `ROOT"KEY` — the raw needle misses
# and the value crosses.
#
# WRITING THIS TEST FOUND THE LEAK, not a hypothetical one. Before the fix the
# run below printed the canary verbatim on the runner's probe channel while the
# same run's `host | Keeper.config` line rendered it redacted, and the cause was
# the registry rather than the sink: `_needles` registered the raw value only,
# so EVERY sink was uncovered for such a value — the trace, the WAL, the
# conductor inventory and the seam reply alike — while the same value without
# the quote was scrubbed everywhere. A `Secret[Str]` holding a password or a DSN
# with a `"` in it is ordinary, so the gap was total for a whole class of
# secrets rather than partial.
#
# The fix is in `confidential._renderings`: a registered string contributes the
# bytes the encoders that render host text actually write (raw, a `json.dumps`
# body, a `repr` body). The match stays EXACT and still runs over rendered text;
# what changed is that the needle is now one of the renderings instead of an
# assumption about which one.
#
# The marking only registers a value of len >= 4, so the canary is long enough
# to register AND ugly enough to render differently: `SEC"RET\815-CANARY-ESCAPED`.

ESCAPED_CANARY = 'SEC"RET\\815-CANARY-ESCAPED'

ESCAPED = """
extern emission[vault.connect] fn connect(key: Secret[Str], url: Str, user: Str) -> Unit
  = @py { raise KeyError(key + " @ " + url) }

service Vault { emission fn open(user: Str) -> Str }

component Keeper provides vault: Vault {
  config { url: Str = "pg://main", api_key: Secret[Str] = "SEC\\"RET\\\\815-CANARY-ESCAPED" }
  provide vault {
    fn open(user) {
      emit connect(config.api_key, config.url, user)
      return "opened"
    }
  }
}
"""

ESCAPED_PLACEMENT = """
[processes.provider]
components = ["Keeper"]
probe = ["vault.open('alice')"]
"""


def test_the_canary_really_is_escaped_where_the_harness_says_it_is():
    """Guards THIS test file: if revl's lexer stopped unescaping `\\"`/`\\\\`, the
    canary would be a plain value again and the run test below would quietly
    stop covering the escaped render. It is the same class of vacuity the audit
    found (an assertion that passes because the input stopped being what the
    test says it is)."""
    from revl import compile_source  # noqa: PLC0415

    ir = compile_source(ESCAPED)
    field = {f["name"]: f for f in ir["components"][0]["config"]}["api_key"]
    assert field["default"] == ESCAPED_CANARY
    assert '"' in ESCAPED_CANARY and "\\" in ESCAPED_CANARY


def test_one_registration_covers_every_face_a_sink_renders():
    """The registry, at the level the leak actually lived.

    One registration, one sink per encoder: the raw interpolation, what
    `json.dumps` writes (the WAL, the conductor inventory, the seam wire) and
    what `repr` writes (a container, a `str(exception)`). Before the fix only
    the first was scrubbed, so a value holding a quote crossed every sink
    verbatim. This runs without cordis, so the fix stays covered on a machine
    where the end-to-end test below is skipped."""
    confidential.register_secret_value(ESCAPED_CANARY)

    raw = f"api_key={ESCAPED_CANARY}"
    json_face = json.dumps({"api_key": ESCAPED_CANARY})
    repr_face = repr({"api_key": ESCAPED_CANARY})

    # non-vacuity: these really are DIFFERENT renderings of the value, so a
    # registry that held only the raw bytes could not match them
    assert json.dumps(ESCAPED_CANARY)[1:-1] != ESCAPED_CANARY
    assert repr_face != raw
    assert ESCAPED_CANARY in raw                      # the raw render matches
    assert ESCAPED_CANARY not in json_face, json_face  # the others are escaped
    assert ESCAPED_CANARY not in repr_face, repr_face

    for label, text in (("raw", raw), ("json", json_face), ("repr", repr_face)):
        scrubbed = confidential.redact_text(text)
        assert ESCAPED_CANARY not in scrubbed, (label, scrubbed)
        assert json.dumps(ESCAPED_CANARY)[1:-1] not in scrubbed, (label, scrubbed)
        assert confidential.REDACTED in scrubbed, (label, scrubbed)
        # the shape around the value survives on every face, so the sink is
        # still saying which field and which value class leaked
        assert "api_key" in scrubbed, (label, scrubbed)

    # and the false-positive half: a value that merely LOOKS escaped is not a
    # needle, because the faces are derived from a registered value, never
    # pattern-matched from the text
    ordinary = repr({"api_key": 'unrelated"value\\here'})
    assert confidential.redact_text(ordinary) == ordinary


@needs_cordis
def test_no_sink_carries_the_escaped_canary_in_its_escaped_or_raw_form(tmp_path):
    """The escaped render, through every sink a real run reaches: the `host`
    trace (`_record`, the registry funnel) and the runner's own `probe` channel
    (the leak this file's fix closed). Both forms are asserted absent — the raw
    bytes AND what `json.dumps` would have written for them — because a sink
    that encodes before it scrubs produces the second one."""
    source = tmp_path / "escaped.rvl"
    source.write_text(ESCAPED, encoding="utf-8")
    toml = tmp_path / "escaped.toml"
    toml.write_text(ESCAPED_PLACEMENT, encoding="utf-8")

    done = subprocess.run(
        [CORDIS_PY, "-m", "revl", "run", str(source),
         "--placement", str(toml), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(source.parent),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    trace = done.stdout + done.stderr
    assert done.returncode == 0, trace

    # the sinks fired (else the absences below are emptiness)
    assert "probe | vault.open('alice')| ERROR KeyError:" in trace, trace
    assert "host  | Keeper.config" in trace, trace

    escaped = json.dumps(ESCAPED_CANARY)[1:-1]
    assert escaped != ESCAPED_CANARY          # the two forms really do differ
    assert ESCAPED_CANARY not in trace, trace
    assert escaped not in trace, trace
    # ...and each sink says why it is empty: the marker, in the message the
    # runner built, with the non-secret half of that message still readable
    assert f"KeyError: '{REDACTED_SECRET} @ pg://main'" in trace, trace
    assert "pg://main" in trace, trace


# ---------------------------------------------------------------------------
# boundary floors: _MIN_MARKABLE=4 / _MIN_MATCHABLE_ARG=3, and the reformat
# ---------------------------------------------------------------------------


def test_the_length_floors_are_pinned():
    """The floors are load-bearing constants of the exact-match tradeoff
    (issue #813 documents them; this pins the edges themselves). The audit
    found no test naming them, so a change to either would land silently."""
    assert confidential._MIN_MARKABLE == 4
    assert confidential._MIN_MATCHABLE_ARG == 3


def test_at_the_markable_floor_a_secret_is_scrubbed_one_below_is_not():
    """The edge of the value-registration floor, both sides: len 4 registers
    (and scrubs), len 3 does not (and prints verbatim). Pinning the EDGE is
    what a silent floor change would break."""
    # at the floor
    confidential.register_secret_value("abcd")
    out = confidential.redact_text("token=abcd")
    assert "abcd" not in out and REDACTED_SECRET in out
    # one below
    confidential.register_secret_value("abc")
    out = confidential.redact_text("token=abc")
    assert out == "token=abc"


def test_at_the_matchable_arg_floor_an_argument_is_scrubbed_one_below_is_not():
    """The edge of the argument floor (deliberately one lower than the secret
    floor): len 3 matches, len 2 does not."""
    # at the floor
    text = bridge.seam_failure(KeyError("xyz"), ["xyz"])
    assert "xyz" not in text and REDACTED_ARG in text
    # one below
    text = bridge.seam_failure(KeyError("id"), ["id"])
    assert "id" in text and REDACTED_ARG not in text


def test_a_reformatted_argument_bypasses_the_exact_match():
    """The reformat-bypass residual as a REGRESSION SEAM: an exception message
    that case-folds its argument renders the canary in a form no needle equals,
    so the scrub misses it BY DESIGN (the exact-match contract, #813). If this
    test FAILS, the matcher was widened — revisit the documented tradeoff, do
    not delete the test."""
    confidential.register_secret_value(CANARY)
    folded = CANARY.lower()
    text = bridge.seam_failure(ValueError(f"no row for {folded} in ledger"), [CANARY])
    # the exact needle was scrubbed where it appeared verbatim...
    assert CANARY not in text
    # ...and the case-folded rendering crossed — the documented bypass
    assert folded in text, "the case-fold residual was closed: update #813's doc"


def test_a_truncated_argument_bypasses_the_exact_match():
    """The same residual on the truncation axis: a host that prints the first
    5 bytes of an 8-byte secret emits no exact needle, and the text crosses."""
    confidential.register_secret_value(CANARY)
    head = CANARY[:5]
    text = bridge.seam_failure(ValueError(f"short token: {head}"), [CANARY])
    assert CANARY not in text
    assert head in text, "the truncation residual was closed: update #813's doc"
