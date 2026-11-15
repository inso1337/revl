"""Re-run a recorded history against a changed component (issue #2713).

`revl simulate swap OLD NEW --history RUN.wal` answers a question the static
half of this tree cannot: what would the recorded calls into one component have
returned if that component had been the version in NEW. `revl simulate
policy-diff` regrades recorded crossings under another POLICY and `revl replay
--under` regrades them under an alternate policy against a candidate, but both
GRADE what was recorded. Neither re-runs anything, so a changed pure component
whose return value moved is invisible to them: the old value is on the record
and the new value is not.

This module is the bounded re-run. It is deliberately narrow, and every bound
is a named fact rather than a caveat in prose:

* **Slice 1, preflight only.** Before it executes a single step it reads the
  history and says whether that history can be re-run at all: the WAL must be
  whole (a torn tail or a missing `activation-complete` is refused), every
  recorded crossing into the swapped component must carry its recorded outcome
  and not just its `begin`, the history must carry a `determinism` record, and
  the swapped component must be inside the pure subset this command can execute
  offline. A missing fact is named, never guessed. When any is missing the
  command runs nothing.
* **Slice 2, the re-run.** For every recorded crossing into the swapped
  component, in recorded order, the OLD version is executed first against the
  recorded inputs. If it does not return the recorded outcome the history is
  not reproducible from OLD and that is reported, not papered over. Then the
  NEW version is executed. The first step whose value moved is named and the
  re-run stops there; dependency tracking between a changed value and a later
  crossing is not defined yet, so nothing downstream is claimed. A difference
  exits non-zero.
* **Zero live effect authority.** The re-run executes the emitted py backend in
  THIS process against a stub context that can only provide, set and get: no
  cordis, no fiber, no workspace, no file, no socket, no clock, no subprocess.
  That is only sound because the preflight refuses a component with a
  dependency: a component that requires a service, carries config, or has an
  effect, emission, await or host builtin in its body is not re-run, it is
  refused with the fact named (`purity`). The command therefore never
  dispatches a host call and never mutates the history it reads.

What a recorded outcome is
-------------------------
The WAL does not record the return of an ordinary service call; it records the
crossing. Under a two-phase admission the runtime DOES journal the outcome of a
fenced crossing (`replay.WriteAheadLog.record_fenced_crossing_complete`), and
that outcome is the value the call returned. `simulate swap` reads exactly that
channel (`admit-crossing` begin/complete pairs keyed by decision and ordinal,
design 460 §4), so it can serve a recorded value instead of inventing one. A
crossing whose method takes parameters is refused (`callArgs`): the WAL
journals the receiver and the method, not the arguments, and a re-run that
invented them would not be a re-run.

Determinism
-----------
A value diff is sound only when the clock and seed are captured. The WAL does
not carry them today, so this command reads a `determinism` record (see
:data:`revl.wal.RECORD_DETERMINISM`) and refuses a history without one
(`seedsAndClock`). A recorder that writes that record is what will make a real
run re-runnable; until then the fact is reported as missing, which is the
honest state of the capability.

The record is a precondition on the HISTORY, not an input the pure executor
reads: the subset has no clock and no RNG reach (a call to either is outside
it), so no replayed step can consume the seed or the readings. It is required
because the soundness rule is stated over the history, and because a future
executor for a host-dependent component is what would have to consume it. The
report says this in its `notAnswerable` section rather than implying a
connection the execution does not have.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from typing import Optional

from .errors import RevlError
from .refusal import refusals
from .wal import WALIntegrityError, read_wal

#: The journal record a two-phase admission writes for a fenced crossing
#: (design 460 §4). Its `complete` phase carries the call's recorded outcome.
RECORD_ADMIT_CROSSING = "admit-crossing"

#: The record that makes a re-run sound: the RNG seed and the clock readings
#: the run recorded. Absent from every run today, which the preflight reports.
RECORD_DETERMINISM = "determinism"

#: The IR expression kinds the offline re-run evaluates without a host. This is
#: a whitelist, not a filter: a kind not named here is refused by name rather
#: than evaluated by a fallback, so a shape this module has not reasoned about
#: can never reach the executor.
PURE_EXPRESSIONS = frozenset({
    "lit", "var", "name", "bin", "un", "field", "fn",
})

#: The steps a pure component's activation body may carry. A component body is
#: either a set of `provide` steps or it records effects (the compiler refuses a
#: plain `let` there), so this is the whole pure surface.
PURE_ACTIVATION_STEPS = frozenset({"provide"})

#: The steps a pure provide-method may carry.
PURE_METHOD_STEPS = frozenset({"return"})


class SwapError(RuntimeError):
    """A swap question cannot be answered: an unreadable input, a component
    that is not declared, or a document that does not compile."""


# ---------------------------------------------------------------------------
# reading the history
# ---------------------------------------------------------------------------


def load_history(path: str) -> dict:
    """Read one WAL, re-raising a read failure as a `SwapError` so the CLI has
    one error type to catch (the same shape `policy_diff.load_history` and
    `branch._load` set)."""
    try:
        return read_wal(path)
    except OSError as error:
        raise SwapError(f"cannot read history {path}: {error}") from None
    except WALIntegrityError as error:
        raise SwapError(str(error)) from None


def determinism(records: list) -> Optional[dict]:
    """The history's `determinism` record, or None when it carries none.

    None is the honest answer for every WAL written today: no recorder writes
    the seed or the clock readings, so a value diff over it would rest on a
    determinism this record does not establish."""
    return next((r for r in records if r.get("record") == RECORD_DETERMINISM),
                None)


def recorded_crossings(records: list) -> list:
    """Every fenced crossing the history journalled, in recorded order.

    Each entry joins the `begin` (receiver, method, ordinal) to its `complete`
    (outcome). A `begin` with no `complete` is kept with `complete: False` so
    the preflight can name the exact crossing whose host response is missing;
    dropping it would report a history as whole that is not. Order is the seq
    the writer owns, with a record that carries no seq sorted last rather than
    dropped (the same rule `counterfactual.recorded_crossings` follows)."""
    begins: dict = {}
    completes: dict = {}
    for record in records:
        if record.get("record") != RECORD_ADMIT_CROSSING:
            continue
        key = (record.get("decisionId"), record.get("ordinal"))
        if record.get("phase") == "begin":
            begins[key] = record
        elif record.get("phase") == "complete":
            completes[key] = record
    out = []
    for key in sorted(begins, key=lambda k: (begins[k].get("seq") is None,
                                             begins[k].get("seq") or 0)):
        begin = begins[key]
        complete = completes.get(key)
        call = begin.get("call") or {}
        out.append({
            "decisionId": key[0],
            "ordinal": key[1],
            "seq": begin.get("seq"),
            "receiver": call.get("receiver"),
            "method": call.get("method"),
            "tier": begin.get("tier"),
            "complete": complete is not None,
            "outcome": complete.get("outcome") if complete is not None else None,
            "completeSeq": complete.get("seq") if complete is not None else None,
        })
    return out


# ---------------------------------------------------------------------------
# reading the changed component
# ---------------------------------------------------------------------------


def _compile(path: str) -> dict:
    from .compiler import compile_files  # noqa: PLC0415 - off the read path

    try:
        ir = compile_files([path])
    except OSError as error:
        raise SwapError(f"cannot read component {path}: {error}") from None
    except RevlError as error:
        raise SwapError(f"the component {path} does not compile: {error}") \
            from None
    if ir.get("holes"):
        raise SwapError(
            f"the component {path} has open typed holes; fill them before a "
            "swap, because a hole is a step no re-run can name")
    return ir


def _component(ir: dict, name: Optional[str], where: str) -> dict:
    """The one component a swap document names.

    `name` picks it explicitly. Without a name the document must declare exactly
    one component, and the error says which ones it saw rather than picking one
    silently."""
    components = list(ir.get("components") or ())
    if name is not None:
        found = next((c for c in components if c.get("name") == name), None)
        if found is None:
            known = ", ".join(sorted(c.get("name") or "?" for c in components))
            raise SwapError(
                f"component {name!r} is not declared in {where} "
                f"(declared: {known or 'none'})")
        return found
    if len(components) == 1:
        return components[0]
    known = ", ".join(sorted(c.get("name") or "?" for c in components))
    raise SwapError(
        f"{where} declares {len(components)} components ({known or 'none'}); "
        "name the one to swap with --component")


def _same_component(old: dict, new: dict) -> str:
    if old.get("name") != new.get("name"):
        raise SwapError(
            "OLD and NEW declare different components "
            f"({old.get('name')!r} and {new.get('name')!r}); a swap changes one "
            "component, not its identity")
    return old.get("name")


def _method(component: dict, receiver: str, method: str) -> Optional[dict]:
    """The provided method's IR, or None when this version does not provide it.

    A method the NEW version removed is a REAL difference the report must name,
    not a preflight failure, so this returns None rather than raising."""
    if receiver not in (component.get("provides") or {}):
        return None
    for step in component.get("body") or ():
        if step.get("step") != "provide" or step.get("name") != receiver:
            continue
        for entry in step.get("methods") or ():
            if entry.get("name") == method:
                return entry
    return None


# ---------------------------------------------------------------------------
# the pure subset (the only component this command executes)
# ---------------------------------------------------------------------------


def _pure_expression(node, functions: dict, seen: frozenset) -> Optional[str]:
    """Why one expression is outside the pure subset, or None when it is inside.

    The walk allows a closed set of constructor/arithmetic/field forms and a
    call to a user-declared `fn`, recursing into that function's body. Every
    other kind is refused by name. `call` is not in the whitelist because it is
    the shape a required service or a host builtin reaches through, and `req`
    is refused for the same reason: a dependency is exactly what a re-run with
    no live world cannot supply."""
    if not isinstance(node, dict):
        return f"unreadable expression {node!r}"
    kind = node.get("kind")
    if kind not in PURE_EXPRESSIONS:
        return f"expression kind `{kind}` is not in the pure subset"
    if kind == "fn":
        name = node.get("name")
        # The ARGUMENTS are evaluated in the caller's scope and are exactly
        # where an impure call can hide: checking only the callee body would
        # let `inc(host_call())` through with the host call unchecked.
        for child in _children(node):
            reason = _pure_expression(child, functions, seen)
            if reason is not None:
                return reason
        callee = functions.get(name)
        if callee is None:
            return f"call to undeclared function `{name}`"
        if name in seen:
            return None
        for statement in callee.get("body") or ():
            reason = _pure_statement(statement, functions, seen | {name})
            if reason is not None:
                return reason
        return None
    for child in _children(node):
        reason = _pure_expression(child, functions, seen)
        if reason is not None:
            return reason
    return None


def _children(node: dict):
    """Every nested expression a whitelisted node may carry."""
    for key in ("left", "right", "operand", "value", "expr", "target",
                "condition", "then", "otherwise", "scrutinee", "else"):
        child = node.get(key)
        if isinstance(child, dict):
            yield child
        elif isinstance(child, list):
            yield from (item for item in child if isinstance(item, dict))
    for key in ("args", "fields", "items", "arms", "entries", "values"):
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    yield child


def _pure_statement(statement: dict, functions: dict,
                    seen: frozenset) -> Optional[str]:
    if not isinstance(statement, dict):
        return f"unreadable statement {statement!r}"
    step = statement.get("step")
    if step not in PURE_METHOD_STEPS:
        return f"method step `{step}` is not in the pure subset"
    return _pure_expression(statement.get("expr"), functions, seen)


def purity(component: dict, ir: dict) -> list:
    """The reasons one component is outside the re-runnable pure subset.

    An empty list means the component is pure: it requires no service, carries
    no config, its activation body is nothing but `provide` steps, and every
    provided method body is a `return` of an expression the offline evaluator
    can compute. Anything else is named, because "we did not re-run it" must
    read differently from "it matched"."""
    reasons = []
    if component.get("requires"):
        reasons.append(
            "requires %s: a dependency is a host response this re-run has no "
            "authority to dispatch" % ", ".join(sorted(component["requires"])))
    if component.get("config"):
        reasons.append(
            "declares config: the values the run was configured with are not on "
            "the record")
    functions = {fn.get("name"): fn for fn in (ir.get("functions") or ())}
    services = ir.get("services") or {}
    for statement in component.get("body") or ():
        step = statement.get("step")
        if step not in PURE_ACTIVATION_STEPS:
            reasons.append(
                f"activation step `{step}` is not in the pure subset")
            continue
        declared = (services.get(statement.get("service")) or {}).get(
            "methods") or {}
        for entry in statement.get("methods") or ():
            method = declared.get(entry.get("name")) or {}
            if method.get("async"):
                reasons.append(
                    f"method `{entry.get('name')}` is async: an await needs a "
                    "runtime this re-run does not boot")
            for body in entry.get("body") or ():
                reason = _pure_statement(body, functions, frozenset())
                if reason is not None:
                    reasons.append(
                        f"method `{entry.get('name')}`: {reason}")
    return reasons


# ---------------------------------------------------------------------------
# the offline executor
# ---------------------------------------------------------------------------


class _OfflineContext:
    """The whole host a pure component gets.

    It can provide a key, set a provision and get it back, and it drives the
    one activation generator the emitted body yields. It has no `effect` that
    could reach a world, no on/subscribe, no file or socket, and no clock. A
    pure component needs nothing else by construction; a component that reached
    for anything else would have been refused by `purity` before this class was
    constructed."""

    def __init__(self) -> None:
        self.provisions: dict = {}

    def effect(self, body, name: Optional[str] = None):
        generator = body()
        for _ in generator:
            pass
        return None

    def provide(self, key: str):
        return lambda: None

    def set(self, key: str, value) -> None:
        self.provisions[key] = value

    def get(self, key: str):
        if key not in self.provisions:
            raise KeyError(
                f"the component provides no key {key!r}; the recorded crossing "
                "names one it never provided")
        return self.provisions[key]


def _execute(ir: dict, component: str, receiver: str, method: str):
    """Run one recorded call against one component version, offline.

    Emits the py backend (the tier the compile graph already owns), executes the
    module in-process, activates the component against `_OfflineContext`, and
    calls the recorded method with no arguments. Nothing here boots a runtime or
    touches a host seam: the only effect driver is the stub's, which iterates the
    component's activation generator.

    Returns the value the method returned. Raises `SwapError` when the document
    cannot be emitted or the call cannot be made; the caller reports that as a
    step that could not be re-run rather than as a value."""
    from ._paths import backends_root  # noqa: PLC0415 - only when executing
    from .placement import backend_emit_module  # noqa: PLC0415

    backend = str(backends_root() / "python")
    inserted = backend not in sys.path
    if inserted:
        sys.path.insert(0, backend)
    module_name = "revl_simulate_swap_module"
    try:
        emit = backend_emit_module("python")
        try:
            source = emit.emit(ir)
        except refusals(emit) as error:  # a refusal is an answer, reported
            raise SwapError(
                f"the py emitter refused the document: {error}") from None
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
        try:
            exec(compile(source, "<simulate-swap>", "exec"),
                 module.__dict__)
        except Exception as error:
            raise SwapError(
                f"the emitted document did not load: {error}") from None
        entry = (getattr(module, "COMPONENTS", None) or {}).get(component)
        if entry is None:
            raise SwapError(
                f"the emitted document has no component {component!r}")
        context = _OfflineContext()
        try:
            entry["apply"](context, {})
        except Exception as error:
            raise SwapError(
                f"activating {component!r} offline raised: {error}") from None
        try:
            service = context.get(receiver)
        except KeyError:
            # The recorded crossing names a key this version does not provide
            # (a rename or a removal on the NEW side). That is a step that could
            # not be re-run, not a crash: the caller reports it and exits 1.
            raise SwapError(
                f"{component!r} provides no key {receiver!r}: the recorded "
                "crossing names a provision this version does not make") \
                from None
        target = getattr(service, method, None)
        if target is None:
            raise SwapError(
                f"{component!r} provides no method {receiver}.{method}")
        try:
            value = target()
        except Exception as error:
            raise SwapError(
                f"calling {receiver}.{method} on {component!r} raised: {error}") \
                from None
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise SwapError(
                f"{receiver}.{method} returned an awaitable; an async method "
                "is not in the pure subset this re-run executes")
        return value
    finally:
        sys.modules.pop(module_name, None)
        if inserted:
            try:
                sys.path.remove(backend)
            except ValueError:  # pragma: no cover - another caller removed it
                pass


# ---------------------------------------------------------------------------
# the preflight and the swap
# ---------------------------------------------------------------------------


def _params(component: dict, receiver: str, method: str) -> Optional[list]:
    entry = _method(component, receiver, method)
    if entry is None:
        return None
    return list(entry.get("params") or ())


def preflight(old_ir: dict, new_ir: dict, wal: dict,
              component: Optional[str] = None) -> dict:
    """Slice 1: can this history be re-run against this component, and if not,
    which fact is missing.

    Runs nothing. The result carries one entry per requirement and a `missing`
    list whose entries name the fact and the exact crossing or reason, so the
    command's refusal is actionable instead of a bare "not replayable"."""
    old_component = _component(old_ir, component, "OLD")
    new_component = _component(new_ir, component, "NEW")
    name = _same_component(old_component, new_component)
    provided = set(old_component.get("provides") or ()) | \
        set(new_component.get("provides") or ())

    records = wal.get("records") or ()
    run_complete = any(r.get("record") == "run-complete" for r in records)
    crossings = recorded_crossings(records)
    ours = [c for c in crossings if c.get("receiver") in provided]
    missing = []

    if wal.get("torn"):
        missing.append({
            "fact": "complete",
            "why": "the final record of the history is half written (the crash "
                   "itself), so the recorded order may be a prefix of the run "
                   "and a step after the tear is not on the record",
        })
    elif not wal.get("complete"):
        missing.append({
            "fact": "complete",
            "why": "the history carries no `activation-complete` record, so it "
                   "holds an activation prefix rather than a finished run",
        })
    elif not run_complete:
        missing.append({
            "fact": "complete",
            "why": "the history carries `activation-complete` but no "
                   "`run-complete`, so the run never shut down cleanly: a "
                   "steady-state kill leaves the post-activation crossings it "
                   "had not yet accounted for orphaned, and there is no torn "
                   "tail to mark it. The recorded order is a prefix",
        })

    unanswered = [c for c in ours if not c["complete"]]
    if unanswered:
        missing.append({
            "fact": "hostResponses",
            "why": "a recorded crossing into this component has no recorded "
                   "outcome: its `begin` is on the record and its `complete` is "
                   "not, so the value the call returned is not available and "
                   "the re-run would have to invent it",
            "crossings": [{"decisionId": c["decisionId"],
                           "ordinal": c["ordinal"], "seq": c["seq"],
                           "receiver": c["receiver"], "method": c["method"]}
                          for c in unanswered],
        })

    determinism_record = determinism(records)
    if determinism_record is None:
        missing.append({
            "fact": "seedsAndClock",
            "why": "the history carries no `determinism` record, so the seed and "
                   "clock readings the soundness rule names were never "
                   "captured: this command will not call the history replayable "
                   "without them rather than assume a determinism it did not "
                   "record",
        })
    else:
        absent = [key for key in ("seed", "clock")
                  if determinism_record.get(key) is None]
        if absent:
            missing.append({
                "fact": "seedsAndClock",
                "why": "the history's `determinism` record does not carry "
                       + " and ".join(absent),
                "absent": absent,
            })

    # A crossing whose method takes parameters cannot be re-run: the journal
    # records the receiver and method, never the arguments, and inventing them
    # would not be a re-run of the recorded call.
    parameterized = [c for c in ours
                     if c["complete"]
                     and (_params(old_component, c["receiver"], c["method"])
                          or [])]
    if parameterized:
        missing.append({
            "fact": "callArgs",
            "why": "the history journals a crossing's receiver and method but "
                   "not its arguments, so a parameterized method cannot be "
                   "re-run from the record",
            "crossings": [{"decisionId": c["decisionId"], "ordinal": c["ordinal"],
                           "receiver": c["receiver"], "method": c["method"],
                           "params": _params(old_component, c["receiver"],
                                             c["method"])}
                          for c in parameterized],
        })

    old_reasons = purity(old_component, old_ir)
    new_reasons = purity(new_component, new_ir)
    if old_reasons or new_reasons:
        entry = {
            "fact": "purity",
            "component": name,
            "why": "a component outside the pure subset is not re-run: the "
                   "command has no live world to supply its dependencies, and "
                   "executing it would give the command an effect authority "
                   "this surface must not have",
        }
        if old_reasons:
            entry["old"] = old_reasons
        if new_reasons:
            entry["new"] = new_reasons
        missing.append(entry)

    steps = []
    for crossing in ours:
        if not crossing["complete"]:
            continue
        params = _params(old_component, crossing["receiver"], crossing["method"])
        if params:
            continue
        steps.append({
            "ordinal": crossing["ordinal"],
            "seq": crossing["seq"],
            "decisionId": crossing["decisionId"],
            "receiver": crossing["receiver"],
            "method": crossing["method"],
            "recorded": crossing["outcome"],
        })

    if not ours:
        missing.append({
            "fact": "recordedSteps",
            "why": "the history records no crossing into this component, so "
                   "there is no recorded call to re-run",
        })

    return {
        "component": name,
        "complete": bool(wal.get("complete")) and not wal.get("torn"),
        "torn": bool(wal.get("torn")),
        "replayable": not missing,
        "missing": missing,
        "requirements": {
            "complete": not any(m["fact"] == "complete" for m in missing),
            "hostResponses": not any(m["fact"] == "hostResponses"
                                     for m in missing),
            "seedsAndClock": determinism_record is not None
            and all(determinism_record.get(key) is not None
                    for key in ("seed", "clock")),
            "callArgs": not parameterized,
            "purity": not old_reasons and not new_reasons,
        },
        "determinism": None if determinism_record is None else {
            "seed": determinism_record.get("seed"),
            "clock": determinism_record.get("clock"),
        },
        "crossings": len(crossings),
        "recordedSteps": len(steps),
        "steps": steps,
    }


def _same_value(left, right) -> bool:
    """Compare two JSON-shaped values without letting Python's `0 == False` or
    a tuple/list difference pass for equality."""
    try:
        return json.dumps(left, sort_keys=True) == \
            json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return left == right


def _surface(old_ir: dict, new_ir: dict, name: str) -> dict:
    """The static lifecycle and capability surface of OLD versus NEW.

    This half runs nothing and is not the re-run; it is what the swap report can
    still say about a component it refused to execute, so a lifecycle change is
    never silently absent. Capabilities come from the audit graph keyed by the
    same component name the boundary surface uses."""
    from .audit_diff import audit_report  # noqa: PLC0415

    def component(ir):
        return next((c for c in ir.get("components") or ()
                     if c.get("name") == name), {})

    def provided(comp):
        out = {}
        for key, service in sorted((comp.get("provides") or {}).items()):
            methods = []
            for step in comp.get("body") or ():
                if step.get("step") == "provide" and step.get("name") == key:
                    methods = sorted(m.get("name") for m in
                                     step.get("methods") or ())
            out[key] = {"service": service, "methods": methods}
        return out

    def activation(comp):
        return sorted(str(step.get("step")) for step in comp.get("body") or ())

    try:
        old_audit = audit_report(old_ir)
        new_audit = audit_report(new_ir)
    except Exception:  # the audit graph is decoration, the diff is not
        old_audit = new_audit = {}

    def capabilities(audit):
        boundary = (audit.get("boundary") or {}).get(name) or {}
        return {
            "emissions": sorted(json.dumps(e, sort_keys=True)
                                for e in boundary.get("emissions") or ()),
            "capabilities": boundary.get("capabilities") or {},
            "externs": sorted(json.dumps(e, sort_keys=True)
                              for e in boundary.get("externs") or ()),
        }

    old_provides = provided(component(old_ir))
    new_provides = provided(component(new_ir))
    old_keys, new_keys = set(old_provides), set(new_provides)
    old_requires = component(old_ir).get("requires") or {}
    new_requires = component(new_ir).get("requires") or {}
    old_caps = capabilities(old_audit)
    new_caps = capabilities(new_audit)
    changed = bool(
        old_keys != new_keys
        or any(old_provides[k] != new_provides.get(k) for k in old_keys & new_keys)
        or old_requires != new_requires
        or old_caps != new_caps
        or activation(component(old_ir)) != activation(component(new_ir)))
    return {
        "providesAdded": sorted(new_keys - old_keys),
        "providesRemoved": sorted(old_keys - new_keys),
        "methodsChanged": sorted(
            key for key in old_keys & new_keys
            if old_provides[key] != new_provides.get(key)),
        "requiresAdded": sorted(set(new_requires) - set(old_requires)),
        "requiresRemoved": sorted(set(old_requires) - set(new_requires)),
        "lifecycle": {
            "old": activation(component(old_ir)),
            "new": activation(component(new_ir)),
        },
        "capabilities": {
            "old": old_caps,
            "new": new_caps,
            "gained": sorted(set(new_caps["emissions"]) - set(old_caps["emissions"])),
            "lost": sorted(set(old_caps["emissions"]) - set(new_caps["emissions"])),
        },
        "changed": changed,
    }


def swap(old: str, new: str, history: str,
         component: Optional[str] = None) -> dict:
    """The whole `revl simulate swap` document.

    Reads the three inputs, runs the preflight, and (only when it is replayable)
    re-runs the recorded crossings in order against OLD and NEW, stopping at the
    first difference. Never admits, refuses, dispatches, or writes."""
    old_ir = _compile(old)
    new_ir = _compile(new)
    wal = load_history(history)
    pre = preflight(old_ir, new_ir, wal, component=component)
    name = pre["component"]

    document = {
        "kind": "revl.simulate-swap",
        "schemaVersion": "1.0",
        "old": old,
        "new": new,
        "history": history,
        "component": name,
        "complete": pre["complete"],
        "torn": pre["torn"],
        "replayable": pre["replayable"],
        "missing": pre["missing"],
        "requirements": pre["requirements"],
        "determinism": pre["determinism"],
        "steps": [],
        "firstDifference": None,
        "surface": _surface(old_ir, new_ir, name),
        "blastRadius": {
            "recordedSteps": pre["recordedSteps"],
            "rerunSteps": 0,
            "unrunSteps": pre["recordedSteps"],
        },
        "notAnswerable": [
            {"axis": "allowed/denied",
             "why": "the history carries no per-crossing admission verdict, so "
                    "whether a NEWLY declared capability would be allowed or "
                    "denied needs the policy the run was admitted under; "
                    "`simulate policy-diff` answers that over the recorded "
                    "action set"},
            {"axis": "causal reach",
             "why": "dependency tracking between a changed value and a later "
                    "crossing is not defined yet, so this report names the "
                    "first difference and stops rather than claiming what "
                    "downstream steps it reaches"},
            {"axis": "other components",
             "why": "only the changed component is re-run; a crossing that "
                    "some other component took is left exactly as recorded"},
            {"axis": "live effect authority",
             "why": "the re-run dispatches nothing. A component whose body "
                    "would require a host response is refused (`purity`) "
                    "rather than executed against a live world"},
            {"axis": "determinism use",
             "why": "the determinism record is a preflight precondition, not an "
                    "input the pure executor reads: the subset has no clock and "
                    "no RNG, so no replayed step consumes the seed or the "
                    "readings. A future executor for a host-dependent component "
                    "is what would have to consume them"},
        ],
    }

    if not pre["replayable"]:
        return document

    for step in pre["steps"]:
        receiver, method = step["receiver"], step["method"]
        try:
            old_value = _execute(old_ir, name, receiver, method)
        except SwapError as error:
            document["firstDifference"] = {
                **step, "kind": "not-reproducible",
                "value": None, "reason": str(error)}
            break
        if not _same_value(old_value, step["recorded"]):
            document["firstDifference"] = {
                **step, "kind": "not-reproducible",
                "value": old_value,
                "reason": "OLD did not reproduce the recorded outcome, so the "
                          "history is not a replay of this version"}
            break
        try:
            new_value = _execute(new_ir, name, receiver, method)
        except SwapError as error:
            document["steps"].append({
                **step, "old": old_value, "new": None, "same": False,
                "error": str(error)})
            document["firstDifference"] = {
                **step, "kind": "error", "value": None,
                "reason": str(error)}
            break
        same = _same_value(new_value, step["recorded"])
        document["steps"].append({
            **step, "old": old_value, "new": new_value, "same": same})
        if not same:
            document["firstDifference"] = {
                **step, "kind": "value", "value": new_value,
                "reason": "the changed component returned a different value"}
            break

    document["blastRadius"]["rerunSteps"] = len(document["steps"])
    document["blastRadius"]["unrunSteps"] = max(
        0, pre["recordedSteps"] - len(document["steps"]))
    return document


def ok(document: dict) -> bool:
    """Whether the swap is a clean answer: replayable, every recorded step
    reproduced, and no step moved. The CLI reads THIS rather than re-deriving
    the condition, so the exit status cannot drift from the document."""
    return bool(document["replayable"]) and document["firstDifference"] is None


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render(document: dict) -> str:
    """The human view: the facts the preflight found, the re-run it did, the
    first difference, and the static lifecycle/capability surface."""
    lines = ["swap %s over %s" % (document["component"], document["history"]),
             "  old        %s" % document["old"],
             "  new        %s" % document["new"]]
    lines.append("  history    %s%s" % (
        "complete" if document["complete"] else "not complete",
        ", torn tail" if document["torn"] else ""))
    lines.append("  preflight  %s" % (
        "replayable" if document["replayable"] else "REFUSED (runs nothing)"))
    for entry in document["missing"]:
        lines.append("    missing %-14s %s" % (entry["fact"], entry["why"]))
        for crossing in entry.get("crossings") or ():
            lines.append("      crossing %s %s.%s (seq %s, ordinal %s)" % (
                crossing.get("decisionId"), crossing.get("receiver"),
                crossing.get("method"), crossing.get("seq"),
                crossing.get("ordinal")))
        for reason in (entry.get("old") or ()) + (entry.get("new") or ()):
            lines.append("      %s" % reason)

    if document["steps"]:
        lines.append("")
        lines.append("  re-run (recorded order, first difference stops it):")
        for step in document["steps"]:
            mark = "same" if step.get("same") else "DIFFERS"
            lines.append("    %-14s %-18s recorded %s  old %s  new %s  %s" % (
                step["receiver"] + "." + step["method"],
                "(seq %s)" % step.get("seq"), _value(step.get("recorded")),
                _value(step.get("old")), _value(step.get("new")), mark))
            if step.get("error"):
                lines.append("      error %s" % step["error"])

    first = document["firstDifference"]
    if first is not None:
        lines.append("")
        lines.append("  FIRST DIFFERENCE at %s.%s (seq %s, ordinal %s): %s" % (
            first["receiver"], first["method"], first.get("seq"),
            first.get("ordinal"), first["reason"]))
        lines.append("    recorded %s" % _value(first.get("recorded")))
        lines.append("    value    %s" % _value(first.get("value")))
    elif document["replayable"]:
        lines.append("")
        lines.append("  no difference: every recorded step reproduced")

    radius = document["blastRadius"]
    lines.append("")
    lines.append("  blast radius  %d recorded step(s); %d re-run; %d not run" % (
        radius["recordedSteps"], radius["rerunSteps"], radius["unrunSteps"]))

    surface = document["surface"]
    lines.append("")
    lines.append("  surface (static, computed without running the component):")
    if surface["providesAdded"]:
        lines.append("    provides +  %s" % ", ".join(surface["providesAdded"]))
    if surface["providesRemoved"]:
        lines.append("    provides -  %s" % ", ".join(surface["providesRemoved"]))
    if surface["methodsChanged"]:
        lines.append("    methods ~   %s" % ", ".join(surface["methodsChanged"]))
    if surface["requiresAdded"]:
        lines.append("    requires +  %s" % ", ".join(surface["requiresAdded"]))
    if surface["requiresRemoved"]:
        lines.append("    requires -  %s" % ", ".join(surface["requiresRemoved"]))
    if surface["capabilities"]["gained"]:
        lines.append("    caps +      %s"
                     % ", ".join(surface["capabilities"]["gained"]))
    if surface["capabilities"]["lost"]:
        lines.append("    caps -      %s"
                     % ", ".join(surface["capabilities"]["lost"]))
    lines.append("    lifecycle   %s -> %s" % (
        ", ".join(surface["lifecycle"]["old"]) or "(none)",
        ", ".join(surface["lifecycle"]["new"]) or "(none)"))
    if not surface["changed"]:
        lines.append("    no static surface change")

    lines.append("")
    lines.append("  cannot say:")
    for entry in document["notAnswerable"]:
        lines.append("    cannot say %-20s %s" % (entry["axis"], entry["why"]))
    return "\n".join(lines)


def _value(value) -> str:
    """One value for the human view: JSON, so a string and a number cannot be
    mistaken for one another."""
    if value is None:
        return "null"
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)
