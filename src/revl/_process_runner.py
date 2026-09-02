"""One process of a placement composition (spawned by src/revl/placement.py).

Reads a spec (argv[1] -> JSON) describing this process's slice of the
composition, and brings it up on a cordis-py Context:

  * load the keys it must consume from other processes as bridge proxies —
    each same-composition proxy is first RE-ADMITTED against this process's
    own running manifest (item 337 Seam 2, `_boot_wiring_decision`) before it
    is wired; a proxy that fails admission is refused, never wired, and the
    refusal FAILS THE BOOT (`BootRefused`: no `UP`, non-zero exit) rather than
    leaving a half-dead composition behind;
  * load its own components (in IR load order);
  * serve the keys it provides that other processes need;
  * run any probe expressions against its provided services;
  * hold until SIGTERM, then tear down (consumers first) and exit.

While it holds, it also reads newline-delimited JSON *control* commands on
stdin — the channel `revl swap <component> --to <backend>` uses to drive a
live migration (docs/swap.md). Today one command is understood:

  {"op": "repoint", "key": "<k>", "socket": "<successor.sock>"}

  {"op": "repoint", "key": "<k>", "socket": "<successor.sock>",
   "component": "<c>", "backend": "<tier>"}

which re-points the proxy for `<k>` from its current provider to a successor
serving at `<socket>` — a *planned cutover*, not the peer-death withdrawal a
provider vanishing would trigger (`bridge._Client.repoint`). The `component`
and `backend` fields carry the admissible identity of the successor so this
process can RE-ADMIT it against its own running manifest before accepting the
cutover (item 337, `_repoint_decision`); a socket-only repoint with no such
reference is refused fail-closed. The wire's `component` is only a SELECTOR,
never an authorization: this process binds it to `<k>` against its own manifest
(`_providers_of`) and sanctions `<socket>` against its own placement directory
(`_sanction_address`) before the cutover is applied, so a selector naming an
unrelated component, or an address the receiver does not sanction, is refused.
The process acknowledges an accepted repoint with `[name] REPOINTED <k> -> <socket>`.

All output is line-prefixed with the process name so the conductor can
interleave several of these into one readable log. `[name] UP` marks a process
fully loaded; `[name] DOWN` marks a clean teardown, followed by a per-process
no-residue proof (`[name] residue ...`) so a provider torn down by a swap
proves it left nothing behind.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import signal
import sys
import threading
import types
import uuid
from pathlib import Path

from ._paths import backends_root


def _eval_probe(expr: str, namespace: dict):
    """Evaluate one probe: `key.method(literal, ...)` — and nothing else.

    A placement file is *data*, not a program. Probes are therefore parsed and
    dispatched, never `eval`'d: the admitted grammar is exactly the one the
    rust backend already required (`placement.py::_parse_probe`) — one method
    call on one key this process holds, with literal arguments
    (`ast.literal_eval`). No builtins, no imports, no attribute chains, no
    expressions. Anything else is refused with a message naming the form.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse probe (expected key.method(arg, ...)): {exc}") from exc
    call = tree.body
    if (not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.keywords):
        raise ValueError("probe must be of the form key.method(arg, ...)")
    key, method = call.func.value.id, call.func.attr
    if key not in namespace:
        held = ", ".join(sorted(namespace)) or "none"
        raise ValueError(f"{key!r} is not a key this process holds (holds: {held})")
    try:
        args = [ast.literal_eval(arg) for arg in call.args]
    except ValueError as exc:
        raise ValueError(f"probe arguments must be literals ({exc})") from exc
    target = getattr(namespace[key], method, None)
    if not callable(target):
        raise ValueError(f"{key!r} has no method {method!r}")
    return target(*args)


class BootRefused(RuntimeError):
    """This process REFUSED to wire one or more of its boot-time seams and can
    therefore not come up (item 337 Seam 2).

    A refusal at the REPOINT seam leaves a healthy composition running — the
    proxy keeps its current target, no blip — so a log line is the right
    weight there. A refusal at the BOOT seam has no healthy prior state to fall
    back on: the consumer's own components were compiled against a dependency
    that is now unwired, so every call into it fails at runtime with a
    `'<key>' has no method ...`. Continuing produced a half-dead composition
    that still printed `UP` and still exited 0, which is the failure mode that
    let a broken seam ship. So a refused boot proxy is a BOOT FAILURE: the
    process names every refused key, never prints `UP`, and exits non-zero, and
    the conductor's own `--once` gate ("processes did not come up") turns that
    into a non-zero `revl run --placement` exit.
    """


def _load_module(ir: dict) -> types.ModuleType:
    import emit  # noqa: PLC0415  backend dir already on sys.path

    source = emit.emit(ir)
    module = types.ModuleType("revl_proc_mod")
    sys.modules[module.__name__] = module
    exec(compile(source, "<revl-proc>", "exec"), module.__dict__)
    return module


# ---------------------------------------------------------------------------
# item 337 admission seams: the anchors the RECEIVER holds on its own.
#
# Mesh property 1 (docs/design/337-polyglot-admission-mesh.md:94) is that every
# input to a seam decision is receiver-controlled: "the wire supplies only which
# component to re-admit and onto which tier". A selector is therefore not an
# authorization. Two things must be receiver-derived beyond the manifest itself:
#
#   * the BINDING of the selector to the key whose proxy moves — otherwise any
#     admissible (component, backend) pair in the composition is a pass token
#     for ANY key;
#   * the ADDRESS the proxy is actually pointed at — the address IS the whole
#     effect of a repoint or a boot wiring, so admitting a selector and then
#     applying an unjudged address judges the wrong thing entirely.
#
# `_providers_of` supplies the first, `_seam_anchor` + `_sanction_address` the
# second. All three read only state this process already holds: its own
# compiled manifest, and the placement directory it was handed its own spec in.
# ---------------------------------------------------------------------------


def _providers_of(running_ir: dict, key: str) -> set[str]:
    """The components of THIS process's own running manifest that provide `key`.

    The same key -> component map `placement.py` builds (`key_component`,
    `placement.py:1742`), recomputed here from `running_ir =
    compile_files(spec["files"])` rather than believed off the wire. It answers
    two receiver-derived questions: is the selector the wire named genuinely the
    provider of this key, and — since a key this composition provides nowhere is
    by definition not a member of it — is a proxy a same-composition seam at
    all. The `remote` flag is never consulted for the latter: a flag is wire
    state, and a same-composition key carrying `remote: true` must not skip the
    gate.
    """
    return {c["name"] for c in (running_ir.get("components") or [])
            if key in (c.get("provides") or {})}


def _tls_identity(tls) -> tuple | None:
    """The (cert, key, ca, identity) a seam's mTLS block presents, realpath'd —
    or None when the block is absent or incomplete. `server_hostname` is
    deliberately excluded: it legitimately differs per peer (`placement.py`
    `_sni`), while the identity material does not."""
    if not isinstance(tls, dict):
        return None
    cert, key, ca, identity = (tls.get("cert"), tls.get("key"),
                               tls.get("ca"), tls.get("identity"))
    if not (cert and key and ca and identity):
        return None
    return (os.path.realpath(str(cert)), os.path.realpath(str(key)),
            os.path.realpath(str(ca)), str(identity))


def _seam_anchor(spec: dict, spec_path=None) -> dict:
    """The address anchors this process holds INDEPENDENTLY of any one seam
    entry, computed once at boot. Returns `{"dir": <placement dir or None>,
    "tls": <this process's own mTLS identity or None>}`.

    * `dir` is the directory this process was handed its OWN spec in
      (`argv[1]`), which the conductor creates with `mkdtemp` at 0700 and is the
      only place it ever binds a seam socket (`placement.py:1751-1754`,
      `2316`, and the swap successor's `new_sock`, `2427`). It comes from the
      exec'd argv, never from the spec JSON, so an edit to the JSON cannot move
      it — that is exactly what makes it a receiver-side anchor for a UDS
      address.
    * `tls` is the one mTLS identity this process itself presents on network
      seams; placement stamps the SAME `certs[pname]` material on this process's
      `serve` endpoint and on every network proxy it holds
      (`placement.py:1961-1969`), so a unanimous value is this receiver's own
      trust anchor. Disagreement (an injected entry carrying foreign material)
      collapses it to None, which refuses every network seam — fail closed.
    """
    directory = None
    if spec_path:
        directory = os.path.realpath(os.path.dirname(os.path.abspath(str(spec_path))))
    blocks = [((spec.get("serve") or {}).get("endpoint") or {}).get("tls")]
    for info in (spec.get("proxies") or {}).values():
        if isinstance(info, dict) and isinstance(info.get("endpoint"), dict):
            blocks.append(info["endpoint"].get("tls"))
    identities = {ident for ident in (_tls_identity(b) for b in blocks) if ident}
    return {"dir": directory, "tls": identities.pop() if len(identities) == 1 else None}


def _sanction_address(target, anchor: dict | None) -> str | None:
    """Judge the ADDRESS a proxy is about to be pointed at against what this
    receiver independently sanctions. Returns None when sanctioned, else the
    reason to REFUSE.

    A local UDS seam is sanctioned by containment in this process's own
    placement directory: the conductor binds every seam socket there and nowhere
    else, the directory is 0700, and the receiver learned it from its argv
    rather than from the spec. `/tmp/attacker.sock` is therefore not an address
    this receiver sanctions, whoever named it.

    A network seam is sanctioned by this process's own mTLS trust anchor: the
    receiver cannot enumerate the composition's machines (the addresses live in
    the placement manifest, which it does not hold), but it does hold the
    identity + CA it was itself issued, and a network seam is by construction
    "the two processes that hold CA-signed certs" (`bridge.TlsConfig`), not
    whoever can reach a port. An endpoint with no mTLS material, or material
    that is not this process's own, is refused. THE HONEST LIMIT, named rather
    than smuggled past: a spec edit that copies this process's own material onto
    a foreign host:port passes this check and is then stopped one layer down, at
    the handshake, because the foreign host holds no CA-signed leaf.

    Indeterminate is a refusal in both shapes: no placement directory, or no
    single mTLS identity of its own, means the receiver cannot sanction the
    address and therefore does not.
    """
    if isinstance(target, str):
        target = {"socket": target}
    if not isinstance(target, dict):
        return "seam target is neither a socket path nor an endpoint mapping"
    anchor = anchor or {}
    if target.get("host") is None:
        path = target.get("socket") or target.get("path")
        if not path:
            return "seam target carries no address; refused fail-closed (item 337)"
        anchor_dir = anchor.get("dir")
        if not anchor_dir:
            return ("receiver holds no placement directory of its own to sanction a "
                    "socket address against; refused fail-closed (item 337)")
        path = str(path)
        parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if parent != anchor_dir:
            return (f"socket {path!r} is outside this process's placement directory "
                    f"{anchor_dir!r} — not an address this receiver sanctions")
        if not os.path.basename(path).endswith(".sock"):
            return f"socket {path!r} is not a seam socket in the placement directory"
        return None
    if not str(target.get("host") or "") or not isinstance(target.get("port"), int):
        return "network seam target has no host/port; refused fail-closed (item 337)"
    identity = _tls_identity(target.get("tls"))
    if identity is None:
        return ("network seam target carries no complete mTLS material "
                "(cert/key/ca/identity) — not an address this receiver sanctions")
    if anchor.get("tls") is None:
        return ("receiver holds no single mTLS identity of its own to sanction a "
                "network seam against; refused fail-closed (item 337)")
    if identity != anchor["tls"]:
        return ("network seam target is not anchored on this process's own mTLS "
                "identity and CA — not an address this receiver sanctions")
    return None


def _selector_binding(running_ir: dict, key: str, component: str) -> str | None:
    """Bind the wire's SELECTOR to the key whose proxy actually moves. Returns
    None when `component` is genuinely a provider of `key` in this process's own
    running manifest, else the reason to REFUSE. Without this, admission of any
    component in the composition is a pass token for a repoint or a wiring of
    any OTHER key."""
    providers = _providers_of(running_ir, key)
    if not providers:
        return (f"key {key!r} is provided by no component of this process's own "
                f"running manifest; refused fail-closed (item 337)")
    if component not in providers:
        return (f"component {component!r} does not provide key {key!r} — this "
                f"process's own manifest names {', '.join(sorted(providers))}; an "
                "unrelated component is not an admission token for this key")
    return None


def _repoint_decision(spec_files: list, running_ir: dict, cmd: dict,
                      anchor: dict | None = None) -> tuple[bool, str | None]:
    """Decide whether a `repoint` control command may be accepted, item 337.

    A `repoint` re-points a live proxy onto a successor provider. It must pass
    the SAME admission gate boot and `revl swap` use, so it can never substitute
    an un-admitted provider at a placement seam. The wire command therefore
    carries the successor's admissible identity (`component`, `backend`), and
    this process re-admits that component against its OWN running manifest via
    `placement.swap_admission` (the standalone admission gate, `placement.py`),
    which is exactly what the conductor ran and is what this process has in
    scope: every process spec carries the full composition source, so
    `running_ir = compile_files(spec["files"])` IS the running manifest, with no
    extra transport. (`gate.admit_into` admits a fresh source into a manifest,
    the add-a-component shape, not a tier re-point, so `swap_admission` is the
    fit here.) Returns (True, None) to accept, or (False, reason) to REFUSE.

    Fail-closed: a legacy socket-only command that carries no admissible
    reference is REFUSED, never silently accepted. This is a planned-cutover
    gate only; the peer-death withdrawal path (`bridge._Client`) is untouched.

    The selector alone is NOT the decision. Before `swap_admission` runs at all,
    two receiver-derived checks bind the verdict to the thing that actually
    changes: `_selector_binding` refuses a `component` that this process's own
    manifest does not name as the provider of `cmd["key"]`, and
    `_sanction_address` refuses a `cmd["socket"]` outside the placement
    directory this process was handed its own spec in. Admitting a component
    while re-pointing a key at an unjudged address judged the wrong thing.
    """
    key = cmd.get("key")
    component, backend = cmd.get("component"), cmd.get("backend")
    if not key:
        return False, "repoint names no key; refused fail-closed (item 337)"
    if not component or not backend:
        return False, ("repoint carries no admissible successor reference "
                       "(component/backend); refused fail-closed (item 337)")
    problem = _selector_binding(running_ir, key, component)
    if problem:
        return False, problem
    problem = _sanction_address(cmd.get("socket"), anchor)
    if problem:
        return False, problem
    from revl.placement import swap_admission  # noqa: PLC0415
    try:
        _candidate, error = swap_admission(list(spec_files), running_ir, component, backend)
    except Exception as exc:  # noqa: BLE001  any admission failure fails closed
        return False, f"admission raised {type(exc).__name__}: {exc}"
    if error is not None:
        return False, error.splitlines()[0]
    return True, None


def _boot_wiring_decision(spec_files: list, running_ir: dict, key: str, info: dict,
                          anchor: dict | None = None) -> tuple[bool, str | None]:
    """Decide whether a boot-time bridge proxy may be wired to its provider,
    item 337 Seam 2 (`docs/design/337-polyglot-admission-mesh.md`).

    This is the landed repoint seam (`_repoint_decision`, above) moved one
    event earlier: instead of re-admitting a successor at a live cutover, the
    CONSUMER re-admits its provider before the INITIAL proxy wiring at boot.
    `info` is this process's own `spec["proxies"][key]` entry; placement.py
    already stamps the provider's admissible identity (`component`, `backend`)
    onto every same-composition proxy entry, so the selector is already in
    hand — no new transport. The gate runs against `running_ir =
    compile_files(spec["files"])`, exactly the manifest this process already
    computes at boot.

    THE QUESTION IS A RE-ADMISSION, NOT A SWAP. `_repoint_decision` asks
    `swap_admission`: "may this component be MOVED to that tier", which is
    right at a cutover, where a live consumer's in-address-space call site is
    about to become a cross-process one. At boot nothing moves: the provider is
    already on its tier, the seam already exists, and this consumer's call sites
    were compiled against that seam from the start. Asking the swap question
    here refused seams the conductor deliberately sanctioned — an
    address-space-bound (sync `fn`/`emission`) service across a same-tier
    py<->py seam is PERMITTED at plan time by construction, and across tiers it
    is permitted-and-reported — so a legitimate composition booted half-dead,
    its proxy silently unwired. `placement.seam_readmission` asks the plan-time
    question instead: recompile against the running manifest, then run the
    conductor's own `cross_tier_boundary_check` over the seam topology. Same
    gate, same verdict, independently re-derived by the receiver.

    Fail-closed, same three properties as `_repoint_decision`: a proxy entry
    carrying no admissible reference (component/backend) is REFUSED, never
    silently wired; any failure to reach a verdict fails closed; and a refused
    proxy is never wired at all. Whether an entry is a same-composition seam at
    all is decided by the CALLER from the receiver's own manifest
    (`_apply_boot_wiring` -> `_providers_of`), never from the entry's `remote`
    flag; a genuine cross-composition seam (item 151) is Seam 3, a different,
    deferred seam.

    And, as at the repoint seam, the selector is not the decision: the entry's
    `component` must be bound to `key` by this process's own manifest
    (`_selector_binding`), and the address the proxy would actually be pointed
    at — `info["endpoint"] or info["socket"]`, which is the whole effect of the
    wiring — must be sanctioned by the receiver's own anchors
    (`_sanction_address`). Keeping an honest selector while swapping the
    address underneath it is exactly the "injected wiring" this seam exists to
    catch. Those two are receiver-side BINDING and ADDRESS questions, orthogonal
    to which admission question the bound selector is then put to: they run
    first and unchanged, so `seam_readmission` only ever judges a selector this
    manifest already names as the provider of `key`, at an address this receiver
    already sanctions.

    The honest limit (named in the design, not smuggled past it): at boot the
    consumer holds the SAME centrally-sliced source the conductor already
    compiled from, so this catches a race or an injected wiring (a proxy
    pointed somewhere the manifest does not sanction) and a tier-seam
    violation — it cannot catch a provider whose actual running bytes differ
    from the source this consumer holds.
    """
    component, backend = info.get("component"), info.get("backend")
    if not component or not backend:
        return False, ("boot proxy carries no admissible provider reference "
                       "(component/backend); refused fail-closed (item 337)")
    problem = _selector_binding(running_ir, key, component)
    if problem:
        return False, problem
    problem = _sanction_address(info.get("endpoint") or info.get("socket"), anchor)
    if problem:
        return False, problem
    from revl.placement import seam_readmission  # noqa: PLC0415
    try:
        # `from_backend="py"` is this consumer's OWN tier — it is the py runner,
        # so the seam being judged is (py consumer <- `backend` provider),
        # which is the seam the conductor planned, not a hypothetical move.
        _candidate, error = seam_readmission(list(spec_files), running_ir,
                                             component, backend, from_backend="py")
    except Exception as exc:  # noqa: BLE001  any admission failure fails closed
        return False, f"admission raised {type(exc).__name__}: {exc}"
    if error is not None:
        return False, error.splitlines()[0]
    return True, None


async def _apply_boot_wiring(key: str, info: dict, spec_files: list, running_ir: dict,
                             wire, log=None, anchor: dict | None = None) -> bool:
    """Decide, then (only if admitted) perform one boot-time proxy wiring —
    item 337 Seam 2. `wire` is the actual proxy setup (async: builds the bridge
    proxy and awaits its fiber, in `run()` below); it is invoked ONLY when the
    named provider passes `_boot_wiring_decision`. Returns True when the proxy
    was wired, False when it was REFUSED — in which case `wire` is never
    called at all, so the refusal actually blocks the wiring rather than
    merely producing a diagnostic. Split out of `run()`'s boot loop so the
    admission seam is unit-testable without a live Context, mirroring
    `_apply_repoint`.

    Whether this entry is in scope is decided from the RECEIVER's own manifest,
    not from the entry: a key some component of `running_ir` provides is a
    same-composition seam and IS gated, whatever the entry's `remote` flag
    says. Only a key this composition provides nowhere is a genuine
    cross-composition handoff (item 151), which is Seam 3 — a different,
    deferred seam with its own trust-anchor requirement — and wires ungated.
    Believing a wire-carried `remote: true` instead let one flag skip the gate
    on a key this process's own manifest owns.
    """
    if _providers_of(running_ir, key):
        ok, reason = _boot_wiring_decision(spec_files, running_ir, key, info, anchor)
        if not ok:
            if log:
                log("proxy", key, f"REFUSED (admission): {reason}")
            return False
    elif log:
        log("proxy", key, "cross-composition remote (Seam 3): not Seam-2 gated")
    await wire()
    return True


def _apply_repoint(cmd: dict, clients: dict, spec_files: list, running_ir: dict,
                   log=None, anchor: dict | None = None) -> bool:
    """Apply one `repoint` command: re-admit the successor against the running
    manifest, and ONLY on admission re-point the proxy's `_Client`. Returns True
    when the proxy was re-pointed, False when the command was refused (the proxy
    keeps serving its CURRENT target, no blip). Split out of the control loop so
    the admission seam is unit-testable (item 337)."""
    key, sock = cmd.get("key"), cmd.get("socket")
    client = clients.get(key)
    if client is None:
        if log:
            log("repoint", key or "?", "no such proxy in this process")
        return False
    ok, reason = _repoint_decision(spec_files, running_ir, cmd, anchor)
    if not ok:
        if log:
            log("repoint", key, f"REFUSED (admission): {reason}")
        return False
    try:
        client.repoint(sock)
        return True
    except OSError as exc:
        if log:
            log("repoint", key, f"FAILED {type(exc).__name__}: {exc}")
        return False


async def _flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def run(spec: dict, spec_path=None) -> None:
    name = spec["name"]
    # item 337: the address anchors this process holds independently of any one
    # seam entry — the placement directory it was handed its own spec in (from
    # argv, not from the spec JSON) and the mTLS identity it presents itself.
    # Every seam address, at boot and at a live cutover, is judged against these.
    anchor = _seam_anchor(spec, spec_path)
    backend_dir = str(backends_root() / "python")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    import bridge  # noqa: PLC0415
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    def log(channel: str, subject: str, detail: str = "") -> None:
        print(f"[{name}] {channel:<6}| {subject:<16}| {detail}".rstrip(), flush=True)

    runtime_mod.set_trace(lambda event: log("host", event.split(" ", 1)[0],
                                            event.split(" ", 1)[1] if " " in event else ""))

    from revl.compiler import compile_files  # noqa: PLC0415
    # The full composition IR this process compiles from `spec["files"]`. Every
    # process spec carries the whole composition source, so this is the RUNNING
    # manifest a re-pointed successor must re-admit against at the seam (item
    # 337, `_repoint_decision`); it also feeds the emitter for this slice.
    running_ir = compile_files(spec["files"])
    module = _load_module(running_ir)
    root = Context()
    root.on("internal/status", lambda _this, fiber, old:
            log("fiber", fiber.name, f"{FiberState(old).name} -> {FiberState(fiber.state).name}"))

    fibers: list[tuple[str, object]] = []
    # key -> the proxy's _Client, so a `repoint` control command can carry the
    # seam to a successor without disposing the proxy fiber.
    clients: dict[str, object] = {}
    baseline_disposables = root.fiber._disposables.length

    # 1. proxies for keys provided by other processes. A proxy targets either a
    # local UDS (`socket`) or a network TCP+mTLS seam (`endpoint`); the bridge
    # normalizes both to an `Endpoint`. The deadline/withdrawal/canonical machinery
    # applies unchanged over either transport (docs/network-placement.md).
    refused: list[str] = []
    for key, info in (spec.get("proxies") or {}).items():
        # item 337 Seam 2: before wiring a same-composition proxy, re-admit its
        # provider against OUR OWN running manifest (`_apply_boot_wiring` ->
        # `_boot_wiring_decision`), asking the plan-time seam question the
        # conductor answered rather than the relocation question `revl swap`
        # asks. A refused provider is never wired — `_wire` never runs — and
        # the refusal is FATAL to this process (`BootRefused` below): an unwired
        # dependency is a half-dead composition, not a survivable degradation.
        # That covers a refused ADDRESS too, judged there against this process's
        # own anchors. Whether an entry is in scope is read off our own manifest
        # (`_providers_of`), never off its `remote` flag; only a key this
        # composition provides nowhere is the genuinely separate composition of
        # item 151, Seam 3, out of scope here, and wires ungated.
        async def _wire(key=key, info=info) -> None:
            target = info.get("endpoint") or info["socket"]
            # item 118 S1 / roadmap 421 F8: placement.py hands every local (UDS)
            # proxy entry a `correlation` block, namely this consumer's own
            # peer identity and secret, plus the composition it is scoped to.
            # Seal a fresh envelope per call (a fresh idempotency key each
            # time, so a captured-and-replayed request collides with the one
            # already admitted and is refused) rather than one static envelope
            # reused across calls. Absent (a network or item-151 seam, or a
            # spec from before this wiring existed), the wire is byte-identical
            # to before.
            corr = info.get("correlation")
            correlation = None
            if corr:
                from revl.deploy import Correlation, seal  # noqa: PLC0415
                secret = bytes.fromhex(corr["secret"])
                composition_id = corr["composition_id"]
                peer_identity = corr["peer_identity"]

                def correlation(k, method, _secret=secret, _cid=composition_id,
                                _peer=peer_identity):
                    return seal(Correlation(composition_id=_cid, generation=0,
                                            peer_identity=_peer,
                                            effect_id=f"{k}.{method}",
                                            idempotency_key=uuid.uuid4().hex),
                               _secret)
            proxy = bridge.proxy_component(key, info["methods"], target, module,
                                           deadline=info.get("deadline"),
                                           deadlines=info.get("deadlines"),
                                           async_methods=info.get("async_methods"),
                                           correlation=correlation)
            clients[key] = proxy["_client"]
            fiber = root.plugin(proxy)
            await fiber
            await _flush()
            fibers.append((f"{key}-proxy", fiber))
            log("proxy", key, f"-> {bridge.Endpoint.from_spec(target).describe()}")

        if not await _apply_boot_wiring(key, info, spec["files"], running_ir,
                                        _wire, log=log, anchor=anchor):
            refused.append(key)

    if refused:
        # Every refused seam is already logged with its reason; fail once,
        # naming them all, BEFORE loading anything. Nothing is up yet (the
        # proxy loop is the first boot phase), so there is nothing to tear down.
        raise BootRefused(
            f"[{name}] BOOT REFUSED: seam(s) {', '.join(refused)} failed "
            "re-admission at this consumer (item 337 Seam 2); a process cannot "
            "come up with an unwired dependency — see the REFUSED line(s) above")

    # 2. this process's own components. G3 proves the dependency graph is a
    # checked DAG, so independent branches are provably independent and boot
    # concurrently; only real provider -> consumer edges serialize (§46,
    # docs/parallel-activation.md). `depends` carries those intra-process edges,
    # reconstructed by placement.py from the compiler's inject/provides
    # structure. Absent it (a spec written before §46), fall back to the old
    # strictly-sequential chain so nothing becomes *less* ordered.
    from revl.activation import (activate_concurrent,  # noqa: PLC0415
                                 sequential_prereqs, teardown_lifo)

    components = list(spec["components"])
    prereqs = spec.get("depends")
    if prereqs is None:
        prereqs = sequential_prereqs(components)

    async def _activate(component: str):
        config = (spec.get("config") or {}).get(component, {})
        fiber = root.plugin(getattr(module, component), config)
        await fiber
        await _flush()
        log("load", component, f"state={FiberState(fiber.state).name}")
        return fiber

    completion, errors = await activate_concurrent(components, prereqs, _activate)
    # completion is a valid topological order (a task finishes only after its
    # prereqs did); teardown reverses it, so consumers fall before providers —
    # LIFO within every chain, revert semantics preserved (G7).
    for component, fiber in completion:
        fibers.append((component, fiber))
    for component, exc in errors:
        log("load", component, f"ERROR {type(exc).__name__}: {exc}")

    # 3. serve the keys other processes need
    server = None
    serve = spec.get("serve")
    if serve:
        # `methods` (key -> declared operations) is the stub's allowlist; fall
        # back to the bare key list for a spec written before it existed. The
        # served endpoint is a UDS (`socket`) or a network TCP+mTLS seam
        # (`endpoint`); over TCP the provider demands the consumer's cert (mTLS).
        serve_target = serve.get("endpoint") or serve["socket"]
        # item 118 S1 / roadmap 421 F8: a local (UDS) `serve` block carries the
        # secret table placement.py built for it, one entry per consumer of
        # these keys within THIS composition, so this seam runs the same
        # `CorrelationGuard` `tests/test_deploy_118.py` exercises instead of
        # leaving it unwired. Absent (a network provider, or a spec predating
        # this wiring) `correlation` stays None and the wire is unchanged.
        guard = None
        corr = serve.get("correlation")
        if corr:
            from revl.deploy import CorrelationGuard  # noqa: PLC0415
            guard = CorrelationGuard({identity: bytes.fromhex(secret_hex)
                                      for identity, secret_hex in corr["peers"].items()})
        # item 118 §1.4b: a NETWORK `serve` block may instead carry `peers`, the
        # declared set of mTLS identities allowed to call this seam. It is the
        # network counterpart of the correlation guard and a WEAKER property —
        # the handshake proves who is calling, the allowlist decides whether that
        # one may, and neither can dedup, because an off-placement peer cannot
        # hold this boot's secret and so sends no envelope. Absent, the wire is
        # unchanged and every identity the shared CA signed is answered.
        allow = None
        if serve.get("peers"):
            from revl.deploy import PeerAllowlist  # noqa: PLC0415
            allow = PeerAllowlist(serve["peers"])
        server = await bridge.serve(root, serve.get("methods") or serve["keys"], serve_target,
                                    module=module, correlation=guard, peers=allow)
        # The level this seam achieved, named on the seam's own log line so it is
        # readable where the seam is, not only in the conductor's audit block.
        if guard is not None:
            level = " (correlation-guarded)"
        elif allow is not None:
            level = f" (peer-pinned: {len(allow)} declared peer(s))"
        else:
            level = " (UNVERIFIED: no peer admission — any caller that reaches it)"
        log("serve", ", ".join(serve["keys"]),
            f"-> {bridge.Endpoint.from_spec(serve_target).describe()}" + level)

    # 4. probes: call provided services (may cross a seam), print results
    namespace = {key: root.get(key) for key in (spec.get("provides") or [])}
    for key in spec.get("proxies") or {}:
        namespace[key] = root.get(key)
    for expr in spec.get("probe") or []:
        try:
            value = _eval_probe(expr, namespace)
            if hasattr(value, "__await__"):
                value = await value
            await _flush()
            log("probe", expr, f"=> {value!r}")
        except Exception as exc:  # noqa: BLE001
            log("probe", expr, f"ERROR {type(exc).__name__}: {exc}")

    print(f"[{name}] UP", flush=True)

    # 5. hold until the conductor stops us, then tear down consumers first.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover (non-unix)
            pass

    # A control channel on stdin: the conductor pushes `repoint` commands here
    # to migrate a proxy to a successor provider (`revl swap`). Runs on a
    # daemon thread — `_Client.repoint` is thread-safe (its own lock) — and
    # hands results back to the loop only to log them in order.
    def control_reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("op") == "repoint":
                # item 337: a repoint must pass the SAME admission gate as boot
                # and `revl swap` before it can substitute a provider at this
                # seam. `_apply_repoint` re-admits the named successor against
                # our running manifest and re-points the `_Client` ONLY on
                # admission; on refusal the proxy keeps its current target (no
                # blip). This is the planned-cutover path only, distinct from
                # the peer-death withdrawal `bridge._Client` drives.
                if _apply_repoint(cmd, clients, spec["files"], running_ir, log=log,
                                  anchor=anchor):
                    print(f"[{name}] REPOINTED {cmd.get('key')} -> "
                          f"{cmd.get('socket')}", flush=True)

    threading.Thread(target=control_reader, name="revl-control", daemon=True).start()

    await stop.wait()

    # LIFO teardown: `fibers` is [proxies..., own components in completion
    # order], and completion is a valid topological order, so reversing it tears
    # consumers down before providers within every chain (G7 revert semantics),
    # then releases the external proxies last. teardown_lifo centralizes that
    # reverse-order guarantee; disposal stays sequential (best-effort).
    async def _dispose(_label, fiber):
        try:
            await fiber.dispose()
            await _flush()
        except Exception:  # noqa: BLE001  teardown is best-effort
            pass

    await teardown_lifo(fibers, _dispose)
    if server is not None:
        server.close()

    # per-process no-residue proof: a provider torn down by a swap (or the
    # whole placement stopping) must leave its Context empty — the distributed
    # form of `revl run`'s residue report (src/revl/run.py::_teardown).
    checks = {
        "registry": root.registry.size == 0,
        "provisions": root.reflect.store == {},
        "effects": root.fiber._disposables.length == baseline_disposables,
    }
    detail = (f"registry={root.registry.size} provisions={sorted(root.reflect.store)} "
              f"disposables={root.fiber._disposables.length}/{baseline_disposables}")
    verdict = "no residue" if all(checks.values()) else "RESIDUE LEFT"
    print(f"[{name}] residue {verdict} | {detail}", flush=True)
    print(f"[{name}] DOWN", flush=True)


def main() -> None:
    # The spec PATH, not just its contents: the directory the conductor wrote
    # this process's spec into is the placement directory (0700, mkdtemp) and is
    # the receiver's anchor for judging seam addresses (item 337,
    # `_seam_anchor`). It arrives through argv, so an edit to the spec JSON
    # cannot move it.
    spec_path = sys.argv[1]
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    try:
        asyncio.run(run(spec, spec_path=spec_path))
    except BootRefused as exc:
        # On stdout, so it lands in the conductor's interleaved trace next to
        # the REFUSED line that caused it — and exit non-zero, so a refused
        # seam is machine-visible instead of a note under a green run.
        print(str(exc), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
