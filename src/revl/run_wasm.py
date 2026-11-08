"""Runtime driver for the wasm tier (docs/v2.0-roadmap.md §2, "Toward early
production").

`revl run <manifest> --backend wasm` wired behind the *same driver contract*
the py, rust and java tiers use: compile -> emit the cordis-wasm modules ->
boot them -> tear down LIFO -> prove no residue -> exit. The wasm tier is the
substrate tier: components compile to WAT modules (backends/wasm/emit.py) that
run on the cordis-wasm runtime, where the paradigm is enforced by the wasmtime
sandbox (the coeffect specification *is* the emitted import section; a provision
*is* an export). That runtime lives in its own repo with its own wasmtime-bearing
interpreter, so — exactly as the rust tier boots a separate cordis-rs process
over the bridge seam (:mod:`revl.run_rust`) — this driver boots a separate
process under the cordis-wasm interpreter: the once-mode harness
(``backends/wasm/run_harness.py``). The driver does the revl-side work here
(compile + emit the WAT); the harness needs only wasmtime + the runtime.

What is wired and runs live (wherever the cordis-wasm runtime is present):

* **--once** — the boot -> LIFO teardown -> no-residue-proof -> exit round-trip.
  The harness plugs every module in load order on one cordis-wasm ``Runtime``
  (a consumer stays inactive until its coeffect is satisfied, so provider-first
  order brings the mesh to ``ACTIVE``), reports ``UP``, unplugs LIFO (the
  compiled ``deactivate`` state machine replays each component's inverses), and
  asserts the live runtime holds nothing afterwards (``rt.fibers`` empty and the
  coeffect table ``rt.table`` empty — the substrate mirror of the py driver's
  ``registry.size``/``reflect.store`` check). ``NO-RESIDUE`` is printed only when
  that holds.

What is NOT yet wired (honestly fenced, not faked):

* the **interactive REPL** over provided wasm services, for the same reason it
  is unwired on the rust/java tiers (see :mod:`revl.run_rust`). Without ``--once``
  the driver notes the gap and completes the same once round-trip.

The wasm tier is also the strictest emitter: ``config`` blocks, host builtins,
method-time effects, non-Int component services and a few other constructs are
hard ``EmitError`` (docs live in backends/wasm/README.md). A composition that
uses one is reported as an emit failure (exit 1) — the compile is fine, the
substrate simply does not carry it.

Runtime availability is a gate, not a lie: with no cordis-wasm runtime (no
wasmtime, or the harness cannot import it) the driver *skips with a reason* and
exits nonzero, exactly as the py tier does for a missing cordis-py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._once_proof import SPEC_FLAG, OnceProof
from ._paths import backends_root, venv_python
from .errors import RevlError
from .refusal import refusals
from .wal import header_record

_BACKENDS_DIR = backends_root()
_WASM_DIR = _BACKENDS_DIR / "wasm"
_HARNESS = _WASM_DIR / "run_harness.py"


# ---------------------------------------------------------------------------
# durable WAL: the host-side drain (item 322 Slice 2)
# ---------------------------------------------------------------------------
#
# The wasm sandbox has no direct filesystem, so — unlike the go tier, whose
# emitted code opens ``REVL_WAL`` and fsyncs directly (backends/go/emit.py's
# ``revlRecordTransactional``) — a record-mode wasm module FRAMES each witnessed
# transactional discharge-descriptor's runtime values out through the
# ``coeffect:revl:wal.record`` host import. The harness reads those values back
# from the module's memory and RELAYS them to stdout as one ``[wal] {…}`` frame
# per registration (the exact stdout-framing pattern ``run_go.py`` drains for
# its ``[run] …`` lines). This driver DRAINS those frames and writes+fsyncs them
# into the durable host WAL, in the SAME py JSONL schema recover reads from any
# tier (:mod:`revl.wal`): a ``header`` first, then one ``discharge-descriptor``
# per drained frame, then — on a clean unload only — ``discharge`` +
# ``activation-complete``. A crash before that terminal marker (the fsynced
# descriptors already durable) is the roll-back case recover handles.

_WAL_FRAME_PREFIX = "[wal] "


def wal_header() -> dict:
    """The WAL's first line: the schema the go/py tiers write, from the one
    shared builder (:func:`revl.wal.header_record`), so recover reads a wasm WAL
    through the tier-agnostic core with no wasm-specific casing."""
    return header_record(1)


def wal_descriptor(seq: int, receiver: str, method: str, witness) -> dict:
    """Assemble one witnessed transactional ``discharge-descriptor`` from a
    module frame's runtime values. Field-for-field the go tier's record
    (backends/go/emit.py ``revlRecordTransactional``): the re-issuable named
    call ``{receiver, method, args:[witness]}`` recover replays LIFO to undo the
    mutation, plus the forward ``origin`` it reverses."""
    call = {"receiver": receiver, "method": method, "args": [witness]}
    return {"record": "discharge-descriptor", "seq": seq, "entry": "transactional",
            "call": call, "origin": call, "witness": None, "idempotency": None}


def wal_discharge(seqs) -> dict:
    """The commit-path proof that every recorded seq COMMITTED — recover SKIPS
    a discharged seq (a committed transaction is never rolled back). Written
    only on a clean unload."""
    return {"record": "discharge", "discharged": list(seqs)}


def wal_activation_complete() -> dict:
    """The terminal marker: its PRESENCE is the whole roll-forward decision, its
    ABSENCE (a crash) is roll-back. Written only after a clean unload."""
    return {"record": "activation-complete", "generation": 1, "components": []}


class WALDurabilityError(RuntimeError):
    """A WAL record could not be made durable (its fsync failed) and the run
    did not opt out. Raised instead of passing silently (issue #2345)."""


#: The explicit, visible opt-out for a WAL target that cannot fsync (a pipe,
#: a character device). The same variable the go, rust and java sinks read; the
#: log records it in its header and in a ``durability-opt-out`` record.
ALLOW_UNSYNCED_ENV = "REVL_WAL_ALLOW_UNSYNCED"


def wal_allows_unsynced() -> bool:
    return os.environ.get(ALLOW_UNSYNCED_ENV) == "1"


def write_wal_record(handle, record: dict, *,
                     allow_unsynced: bool | None = None) -> None:
    """Append one JSONL record and fsync it: the write-ahead discipline the go
    tier uses, so a descriptor a run saw drained is on disk before the effect it
    describes is allowed to matter. A failed write or flush always raises
    :class:`WALDurabilityError`; a failed fsync raises it unless the run opted
    out (``allow_unsynced``, defaulting to ``REVL_WAL_ALLOW_UNSYNCED=1``)."""
    if allow_unsynced is None:
        allow_unsynced = wal_allows_unsynced()
    start = _wal_end_offset(handle)
    try:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
    except OSError as error:
        raise WALDurabilityError(
            f"write-ahead log write failed: {error}") from error
    try:
        os.fsync(handle.fileno())
    except (OSError, ValueError) as error:
        if not allow_unsynced:
            # the record is not durable, so it is not left in the file: the
            # same cut the py writer makes (backends/python/replay.py,
            # `WriteAheadLog._write` and `_drop_unsynced_tail`)
            _drop_unsynced_tail(handle, start)
            raise WALDurabilityError(
                f"write-ahead log fsync failed: {error}") from error


def _wal_end_offset(handle) -> int | None:
    """The byte length of the open WAL ``handle`` before a record is appended,
    or None when it has no file descriptor to measure."""
    try:
        return os.fstat(handle.fileno()).st_size
    except (OSError, ValueError, AttributeError):
        return None


def _drop_unsynced_tail(handle, start: int | None) -> None:
    """Truncate a just-appended record whose fsync failed (issue #2345). Best
    effort: the caller is already raising, so a failed truncate leaves the
    record and the run still stops."""
    if start is None:
        return
    try:
        os.ftruncate(handle.fileno(), start)
        handle.seek(0, os.SEEK_END)
    except (OSError, ValueError):
        return
    try:
        # make the cut itself durable, or a power loss could bring the record
        # back; on a target that refused the record's fsync this fails too,
        # and the run is stopping anyway
        os.fsync(handle.fileno())
    except (OSError, ValueError):
        pass


def drain_wal_frame(handle, line: str,
                    prefix: str = _WAL_FRAME_PREFIX) -> int | None:
    """If ``line`` is a ``<prefix>{…}`` frame, assemble its discharge-descriptor
    and write+fsync it to the open WAL ``handle``; return its seq. Otherwise
    return ``None``. This is the host-side drain that makes a wasm module's
    framed residue durable.

    :func:`run_wasm` passes the run's token-tagged prefix (``[wal#<token>] ``,
    issue #1621), so only the harness's own record channel reaches the WAL. A
    bare ``[wal] …`` line is then ordinary output: the frames share stdout with
    the program, and an undischarged descriptor is what recover replays."""
    text = line.strip()
    if not text.startswith(prefix):
        return None
    frame = json.loads(text[len(prefix):])
    seq = frame["seq"]
    write_wal_record(handle, wal_descriptor(
        seq, frame["receiver"], frame["method"], frame["witness"]))
    return seq


def _cordis_wasm_dir() -> Path:
    return Path(os.environ.get("CORDIS_WASM") or (Path.home() / "Projects" / "cordis-wasm"))


def _cordis_wasm_python() -> str | None:
    """The interpreter that runs the harness — the cordis-wasm venv (it carries
    wasmtime), overridable for CI. ``None`` when none is configured/present."""
    override = os.environ.get("REVL_CORDIS_WASM_PYTHON")
    if override and Path(override).exists():
        return override
    venv = venv_python(_cordis_wasm_dir() / ".venv")
    return str(venv) if venv.exists() else None


def wasm_runtime_reason() -> str | None:
    """``None`` when the wasm tier can actually run here, else why it cannot.

    The harness boots the emitted WAT on the cordis-wasm ``Runtime``, which is
    backed by the ``wasmtime`` Python bindings; the gate is that some interpreter
    can import both ``wasmtime`` and the cordis-wasm ``runtime`` module. A quick
    ``-c`` probe (imports only, boots nothing) is the fast, honest check — the
    wasm sibling of the rust tier's ``cargo generate-lockfile`` resolve.
    """
    cordis_wasm = _cordis_wasm_dir()
    if not cordis_wasm.is_dir():
        return (f"the cordis-wasm runtime was not found at {cordis_wasm} — "
                "clone it (https://github.com/inso1337/cordis-wasm) or set "
                "CORDIS_WASM to its checkout, then re-run")
    python = _cordis_wasm_python()
    if python is None:
        return (f"no cordis-wasm interpreter found (looked for {cordis_wasm}/.venv, "
                "or set REVL_CORDIS_WASM_PYTHON) — a wasmtime-bearing venv is "
                "needed to boot the WAT modules")
    probe = subprocess.run(
        [python, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import wasmtime; "
         "from runtime import Runtime", str(cordis_wasm)],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        return None
    detail = (probe.stderr or "").strip().splitlines()
    return (f"the cordis-wasm runtime could not be imported by {python}: "
            f"{detail[-1] if detail else '?'}")


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _wasm_emitter():
    """The wasm emitter module, from the one cached loader
    (`placement.backend_emit_module`, issue #2204).

    The caller keeps the module it is handed because `EmitError` is a class on
    the module OBJECT: a second `exec_module` produces a different class, and
    an `except` against it would not catch the instance the first module raised
    (issue #1406). Sharing one module per process makes that hold across
    loaders too, not only within one call. Unlike the java and go emitters this
    one holds no `global` render state, so nothing here needs a fresh copy."""
    from .placement import backend_emit_module  # noqa: PLC0415 - off the compile graph

    return backend_emit_module("wasm", root=_BACKENDS_DIR)


def _emit_modules(ir: dict, record: bool = False, emit_module=None) -> dict[str, str]:
    emit_module = _wasm_emitter() if emit_module is None else emit_module
    return emit_module.emit(ir, record=record)


def run_wasm(ir: dict, config: dict, files, once: bool = False,
             interactive: bool = False, policy=None,
             proof_out: dict | None = None) -> int:
    """Emit -> boot the composition on the cordis-wasm runtime as a process,
    then run the once round-trip (LIFO teardown + no-residue proof) and exit.
    Returns 0 on a clean ``UP`` -> ``NO-RESIDUE`` -> ``DOWN``; nonzero otherwise.
    A missing cordis-wasm runtime is a skip-with-reason and exit 3, mirroring the
    py/rust/java tiers (never a feint at passing).

    ``policy`` (item 289) is an optional boundary policy. Emit is pure and needs
    no runtime, so the least-authority chain that reads the emitted import
    sections runs BEFORE the runtime-availability gate: an over-authority wasm
    cell is refused as an admission decision whether or not wasmtime is present.
    """
    # item 322 Slice 2: with REVL_WAL set, emit in RECORD mode and drain the
    # module's framed discharge-descriptors into that durable host WAL. Unset
    # (every ordinary run), record mode is off and emission is byte-identical,
    # so a normal wasm run is exactly as before.
    wal_path = os.environ.get("REVL_WAL")
    record = wal_path is not None

    order = _load_order(ir)
    # issue #1406: `refusals(emit_module)` is the wasm emitter's own `EmitError`
    # and nothing wider. This used to be `except Exception`, which reported a
    # FAULT inside the emitter with the same sentence as a tier limit -- so an
    # `AttributeError` from an emitter bug arrived as "could not emit the wasm
    # composition (the substrate tier is the strictest emitter)", which reads as
    # a property of the author's document. A refusal is an answer and is
    # reported; a fault is a bug and keeps its traceback.
    emit_module = _wasm_emitter()
    try:
        modules = _emit_modules(ir, record=record, emit_module=emit_module)
    except refusals(emit_module) as refusal:
        print(f"error: could not emit the wasm composition (the substrate tier "
              f"is the strictest emitter; see backends/wasm/README.md):\n{refusal}",
              file=sys.stderr)
        return 1
    missing = [name for name in order if name not in modules]
    if missing:
        print(f"error: the wasm emitter produced no module for: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    # item 289: the least-authority chain. The import-subset-of-declared leg
    # already fired inside the emitter (a by-construction invariant); with a
    # policy in force we also refuse a wasm cell whose declared caps exceed the
    # policy allow-list. No policy: the emit-time leg is the whole check and
    # this is a no-op.
    from .least_authority import enforce_wasm_least_authority  # noqa: PLC0415
    try:
        enforce_wasm_least_authority(ir, policy, modules)
    except RevlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    reason = wasm_runtime_reason()
    if reason is not None:
        print(f"error: the cordis-wasm runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        print("note: the interactive REPL is wired for the py tier only; the "
              "wasm tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="revl_run_wasm_"))
    wal_handle = None
    drained_seqs: list[int] = []
    try:
        proof = OnceProof("run")
        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps({
            "name": "run",
            "once": True,
            SPEC_FLAG: True,
            "record": record,
            "order": order,
            "modules": {name: modules[name] for name in order},
        }), encoding="utf-8")

        if record:
            # open the durable WAL and stamp the header BEFORE the harness boots,
            # so a descriptor the drain sees is appended to a well-formed log. A
            # header that cannot be made durable stops the run here, before the
            # harness (and so any host body) starts (issue #2345).
            wal_handle = open(wal_path, "w", encoding="utf-8")
            header = wal_header()
            unsynced = wal_allows_unsynced()
            if unsynced:
                print(f"warning: {ALLOW_UNSYNCED_ENV}=1 disables the WAL fsync "
                      f"guarantee", file=sys.stderr)
                header["durability"] = "unsynced-opt-out"
            try:
                write_wal_record(wal_handle, header)
                if unsynced:
                    write_wal_record(wal_handle, {"record": "durability-opt-out",
                                                  "fromSeq": 0})
            except WALDurabilityError as error:
                print(f"error: {error}; the wasm composition was not started "
                      f"(set {ALLOW_UNSYNCED_ENV}=1 to opt out for a target "
                      f"that cannot fsync)", file=sys.stderr)
                return 1

        env = dict(os.environ)
        env["CORDIS_WASM"] = str(_cordis_wasm_dir())
        print("== load composition (wasm tier) ==", flush=True)
        proc = subprocess.Popen(
            [_cordis_wasm_python(), str(_HARNESS), str(spec_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env,
        )
        # the proof token is stdin's only line; then stdin closes, as before,
        # so the runner never blocks on it (issue #1621)
        proof.send(proc.stdin)

        assert proc.stdout is not None
        for line in proc.stdout:
            # drain a `[wal] {…}` frame into the durable WAL (item 322 Slice 2);
            # every other line is the harness's own `[run] …`/`_log` output.
            if wal_handle is not None:
                try:
                    seq = drain_wal_frame(wal_handle, line, proof.tag("wal"))
                except WALDurabilityError as error:
                    # a descriptor that is not durable stops the composition
                    # before it can run on past it (issue #2345)
                    proc.kill()
                    proc.wait()
                    print(f"error: {error}; the wasm composition was stopped",
                          file=sys.stderr)
                    return 1
                if seq is not None:
                    drained_seqs.append(seq)
                    continue
            # only a token-tagged line is the runtime's proof; anything else,
            # a program's own `[run] NO-RESIDUE` included, is output (#1621)
            sys.stdout.write(proof.line(line))
            sys.stdout.flush()
        rc = proc.wait()

        # a clean unload commits: stamp the discharge (recover then SKIPS those
        # seqs — a committed transaction is never rolled back) and the terminal
        # marker (its presence rolls forward). A nonzero exit / residue leaves
        # the descriptors on disk with no marker — the roll-back state.
        if wal_handle is not None and rc == 0 and proof.down and proof.no_residue:
            try:
                write_wal_record(wal_handle, wal_discharge(drained_seqs))
                write_wal_record(wal_handle, wal_activation_complete())
            except WALDurabilityError as error:
                print(f"error: {error}; the commit markers are not durable, so "
                      f"recover treats this run as not complete", file=sys.stderr)
                return 1
    finally:
        if wal_handle is not None:
            wal_handle.close()
        shutil.rmtree(tmp, ignore_errors=True)

    proof.record(proof_out)
    if rc != 0:
        print(f"error: the wasm composition process exited {rc}", file=sys.stderr)
        return 1
    if not (proof.up and proof.down):
        print("error: the wasm composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if proof.residue_left or not proof.no_residue:
        print("error: the wasm composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
