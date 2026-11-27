"""Check the arguments of a `Session.call` against the parameter types the
service declares (issue #3345).

`Session.call` hands `args` to the provided method. Without a check a value of
the wrong shape is accepted at the call: a host error then surfaces later from
inside the method, far from the cause, or nothing surfaces and a value that does
not have its declared type keeps flowing (a `Box` with no `h` returned as a
`Box`). The declaration is the contract, so the call is where it is enforced.

The check reads the declared types from the IR and walks the LIVE Python value,
not a JSON document: the HTTP face validates a request body against
`json_schema_for` (`http_face._schema_error`), but an embedder hands over native
values, where an ADT case is an emitted case instance and a record may be a
dataclass. So the walk below accepts every form a live value takes at the call
(a native case, the `{"$kind": ...}` wire form, the `{"tag": ...}` form the
validated schema pins, or a bare case name for an all-nullary variant) and
refuses the shapes that cannot be the declared type.

What is checked, and what is deliberately not:

* `Str` / `Int` / `Float` / `Bool` / `Bytes` / `Unit`: the scalar kind. `Int`
  refuses a `bool` and any `float`; `Float` takes an `int`.
* `List[T]`, `Map[K, V]`, `Opt[T]`: the container and every element.
* a record: a mapping (or a dataclass) holding every declared field, each of
  the declared type. An extra key is not refused, as in the derived schema.
* a variant and `Result`: one of the declared cases, and its payload.
* any other name (a type parameter, an extern or opaque host type, an unknown
  nominal): not checked. The projection does not know its shape, the same
  degradation `json_schema_for` makes.

The first mismatch is reported with the method, the parameter and the path:
``shapes.keep: b.h is missing``.
"""

from __future__ import annotations

import dataclasses

from ..type_schema import _parse_type

_MISSING = object()


def declared_params(ir: dict | None, key: str, method: str):
    """The declared parameter list of the service operation provided at `key`,
    or ``None`` when the IR does not say (nothing is then checked)."""
    if not ir:
        return None
    services = ir.get("services") or {}
    for component in ir.get("components") or []:
        service_name = (component.get("provides") or {}).get(key)
        if service_name is None:
            continue
        op = ((services.get(service_name) or {}).get("methods") or {}).get(method)
        if op is None:
            return None
        return list(op.get("params") or [])
    return None


def args_error(ir: dict | None, key: str, method: str, args) -> str | None:
    """The first way `args` fails the declared parameters of `key.method`, as a
    message naming the method, the parameter and the path, or ``None``."""
    params = declared_params(ir, key, method)
    if params is None:
        return None
    types = (ir or {}).get("types") or {}
    args = list(args or [])
    where = f"{key}.{method}"
    named = []
    for param in params:
        if isinstance(param, dict):
            named.append((param.get("name"), param.get("type")))
        else:
            named.append((param, None))
    if len(args) > len(named):
        return (f"{where}: takes {len(named)} argument(s), got {len(args)}")
    for index, (pname, ptype) in enumerate(named):
        if index >= len(args):
            # an omitted trailing `Opt` is refused too: the method takes every
            # parameter positionally, so it would fail with a host `TypeError`
            return f"{where}: argument {pname} is missing"
        err = _type_error(args[index], ptype, types, pname, frozenset())
        if err is not None:
            return f"{where}: {err}"
    return None


def _subst(type_name: str, env: dict) -> str:
    """`type_name` with every type parameter in `env` replaced by its argument."""
    head, targs = _parse_type(type_name)
    if not targs:
        return env.get(head, type_name)
    return f"{head}[{', '.join(_subst(a, env) for a in targs)}]"


def _instantiate(spec: dict, targs: list):
    """The record fields or variant cases of a (possibly generic) type with its
    parameters bound to `targs`, or ``None`` when the argument count does not
    match the declared parameters (nothing is then checked)."""
    params = list(spec.get("params") or [])
    if len(params) != len(targs):
        return None
    env = dict(zip(params, targs))
    if not env:
        return spec
    if spec.get("kind") == "record":
        return {**spec, "fields": {n: _subst(t, env)
                                   for n, t in (spec.get("fields") or {}).items()}}
    return {**spec, "cases": [
        {**c, "payload": _subst(c["payload"], env) if c.get("payload") else
         c.get("payload")} for c in spec.get("cases") or []]}


def _kind(value) -> str:
    return type(value).__name__


def _type_error(value, type_name, types, path, seen) -> str | None:
    if not type_name:
        return None
    if type_name == "Unit":
        return None if value is None else _expected(path, "Unit", value)
    if type_name == "Str":
        return None if isinstance(value, str) else _expected(path, "Str", value)
    if type_name == "Bool":
        return None if isinstance(value, bool) else _expected(path, "Bool", value)
    if type_name == "Int":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else _expected(path, "Int", value)
    if type_name == "Float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if ok else _expected(path, "Float", value)
    if type_name == "Bytes":
        ok = isinstance(value, (bytes, bytearray, str))
        return None if ok else _expected(path, "Bytes", value)
    head, targs = _parse_type(type_name)
    if head == "Opt" and targs:
        if value is None:
            return None
        return _type_error(value, targs[0], types, path, seen)
    if head == "List" and targs:
        if not isinstance(value, (list, tuple)):
            return _expected(path, type_name, value)
        for i, item in enumerate(value):
            err = _type_error(item, targs[0], types, f"{path}[{i}]", seen)
            if err is not None:
                return err
        return None
    if head == "Map" and len(targs) == 2:
        if not isinstance(value, dict):
            return _expected(path, type_name, value)
        for n, item in enumerate(value.values()):
            # the position, never the key: a key may be a credential
            err = _type_error(item, targs[1], types, f"{path}[<key {n}>]", seen)
            if err is not None:
                return err
        return None
    if head == "Result" and len(targs) == 2:
        return _case_error(value, [{"name": "Ok", "payload": targs[0]},
                                   {"name": "Err", "payload": targs[1]}],
                           type_name, types, path, seen)
    # a value that contains itself (a cycle) is checked once on the way in; a
    # recursive TYPE over a finite value terminates by the value running out
    mark = (type_name, id(value))
    if mark in seen:
        return None
    # the head names the declaration; an instantiated generic (`Pair[Int, Str]`)
    # is checked with its parameters bound to the arguments
    spec = types.get(head)
    spec = _instantiate(spec, targs) if spec else None
    if spec and spec.get("kind") == "record":
        return _record_error(value, spec, type_name, types, path,
                             seen | {mark})
    if spec and spec.get("kind") == "variant":
        return _case_error(value, spec.get("cases") or [], type_name, types,
                           path, seen | {mark})
    return None


def _expected(path: str, type_name: str, value) -> str:
    return f"{path} expected {type_name}, got {_kind(value)}"


def _field(value, name):
    if isinstance(value, dict):
        return value.get(name, _MISSING)
    return getattr(value, name, _MISSING)


def _record_error(value, spec, type_name, types, path, seen) -> str | None:
    if not isinstance(value, dict) and not (
            dataclasses.is_dataclass(value) and not isinstance(value, type)):
        return _expected(path, type_name, value)
    for fname, ftype in (spec.get("fields") or {}).items():
        item = _field(value, fname)
        if item is _MISSING:
            return f"{path}.{fname} is missing"
        err = _type_error(item, ftype, types, f"{path}.{fname}", seen)
        if err is not None:
            return err
    return None


def _lookup_case(by_name: dict, spelled):
    """The declared case a live spelling names. The python emitter appends `_` to
    a case named like a python keyword or an emitted builtin (`None` is emitted
    as `None_`), and the wire `$kind` carries that spelling, so the exact name is
    tried first and the un-mangled one after."""
    if spelled in by_name:
        return by_name[spelled]
    if isinstance(spelled, str) and spelled.endswith("_"):
        return by_name.get(spelled.rstrip("_"))
    return None


def _case_error(value, cases, type_name, types, path, seen) -> str | None:
    by_name = {c.get("name"): c for c in cases}
    names = ", ".join(sorted(str(n) for n in by_name))
    if isinstance(value, str):
        case = _lookup_case(by_name, value)
        if case is not None and not case.get("payload"):
            return None
        return f"{path} expected {type_name} (one of {names}), got a Str"
    if isinstance(value, dict):
        for tag_key, payload_key in (("$kind", "$value"), ("tag", "value")):
            if tag_key in value:
                case = _lookup_case(by_name, value[tag_key])
                if case is None:
                    return (f"{path} expected {type_name} (one of {names}), "
                            f"got an unknown case")
                payload = value.get(payload_key, _MISSING)
                if payload is _MISSING and case.get("payload"):
                    return f"{path}.{case.get('name')} is missing"
                return _payload_error(payload, case, types, path, seen)
        return _expected(path, f"{type_name} (one of {names})", value)
    if value is None or isinstance(value, (bool, int, float, list, tuple, bytes)):
        return _expected(path, f"{type_name} (one of {names})", value)
    case = _lookup_case(by_name, type(value).__name__)
    if case is None:
        return (f"{path} expected {type_name} (one of {names}), "
                f"got {_kind(value)}")
    return _payload_error(getattr(value, "value", _MISSING), case, types,
                          path, seen)


def _payload_error(payload, case, types, path, seen) -> str | None:
    ptype = case.get("payload")
    if not ptype or payload is _MISSING:
        return None
    return _type_error(payload, ptype, types, f"{path}.{case.get('name')}", seen)
