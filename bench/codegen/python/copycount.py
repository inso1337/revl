"""A deterministic ELEMENT-COPY counter.

The opcode counter in `run.py` measures interpreter work, and it is blind to
everything the interpreter hands to C in a single instruction. `out + [x]` is
one `BINARY_OP` that copies `len(out)` pointers, and `{**m, k: v}` is one
`DICT_UPDATE` that copies `len(m)` entries, so a quadratic accumulation loop
and a linear one have nearly identical opcode counts. That blind spot hides the
largest finding in this audit, and no clock is allowed to fill it: this file
fills it deterministically instead.

HOW. The module under test is re-executed with its AST mechanically rewritten:
every construct that copies a container is routed through a counting shim that
records HOW MANY ELEMENTS it moved and then performs the original operation.
The rewrite is value-preserving, so the instrumented function returns exactly
what the original returns, and `verify()` asserts that on every case. The
resulting count is a property of the program, not of the machine: it is
identical on every run, at any load, on any host.

WHAT COUNTS AS A COPY

    a + b                 list/str concatenation -> len(a) + len(b)
    {**a, k: v, ...}      dict display with a spread -> len(a) + 1 per literal
    dict(x) / list(x)     an explicit rebuild -> len(result)
    xs.append(v)          one element
    d.pop(k[, default])   one element
    d[k] = v              one element

Everything else is left alone. The set is small on purpose: these are the
constructs the cordis-py emitter actually reaches for, and the hand-written
comparators are measured with the SAME instrumentation, so an append-based loop
scores n and a concat-based loop scores n(n-1)/2 on the same scale.
"""

from __future__ import annotations

import ast
import types

_COUNT = [0]


def reset() -> None:
    _COUNT[0] = 0


def total() -> int:
    return _COUNT[0]


# ------------------------------------------------------------------- the shims
def _rc_add(a, b):
    if isinstance(a, (list, tuple, str, bytes)) and type(a) is type(b):
        _COUNT[0] += len(a) + len(b)
    return a + b


def _rc_merge(base, rest):
    """`{**base, **rest}` (the emitted spelling of a persistent map/record set)."""
    _COUNT[0] += len(base) + len(rest)
    out = dict(base)
    out.update(rest)
    return out


def _rc_rebuild(ctor, arg):
    out = ctor(arg)
    _COUNT[0] += len(out)
    return out


def _rc_one(value):
    _COUNT[0] += 1
    return value


def _rc_produced(value):
    """A comprehension: one counted element per element it yields, so the
    idiomatic `[f(x) for x in xs]` is charged the n appends it really does and
    the comparison against a copy-per-step loop stays honest."""
    _COUNT[0] += len(value)
    return value


class _Rewriter(ast.NodeTransformer):
    """Mechanically route every container-copying construct through a shim."""

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if not isinstance(node.op, ast.Add):
            return node
        return ast.Call(func=ast.Name(id="_rc_add", ctx=ast.Load()),
                        args=[node.left, node.right], keywords=[])

    def visit_Dict(self, node):
        self.generic_visit(node)
        if not any(k is None for k in node.keys):
            return node
        # `{**base, 'k': v}` -> `_rc_merge(base, {'k': v})`. Several spreads
        # fold left to right, which is the display's own evaluation order.
        base = None
        literal_keys, literal_values = [], []
        for key, value in zip(node.keys, node.values):
            if key is None:
                base = value if base is None else ast.Call(
                    func=ast.Name(id="_rc_merge", ctx=ast.Load()),
                    args=[base, value], keywords=[])
            else:
                literal_keys.append(key)
                literal_values.append(value)
        rest = ast.Dict(keys=literal_keys, values=literal_values)
        return ast.Call(func=ast.Name(id="_rc_merge", ctx=ast.Load()),
                        args=[base, rest], keywords=[])

    def visit_ListComp(self, node):
        self.generic_visit(node)
        return ast.Call(func=ast.Name(id="_rc_produced", ctx=ast.Load()),
                        args=[node], keywords=[])

    def visit_DictComp(self, node):
        self.generic_visit(node)
        return ast.Call(func=ast.Name(id="_rc_produced", ctx=ast.Load()),
                        args=[node], keywords=[])

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in ("dict", "list") \
                and len(node.args) == 1 and not node.keywords:
            return ast.Call(
                func=ast.Name(id="_rc_rebuild", ctx=ast.Load()),
                args=[ast.Name(id=node.func.id, ctx=ast.Load()), node.args[0]],
                keywords=[])
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("append", "pop"):
            return ast.Call(func=ast.Name(id="_rc_one", ctx=ast.Load()),
                            args=[node], keywords=[])
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript):
            return [node,
                    ast.Expr(value=ast.Call(
                        func=ast.Name(id="_rc_one", ctx=ast.Load()),
                        args=[ast.Constant(value=None)], keywords=[]))]
        return node


def instrument(source: str, name: str, extra_globals: dict | None = None):
    """Return a module whose functions are the source's, element-counted."""
    tree = _Rewriter().visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    mod = types.ModuleType(name)
    mod.__dict__.update({
        "_rc_add": _rc_add, "_rc_merge": _rc_merge,
        "_rc_rebuild": _rc_rebuild, "_rc_one": _rc_one,
        "_rc_produced": _rc_produced,
    })
    if extra_globals:
        mod.__dict__.update(extra_globals)
    import sys
    sys.modules[name] = mod
    exec(compile(tree, f"<instrumented {name}>", "exec"), mod.__dict__)
    return mod


def measure(fn, *args) -> int:
    reset()
    fn(*args)
    return total()


def verify(instrumented_fn, plain_fn, *args) -> None:
    """The rewrite must be value-preserving, or the count describes nothing."""
    reset()
    got, want = instrumented_fn(*args), plain_fn(*args)
    if got != want:
        raise AssertionError(
            f"instrumentation changed the result of {plain_fn.__name__}")
