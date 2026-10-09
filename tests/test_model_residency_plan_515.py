"""What a host already holds reaches the scheduler that ranks by it (roadmap
item 515, issue #1189 - the acquisition half of design note 539 section 11.6
item 1, which is the exit clause of item 2118).

`tests/test_model_schedule_515.py` pins the DECISION: an arm that wrote
`prefer resident` tries the candidates the host already holds first, every
other arm is ordered by the written set, and a residency read by no arm is not
part of the decision. What was missing was a caller. The clause ranked only
when a caller handed the search a residency, and the one producer
(`model_schedule.resident_roles`) reads a provision that already happened - so
a fresh plan, where nothing is loaded yet, had nothing to hand it.

This file pins the acquisition and the wiring:

* `revl.providers.plan_time_residency` asks each server what it holds for the
  candidate roles of the arms that opted in, and derives the device CLASS from
  the server's own memory report;
* nothing is asked - not one request, not even the configuration read - unless
  `--providers` names the bindings AND an arm this placement schedules wrote
  the clause, so a composition that does not use it plans exactly as it did;
* a server that cannot be asked is a refusal naming the host and the server,
  raised before anything spawns, rather than a default;
* the conductor hands what it read to the scheduler, so the clause ranks, and
  the spec it writes carries the residency it ranked on, so the child
  re-derives the SAME decision instead of a second one.

Each group is written to be RED on the mutation of the claim it pins: ask
unconditionally (group 1), hand the scheduler no residency (group 2),
synthesise a held role the server did not report (group 1, the unsized case),
or drop the residency from the spec (group 3).

Every endpoint is a loopback fake that speaks the one endpoint this slice asks
and records what it was sent; nothing reaches a real provider.

Programs are inline for the census reason `tests/test_model_portfolio_515.py`
gives.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import model_schedule as ms  # noqa: E402
from revl import placement as _placement  # noqa: E402
from revl import providers as _providers_mod  # noqa: E402


# --------------------------------------------------------------------------
# a fake server: the one endpoint this slice asks
# --------------------------------------------------------------------------

#: The fake's model size, and the GPU bytes a CPU load still holds for its
#: compute graph (0.3%, which `ollama ps` prints as "100% CPU").
SIZE = 2_000_000
GRAPH = 6_000


class FakeServer:
    """Answers `GET /api/ps` and records every request path it is sent.

    This slice asks a server one question, so the fake speaks one endpoint:
    what it holds, as `{model: size_vram}` in `held`. `mode` is the ways the
    question can fail - "error" (the endpoint answers 500) and "unsized" (the
    report names the member without sizing it, which is the case the read must
    leave OUT rather than guess at).
    """

    def __init__(self) -> None:
        self.requests: list = []
        self.held: dict = {}
        self.mode = "ok"
        #: when set, the body `/api/ps` answers with, verbatim
        self.payload = None
        #: seconds the server waits before answering
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                if self.path != "/api/ps":
                    self._send({"error": "unknown"}, 404)
                    return
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.payload is not None:
                    self._send(outer.payload)
                    return
                if outer.mode == "error":
                    self._send({"error": "busy"}, 500)
                    return
                models = []
                for model, vram in outer.held.items():
                    entry = {"name": model, "model": model, "size_vram": vram}
                    if outer.mode != "unsized":
                        entry["size"] = SIZE
                    models.append(entry)
                self._send({"models": models})

            def _send(self, payload, code=200):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    fake = FakeServer()
    yield fake
    fake.close()


@pytest.fixture
def other():
    """A second server, so a test can show that one was never asked."""
    fake = FakeServer()
    yield fake
    fake.close()


# --------------------------------------------------------------------------
# the composition, and the placement it is scheduled against
# --------------------------------------------------------------------------

# Three routed actions on one host, each earning its place:
#
#   classify  opts in to residency, on the candidate set the other arm shares
#             with it, so a ranking here is visible;
#   label     the SAME candidate set and no clause, so a residency leaking out
#             of the arm that asked is visible as a reordering;
#   other     a role of its own (`cloud`), routed by an arm that did not ask,
#             which is the role a read must not ask a server about.
TWO_ARMS = """
model role fast on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory 512 quant int8
model role cloud off_device

service Answer {
  fn classify(text: Str) -> Str
  fn label(text: Str) -> Str
  fn other(text: Str) -> Str
}

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | small prefer resident
  }
  route model on label {
    * -> fast | small
  }
  route model on other {
    * -> cloud
  }
  provide out {
    fn classify(text) = text
    fn label(text) = text
    fn other(text) = text
  }
}
"""

#: The same composition with the clause removed, byte for byte otherwise.
WRITTEN_ORDER = TWO_ARMS.replace(" prefer resident", "")

#: The arm that opted in, the arm that did not, and the arm on `cloud`.
OPTED_IN = ("Classifier", "classify", "confidential")
SAME_SET = ("Classifier", "label", "*")
OFF_DEVICE = ("Classifier", "other", "*")

DEVICES = [{"name": "gpu0", "device": "gpu", "memory_mib": 8192,
            "quantisation": ["q4_k_m"]},
           {"name": "cpu0", "device": "cpu", "memory_mib": 16384,
            "quantisation": ["int8"]}]

DEVICES_TOML = """
[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]

[[processes.edge.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 16384
quantisation = ["int8"]
"""


def _program(tmp_path, source: str = TWO_ARMS, host: str = "edge") -> tuple:
    """Write the composition and return `(files, processes)`."""
    app = tmp_path / "app.rvl"
    app.write_text(source, encoding="utf-8")
    return ([str(app)],
            {host: {"components": ["Classifier"],
                    "devices": [dict(d) for d in DEVICES]}})


def _role(base: str, model: str = "tiny:1b") -> dict:
    """A managed Ollama binding: the only wire that reports residency."""
    return {"provider": "ollama", "base_url": base, "model": model,
            "devices": {"cpu0": {"num_gpu": 0}, "gpu0": {}}}


def _providers(tmp_path, roles: dict, name: str = "providers.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"roles": roles}), encoding="utf-8")
    return str(path)


def _read(files, processes, path):
    return _placement._plan_time_residency(files, processes, path)


def _chosen(entry) -> dict:
    """`{(component, action, origin): (role, device)}` from a handoff entry."""
    return {(p["component"], p["action"], p["origin"]): (p["role"], p["device"])
            for p in entry["schedule"]["placements"]}


def _closed_port() -> str:
    """A URL nothing is listening on: a server that cannot be asked."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


# --------------------------------------------------------------------------
# 1. What the read asks, and what it asks nothing about
# --------------------------------------------------------------------------

def test_a_composition_that_did_not_opt_in_is_asked_nothing(tmp_path, server):
    """F5, and what makes this safe to add: a placement that ranks nothing
    must not be planned against a server it does not otherwise need. Not one
    request reaches it, even with `--providers` naming three bindings."""
    files, processes = _program(tmp_path, WRITTEN_ORDER)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b"),
                                 "cloud": _role(server.base, "cloud:1b")})
    assert _read(files, processes, path) == ({}, None)
    assert server.requests == []


def test_a_composition_that_did_not_opt_in_does_not_read_the_config(tmp_path):
    """The gate is BEFORE the configuration is loaded, so a composition that
    does not use the clause cannot be made to fail on a providers file it
    never needed - and a caller cannot make it read one by passing a path."""
    files, processes = _program(tmp_path, WRITTEN_ORDER)
    absent = str(tmp_path / "no-such-providers.json")
    assert _read(files, processes, absent) == ({}, None)


def test_without_the_flag_no_server_is_asked(tmp_path, server):
    """`--providers` is what names the bindings, so without it there is
    nothing to ask about and nothing to rank by."""
    files, processes = _program(tmp_path)
    assert _read(files, processes, None) == ({}, None)
    assert server.requests == []


def test_the_candidate_read_asks_no_server(tmp_path, server):
    """`residency_candidates` is what decides whether ANY server is asked, so
    it has to be answerable from the composition and the placement alone. It
    is pure, and it names the candidates of the arm that opted in."""
    files, processes = _program(tmp_path)
    assert ms.residency_candidates(files, processes) == {
        "edge": ("fast", "small")}
    assert ms.residency_wanted(files, processes) is True

    files2, processes2 = _program(tmp_path, WRITTEN_ORDER)
    assert ms.residency_candidates(files2, processes2) == {}
    assert ms.residency_wanted(files2, processes2) is False
    assert server.requests == []


def test_a_role_the_configuration_does_not_bind_is_skipped(tmp_path, server):
    """`fast` is a candidate and is bound nowhere. A role with no binding is
    one revl does not load, so what a server holds for it is not something
    this placement can reuse: it is left out rather than asked about through
    another role's binding, and the request count says so."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})
    server.held = {"tiny:1b": SIZE}
    assert ms.residency_candidates(files, processes) == {
        "edge": ("fast", "small")}
    assert _read(files, processes, path) == ({"edge": {"small": "gpu"}}, None)
    assert len(server.requests) == 1      # `small`'s binding, and only it


def test_a_binding_revl_does_not_load_is_not_asked(tmp_path, server):
    """A role bound to an endpoint revl does not load has no load for this
    placement to reuse, so no server is asked and the clause ranks nothing.
    That is not a refusal: the arm asked, and nothing is loaded."""
    files, processes = _program(tmp_path)
    unmanaged = {"provider": "openai-compatible", "base_url": server.base,
                 "model": "tiny:1b"}
    path = _providers(tmp_path, {"fast": dict(unmanaged),
                                 "small": dict(unmanaged)})
    assert _read(files, processes, path) == ({}, None)
    assert server.requests == []


def test_the_class_is_the_one_the_server_reports_holding(tmp_path, server):
    """`/api/ps` answers which memory a member occupies and not which device
    it sits in, so the class is derived from the server's own accounting the
    way `provision.gpu_share` reads it: entirely in GPU memory is a `gpu`, a
    load that holds only its compute graph there is a `cpu`, and a server that
    holds nothing reports nothing."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})

    server.held = {"tiny:1b": SIZE}
    assert _read(files, processes, path) == ({"edge": {"small": "gpu"}}, None)

    server.held = {"tiny:1b": GRAPH}
    assert _read(files, processes, path) == ({"edge": {"small": "cpu"}}, None)

    server.held = {}
    assert _read(files, processes, path) == ({}, None)


def test_a_member_the_server_does_not_size_is_left_out(tmp_path, server):
    """A report that names a member without sizing it establishes no class,
    and the class is carried into the host's spec where the child re-derives
    it as a fact. So it is left OUT rather than guessed at, and the arm ranks
    nothing - the same decision it makes against a server holding nothing."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})
    server.held = {"tiny:1b": GRAPH}
    server.mode = "unsized"
    assert _read(files, processes, path) == ({}, None)
    assert server.requests == ["/api/ps"]   # asked, and honestly unanswered


def test_a_server_that_cannot_be_asked_refuses_by_name(tmp_path, server):
    """F6. A host whose arm wrote `prefer resident` is planned against what it
    holds, so a plan that cannot see that is refused rather than decided on an
    assumption - and the refusal names the host and the server, which is what
    a reader needs to fix it."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})
    server.mode = "error"
    residency, problem = _read(files, processes, path)
    assert residency == {}
    assert "host `edge`" in problem
    assert server.base in problem
    assert "prefer resident" in problem
    assert "start the server" in problem


def test_an_unreachable_server_refuses_by_name(tmp_path):
    """The other half of F6: a server that never answers is the same refusal,
    not a traceback and not an empty residency."""
    files, processes = _program(tmp_path)
    dead = _closed_port()
    path = _providers(tmp_path, {"small": _role(dead)})
    residency, problem = _read(files, processes, path)
    assert residency == {}
    assert "host `edge`" in problem and dead in problem


# --------------------------------------------------------------------------
# 1b. The binding is checked before any server is asked, each server is asked
#     once, its answer must say what it holds, and the wait is bounded
# --------------------------------------------------------------------------

def test_a_binding_the_placement_refuses_is_not_asked(tmp_path, server):
    """`small` is declared `on_device`; a binding that says its endpoint is
    `off_device` is refused by the placement check. That check runs BEFORE
    the plan-time read, so the server (and any credential) is never sent a
    request for a binding the plan goes on to refuse."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {
        "small": {**_role(server.base), "residence": "off_device"}})
    residency, problem = _read(files, processes, path)
    assert residency == {}
    assert problem and "on_device" in problem
    assert server.requests == []


def test_a_binding_for_an_undeclared_role_is_not_asked(tmp_path, server):
    """A configuration that binds a role the program does not declare is
    refused as a whole, before any of its servers is asked."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base),
                                 "ghost": _role(server.base, "ghost:1b")})
    residency, problem = _read(files, processes, path)
    assert residency == {}
    assert problem and "ghost" in problem
    assert server.requests == []


@pytest.mark.parametrize("payload", [
    {}, {"models": 5}, {"models": {"tiny:1b": {}}}, {"models": "tiny:1b"},
    {"models": None}])
def test_an_answer_without_a_list_of_models_refuses_by_name(tmp_path, server,
                                                            payload):
    """`{}` or a `models` that is not a list says nothing about what the
    server holds. Read as "holds nothing" it would silently drop the
    `prefer resident` the author wrote, and a non-list would be a traceback
    rather than a refusal; it is a refusal naming the host and the server."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})
    server.payload = payload
    residency, problem = _read(files, processes, path)
    assert residency == {}
    assert problem and "host `edge`" in problem and server.base in problem
    assert "`models`" in problem
    assert server.requests == ["/api/ps"]


def test_each_server_is_asked_once_per_plan(tmp_path, server):
    """Two candidate roles bound to the same server are one question, not
    two: every role is ranked against the same snapshot, and a slow server
    costs the plan one wait rather than one per role."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"fast:1b": SIZE, "small:1b": GRAPH}
    assert _read(files, processes, path) == (
        {"edge": {"fast": "gpu", "small": "cpu"}}, None)
    assert server.requests == ["/api/ps"]


def test_the_plan_time_question_waits_a_bounded_time(tmp_path, server,
                                                     monkeypatch):
    """The binding's completion timeout (120s by default) is sized for
    generating text; the plan-time listing waits at most
    `PLAN_PROBE_TIMEOUT`, and a server that does not answer within it is the
    same named refusal as one that is down."""
    monkeypatch.setattr(_providers_mod, "PLAN_PROBE_TIMEOUT", 0.3)
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"small": _role(server.base)})
    server.delay = 2.0
    started = time.monotonic()
    residency, problem = _read(files, processes, path)
    assert time.monotonic() - started < 1.5
    assert residency == {}
    assert problem and "host `edge`" in problem and server.base in problem


def test_a_providers_file_that_cannot_be_read_refuses(tmp_path, server):
    """A host that opted in and a configuration that cannot be loaded is the
    same refusal: the plan cannot rank, and ranking nothing silently would
    undo the preference the author wrote."""
    files, processes = _program(tmp_path)
    broken = tmp_path / "providers.json"
    broken.write_text("{not json", encoding="utf-8")
    residency, problem = _read(files, processes, str(broken))
    assert residency == {}
    assert problem and "not valid JSON" in problem


# --------------------------------------------------------------------------
# 2. The decision the conductor writes with it
# --------------------------------------------------------------------------

def test_the_read_reaches_the_decision_the_conductor_writes(tmp_path, server,
                                                            capsys):
    """F1, the whole slice: the clause ranked nothing before this because no
    caller had a residency to hand the scheduler. The conductor reads one and
    passes it, and the arm that asked settles on the candidate the host
    already holds - and says so, because that decision is not reproducible
    from the composition alone."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}          # the host holds `small`
    residency, problem = _read(files, processes, path)
    assert (residency, problem) == ({"edge": {"small": "cpu"}}, None)

    refusal, entries = _placement._model_schedules(files, processes, residency)
    assert refusal is None
    assert _chosen(entries["edge"])[OPTED_IN] == ("small", "cpu0")
    out = capsys.readouterr().out
    assert ("ranked by what the server reported holding at plan time "
            "(small (cpu))") in out
    assert "not reproducible from the composition alone" in out


def test_without_the_read_the_same_composition_takes_the_written_order(
        tmp_path):
    """The mutation F1 is red on: hand the scheduler no residency and the same
    program settles on the candidate it always did, with an entry that has no
    `residency` key. Nothing about the program differs between the two runs."""
    files, processes = _program(tmp_path)
    refusal, entries = _placement._model_schedules(files, processes, None)
    assert refusal is None
    assert _chosen(entries["edge"])[OPTED_IN] == ("fast", "gpu0")
    assert "residency" not in entries["edge"]


def test_an_arm_that_did_not_opt_in_is_not_reordered(tmp_path, server):
    """F2. The residency is read by the steps whose arm wrote the clause and by
    no others. `label` routes the SAME two candidates and did not ask, so a
    residency leaking out of `classify` would show up here as `small`."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}
    residency, problem = _read(files, processes, path)
    assert problem is None
    refusal, entries = _placement._model_schedules(files, processes, residency)
    assert refusal is None
    decided = _chosen(entries["edge"])
    assert decided[OPTED_IN] == ("small", "cpu0")     # asked, and ranked
    assert decided[SAME_SET] == ("fast", "gpu0")      # did not ask, and did not
    assert decided[OFF_DEVICE] == ("cloud", None)


def test_a_role_of_an_arm_that_did_not_ask_is_not_asked_about(tmp_path,
                                                              server, other):
    """The other half of the candidate set: `cloud` is routed on this host by
    an arm that did not write the clause, so its server is never asked even
    though the host ranked its other arm. Asking would be a request the
    decision cannot use."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b"),
                                 "cloud": _role(other.base, "cloud:1b")})
    server.held = {"small:1b": GRAPH}
    residency, problem = _read(files, processes, path)
    assert problem is None
    assert residency == {"edge": {"small": "cpu"}}
    assert other.requests == []


def test_ranking_changes_only_which_candidate_is_settled_on(tmp_path, server):
    """F7. The residency reorders the candidates the plan TRIES; it does not
    rewrite the candidate set, and `rank` is still an index into the set the
    program wrote. A reader of the plan reads the same candidate list either
    way, and only the settled role differs."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}
    residency, _ = _read(files, processes, path)
    _, ranked = _placement._model_schedules(files, processes, residency)
    _, written = _placement._model_schedules(files, processes, None)
    assert residency == {"edge": {"small": "cpu"}}

    a = ranked["edge"]["schedule"]
    b = written["edge"]["schedule"]
    assert [p["candidates"] for p in a["placements"]] == \
           [p["candidates"] for p in b["placements"]]
    # `rank` indexes the WRITTEN set, not the order tried: the ranked arm
    # settled on `small`, which the program wrote SECOND, and says so.
    assert [p["rank"] for p in a["placements"]] == [1, 0, 0]
    assert [p["rank"] for p in b["placements"]] == [0, 0, 0]
    assert a["resident"] == {"fast": "gpu0", "small": "cpu0"}
    assert b["resident"] == {"fast": "gpu0"}


def test_a_residency_for_an_arm_that_did_not_opt_in_is_not_carried(tmp_path):
    """F3, the guard that keeps the read from inventing an input: a caller may
    hand a residency for a host that does not read it (the read is per host),
    and the decision is then the written order AND the entry is the entry it
    always was, with no `residency` key."""
    files, processes = _program(tmp_path, WRITTEN_ORDER)
    _, entries = _placement._model_schedules(files, processes,
                                             {"edge": {"small": "cpu"}})
    entry = entries["edge"]
    assert "residency" not in entry
    assert _chosen(entry)[OPTED_IN] == ("fast", "gpu0")


# --------------------------------------------------------------------------
# 3. The spec carries it, so the child derives the same decision
# --------------------------------------------------------------------------

def test_the_spec_carries_the_residency_so_the_child_derives_the_same(
        tmp_path, server):
    """F4. The residency is an INPUT of the decision, exactly as the host's
    declared devices are, so the conductor carries it. The child re-derives
    from the files and this input and agrees; drop the carry and it derives
    the written order, which is not the decision it was handed, and refuses to
    boot."""
    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}
    residency, _ = _read(files, processes, path)
    _, entries = _placement._model_schedules(files, processes, residency)
    entry = entries["edge"]
    assert entry["residency"] == {"small": "cpu"}
    assert ms.verify_handoff(files, "edge", ["Classifier"], entry) == {
        "fast": "gpu0", "small": "cpu0"}

    del entry["residency"]
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff(files, "edge", ["Classifier"], entry)
    assert "does not match the one derived" in str(excinfo.value)


def test_a_spec_carrying_a_residency_no_arm_reads_is_refused(tmp_path):
    """F3 at the child: a residency can only ever reach a decision that reads
    it. A spec that carries one for a host whose arms all left the clause out
    is refused rather than carried, so the input cannot outlive its reader."""
    files, processes = _program(tmp_path, WRITTEN_ORDER)
    _, entries = _placement._model_schedules(files, processes, None)
    entry = entries["edge"]
    entry["residency"] = {"small": "cpu"}
    with pytest.raises(ms.ScheduleRefusal) as excinfo:
        ms.verify_handoff(files, "edge", ["Classifier"], entry)
    assert "no arm placed here wrote `prefer resident`" in str(excinfo.value)


def test_the_child_that_derived_it_installs_the_device_not_the_class(
        tmp_path, server):
    """The residency value is the CLASS a server report can establish, and the
    value the child INSTALLS is still the device name the scheduler chose. The
    class never reaches `model_placement`, so no host code can be answered
    with a class where a device belongs."""
    from revl import model_placement as mp  # noqa: PLC0415

    files, processes = _program(tmp_path)
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}
    residency, _ = _read(files, processes, path)
    _, entries = _placement._model_schedules(files, processes, residency)
    derived = ms.verify_handoff(files, "edge", ["Classifier"], entries["edge"])
    assert derived == {"fast": "gpu0", "small": "cpu0"}

    before = mp.installed()
    mp.uninstall()
    try:
        mp.install("edge", derived)
        assert mp.device_for("small") == "cpu0"
        assert mp.claim("small", "cpu0") == "cpu0"
    finally:
        mp.uninstall()
        if before is not None:
            mp.install(before["host"], before["resident"])


# --------------------------------------------------------------------------
# 4. End to end through the conductor
# --------------------------------------------------------------------------

class _SpawnReached(Exception):
    """The run got as far as starting a process."""


def test_the_conductor_refuses_before_spawning_when_a_server_cannot_be_asked(
        tmp_path, monkeypatch, capsys):
    """F6 where it matters: at the conductor, before anything spawns, with the
    host and the server named. `_preflight` is stubbed because it is the step
    that needs the cordis-py runtime and it runs BEFORE this one - the
    residency read is before every spawn either way, which is the claim."""
    spawned: list = []
    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_placement.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd))

    app = tmp_path / "app.rvl"
    app.write_text(TWO_ARMS, encoding="utf-8")
    plc = tmp_path / "placement.toml"
    plc.write_text('[processes.edge]\ncomponents = ["Classifier"]\n'
                   + DEVICES_TOML, encoding="utf-8")
    dead = _closed_port()
    path = _providers(tmp_path, {"fast": _role(dead, "fast:1b"),
                                 "small": _role(dead, "small:1b")})

    rc = _placement.run_placement([str(app)], str(plc), once=True,
                                  providers=path)
    err = capsys.readouterr().err
    assert rc != 0
    assert spawned == []
    assert "host `edge`" in err and dead in err
    assert "prefer resident" in err


def test_the_conductor_spawns_exactly_as_before_when_nothing_opted_in(
        tmp_path, monkeypatch, capsys):
    """F5 at the conductor: the same placement and the same providers file,
    with a composition that does not use the clause. The run reaches the
    spawn step (so it passed the residency step) having asked no server, so
    the read cannot make an existing placement depend on one it never needed.
    """
    server = FakeServer()
    try:
        spawned: list = []

        class SpawnReached(Exception):
            """The run got as far as starting a process."""

        def popen(cmd, **kwargs):
            spawned.append(cmd)
            raise SpawnReached

        monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
        monkeypatch.setattr(_placement.subprocess, "Popen", popen)

        app = tmp_path / "app.rvl"
        app.write_text(WRITTEN_ORDER, encoding="utf-8")
        plc = tmp_path / "placement.toml"
        plc.write_text('[processes.edge]\ncomponents = ["Classifier"]\n'
                       + DEVICES_TOML, encoding="utf-8")
        path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                     "small": _role(server.base, "small:1b")})

        with pytest.raises(SpawnReached):
            _placement.run_placement([str(app)], str(plc), once=True,
                                     providers=path)
        assert spawned
        assert server.requests == []
    finally:
        server.close()


def test_the_conductor_hands_it_to_the_scheduler_and_writes_it_down(
        tmp_path, monkeypatch, capsys, server):
    """F1 where it is load-bearing: the whole way through `run_placement`, the
    only caller of the scheduler. The composition opts in, the server holds
    `small`, and the spec the conductor writes for the child settles on `small`
    on `cpu0` and carries the residency it ranked on. Mutating the one argument
    at the call site - handing `_model_schedules` no residency - leaves this
    spec on `fast` with no `residency` key, which is what the clause did before
    this slice: ranked nothing, because nothing was there to hand it."""
    seen: dict = {}

    def popen(cmd, **kwargs):
        seen["spec"] = json.loads(
            Path(cmd[-1]).read_text(encoding="utf-8"))
        raise _SpawnReached

    monkeypatch.setattr(_placement, "_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_placement.subprocess, "Popen", popen)

    app = tmp_path / "app.rvl"
    app.write_text(TWO_ARMS, encoding="utf-8")
    plc = tmp_path / "placement.toml"
    plc.write_text('[processes.edge]\ncomponents = ["Classifier"]\n'
                   + DEVICES_TOML, encoding="utf-8")
    path = _providers(tmp_path, {"fast": _role(server.base, "fast:1b"),
                                 "small": _role(server.base, "small:1b")})
    server.held = {"small:1b": GRAPH}

    with pytest.raises(_SpawnReached):
        _placement.run_placement([str(app)], str(plc), once=True,
                                 providers=path)
    entry = seen["spec"][ms.SPEC_KEY]
    assert entry["host"] == "edge"
    assert entry["residency"] == {"small": "cpu"}
    assert _chosen(entry)[OPTED_IN] == ("small", "cpu0")
    out = capsys.readouterr().out
    assert ("ranked by what the server reported holding at plan time "
            "(small (cpu))") in out
