"""Hole-directed generation: enrich an open hole into a *fill spec*.

An open typed hole (docs/holes.md) is an obligation — it tells an agent *that*
it owes an expression of some type at some position. That is 60% of what an
agent needs. The obligation says what the hole must eventually *be*; it does
not say what the agent has to work *with*. The missing 40% is everything the
checker already knew standing at that position:

* the **expected** type the fill must meet;
* the **capability** upper bound — may an expression here cross the emission
  boundary, and within which named bound (the G4 question, docs/capabilities.md);
* the **bindings** in scope, with their types; and
* the **reachable services** — the injected dependencies whose methods a fill
  may call, with full signatures.

None of this is new inference. Every field is read straight off the
fully-lowered IR document (services table, component `requires`/`config`, the
enclosing provide-method's declared emission, and the `let`/param bindings that
precede the hole). This module is read-only on the checker: it *serializes*
what a check already established, so an agent can scaffold a component with
holes and fill them one at a time, each fill constrained by a spec that makes
most wrong answers unrepresentable.

The shape added to every obligation in `revl_check` is::

    "fillSpec": {
      "version": 2,
      "expected": "Str",
      "grammarCategory": "expression",
      "construct": "provide-method",
      "idiom": {"name": "provide-method", "rules": ["G4: ..."],
                "fill": "db.get(key)", "example": "..."},
      "capability": {"permitsCrossing": false, "mayEmit": false, "bound": [],
                     "reason": "..."},
      "crossing": {"permitted": false, "required": false, "form": null,
                   "calls": [], "rule": "..."},
      "bindings": [{"name": "key", "type": "Str"}, ...],
      "reachableServices": [
        {"service": "Db", "method": "q", "signature": "q(sql: Str) -> Str",
         "instance": "db", "emission": false, "callableHere": true}
      ],
      "externs": {"mayDeclare": true, "placement": "...", "template": "...",
                  "declared": [{"name": "sha", "class": "pure",
                                "signature": "sha(text: Str) -> Str",
                                "write": "sha(<text: Str>)",
                                "callableHere": true}],
                  "reason": "..."}
    }

VERSION 2 (this shape). Version 1 had no `version`, no `crossing`, no
`permitsCrossing` and no `callableHere`; every version-1 field is still here
with its version-1 meaning, so a version-1 reader keeps working. What version 2
fixes is a word: `mayEmit` reads as a property of the HOLE ("this position may
emit") and was read as an obligation ("this hole is an emission position, so
the fill must emit"). It is a permission only: a declared `emission` operation
bounds what its provider may do, and a provider may always be purer than
declared (G4). Version 2 states the permission under a name that cannot be
read as an obligation (`permitsCrossing`), says outright that a crossing is
never `required`, and gives the call-site FORM a crossing is written in, with
the exact crossings available at this position.

`externs` (additive, so still version 2: the version moves only when a
field's MEANING changes) answers where an extern goes, which an agent that
knows extern SYNTAX could not tell from the spec: an extern is a top-level
declaration, never written inside a component or a method; which externs the
program already declares and whether a fill here may call each; and whether
this author may declare one at all. An untrusted author (the MCP server's
default) may neither declare nor reach an extern (`AdmissionProfile.
untrusted_author`, G8), and the spec says so instead of offering a call the
compile would refuse.

`fillable` (additive) says whether THIS author can fill the hole at all. It
lists the `producers` of the expected type at this position (a literal, a
binding, a callable service operation, extern, crossing or function), and
decides one case outright: a type no declaration and no literal can build
(a nominal handle such as an extern's `LogHandle`) with no producer in reach
needs new host code, so it is `needsHostCode`, and for an untrusted author,
who may not declare or reach an extern (G8), it is not fillable at all
(`byThisAuthor: false`). Everything else is reported, never guessed: a `Str`
that "must be a hash" is fillable by a literal as far as types can say, so
the spec lists its producers and stays honest about what it cannot decide.

`split` (additive, issue #1660) decomposes a hole whose obligation is more
than one sentence. When a hole is a provide-method's WHOLE body and its
`crossing.calls` span two or more capability tokens, the hole carries
`split`: one `let <token>_step = hole[T] "..."` statement per token, each
naming only that token's crossings, then a result hole. `revl scaffold`
writes that decomposition directly. Inside such a body, a hole bound by
`let <token>_step = ...` lists only `<token>`'s calls, so every part states
its obligation in one sentence. A hole is never split by operation (two
operations through one boundary are one sentence) and never in a pure
position (it has nothing to cross).

`grammarCategory` (issue #1664, additive too, so still version 2) is the
syntactic category a fill is a document of, a key of
`revl.source_grammar.CATEGORIES`, so a client can pass it to `revl grammar
--format F --category C` (or the MCP `revl_grammar` tool) and constrain its
decoder to the hole's slot. It is read off the grammar derived from the parser
(`source_grammar.hole_category`), not a table: the narrowest category every
parse of the `hole` keyword passes through.

`construct` and `idiom` (issue #1701, additive) name where the hole stands
(`CONSTRUCTS`: a plain or an emission provide method, a component body
statement, the acquisition or the inverse of an `effect`, a function, a test)
and serve that construct's minimal admitted example with the one or two rules
that make it correct (`revl.idioms`), so an agent sees the smallest correct
instance of exactly the construct it is filling.
"""

from __future__ import annotations

import re

from .. import idioms, source_grammar
from ..diagnostics import GUARANTEES
from ..holes import EMITTABLE_SECTIONS
from ..lower import _GENERATABLE_PRIMITIVES, _HOST_WRITE_INVERSE
from ..resources import PRIMITIVE_TYPE_NAMES, _STRUCTURAL_HEADS
from ..typecheck import _HOST_ARG_SIG, _HOST_FAMILIES, _HOST_RESULT_SIG

#: The fillSpec shape this module writes. Version 1 had no `version` key.
FILL_SPEC_VERSION = 2

#: How a crossing is written at a hole, in any position that permits one.
CROSSING_FORM = ("emit <key>.<operation>(<args>) for an injected service, or "
                 "emit <extern>(<args>) for a declared emission extern; write "
                 "`let r = emit ...` to keep its value")

#: What `permitted`/`required` mean, said once, in the spec itself.
CROSSING_RULE = ("A crossing is a PERMISSION, never an obligation: a fill may "
                 "be a pure expression in every position. Where `permitted` "
                 "is true the fill may contain marked crossings (`emit`), one "
                 "marker per crossing, each through a call listed in `calls`; "
                 "where it is false a crossing is refused (G4). `required` is "
                 "always false.")


def _lit_type(value) -> str | None:
    """The type of an IR `lit` node — a declared primitive, not inference."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "Str"
    return None


def _render_signature(method: str, decl: dict) -> str:
    """A service/fn method's declared signature as one readable line."""
    params = ", ".join(
        f"{p['name']}: {p['type']}" for p in decl.get("params", []))
    returns = decl.get("returns")
    rendered = f"{method}({params})"
    if returns is not None:
        rendered += f" -> {returns}"
    return rendered


def _service_return(services: dict, service: str, method: str) -> str | None:
    decl = services.get(service, {}).get("methods", {}).get(method)
    return decl.get("returns") if decl else None


def _expr_type(node, services: dict, functions: dict,
               bindings: dict) -> str | None:
    """The type of an IR expression, resolved from declared signatures only.

    Every branch reads a type that a declaration already fixed — a hole's own
    annotation, a literal's primitive, a name already bound, or the declared
    return of a function or service method. Nothing is inferred; an expression
    whose type no declaration pins down is reported as unknown (`None`), never
    guessed.
    """
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind == "hole":
        return node.get("type")
    if kind == "lit":
        return _lit_type(node.get("value"))
    if kind == "name":
        return bindings.get(node.get("id"))
    if kind == "fn":
        decl = functions.get(node.get("name"))
        return decl.get("returns") if decl else None
    if kind == "call":
        target = node.get("target") or {}
        service = None
        if target.get("kind") in ("req", "provided", "service"):
            service = bindings.get("@service:" + str(target.get("name")))
        if service:
            return _service_return(services, service, node.get("method"))
        return None
    return None


def _crossing_tokens(instance: str, decl: dict, carry: dict) -> set:
    """The capability tokens a crossing through `instance.<op>` contributes,
    as `emission_analysis._method_emissions` computes them: the require KEY,
    or, for a key declared `carrying(...)`, the carried tokens, plus the
    unnameable `*` when the carry cannot bound the operation's own reach."""
    carried = (carry or {}).get(instance)
    if not carried:
        return {instance}
    tokens = set(carried)
    caps = decl.get("capabilities")
    if caps is None or len(set(caps)) > len(tokens):
        tokens.add("*")
    return tokens


def _within(tokens: set, bound) -> bool:
    """Whether a crossing's tokens sit inside a provide-method's bound: `None`
    is bare `emission` (any boundary), a list is a scoped `emission[...]`."""
    if bound is None:
        return True
    return tokens <= set(bound)


def _crossing_calls(requires: dict, services: dict, externs: list,
                    capability: dict, carry: dict,
                    untrusted: bool = False) -> list[dict]:
    """Every crossing a fill at this position may write, already in its
    call-site form: each injected service's emission operations and each
    declared emission extern whose tokens sit inside the bound."""
    if not capability.get("permitsCrossing"):
        return []
    bound = capability.get("bound")
    calls: list[dict] = []
    for instance, service in sorted((requires or {}).items()):
        methods = services.get(service, {}).get("methods", {})
        for method, decl in sorted(methods.items()):
            if not decl.get("emission"):
                continue
            tokens = _crossing_tokens(instance, decl, carry)
            if not _within(tokens, bound):
                continue
            args = ", ".join(f"<{p['name']}: {p['type']}>"
                             for p in decl.get("params", []))
            calls.append({"write": f"emit {instance}.{method}({args})",
                          "returns": decl.get("returns"),
                          "carrier": "service",
                          "capabilities": sorted(tokens)})
    for ext in externs or []:
        if ext.get("class") != "emission":
            continue
        if untrusted:
            # an untrusted author may not reach an extern (G8)
            continue
        tokens = set(ext.get("capabilities") or [ext["name"]])
        if not _within(tokens, bound):
            continue
        args = ", ".join(f"<{p['name']}: {p['type']}>"
                         for p in ext.get("params", []))
        calls.append({"write": f"emit {ext['name']}({args})",
                      "returns": ext.get("returns"),
                      "carrier": "extern",
                      "capabilities": sorted(tokens)})
    return calls


#: Where an `extern` is declared, said once, in the spec itself.
EXTERN_PLACEMENT = (
    "An extern is a TOP-LEVEL declaration: write it at the top level of the "
    "file, beside `service` and `component` declarations, never inside a "
    "component body or a provide method. Its class says what its host body "
    "does: `pure` computes and touches nothing (callable anywhere), "
    "`acquire` takes a resource and declares its inverse (`undo "
    "<inverse>(result)`, called as `effect <name>(...)` in a component "
    "body), `emission` crosses irreversibly (called as `emit <name>(...)` "
    "where a crossing is permitted).")

#: The declaration's shape, for the commonest case a fill needs (a
#: computation the language does not have).
EXTERN_TEMPLATE = "extern pure fn <name>(<param>: <Type>) -> <Type> = @py { ... }"

#: Why an untrusted author is offered no extern.
UNTRUSTED_EXTERNS = (
    "this author compiles under the untrusted-author profile, which refuses "
    "declaring an extern and refuses reaching one (G8); a fill here may use "
    "only the bindings and the services listed, so a completion that needs "
    "new host code cannot be written by this author")


def _extern_write(ext: dict, position: str = "pure") -> str:
    """How a call of `ext` is written at a hole in `position`. In the
    acquisition slot of an `effect` the hole already follows `effect`, so the
    fill is the bare call: `effect effect open()` does not parse (#1846)."""
    args = ", ".join(f"<{p['name']}: {p['type']}>"
                     for p in ext.get("params", []))
    call = f"{ext['name']}({args})"
    cls = ext.get("class")
    if cls == "emission":
        return f"emit {call}"
    if cls in ("acquire", "witnessed") and position != "effect-acquire":
        return f"effect {call}"
    return call


def _extern_signature(ext: dict) -> str:
    """An extern's signature, rendered the same way as a method's."""
    return _render_signature(ext["name"], ext)


def _externs(externs: list, calls: list[dict], position: str,
             untrusted: bool) -> dict:
    """The `externs` block: where an extern is declared, which ones exist,
    whether a fill at this POSITION may call each, and whether this author may
    declare one."""
    crossing = {c["write"].split("(", 1)[0] for c in calls}
    declared = []
    for ext in externs or []:
        cls = ext.get("class")
        write = _extern_write(ext, position)
        if untrusted:
            here = False
        elif cls == "pure":
            here = True
        elif cls == "emission":
            here = write.split("(", 1)[0] in crossing
        else:
            # acquire/witnessed: only the acquisition slot of an `effect`
            here = position == "effect-acquire"
        declared.append({"name": ext["name"], "class": cls,
                         "signature": _extern_signature(ext),
                         "write": write, "callableHere": here})
    return {
        "mayDeclare": not untrusted,
        "placement": EXTERN_PLACEMENT,
        "template": EXTERN_TEMPLATE,
        "declared": declared,
        "reason": UNTRUSTED_EXTERNS if untrusted else (
            "a trusted author may declare an extern; the declaration goes at "
            "the top level of the file, not at this hole"),
    }


def _primitive_literal(name: str) -> str:
    if name == "Str":
        return '"..."'
    if name == "Bool":
        return "false"
    return "0.0" if name in ("Float", "F64") else "0"


#: A literal of each primitive, for `fillable.producers`: the primitives a
#: value can be generated for, so the vocabulary is the compiler's own
#: (`lower._GENERATABLE_PRIMITIVES`). `Unit` is not one: revl has no unit
#: expression (`()` does not parse), so a `Unit` hole is filled by a call that
#: returns nothing (#1846).
_LITERALS = {name: _primitive_literal(name)
             for name in sorted(_GENERATABLE_PRIMITIVES)}


def _type_head(t: str) -> str:
    return t.split("[", 1)[0].strip()


def _declared_return(returns: str | None) -> str:
    """A declaration's return type: one declared without a return type
    returns `Unit`, the same as one that writes `-> Unit` (#1857)."""
    return returns or "Unit"


def _returns_of(signature: str | None) -> str | None:
    """A rendered signature's return type; one with no `->` returns `Unit`."""
    if not signature:
        return None
    if "->" not in signature:
        return "Unit"
    return signature.rsplit("->", 1)[1].strip()


def _literal(expected: str, types: dict) -> str | None:
    """A literal that builds `expected`, or None when none does. A declared
    record or variant is built by its literal or a constructor, a builtin
    carrier by its empty form."""
    head = _type_head(expected)
    if expected in _LITERALS:
        return _LITERALS[expected]
    if head == "Opt":
        return "None"
    if head == "List":
        return "[]"
    if head in _STRUCTURAL_HEADS:
        return f"a `{head}` value"
    decl = types.get(head)
    if decl is not None:
        if decl.get("kind") == "record":
            fields = ", ".join(f"{k}: ..." for k in decl.get("fields") or {})
            return "{ " + fields + " }"
        cases = [c["name"] for c in decl.get("cases") or []]
        return " | ".join(cases) if cases else f"a `{head}` value"
    return None


def _is_handle(expected: str, types: dict) -> bool:
    """A bare nominal name no declaration builds: an extern's handle type
    (item 308 R0). Only this case is decided; any other type with no
    producer stays undecided rather than guessed."""
    if "[" in expected or expected in PRIMITIVE_TYPE_NAMES:
        return False
    return expected not in types and expected.isidentifier()


def _host_family(node) -> str | None:
    """The host family an `effect` acquisition acquires, when it is a builtin
    call: `Map` for `Map.new()`. None for anything else — a hole, an extern's
    declared return, a `spawn`, a `subscribe` — because those name no family,
    so no verb surface is known for the handle they produce."""
    if not isinstance(node, dict) or node.get("kind") != "host":
        return None
    return str(node.get("fn") or "").partition(".")[0] or None


def _resource_family(acquire, acquired_type) -> str | None:
    """The operation family of the resource an `effect` statement acquires.

    A builtin acquisition names it (`Map.new()` acquires a `Map`), and a typed
    one names it too (`effect hole[Map[Str, Int]] ...`) — the shape the
    scaffold writes before the author picks an acquisition, which the checker
    reads the family off as well (`lower.py`, issue #1968). A type naming no
    host family yields None, so `resource.<verb>` is offered only for verbs the
    checker actually knows."""
    if isinstance(acquired_type, str):
        head = acquired_type.strip().partition("[")[0].strip()
        if head in _HOST_FAMILIES:
            return head
    return _host_family(acquire)


def _type_args(declared: str | None) -> list[str]:
    """The type arguments a declared resource type names, in order: `Map[Int,
    Str]` -> `["Int", "Str"]`, `Map` -> `[]`.

    Split at bracket depth, so a nested argument stays whole
    (`Map[Str, List[Int]]` -> `["Str", "List[Int]"]`)."""
    if not isinstance(declared, str) or "[" not in declared:
        return []
    head, _, rest = declared.partition("[")
    if not head.strip() or not rest.rstrip().endswith("]"):
        return []
    body = rest.rstrip()[:-1]
    out, depth, current = [], 0, ""
    for ch in body:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current.strip())
    return out


def _param_name(type_name: str) -> str:
    """A placeholder NAME for a parameter of `type_name`: the lowercased type
    when that is an identifier (`Str` -> `str`), else a flattened one
    (`List[Int]` -> `list_int`). A type is not always a word — the placeholder
    is `<name: Type>`, so the name has to stay writable code."""
    flat = re.sub(r"\W+", "_", type_name.lower()).strip("_")
    return flat if flat.isidentifier() else "arg"


def _resource_writes(name: str, family: str | None,
                     declared: str | None = None) -> list[str]:
    """The operations of the component's OWN resource `name` that write it and
    return nothing — the calls a `Unit` fill can be.

    Read off the checker's own tables rather than guessed: `_HOST_WRITE_INVERSE`
    says which verbs WRITE the family (a read like `Map.get` or a release like
    `Map.drop` is not a write), and `_HOST_RESULT_SIG` says which of those
    return a value (`Map.insert_if_absent` is a `Bool`, so it is not a `Unit`).
    An unknown family yields nothing: this offers what the checker knows, never
    what a name suggests.

    Issue #2034: the checker's `_HOST_ARG_SIG` is the family's *default*
    instantiation (`Map` is Str-keyed there), so on its own it offers
    `Str`/`Str` whatever the author declared. The declared type's arguments ARE
    the family's type parameters, in order — `Map[K, V]`'s `insert` takes
    `(k: K, v: V)` and `remove` takes `(k: K)` — so verb parameter *i* takes
    the declared argument in its position, and the table's own type when the
    declaration supplies none. A resource declared without type arguments
    (`Map`) is therefore offered exactly as before."""
    if not family:
        return []
    args = _type_args(declared)
    out = []
    for key in sorted(_HOST_WRITE_INVERSE):
        fam, _, verb = key.partition(".")
        if fam != family or key in _HOST_RESULT_SIG:
            continue
        params = [args[i] if i < len(args) else p
                  for i, p in enumerate(_HOST_ARG_SIG.get(key) or [])]
        rendered = ", ".join(f"<{_param_name(p)}_{i}: {p}>"
                             for i, p in enumerate(params))
        out.append(f"{name}.{verb}({rendered})")
    return out


def _fillable(expected: str | None, visible: list[dict],
              reachable: list[dict], calls: list[dict], externs: dict,
              functions: dict, types: dict, untrusted: bool,
              resources: tuple = ()) -> dict:
    """Whether this author can fill the hole, and from what."""
    if not expected:
        return {"byThisAuthor": True, "decided": False,
                "needsHostCode": False, "producers": [],
                "reason": "the hole's type is not known here"}
    producers: list[dict] = []
    literal = _literal(expected, types)
    if literal is not None:
        producers.append({"kind": "literal", "write": literal})
    for b in visible:
        if b.get("type") == expected:
            producers.append({"kind": "binding", "write": b["name"]})
    for e in reachable:
        if e.get("callableHere") and not e.get("emission") \
                and _returns_of(e.get("signature")) == expected:
            producers.append({"kind": "service",
                              "write": f"{e['instance']}.{e['signature']}"})
    for c in calls:
        if _declared_return(c.get("returns")) == expected:
            producers.append({"kind": "crossing", "write": c["write"]})
    for ext in externs.get("declared") or []:
        if ext.get("callableHere") and _returns_of(ext["signature"]) == expected:
            producers.append({"kind": "extern", "write": ext["write"]})
    for name, fn in sorted(functions.items()):
        if _declared_return(fn.get("returns")) == expected:
            params = ", ".join(f"<{p['name']}: {p['type']}>"
                               for p in fn.get("params", []))
            producers.append({"kind": "function", "write": f"{name}({params})"})
    # issue #1948: a `Unit` hole can be the write a method makes on the
    # resource its own component acquired. Only `Unit`: these calls return
    # nothing, so they are no fill for a hole that expects a value.
    writes = 0
    if expected == "Unit":
        for res in resources:
            for write in _resource_writes(res.get("name"), res.get("family"),
                                          res.get("type")):
                producers.append({"kind": "resource", "write": write})
                writes += 1
    if producers:
        reason = f"a `{expected}` can be built from the producers listed"
        if writes:
            reason += (": the component acquired this resource itself, so a "
                       "write on it is permitted in its own method (the other "
                       "producers may not be)")
        return {"byThisAuthor": True, "decided": True, "needsHostCode": False,
                "producers": producers,
                "reason": reason}
    if expected == "Unit" and resources:
        names = ", ".join(f"`{r.get('name')}`" for r in resources)
        return {"byThisAuthor": True, "decided": False,
                "needsHostCode": False, "producers": [],
                "reason": f"a `Unit` fill is a write, and the resource this "
                          f"component acquired ({names}) offers no operation "
                          f"here that writes it and returns nothing — a fill "
                          f"must not emit, and a resource the component did "
                          f"not acquire is out of scope"}
    if not _is_handle(expected, types):
        return {"byThisAuthor": True, "decided": False,
                "needsHostCode": False, "producers": [],
                "reason": f"nothing in reach produces a `{expected}`, and "
                          f"its type does not decide whether a fill exists"}
    if untrusted:
        return {"byThisAuthor": False, "decided": True, "needsHostCode": True,
                "producers": [],
                "reason": f"`{expected}` is a handle no declaration builds, "
                          f"and nothing in reach returns one: a fill needs "
                          f"new host code, which this author may neither "
                          f"declare nor reach (untrusted-author profile, G8). "
                          f"Ask the operator to grant a service that returns "
                          f"a `{expected}`, or to trust this author with host "
                          f"code"}
    return {"byThisAuthor": True, "decided": True, "needsHostCode": True,
            "producers": [],
            "reason": f"`{expected}` is a handle no declaration builds, and "
                      f"nothing in reach returns one: a fill needs new host "
                      f"code, an extern returning `{expected}` declared at "
                      f"the top level of the file (see `externs`)"}


def step_name(token: str) -> str:
    """The binding a split statement hole for capability `token` is written
    under: `db_step`, `net_edge_step` for a dotted `net.edge`."""
    if token == "*":
        return "unbounded_step"
    return re.sub(r"[^A-Za-z0-9_]", "_", token) + "_step"


def _tokens_of(calls: list[dict]) -> list[str]:
    return sorted({t for c in calls for t in c.get("capabilities") or ()})


def _split(expected: str | None, method: str | None,
           calls: list[dict]) -> list[dict] | None:
    """The decomposition of a whole-body hole whose crossings span two or
    more capability tokens, or None when there is nothing to split. One
    statement hole per token, then the result."""
    tokens = _tokens_of(calls)
    if len(tokens) < 2:
        return None
    parts = []
    for token in tokens:
        own = [c for c in calls if token in (c.get("capabilities") or ())]
        typ = (own[0].get("returns") if len(own) == 1 and own[0].get("returns")
               else expected) or expected
        forms = ", ".join(c["write"] for c in own)
        parts.append({
            "token": token,
            "calls": own,
            "write": (f'let {step_name(token)} = hole[{typ}] "the crossing '
                      f'through {token}, if any ({forms}); a pure value '
                      f'otherwise"'),
        })
    parts.append({
        "token": None,
        "calls": [],
        "write": (f'return hole[{expected}] "the result of '
                  f'{method or "the method"}, from the steps above"'),
    })
    return parts


def _crossing(capability: dict, calls: list[dict]) -> dict:
    """The `crossing` block: may a fill here cross, must it (never), how a
    crossing is written, and which crossings exist at this position."""
    permitted = bool(capability.get("permitsCrossing"))
    return {
        "permitted": permitted,
        "required": False,
        "form": CROSSING_FORM if permitted else None,
        "calls": calls,
        "rule": CROSSING_RULE,
    }


def _callable_here(entries: list[dict], calls: list[dict]) -> list[dict]:
    """`reachableServices` with `callableHere`: a plain operation is callable
    in every position; an emission operation only where it is one of the
    crossings this position permits (and then only as `emit ...`)."""
    allowed = {c["write"].split("(", 1)[0] for c in calls}
    out = []
    for entry in entries:
        here = (not entry.get("emission")
                or f"emit {entry['instance']}.{entry['method']}" in allowed)
        out.append({**entry, "callableHere": here})
    return out


def _reachable_services(requires: dict, services: dict) -> list[dict]:
    """Every method of every injected dependency, with its signature.

    In a provide-method the reachable boundary is the component's `requires`:
    the services it was handed to call. Each is expanded to its full method
    table so a fill knows exactly what it may call and with what.
    """
    out: list[dict] = []
    for instance, service in sorted((requires or {}).items()):
        methods = services.get(service, {}).get("methods", {})
        for method, decl in methods.items():
            out.append({
                "service": service,
                "method": method,
                "signature": _render_signature(method, decl),
                "instance": instance,
                "emission": bool(decl.get("emission")),
            })
    return out


def _capability(emission: bool, capabilities, in_method: bool,
                reason: str) -> dict:
    """The emission upper bound at a hole's position (the G4 question).

    A hole is a pure expression, so it can never itself be `emit hole`
    (docs/holes.md §2). What the fill that *replaces* it may do is the real
    question: an expression at this position may cross the emission boundary
    only inside a provide-method whose service method is declared `emission`,
    and then only within that method's capability bound. `bound` is the list of
    named capabilities of a scoped `emission[db, log]`; `None` is bare
    `emission` — "any boundary"; an empty list with `mayEmit: false` is a
    position where no emission is permitted at all.
    """
    if not (in_method and emission):
        return {"permitsCrossing": False, "mayEmit": False, "bound": [],
                "reason": reason}
    return {
        # `permitsCrossing` is the version-2 name; `mayEmit` is the same bool
        # under its version-1 name, kept so a version-1 reader keeps working
        "permitsCrossing": True,
        "mayEmit": True,
        # bare `emission` carries no capability list -> unbounded ("any").
        "bound": list(capabilities) if capabilities is not None else None,
        "reason": "an emission-declared provide-method"
        + (f" scoped to {', '.join(capabilities)}" if capabilities else ""),
    }


#: Every construct a fillSpec names, one per hole position (issue #1701). Each
#: has an idiom in `revl.idioms` of the same name.
CONSTRUCTS = ("provide-method", "emission-method", "component-setup",
              "effect-acquire", "effect-undo", "function", "test")


def _construct(position: str | None, capability: dict) -> str:
    """The construct a hole stands in: where it is, and for a provide method
    whether its operation may cross the boundary."""
    if position == "method":
        return ("emission-method" if capability.get("permitsCrossing")
                else "provide-method")
    construct = {"setup": "component-setup"}.get(position, position)
    if construct not in CONSTRUCTS:
        raise ValueError(f"fillspec: a hole at position {position!r} names no construct")
    return construct


def _idiom(construct: str) -> dict:
    """The served idiom for `construct`: the rules and the minimal admitted
    example (`revl.idioms`)."""
    entry = idioms.get(construct)
    if entry is None:
        raise ValueError(f"fillspec: no idiom for construct {construct!r}")
    return idioms.served(entry)


def _obligation(hole: dict, fill_spec: dict) -> dict:
    """One obligation, in the same shape `diagnostics.obligations` produces,
    with the `fillSpec` added. Base fields stay byte-identical so an agent that
    only reads the obligation keeps working."""
    return {
        "severity": "obligation",
        "code": "T3",
        "category": "hole",
        "file": hole.get("file"),
        "line": hole.get("line"),
        "expected": hole.get("type"),
        "message": hole.get("message"),
        "guarantee": GUARANTEES["T3"],
        "fillSpec": fill_spec,
    }


def _walk_body(body, services, functions, bindings, capability,
               collected: list) -> None:
    """Walk a statement body in order, threading the scope forward.

    `bindings` maps a name to its resolved type; the `@service:<name>` entries
    record which service instance a name denotes so a call on it resolves. Each
    `let` extends the scope *after* its own value is examined, so a hole sees
    exactly the bindings that precede it — the checker's scope at that point.
    """
    for stmt in body or []:
        step = stmt.get("step")
        if step == "let":
            value_scope = dict(bindings)
            token = (bindings.get("@step_tokens") or {}).get(stmt.get("name"))
            if token is not None:
                value_scope["@only_token"] = token
            _collect_exprs(stmt.get("value"), services, functions,
                           value_scope, capability, collected)
            bindings[stmt["name"]] = _expr_type(
                stmt.get("value"), services, functions, bindings)
        elif step == "return":
            _collect_exprs(stmt.get("expr"), services, functions,
                           dict(bindings), capability, collected)
        else:
            # if/while/assert/emit and any other statement: their sub-exprs are
            # in the same scope, but they do not introduce a forward binding a
            # later hole would see, so scope is not extended here.
            _collect_exprs(stmt, services, functions, dict(bindings),
                           capability, collected)


def _collect_exprs(node, services, functions, bindings, capability,
                   collected: list) -> None:
    """Find every hole reachable inside one expression/sub-tree and record its
    fill spec against the scope in force here."""
    if isinstance(node, dict):
        if node.get("kind") == "hole":
            visible = [
                {"name": name, "type": typ}
                for name, typ in bindings.items()
                if not name.startswith("@")  # `@service:`/`@reachable`/... internals
            ]
            untrusted = bool(bindings.get("@untrusted"))
            externs = bindings.get("@externs") or []
            calls = _crossing_calls(
                bindings.get("@requires") or {}, services, externs,
                capability, bindings.get("@carry") or {}, untrusted)
            only = bindings.get("@only_token")
            if only is not None:
                # a split statement hole: its own token's crossings only
                calls = [c for c in calls
                         if only in (c.get("capabilities") or ())]
            reachable = _callable_here(bindings.get("@reachable", []), calls)
            extern_block = _externs(externs, calls,
                                    bindings.get("@position") or "pure",
                                    untrusted)
            construct = _construct(bindings.get("@position"), capability)
            collected.append((node, {
                "version": FILL_SPEC_VERSION,
                "expected": node.get("type"),
                "grammarCategory": source_grammar.hole_category(),
                "construct": construct,
                "idiom": _idiom(construct),
                "capability": capability,
                "crossing": _crossing(capability, calls),
                "bindings": visible,
                "reachableServices": reachable,
                "externs": extern_block,
                "fillable": _fillable(
                    node.get("type"), visible, reachable, calls,
                    extern_block, functions, bindings.get("@types") or {},
                    untrusted, bindings.get("@resources") or ()),
            }))
            if bindings.get("@whole_body"):
                parts = _split(node.get("type"), bindings.get("@method"),
                               calls)
                if parts is not None:
                    collected[-1][1]["split"] = parts
            return
        # `step` bodies nested in an expression are rare, but a statement dict
        # threaded here (see `_walk_body` else-branch) should still recurse.
        for value in node.values():
            _collect_exprs(value, services, functions, bindings, capability,
                           collected)
    elif isinstance(node, list):
        if node and all(
                isinstance(v, dict) and "step" in v for v in node):
            # a nested statement body (an `if`/`while` branch): thread its own
            # forward `let` scope, starting from the bindings visible here.
            _walk_body(node, services, functions, dict(bindings), capability,
                       collected)
            return
        for value in node:
            _collect_exprs(value, services, functions, bindings, capability,
                           collected)


def _position_context(component, services, externs,
                      untrusted: bool = False, types: dict | None = None) -> dict:
    """The internal (`@`-prefixed) entries every scope inside `component`
    carries: its reachable service table, its `requires` and `carrying(...)`
    maps, and the program's externs, which the crossing calls are read off."""
    return {
        "@reachable": _reachable_services(component.get("requires"), services),
        "@requires": component.get("requires") or {},
        "@carry": component.get("carry") or {},
        "@externs": externs,
        "@untrusted": untrusted,
        "@types": types or {},
    }


def _method_scope(component, method, service_decl, services, functions,
                  externs, untrusted: bool = False, types: dict | None = None,
                  resources: list | None = None):
    """The binding scope a provide-method's body opens with: the component's
    config fields and the method's parameters, each with a declared type."""
    bindings: dict = _position_context(component, services, externs, untrusted,
                                       types)
    bindings["@position"] = "method"
    # issue #1948: the resources this component acquired before this method.
    # Only methods see them — a setup or `undo` position writes nothing — and
    # the binding itself is in scope there, so it is listed among the method's
    # bindings as well as offered as a producer.
    bindings["@resources"] = list(resources or [])
    for res in resources or []:
        if isinstance(res.get("type"), str):
            bindings[res["name"]] = res["type"]
    for field in component.get("config", []) or []:
        bindings[field["name"]] = field.get("type")
    # record each injected dependency so a call on it resolves to its service.
    for instance, service in (component.get("requires") or {}).items():
        bindings["@service:" + instance] = service
    # method params: the IR method carries names only; their types are the
    # service method's declared parameter types, positionally.
    decl_params = service_decl.get("params", []) if service_decl else []
    for i, pname in enumerate(method.get("params", [])):
        bindings[pname] = decl_params[i]["type"] if i < len(decl_params) else None
    return bindings


def unfillable(obligations: list[dict]) -> list[dict]:
    """The obligations THIS author cannot fill, `{line, expected, reason}`
    each. Empty when every hole is fillable or undecided."""
    out = []
    for ob in obligations:
        fill = (ob.get("fillSpec") or {}).get("fillable") or {}
        if fill.get("byThisAuthor") is False:
            out.append({"line": ob.get("line"), "expected": ob.get("expected"),
                        "reason": fill.get("reason")})
    return out


def _mark_split_body(scope: dict, method: dict, capability: dict,
                     services: dict, externs: list, untrusted: bool) -> None:
    """Mark a provide-method's scope for the split rule (issue #1660): its
    whole-body hole, if its body is one, and the `<token>_step` names whose
    holes carry only that token's crossings. Both apply only when the
    method's crossings span two or more capability tokens."""
    calls = _crossing_calls(scope.get("@requires") or {}, services, externs,
                            capability, scope.get("@carry") or {}, untrusted)
    tokens = _tokens_of(calls)
    if len(tokens) < 2:
        return
    scope["@method"] = method.get("name")
    body = method.get("body") or []
    if (len(body) == 1 and body[0].get("step") == "return"
            and (body[0].get("expr") or {}).get("kind") == "hole"):
        scope["@whole_body"] = True
    scope["@step_tokens"] = {step_name(t): t for t in tokens}


def enrich(ir: dict, untrusted: bool = False) -> list[dict]:
    """Every open hole in `ir`, as an obligation carrying its fill spec.

    `untrusted` says the AUTHOR filling the holes compiles under the
    untrusted-author profile (the MCP server's default): the spec then offers
    no extern to declare or call, because the compile would refuse both.

    Sorted by (file, line) to match `holes.collect`, so this is a drop-in
    replacement for `diagnostics.obligations(ir["holes"])` in `revl_check`.
    """
    services = ir.get("services") or {}
    functions = {f["name"]: f for f in (ir.get("functions") or [])}
    externs = ir.get("externs") or []
    types = ir.get("types") or {}
    collected: list = []

    # Components: provide-methods (may be emission positions) and component-level
    # setup `let`/effect (always a pure position).
    for component in ir.get("components") or []:
        setup_scope: dict = _position_context(component, services, externs,
                                              untrusted, types)
        setup_scope["@position"] = "setup"
        for field in component.get("config", []) or []:
            setup_scope[field["name"]] = field.get("type")
        for instance, service in (component.get("requires") or {}).items():
            setup_scope["@service:" + instance] = service
        pure = _capability(False, None, in_method=False,
                           reason="a component setup position — pure, no emission")
        # issue #1948: the resources this component acquired itself, in the
        # order it acquires them. `lower.py`'s `_ownership_walk_method` admits
        # the owner's own handle where it would refuse another's, so a method
        # may write what its own component acquired.
        owned: list[dict] = []
        for stmt in component.get("body") or []:
            if stmt.get("step") == "provide":
                svc_methods = services.get(
                    stmt.get("service"), {}).get("methods", {})
                for method in stmt.get("methods", []):
                    decl = svc_methods.get(method.get("name"), {})
                    capability = _capability(
                        bool(decl.get("emission")), decl.get("capabilities"),
                        in_method=True,
                        reason="a non-emission provide-method — pure"
                        if not decl.get("emission") else "")
                    scope = _method_scope(
                        component, method, decl, services, functions, externs,
                        untrusted, types, owned)
                    _mark_split_body(scope, method, capability, services,
                                     externs, untrusted)
                    _walk_body(method.get("body"), services, functions,
                               scope, capability, collected)
            elif stmt.get("step") == "let":
                _collect_exprs(stmt.get("value"), services, functions,
                               dict(setup_scope), pure, collected)
                setup_scope[stmt["name"]] = _expr_type(
                    stmt.get("value"), services, functions, setup_scope)
            elif "acquire" in stmt:
                # `effect <acquire> undo <inverse>`: the acquisition slot is
                # where an `acquire`/`witnessed` extern is called
                acquire_scope = {**setup_scope, "@position": "effect-acquire"}
                _collect_exprs(stmt.get("acquire"), services, functions,
                               acquire_scope, pure, collected)
                # the inverse names the acquired value, so its own binding is
                # in scope there (`let c = effect open() undo close(c)`), and
                # in every setup position after the statement
                bound = stmt.get("bind")
                callables = {**{e["name"]: e for e in externs}, **functions}
                acquired = _expr_type(stmt.get("acquire"), services, callables,
                                      setup_scope)
                undo_scope = {**setup_scope, "@position": "effect-undo"}
                if bound:
                    undo_scope[bound] = acquired
                _collect_exprs(stmt.get("undo"), services, functions,
                               undo_scope, pure, collected)
                rest = {k: v for k, v in stmt.items() if k not in ("acquire", "undo")}
                _collect_exprs(rest, services, functions, dict(setup_scope),
                               pure, collected)
                if bound:
                    setup_scope[bound] = acquired
                    owned.append({"name": bound, "type": acquired,
                                  "family": _resource_family(
                                      stmt.get("acquire"), acquired)})
            else:
                _collect_exprs(stmt, services, functions, dict(setup_scope),
                               pure, collected)

    # Top-level functions and tests are pure positions: a hole there may not
    # emit. Their scope is the declared parameters (functions) / nothing (tests).
    pure_fn = _capability(False, None, in_method=False,
                          reason="a function body — pure, no emission")
    for fn in ir.get("functions") or []:
        scope = {"@reachable": [], "@externs": externs, "@types": types,
                 "@untrusted": untrusted, "@position": "function"}
        for p in fn.get("params", []):
            scope[p["name"]] = p.get("type")
        _walk_body(fn.get("body"), services, functions, scope, pure_fn,
                   collected)
    for section in ("tests",):
        for item in ir.get(section) or []:
            _walk_body(item.get("body"), services, functions,
                       {"@reachable": [], "@externs": externs, "@types": types,
                        "@untrusted": untrusted, "@position": "test"},
                       pure_fn, collected)

    collected.sort(key=lambda c: (str(c[0].get("file")), c[0].get("line") or 0))
    return [_obligation(hole, spec) for hole, spec in collected]


__all__ = ["enrich", "CONSTRUCTS", "EMITTABLE_SECTIONS", "FILL_SPEC_VERSION",
           "CROSSING_FORM", "CROSSING_RULE", "EXTERN_PLACEMENT",
           "EXTERN_TEMPLATE", "UNTRUSTED_EXTERNS", "unfillable", "step_name"]
