"""What a served composition puts on the wire (item 569, slice B1).

`revl serve --http` and `revl serve --mcp` used to serve every provided key of
every component and to decode every parameter from the request. Two defects
followed (issues #1502, #1503): an anonymous caller wrote a `Principal` into a
request body and read another user's data, and a caller chose the
`Trusted[List[Str]]` grant list of `stdlib/admit.rvl`'s `admission.admit`.

This module holds the two rules that close them, shared by both faces:

1. **An authority parameter is never decoded from the wire.** A parameter
   whose type carries `Principal` (directly, inside a generic, or inside a
   declared record or variant) or whose declared type carries `Trusted[...]`
   makes its operation WITHHELD: neither face serves it, and a request that
   names it is refused by name (`Withheld.message`). A `Principal` comes only
   from the validator that mints it (`Auth.validate`, stdlib/auth.rvl); a
   `Trusted[...]` value comes only from first-party code.
2. **The HTTP face serves a declared public surface.** That is the routed
   operations (item 457) plus the operations a caller explicitly declares
   public. The language has no per-operation public marking yet, so
   `revl serve --http` declares none and serves the routed surface only.
   `HttpComposedServer(public=...)` is the seam such a marking would feed.

`Trusted[...]` is stripped from declared types before the IR is written
(`taint.extract_and_normalize`), so the IR alone cannot show it.
`declared_param_types` reads the declared types back from the same source files
the composition was compiled from. `Principal` survives into the IR and is read
from it, so a face built from an IR alone still withholds every
`Principal`-taking operation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: the diagnostic category a withheld-operation refusal carries on both faces
AUTHORITY_CATEGORY = "authority"

_PRINCIPAL = "Principal"
_TRUSTED = "Trusted"
_TYPE_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: `{service: {op: {param: declared type}}}`, the pre-strip declared types
DeclaredTypes = dict[str, dict[str, dict[str, str]]]


def _type_words(type_name) -> set[str]:
    return set(_TYPE_WORD_RE.findall(type_name or "")) \
        if isinstance(type_name, str) else set()


@dataclass(frozen=True)
class Withheld:
    """An operation neither face serves, and the parameter that withholds it."""
    key: str
    op: str
    param: str
    type: str

    @property
    def minted_by(self) -> str:
        if _PRINCIPAL in _type_words(self.type):
            return "the validator that mints it (`Auth.validate`)"
        return "first-party code inside the composition"

    def message(self) -> str:
        return (f"`{self.key}.{self.op}` is not served: its parameter "
                f"`{self.param}` has type `{self.type}`, an authority value "
                f"this face never decodes from a request. It can only come "
                f"from {self.minted_by}.")


def _principal_types(ir: dict) -> set[str]:
    """Declared record/variant names that carry a `Principal`, directly or
    through another such type (a `{ who: Principal }` box is a principal)."""
    direct: dict[str, set[str]] = {}
    for name, decl in (ir.get("types") or {}).items():
        words: set[str] = set()
        if isinstance(decl, dict):
            for field_type in (decl.get("fields") or {}).values():
                words |= _type_words(field_type)
            for case in decl.get("cases") or []:
                if isinstance(case, dict):
                    words |= _type_words(case.get("payload"))
        direct[name] = words
    carrying: set[str] = set()
    growing = True
    while growing:
        growing = False
        for name, words in direct.items():
            if name not in carrying and (_PRINCIPAL in words or words & carrying):
                carrying.add(name)
                growing = True
    return carrying


def _authority_type(ir_type: str | None, declared: str | None,
                    principal_types: set[str]) -> str | None:
    """The type to name in a refusal when this parameter is an authority
    parameter, else None."""
    words = _type_words(ir_type) | _type_words(declared)
    if _TRUSTED in _type_words(declared) \
            or _PRINCIPAL in words or words & principal_types:
        return declared or ir_type or _PRINCIPAL
    return None


def withheld_operations(ir: dict, declared: DeclaredTypes | None = None,
                        ) -> dict[tuple[str, str], Withheld]:
    """Every provided `(key, op)` with an authority parameter, keyed for lookup.

    `declared` is `declared_param_types(...)` for the files `ir` was compiled
    from. Without it only `Principal` is visible, because `Trusted[...]` does
    not survive into the IR."""
    declared = declared or {}
    services = ir.get("services") or {}
    principal_types = _principal_types(ir)
    withheld: dict[tuple[str, str], Withheld] = {}
    for component in ir.get("components") or []:
        for key, service_name in (component.get("provides") or {}).items():
            service = services.get(service_name) or {}
            declared_ops = declared.get(service_name) or {}
            for op_name, op in (service.get("methods") or {}).items():
                declared_params = declared_ops.get(op_name) or {}
                for param in op.get("params") or []:
                    name = param.get("name")
                    shown = _authority_type(param.get("type"),
                                            declared_params.get(name),
                                            principal_types)
                    if shown is not None:
                        withheld[(key, op_name)] = Withheld(
                            key, op_name, name, shown)
                        break
    return withheld


def _service_params(services) -> DeclaredTypes:
    return {svc.name: {m.name: {p: t for p, t in m.params}
                       for m in svc.methods.values()}
            for svc in services}


def declared_param_types(paths: list[str]) -> DeclaredTypes:
    """The DECLARED parameter types of the composition's services, read before
    the checker strips `Trusted[...]`/`Untrusted[...]` off them.

    The service table is the one `compile_files` builds: every service a root
    module declares, plus every service a root imports by name. Reads the files
    again with the compiler's own loader; call it with the paths the IR was
    compiled from."""
    from ..compiler import _ModuleLoader, _load_root  # noqa: PLC0415

    loader = _ModuleLoader()
    loader.mark_roots(os.path.abspath(p) for p in paths)
    roots = [_load_root(loader, path) for path in paths]
    services = [svc for module in roots for svc in module.program.services]
    for module in roots:
        for use in module.program.uses:
            if use.names is None:
                continue
            used = loader.load(loader.resolve_use(module.dir, module.path, use))
            services.extend(used.services[name] for name in use.names
                            if name in used.services)
    return _service_params(services)


def declared_param_types_from_source(source: str,
                                     filename: str = "<string>") -> DeclaredTypes:
    """`declared_param_types` for a single in-memory source with no `use`."""
    from ..parser import Parser  # noqa: PLC0415

    return _service_params(Parser(source, filename).parse().services)
