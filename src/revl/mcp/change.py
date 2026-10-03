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
"""

from __future__ import annotations

from .. import query as _query
from ..errors import RevlError
from . import edit as _edit

INTENTS = ("edit", "replace", "withdraw")


class ChangeError(ValueError):
    """The intent itself is malformed. Nothing was loaded or changed."""


def intent_of(arguments: dict) -> str:
    named = [name for name in INTENTS if arguments.get(name) is not None]
    if len(named) != 1:
        raise ChangeError("name exactly one intent: `edit`, `replace` or "
                          "`withdraw`" + (f" (got {', '.join(named)})" if named else ""))
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
