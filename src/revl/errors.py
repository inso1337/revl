"""Compiler diagnostics.

The error messages are a deliverable (DESIGN.md §9): every rejection names
the guarantee it enforces and, where possible, suggests the fix.
"""

from __future__ import annotations

from .why import WhyTrace, render as render_why


class RevlError(Exception):
    def __init__(self, filename: str, line: int, message: str, hint: str | None = None,
                 code: str | None = None, category: str | None = None,
                 expected: str | None = None, actual: str | None = None,
                 why: WhyTrace | None = None, navigate: dict | None = None,
                 fix: str | None = None):
        self.filename = filename
        self.line = line
        self.message = message
        self.hint = hint
        # structured fields for the agent-facing projection (diagnostics.py);
        # all optional — most rejections are classified from their message
        self.code = code
        self.category = category
        self.expected = expected
        self.actual = actual
        # item 274: the navigable-refusal map — the nearest allowed space this
        # refusal enumerates, computed from the tables that refused (navigate.py).
        # A structured field ONLY: it rides on the `classify()`/`--json` record
        # and is DELIBERATELY not rendered into the text below, so the first line
        # and the multi-error census render stay byte-identical (design §5/§7).
        self.navigate = navigate
        # The rewrite for THIS rejection, when it is more specific than the
        # per-code `diagnostics.FIXES` entry (a corrected line, say). A
        # structured field only, like `navigate`: not rendered below.
        self.fix = fix
        # the derivation behind the verdict, where the check ran a search
        # (G4's fixed point, G3's cycle, G2's provider table) — see why.py.
        # It is appended *after* the message and hint so the first line of
        # every rejection is unchanged.
        self.why = why
        rendered = f"{filename}:{line}: {message}"
        if hint:
            rendered += f"\n  {hint}"
        trace = render_why(why)
        if trace:
            rendered += "\n" + trace
        super().__init__(rendered)


class RevlErrors(RevlError):
    """Carrier for a multi-refusal compile (roadmap item 386, Stage 1).

    The frontend used to abort on the FIRST `RevlError`; Stage 1 collects every
    recoverable refusal and raises them together at the end of
    `check_and_lower`. This carrier IS a `RevlError` so all ~70 `except
    RevlError` sites and `classify()` keep working with no change: its primary
    fields (`filename`/`line`/`message`/`code`/`category`/…) MIRROR THE FIRST
    diagnostic, so every legacy single-error consumer sees exactly what it saw
    before. The full ordered list lives on `.errors`; `diagnostics.report`,
    `plan._add` and the LSP iterate it. `__str__` renders the whole list plus a
    census line, so the many `print(f"error: {error}")` sites upgrade for free
    — and, for a lone refusal, renders byte-identically to that one error.
    """

    def __init__(self, errors: "list[RevlError]"):
        if not errors:
            raise ValueError("RevlErrors requires at least one error")
        self.errors: list[RevlError] = list(errors)
        first = self.errors[0]
        super().__init__(first.filename, first.line, first.message,
                         hint=first.hint, code=first.code, category=first.category,
                         expected=first.expected, actual=first.actual, why=first.why,
                         navigate=first.navigate)

    def __str__(self) -> str:
        # A single refusal renders exactly as a plain `RevlError` would: no
        # census line, so the text path stays byte-identical for the common case.
        if len(self.errors) == 1:
            return str(self.errors[0])
        files = {e.filename for e in self.errors}
        n, m = len(self.errors), len(files)
        census = (f"{n} refusals across {m} "
                  f"{'file' if m == 1 else 'files'}")
        return "\n".join([str(e) for e in self.errors] + [census])


def foreign_increment_refusal(filename: str | None, line: int, op: str,
                              left: str | None = None,
                              right: str | None = None) -> RevlError:
    """The refusal for a foreign in/decrement operator (`++` / `--`).

    `++` is not a revl operator in any position, but it concatenates strings in
    JS, PHP, Lua, Perl and SQL, so an author whose task is building a `Str`
    reaches for `a ++ b` before `a + b` — and with a non-`Str` value in the same
    position, `"total: " ++ n`. When an operand is known to be `Str` the refusal
    names the spellings that DO join strings, because a message about numeric
    increment teaches that author nothing and the same sentence comes back turn
    after turn (issue #2150). Every other operand shape keeps the increment
    wording item 384 shipped — correct there, and pinned by
    `examples/rejections/foreign_increment.rvl`.

    Lives here because every end of the pipeline raises it: the parser, on the
    error path where no operand is known (`i++`), the checker
    (`typecheck._binop_type`, which types every binary node on both the parser
    and the IR strata), and lowering (`lower._refuse_foreign_increment`, which
    holds the position for a body the checker does not infer with a filename).
    One wording, one place.
    """
    if op == "++" and "Str" in (left, right):
        if left == "Str" and right == "Str":
            hint = ("both operands are `Str`: `a + b` joins two strings and "
                    "`a.concat(b)` is the method form (docs/stdlib-2.0.md); "
                    "`revl_idiom` serves the `str-concat` example")
        else:
            other = right if left == "Str" else left
            whose = (f"the other operand is `{other}`" if other else
                     "the other operand is not a `Str`")
            hint = (f"`+` joins `Str` to `Str`; {whose}, so convert it with "
                    "`.to_str()` or interpolate it — write `` `n=${n}` `` "
                    "(docs/stdlib-2.0.md); `revl_idiom` serves the "
                    "`str-format` example")
        return RevlError(
            filename, line,
            "`++` is not an operator in revl — string concatenation is `+` "
            "(`a + b`) or `a.concat(b)`",
            hint=hint,
        )
    sign = op[0]
    word = "increment" if sign == "+" else "decrement"
    return RevlError(
        filename, line,
        f"revl has no `{sign}{sign}` {word} operator — expressions are pure "
        "(syntax-2.0 §3.3)",
        hint=f"mutate a `var` with `{sign}= 1` (write `i {sign}= 1`), inside a "
             "`while`/`for` loop body",
    )
