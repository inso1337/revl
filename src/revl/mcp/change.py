"""`revl_change`: one intent-shaped call for the whole change loop (issue #1695).

In an agent benchmark one change took eight calls, five of them `revl_check`
polls, because the surface made the agent orchestrate load, plan, apply and
re-verify itself. `revl_change` takes the intent and runs that loop server-side:

* load, if nothing is loaded and `files`/`source` are given (a holed candidate
  opens a draft, as `revl_load` does);
* plan: for a withdrawal, the reactive cascade the running composition would
  tear down (`query.withdrawal`), reported before anything happens;
* apply the change to a working copy of the server-side source, which is the
  speculative state: nothing about the running composition changes yet;
* verify: admission against the running composition (the checker's
  guarantees), the lease and quarantine gates, and, with `gauntlet: true`, the
  gauntlet's isolated boot and unload of the exact candidate;
* commit, a hot swap (or, for a draft, the boot), only if all of that passed.

It is a translation onto `revl_edit`, not a second implementation of it: every
intent becomes `revl_edit` edits, so the jail, the trust rule, the gates and
the draft handling are the ones `revl_edit` already runs. The intents:

* ``{edit: {target?, edits}}``: `revl_edit`'s own patch;
* ``{replace: {component, source}}``: one declaration replaced by symbol;
* ``{withdraw: "Name" | {component, cascade?}}``: the declaration removed and
  withdrawn from the running composition. A withdrawal whose cascade is not
  empty is refused by admission (a dependent would lose its provider) unless
  `cascade: true`, which withdraws the whole cascade with it.
* ``{add: {source, target?}}``: new declarations appended to a buffer (the
  only one, or `target`), refused if a name is already declared.
* ``{add: {component, provide, methods, config?, target?}}``: the server writes
  the component (issue #1700, `component_source`). `provide` is a key,
  `"key: Service"` (`"kv: Kv"`), or
  `{key, service}`; its service is the one given, or the one the composition
  already knows that key as. Each method's frame comes from the service
  declaration, `methods` holding only the bodies. `requires` is inferred:
  every receiver a body calls (`store.get()`) that is a key of the composition.
  An unknown receiver, a missing or unknown operation, or a `config.` read with
  no `config` given is refused, naming it. `config` is never inferred.

`verified.guarantees` reports the G1-G9 self-check of the verification
compile (#1731's `self_check`), so "verifies the guarantees" is visible per
guarantee, not only as an admission verdict.
"""

from __future__ import annotations

import re

from .. import query as _query
from ..errors import RevlError
from . import authoring_loop as _authoring_loop
from . import edit as _edit

INTENTS = ("edit", "replace", "withdraw", "add")


class ChangeError(ValueError):
    """The intent itself is malformed. Nothing was loaded or changed."""


def intent_of(arguments: dict) -> str:
    named = [name for name in INTENTS if arguments.get(name) is not None]
    if len(named) != 1:
        raise ChangeError("name exactly one intent: `edit`, `replace`, "
                          "`withdraw` or `add`" + (f" (got {', '.join(named)})" if named else ""))
    return named[0]


def _withdraw_spec(value) -> tuple[str, bool]:
    if isinstance(value, str) and value:
        return value, False
    if isinstance(value, dict) and isinstance(value.get("component"), str):
        return value["component"], value.get("cascade") is True
    raise ChangeError("`withdraw` is a component name, or "
                      "{component, cascade?: true}")


def cascade_of(ir: dict | None, component: str) -> dict | None:
    """What withdrawing `component` tears down, from the running IR."""
    if not ir:
        return None
    result = _query.withdrawal(ir, component)
    return result if result.get("ok", True) is not False else None


def _cascade_names(plan: dict | None) -> list[str]:
    return [entry["component"] for entry in (plan or {}).get("cascade") or []]


def cascade_refusal(arguments: dict, plan: dict | None) -> dict | None:
    """A withdrawal that would strand dependents, asked for without
    `cascade: true`: refused from the plan, before anything is applied."""
    component, cascade = _withdraw_spec(arguments["withdraw"])
    stranded = (plan or {}).get("cascade") or []
    if cascade or not stranded:
        return None
    lost = "; ".join(f"{entry['component']} {entry['reason']}" for entry in stranded)
    return {"ok": False, "admitted": False, "swapped": False,
            "diagnostics": [{"severity": "error", "code": "REVL",
                             "category": "admission",
                             "message": f"withdrawing {component} would leave "
                                        f"{len(stranded)} component(s) without a "
                                        f"provider: {lost}"}],
            "hint": "pass {withdraw: {component, cascade: true}} to withdraw the "
                    "whole cascade together, or replace the dependents first",
            "note": "verification failed on the plan; nothing was applied"}


def edit_arguments(intent: str, arguments: dict, plan: dict | None) -> dict:
    """The `revl_edit` call an intent amounts to."""
    carried = {k: arguments[k] for k in ("files", "source", "modules", "config",
                                         "record") if k in arguments}
    value = arguments[intent]
    if intent == "edit":
        if not isinstance(value, dict) or not isinstance(value.get("edits"), list):
            raise ChangeError("`edit` is {target?, edits: [...]}, as revl_edit takes")
        return {**carried, **value}
    if intent == "replace":
        if not isinstance(value, dict) or not isinstance(value.get("component"), str) \
                or not isinstance(value.get("source"), str):
            raise ChangeError("`replace` is {component, source}: the component's "
                              "name and its whole new declaration")
        edit = {"symbol": value["component"], "replacement": value["source"]}
        if value.get("target"):
            edit["target"] = value["target"]
        return {**carried, "edits": [edit]}
    if intent == "add":
        if not isinstance(value, dict) or not isinstance(value.get("source"), str):
            raise ChangeError("`add` is {source, target?}: the new declarations, "
                              "and which buffer to append them to")
        edit = {"append": value["source"]}
        if value.get("target"):
            edit["target"] = value["target"]
        return {**carried, "edits": [edit]}
    component, cascade = _withdraw_spec(value)
    names = [component] + (_cascade_names(plan) if cascade else [])
    return {**carried, "edits": [{"symbol": name, "remove": True} for name in names],
            "replacing": names}


def gauntlet_verifier(session):
    """A pre-commit check that grades the exact candidate in the gauntlet's
    isolated scratch session. Refuses unless it is admissible and its
    lifecycle battery ran clean."""
    from . import gauntlet as _gauntlet  # noqa: PLC0415 — cycle

    def verify(candidate: dict):
        dossier = _gauntlet.run(session, candidate)
        lifecycle = ((dossier.get("tested") or {}).get("lifecycle") or {})
        failed = (lifecycle.get("counts") or {}).get("failed", 0)
        if dossier.get("verdict") == "admissible" and failed == 0 \
                and lifecycle.get("status") != "error":
            verify.dossier = dossier
            return None
        return {"ok": False, "admitted": False, "swapped": False,
                "gauntlet": dossier,
                "diagnostics": [{"severity": "error", "code": "REVL",
                                 "category": "gauntlet",
                                 "message": "the gauntlet did not pass the "
                                            "candidate: verdict "
                                            f"{dossier.get('verdict')!r}, "
                                            f"{failed} lifecycle check(s) failed"}],
                "note": "verification failed; nothing was committed"}

    verify.dossier = None
    return verify


def components_touched(result: dict, plan: dict | None, withdrawn: list[str]) -> list:
    """Every component the change touched, with how: from the symbols the
    edit reports, plus the cascade a withdrawal tore down."""
    seen: dict[str, str] = {}
    for entry in result.get("touched") or []:
        if entry.get("kind") == "component":
            seen[entry["symbol"]] = entry["change"]
        elif entry.get("parentKind") == "component":
            # a member changed (issue #1733): its component changed
            seen.setdefault(entry["parent"], "changed")
    for name in withdrawn:
        seen.setdefault(name, "removed")
    out = [{"component": name, "change": change} for name, change in seen.items()]
    affected = [name for name in _cascade_names(plan) if name not in seen]
    out += [{"component": name, "change": "would lose a provider"}
            for name in affected]
    return out


def shape(intent: str, result: dict, plan: dict | None, withdrawn: list[str],
          verifier) -> dict:
    """`revl_change`'s answer: the outcome, the plan, what was verified, and
    every component touched."""
    committed = bool(result.get("swapped") or result.get("booted"))
    verified = {"admission": "passed" if committed or result.get("admitted")
                else "refused" if result.get("ok") is False else "not reached"}
    guarantees = _guarantees(verified["admission"], result)
    if guarantees is not None:
        verified["guarantees"] = guarantees
    if verifier is not None:
        verified["gauntlet"] = ("passed" if verifier.dossier is not None
                                else "failed" if result.get("gauntlet")
                                else "not reached")
    out = {**result, "intent": intent, "committed": committed,
           "verified": verified,
           "components": components_touched(result, plan, withdrawn if committed
                                             else [])}
    if plan is not None:
        out["plan"] = {"cascade": plan.get("cascade") or [],
                       "withdrawalOrder": plan.get("withdrawalOrder") or [],
                       "orphanedKeys": plan.get("orphanedKeys") or []}
    if result.get("speculative") and result.get("ok") is not False:
        out["note"] = ("proposed and verified; the running composition is "
                       "unchanged. Commit with revl_change {commit: true}, or "
                       "drop it with {discard: true}") if not result.get("holes") \
            else result.get("note")
    elif not committed:
        out["note"] = (result.get("note") or "the change was not committed") \
            + "; the running composition is unchanged"
    return out


def component_source(vs: dict, spec: dict) -> str:
    """The component declaration an `{add: {component, ...}}` intent writes,
    read against the working set `vs`. Raises `ChangeError` naming what is
    missing or unknown; nothing is guessed."""
    from . import symbols  # noqa: PLC0415

    name = spec.get("component")
    if not isinstance(name, str) or not name:
        raise ChangeError("`add.component` must be the component's NAME (a "
                          "non-empty string); `provide` and `methods` are its "
                          "siblings inside `add`, not keys of `component`")
    methods = spec.get("methods")
    if not isinstance(methods, dict):
        got = "nothing" if methods is None else f"a {type(methods).__name__}"
        raise ChangeError("`add.methods` must be an object mapping operation "
                          "names to bodies, e.g. "
                          "{\"get\": \"fn get(k: Str) -> Opt[Str] = ...\"}; "
                          f"got {got}")
    services, keys = _composition_vocabulary(vs)
    key, service = _provided(spec.get("provide"), keys)
    decl = services.get(service)
    if decl is None:
        raise ChangeError(f"no service `{service}` is declared, so `{key}` has no "
                          "operations to write")
    missing = [op for op in decl.methods if op not in methods]
    unknown = [op for op in methods if op not in decl.methods]
    if missing or unknown:
        raise ChangeError(
            f"`{service}` declares {', '.join(decl.methods)}; "
            + "; ".join(part for part in (
                f"no body for {', '.join(missing)}" if missing else "",
                f"no operation {', '.join(unknown)}" if unknown else "") if part))
    frames, receivers = [], set()
    for op, body in methods.items():
        params = [p for p, _type in decl.methods[op].params]
        frames.append(symbols._framed(f"fn {op}({', '.join(params)})",
                                      symbols.canonical(str(body)).rstrip("\n")))
        receivers |= _receivers(str(body), set(params))
    if "config" in receivers and not spec.get("config"):
        raise ChangeError(f"a body of `{name}` reads `config`, and config is not "
                          "inferred: give `config` (its fields, e.g. "
                          "\"{ max_steps: Int = 8 }\")")
    receivers.discard("config")
    receivers.discard(key)
    unresolved = sorted(r for r in receivers if r not in keys)
    if unresolved:
        raise ChangeError(
            f"{', '.join(f'`{r}`' for r in unresolved)} is not a key of this "
            f"composition, so `{name}` cannot require it; provide it first, or "
            f"name the requirement in the body's source with {{add: {{source}}}}")
    requires = ", ".join(f"{r}: {keys[r]}" for r in sorted(receivers))
    header = f"component {name}" + (f" requires {requires}" if requires else "") \
        + f" provides {key}: {service} {{"
    body = []
    if spec.get("config"):
        body.append(f"  config {str(spec['config']).strip()}")
    body.append(f"  provide {key} {{")
    for frame in frames:
        body += ["    " + line for line in frame.rstrip("\n").split("\n")]
    body.append("  }")
    return "\n".join([header, *body, "}"]) + "\n"


def _composition_vocabulary(vs: dict) -> tuple[dict, dict]:
    """Every service declared in the working set, and every key with the
    service it is provided or required as."""
    from ..parser import Parser  # noqa: PLC0415
    from . import symbols  # noqa: PLC0415

    services: dict = {}
    keys: dict[str, set] = {}
    for (_kind, buffer), text in symbols.buffers(vs):
        try:
            program = Parser(text, buffer).parse()
        except RevlError:
            continue
        services.update({svc.name: svc for svc in program.services})
        for comp in program.components:
            for key, service, _line in [*comp.provides, *getattr(comp, "requires", [])]:
                keys.setdefault(key, set()).add(service)
    ambiguous = sorted(k for k, svcs in keys.items() if len(svcs) > 1)
    resolved = {k: next(iter(svcs)) for k, svcs in keys.items() if len(svcs) == 1}
    for key in ambiguous:
        resolved.pop(key, None)
    return services, resolved


_PROVIDE_FORMS = ("`provide` is a bare key (\"kv\") or \"key: Service\" "
                  "(\"kv: Kv\") or {key, service}, e.g. "
                  "{\"key\": \"kv\", \"service\": \"Kv\"}")


# revl identifiers are ASCII (lexer.py), so `\w` (Unicode) would admit too much.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _provided(provide, keys: dict) -> tuple[str, str]:
    if isinstance(provide, dict) and isinstance(provide.get("key"), str):
        key = provide["key"]
        service = provide.get("service") or keys.get(key)
    elif isinstance(provide, str) and ":" in provide:
        key, _, service = (part.strip() for part in provide.partition(":"))
        if not (_IDENT.match(key) and _IDENT.match(service)):
            raise ChangeError(f"{_PROVIDE_FORMS}; got {provide!r}, which is "
                              "not `key: Service` with a name on each side")
    elif isinstance(provide, str) and provide:
        key, service = provide, keys.get(provide)
    else:
        raise ChangeError(f"{_PROVIDE_FORMS}; got {provide!r}")
    if not service:
        raise ChangeError(f"the composition does not know `{key}` yet, so its "
                          "service cannot be inferred: give \"key: Service\" "
                          "or {key, service}, e.g. \"kv: Kv\"")
    return key, service


def _receivers(body: str, params: set) -> set:
    """The lowercase names a body calls a method on (`store.get()`), minus its
    own parameters and `let`/`var` bindings. A type namespace (`Map.new()`)
    starts uppercase and is not a receiver."""
    import re  # noqa: PLC0415

    bound = set(params) | set(re.findall(r"\b(?:let|var)\s+([a-z_]\w*)", body))
    names = set(re.findall(r"(?<![\w.])([a-z_]\w*)\s*\.\s*[a-z_]\w*\s*\(", body))
    if re.search(r"(?<![\w.])config\s*\.", body):
        names.add("config")
    return {n for n in names if n not in bound}


def _guarantees(admission: str, result: dict) -> dict | None:
    """The G1-G9 self-check of the verification compile, when it ran: every
    guarantee passes once admitted, and a refusal names the one that failed
    (with its fix). None when verification was not reached."""
    if admission == "passed":
        return _authoring_loop.self_check(None, result.get("holes"))
    diagnostics = result.get("diagnostics") or []
    if admission == "refused" and any(map(_from_a_compile, diagnostics)):
        return _authoring_loop.self_check(diagnostics)
    return None


#: Refusals made before any compile judged the candidate: a malformed or
#: impossible edit (`session`), the plan-level cascade refusal (`admission`
#: under the generic code), an internal fault.
_NOT_A_COMPILE = ("session", "admission", "internal")


def _from_a_compile(diagnostic: dict) -> bool:
    code = str(diagnostic.get("code") or "")
    return (code.startswith("G") and code[1:].isdigit()) \
        or diagnostic.get("category") not in _NOT_A_COMPILE


def withdrawn_names(edit_args: dict) -> list[str]:
    return list(edit_args.get("replacing") or [])


def running_ir(session) -> dict | None:
    """The IR a withdrawal is planned against: what runs, or a held draft."""
    if session.loaded:
        return session.ir
    from . import draft as _draft  # noqa: PLC0415

    held = _draft.pending(session)
    if held is None:
        return None
    try:
        return _edit.compile_virtual(held["vs"])
    except RevlError:
        return None


__all__ = ["INTENTS", "ChangeError", "intent_of", "cascade_of", "edit_arguments",
           "gauntlet_verifier", "shape", "withdrawn_names", "running_ir"]
