"""The first-class per-turn admit+run crossing — roadmap item 330.

A running composition admits a model-authored per-turn source through the
in-language `admit` crossing, runs it against a granted tool surface, and the
turn's granted-emission + witnessed-fs crossings register into the ENCLOSING
session's 245 frame: they persist on commit and revert residue-free on abort. An
ungranted reach (or smuggled host code) is a refusal VERDICT — data handed back,
not a runtime escape.

The runtime proofs need a live cordis composition (install with
`sh backends/python/setup.sh`, run under its venv).
"""

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from revl import AdmissionProfile, compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the admit+run crossing is proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh`",
)

# The running composition: a granted tool surface (witnessed `stash` + immediate
# emission `shout`) exposed as the `Ops` service, PLUS the in-language admit
# crossing from stdlib/admit.rvl. `Ops` is what a per-turn source is granted to
# reach; the untrusted turn holds no host code of its own.
_BASE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn stash(p: Str)\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)

# The untrusted per-turn source: composes ONLY the granted `ops` tool — a
# witnessed fs mutation and an immediate emission. It declares no host code.
_TURN_OK = (
    "service Turn { emission fn run(p: Str, sink: Str) }\n"
    "component TurnComp requires ops: Ops provides turn: Turn {\n"
    "  provide turn {\n"
    '    fn run(p, sink) { emit ops.stash(p); emit ops.shout(sink, "from-turn") }\n'
    "  }\n"
    "}\n"
)


def _base_ir():
    # the running composition = the base tools + stdlib/admit.rvl, composed as
    # co-root files (components are never `use`-imported).
    from revl import compile_files
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    base_abs = os.path.abspath("base.rvl")
    return compile_files([base_abs, admit_path], sources={base_abs: _BASE})


def _session_loaded(record=False):
    from revl.mcp.session import Session
    s = Session()
    s.load(copy.deepcopy(_base_ir()), record=record)
    return s


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("payload", encoding="utf-8")
    return str(p)


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


def _mutated(path):
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path):
    return os.path.exists(path) and not os.path.exists(path + ".bak")


def _lines(sink):
    return Path(sink).read_text().splitlines() if os.path.exists(sink) else []


# --------------------------------------------------------------------------- #
# Compile-only: the base composition wires the classified admit crossing.
# --------------------------------------------------------------------------- #

def test_base_wires_the_classified_admit_crossing():
    ir = _base_ir()
    # `AdmitGate` provides `admission: Admission`, backed by the classified
    # `host_admit` emission extern — the in-language surface, on the G8 boundary.
    names = {c["name"] for c in ir["components"]}
    assert "AdmitGate" in names
    assert any(e["name"] == "host_admit" and e["class"] == "emission"
               for e in ir.get("externs") or [])


# --------------------------------------------------------------------------- #
# The admit DECISION, direct on the Session (host target of the crossing).
# --------------------------------------------------------------------------- #

@needs_cordis
def test_admit_commit_persists_witnessed_and_emission(artifact, sink):
    session = _session_loaded()
    verdict = session.admit(_TURN_OK, granted=["Ops"])
    assert verdict.admitted, verdict.message
    assert "turn" in verdict.keys

    # run the turn through its handle — crossings register into the 245 frame
    verdict.handle.call("turn", "run", [artifact, sink])
    assert _mutated(artifact), "witnessed mutation did not apply"
    assert _lines(sink) == ["announce:from-turn"]
    # the witnessed crossing rode the enclosing session's owner
    assert session._owner._witnessed_count() == 1

    manifest = session.commit()
    assert manifest["witnessed"]["count"] == 1
    result = session.commit_confirm(manifest["hash"])
    assert result["committed"]
    assert _mutated(artifact), "commit wrongly reverted the witnessed mutation"
    assert result["noResidue"], result["checks"]


@needs_cordis
def test_admit_abort_reverts_residue_free(artifact, sink):
    session = _session_loaded()
    verdict = session.admit(_TURN_OK, granted=["Ops"])
    assert verdict.admitted
    verdict.handle.call("turn", "run", [artifact, sink])
    assert _mutated(artifact)

    result = session.abort()
    assert result["aborted"]
    assert _pristine(artifact), "abort did not revert the turn's witnessed mutation"
    assert result["noResidue"], result["checks"]


@needs_cordis
def test_admit_ungranted_reach_is_a_refusal_verdict_not_an_escape(artifact):
    session = _session_loaded()
    # grant NOTHING: the turn's reach of `ops` is now outside the allowlist
    verdict = session.admit(_TURN_OK, granted=[])
    assert not verdict.admitted
    assert verdict.code == "R2"
    assert "not in the granted set" in (verdict.message or "")
    # the running composition is untouched — no key was wired, nothing ran
    assert "turn" not in session._driver._namespace()


@needs_cordis
def test_admit_smuggled_host_code_is_a_refusal_verdict(artifact):
    session = _session_loaded()
    smuggle = (
        "service Turn { fn run() -> Str }\n"
        "extern pure fn exfil(t: Str) -> Str = @py {\n"
        "    import os\n"
        "    return os.environ.get('HOME', '')\n"
        "}\n"
        "component TurnComp provides turn: Turn {\n"
        "  provide turn { fn run() = exfil(\"x\") }\n"
        "}\n"
    )
    verdict = session.admit(smuggle, granted=["Ops"])
    assert not verdict.admitted
    assert verdict.code == "G8"
    assert "forbids new" in (verdict.message or "")


@needs_cordis
def test_admit_may_not_replace_a_running_component(artifact):
    session = _session_loaded()
    # a turn naming the running `Agent` would be an implicit hot-swap — refused
    replace = (
        "extern emission fn announce2(sink: Str, msg: Str) = @py {\n"
        "    return\n"
        "}\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn stash(p) { emit announce2(p, \"noop\") }\n"
        "    fn shout(sink, msg) { emit announce2(sink, msg) }\n"
        "  }\n"
        "}\n"
    )
    verdict = session.admit(replace, granted=["Ops"])
    assert not verdict.admitted
    # either the no-extern profile (the replacement declares host code) or the
    # additive-only guard refuses it; both are refusals, not escapes.
    assert verdict.code in ("G8", "G2")


# --------------------------------------------------------------------------- #
# The in-language classified crossing: a running composition admits THROUGH
# `admission.admit(...)` (stdlib/admit.rvl), not the host method directly.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_in_language_crossing_admits_and_runs(artifact, sink):
    session = _session_loaded()
    # call the classified crossing the running composition provides
    granted = ["Ops"]
    raw = session.call("admission", "admit", [_TURN_OK, granted])["result"]
    verdict = json.loads(raw)
    assert verdict["admitted"], verdict.get("message")
    assert "turn" in verdict["keys"]

    # the turn is now wired into the running composition; drive it and commit
    session.call("turn", "run", [artifact, sink])
    assert _mutated(artifact)
    assert session._owner._witnessed_count() == 1

    manifest = session.commit()
    result = session.commit_confirm(manifest["hash"])
    assert result["committed"] and result["noResidue"], result.get("checks")
    assert _mutated(artifact)


@needs_cordis
def test_in_language_crossing_refuses_ungranted_reach(artifact, sink):
    session = _session_loaded()
    raw = session.call("admission", "admit", [_TURN_OK, []])["result"]
    verdict = json.loads(raw)
    assert not verdict["admitted"]
    assert verdict["code"] == "R2"
    assert "turn" not in session._driver._namespace()
