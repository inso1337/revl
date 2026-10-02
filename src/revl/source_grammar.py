"""The grammar of revl SOURCE, derived from revl's own parser.

`revl.parser.Parser` is a hand-written recursive-descent parser, and it is the
one definition of what revl source is. This module does not restate it. It
reads the parser's source with `ast` and compiles each parse method into a
grammar rule, so the grammar a constrained decoder is handed is the parser's
own structure, derived mechanically. `revl grammar --format gbnf|lark|ebnf`
prints it; `grammar/` holds the committed copies the drift gate compares.

HOW A PARSE METHOD BECOMES A RULE
---------------------------------
Each `Parser` method is abstract-interpreted as a reader of tokens. Every path
through its body reads a sequence of tokens, so the method's language is the
union of its paths:

* `self.expect(kind, value)` reads one token of that kind (and value);
* `self.next()` reads the token the enclosing condition proved the cursor is
  at. `if self.at("kw", "requires") or self.at("kw", "provides"):
  self.next()` reads `requires` or `provides`; a guard can name tokens ahead
  of the cursor too (`self.peek_ahead(1).kind == ":"`), a method opening with
  `self.next()` takes its callers' guards, and a method called with an
  operator tuple (`self._bin(self._mul, ("+", "-"))`) is compiled once per
  distinct argument list, so its `self.next()` reads one of those operators;
* `self.<method>(...)` reads whatever that method's rule reads;
* `if`/`elif`/`else` is a union, `while`/`for` a repetition, `return`,
  `raise`, `break` and `continue` end their path, and `try` is the union of its
  body and its handlers (the parser backtracks there);
* `self.at`, `self.peek`, `self.peek_ahead` and `self.err` read nothing.

Conditions narrow nothing except the guard a read takes, so the grammar is an
OVER-approximation of the parser: every document the parser accepts is in the
grammar's language, and the grammar can accept some the parser refuses (a
semantic check, a duplicate clause, a lookahead it does not model). That is the
direction a constrained decoder needs. Structure that is in the method shapes
is kept: `component C { requires k: S }` is not in the language, because
`requires` is a keyword, the component header is the only rule that reads it,
and an identifier never matches a keyword.

The lexical layer comes from `revl.lexer` (`KEYWORDS`, the symbols and
operators) and from the parser (`Parser._CONTEXTUAL_NOUNS`). The token classes
the lexer scans by hand (identifiers, numbers, strings, templates, host bodies)
are written here as patterns and held to the lexer by the corpus test.

THE FORMATS
-----------
* `lark`: llguidance's Lark dialect. Keywords are excluded from identifiers
  with `& ~`, whitespace and `//` comments are `%ignore`d, and the grammar is
  exact to the derivation.
* `gbnf`: the GGML BNF the llama.cpp server and XGrammar read. It has no
  negation, so identifiers are built from a trie that steps around every
  keyword, and whitespace is an explicit rule between tokens. GBNF is
  character-level with no tokenizer, so word boundaries are written out: each
  rule is emitted in up to four variants by whether its first and last
  characters are word characters, and two adjacent words are joined by
  `ws1` (at least one space or comment) where anything else takes `ws`. So
  `componentC` is not in its language. A number needs no break after it,
  because the lexer ends a number at the first non-digit (`30s`).
* `ebnf`: the same rules in ISO-style EBNF, for reading.

WHAT THE DRIFT GATE CHECKS
--------------------------
`tests/test_source_grammar_1661.py`: the committed
`grammar/revl.{gbnf,lark,ebnf}` equal a fresh derivation; every corpus document
the parser accepts is accepted by the Lark grammar under llguidance, and a
sample of small ones by the GBNF grammar under a character-level recogniser;
the refusals named in the issue are refused in both. A parser change that
moves the grammar fails until `revl grammar --write` regenerates the files.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from collections import namedtuple

# --------------------------------------------------------------------------
# the grammar IR
# --------------------------------------------------------------------------
#
# ("t", kind, value)  one token; kind None means "the text `value`, any kind";
#                     value None means any token of that kind
# ("any",)            any one token
# ("n", name)         a nonterminal (a parse method, possibly specialised)
# ("seq", (e, ...))   concatenation; ("seq", ()) is the empty string
# ("alt", (e, ...))   union
# ("star", e)         zero or more
#
# None is the empty LANGUAGE (no path), distinct from EPS (the empty string).

EPS = ("seq", ())
ANY = ("any",)


def seq(*parts):
    items = []
    for part in parts:
        if part is None:
            return None
        if part[0] == "seq":
            items.extend(part[1])
        else:
            items.append(part)
    if len(items) == 1:
        return items[0]
    return ("seq", tuple(items))


def alt(*parts):
    items = []
    for part in parts:
        if part is None:
            continue
        for sub in (part[1] if part[0] == "alt" else (part,)):
            if sub not in items:
                items.append(sub)
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return ("alt", tuple(items))


def star(part):
    if part is None or part == EPS:
        return EPS
    if part[0] == "star":
        return part
    return ("star", part)


def opt(part):
    return alt(part, EPS)


Flow = namedtuple("Flow", "ret fall brk cont")
DEAD = Flow(None, None, None, None)

# Parser methods that read no token by construction: lookahead and diagnostics.
LOOKAHEAD = frozenset({"at", "peek", "peek_ahead", "err"})


def _parser_module_source() -> str:
    from . import parser as _parser  # noqa: PLC0415 - the parser imports the lexer
    return inspect.getsource(_parser)


def _contextual_nouns() -> tuple:
    from . import parser as _parser  # noqa: PLC0415
    return tuple(sorted(_parser.Parser._CONTEXTUAL_NOUNS))


# --------------------------------------------------------------------------
# guards: what a condition proves about the tokens at and after the cursor
# --------------------------------------------------------------------------
#
# A guard is a tuple of position terms, one per token from the cursor on; a
# position is a term (("t", ...) or an alt of them) or None (unknown). A
# value-only position is ("v", value) until it meets a kind.

def _subs(term):
    return term[1] if term[0] == "alt" else (term,)


def _position_meet(a, b):
    if a is None:
        return b
    if b is None:
        return a
    kinds = [x for x in _subs(a) + _subs(b) if x[0] == "t"]
    values = [x for x in _subs(a) + _subs(b) if x[0] == "v"]
    if kinds and values:
        out = [("t", k[1], v[1]) for k in kinds for v in values
               if k[2] is None or k[2] == v[1]]
        return alt(*out)
    # two kind-only positions: keep the narrower (the first one stated)
    return a


def _guard_meet(a, b):
    if a is None:
        return b
    if b is None:
        return a
    n = max(len(a), len(b))
    a = a + (None,) * (n - len(a))
    b = b + (None,) * (n - len(b))
    return tuple(_position_meet(x, y) for x, y in zip(a, b))


def _guard_union(a, b):
    """`x or y`: only the cursor position survives, and only when both name it."""
    if a is None or b is None or a[0] is None or b[0] is None:
        return None
    return (alt(a[0], b[0]),)


def _settle(term):
    """A position term as a token term: a value-only term reads as the text."""
    if term is None:
        return None
    return alt(*((("t", None, s[1]) if s[0] == "v" else s) for s in _subs(term)))


# --------------------------------------------------------------------------
# compiling parse methods
# --------------------------------------------------------------------------

class _Compiler:
    def __init__(self, methods: dict, entries: dict | None = None):
        self.methods = methods
        self.entries = entries or {}
        self.rules: dict = {}           # rule name -> grammar
        self.specs: dict = {}           # rule name -> (method, env)
        self.calls: dict = {}           # rule name -> [guard at each call site]
        self.notes: dict = {}           # method -> loosely modelled constructs
        self.unguarded: list = []       # (method, line) of a next() read as any token
        self._inline: dict = {}

    def note(self, method, what):
        self.notes.setdefault(method, set()).add(what)

    # -- specialisation ----------------------------------------------------

    def _relevant_params(self, method):
        fn = self.methods[method]
        params = [a.arg for a in fn.args.args[1:]]
        used = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in params:
                    used.add(node.func.id)
                if _self_method(node) in ("at", "expect"):
                    used.update(a.id for a in node.args if isinstance(a, ast.Name))
            if (isinstance(node, ast.For) and isinstance(node.iter, ast.Name)
                    and node.iter.id in params):
                used.add(node.iter.id)
        return [p for p in params if p in used]

    def rule_for_call(self, method, call, env):
        """The rule a call reads: `method`, or a specialisation of it when the
        call passes constants or parse methods to a parameter that reads."""
        relevant = self._relevant_params(method)
        if not relevant:
            return method
        params = [a.arg for a in self.methods[method].args.args[1:]]
        bound = dict(zip(params, call.args))
        bound.update({kw.arg: kw.value for kw in call.keywords if kw.arg})
        callee_env = {}
        for param in relevant:
            if param in bound:
                desc = _describe(bound[param], env)
                if desc is not None:
                    callee_env[param] = desc
        if not callee_env:
            return method
        tag = "__".join(_desc_tag(callee_env[p]) for p in sorted(callee_env))
        name = f"{method}__{tag}"
        self.specs.setdefault(name, (method, callee_env))
        return name

    # -- guards ------------------------------------------------------------

    def guard_of(self, test, ctx):
        if isinstance(test, ast.BoolOp):
            guards = [self.guard_of(v, ctx) for v in test.values]
            if isinstance(test.op, ast.Or):
                out = guards[0]
                for g in guards[1:]:
                    out = _guard_union(out, g)
                return out
            out = None
            for g in guards:
                out = _guard_meet(out, g)
            return out
        if isinstance(test, ast.Call):
            name = _self_method(test)
            if name == "at":
                term = self._term_from_args(test.args, ctx)
                return None if term is None else (term,)
            if name == "_is_name_tok":
                return (alt(("t", "ident", None),
                            *(("t", "kw", w) for w in _contextual_nouns())),)
            if name in self.methods:
                return self._predicate_guard(name)
            return None
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            return self._compare_guard(test, ctx)
        return None

    def _predicate_guard(self, name):
        """The guard a lookahead method's `return <test>` proves."""
        if name in self._inline:
            return self._inline[name]
        self._inline[name] = None
        ctx = {"peekvars": {}, "env": {}}
        guard = None
        for stmt in self.methods[name].body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                offset = self._peek_offset(stmt.value, ctx)
                if offset is not None:
                    ctx["peekvars"][stmt.targets[0].id] = offset
                continue
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                guard = self.guard_of(stmt.value, ctx)
            break
        self._inline[name] = guard
        return guard

    def _peek_offset(self, node, ctx):
        """`k` when `node` is the token `k` past the cursor."""
        if isinstance(node, ast.Name):
            return ctx["peekvars"].get(node.id)
        if isinstance(node, ast.Call):
            name = _self_method(node)
            if name == "peek":
                return 0
            if name == "peek_ahead":
                if not node.args:
                    return 1
                if isinstance(node.args[0], ast.Constant):
                    return node.args[0].value
            return None
        if isinstance(node, ast.IfExp):
            return self._peek_offset(node.body, ctx)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "toks"):
            index = node.slice
            if isinstance(index, ast.Attribute) and index.attr == "pos":
                return 0
            if (isinstance(index, ast.BinOp) and isinstance(index.op, ast.Add)
                    and isinstance(index.left, ast.Attribute) and index.left.attr == "pos"
                    and isinstance(index.right, ast.Constant)):
                return index.right.value
        return None

    def _compare_guard(self, test, ctx):
        left, op, right = test.left, test.ops[0], test.comparators[0]
        if not (isinstance(left, ast.Attribute) and left.attr in ("kind", "value")):
            return None
        offset = self._peek_offset(left.value, ctx)
        if offset is None:
            return None
        if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
            values = (right.value,)
        elif isinstance(op, ast.In):
            values = _constant_strings(right)
            if values is None:
                return None
        else:
            return None
        if left.attr == "kind":
            term = alt(*(("t", v, None) for v in values))
        else:
            term = alt(*(("v", v) for v in values))
        return (None,) * offset + (term,)

    def _term_from_args(self, args, ctx):
        if not args:
            return None
        first = args[0]
        if isinstance(first, ast.Name):
            desc = ctx["env"].get(first.id)
            if desc and desc[0] == "consts":
                return alt(*(("t", v, None) for v in desc[1]))
            return None
        if not isinstance(first, ast.Constant):
            return None
        if len(args) > 1 and isinstance(args[1], ast.Constant):
            return ("t", first.value, args[1].value)
        return ("t", first.value, None)

    # -- expressions -------------------------------------------------------

    def _read(self, ctx):
        """The token one read takes: the guard's first position, or None."""
        guard = ctx["guard"]
        term = _settle(guard[0]) if guard else None
        ctx["guard"] = guard[1:] if guard and len(guard) > 1 else None
        return term

    def consume(self, node, ctx):
        """The tokens evaluating `node` reads, in evaluation order."""
        if node is None:
            return EPS
        if isinstance(node, ast.Call):
            return self._consume_call(node, ctx)
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp,
                             ast.DictComp, ast.GeneratorExp)):
            inner = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and _self_method(n) in self.methods]
            if inner:
                self.note(ctx["method"], "a parse call inside a comprehension or lambda")
                return star(alt(*(("n", self.rule_for_call(_self_method(n), n, ctx["env"]))
                                  for n in inner)))
            return EPS
        if isinstance(node, ast.BoolOp):
            parts = [self.consume(v, ctx) for v in node.values]
            out = parts[0]
            for part in parts[1:]:
                out = seq(out, opt(part))
            return out
        if isinstance(node, ast.IfExp):
            pre = self.consume(node.test, ctx)
            guard = _guard_meet(ctx["guard"], self.guard_of(node.test, ctx))
            return seq(pre, alt(self.consume(node.body, dict(ctx, guard=guard)),
                                self.consume(node.orelse, dict(ctx))))
        parts = [self.consume(child, ctx) for child in ast.iter_child_nodes(node)
                 if isinstance(child, ast.expr)]
        return seq(*parts) if parts else EPS

    def _consume_call(self, node, ctx):
        name = _self_method(node)
        pre = seq(*(self.consume(a, ctx) for a in node.args),
                  *(self.consume(k.value, ctx) for k in node.keywords))
        if name is None:
            func = node.func
            if isinstance(func, ast.Name) and func.id in ctx["env"]:
                desc = ctx["env"][func.id]
                if desc[0] == "methods":
                    ctx["guard"] = None
                    return seq(pre, alt(*(("n", m) for m in desc[1])))
            return seq(self.consume(func, ctx), pre)
        if name in LOOKAHEAD or _is_predicate(self.methods.get(name)):
            return pre
        if name == "next":
            term = self._read(ctx)
            if term is None:
                term = self._param_guess(ctx)
            if term is None:
                self.note(ctx["method"], "next() under no guard")
                self.unguarded.append((ctx["method"], node.lineno))
                term = ANY
            return seq(pre, term)
        if name == "expect":
            self._read(ctx)
            term = self._term_from_args(node.args, ctx)
            if term is None:
                self.note(ctx["method"], "expect() of a computed token")
                term = ANY
            return seq(pre, term)
        if name in self.methods:
            rule = self.rule_for_call(name, node, ctx["env"])
            self.calls.setdefault(rule, []).append(ctx["guard"])
            ctx["guard"] = None
            return seq(pre, ("n", rule))
        return pre

    def _param_guess(self, ctx):
        """A read under no guard, in a method specialised on one token tuple
        (`_bin`'s `for candidate in ops: if self.at(candidate)`): one of them."""
        consts = [d for d in ctx["env"].values() if d[0] == "consts"]
        if len(consts) == 1:
            return alt(*(("t", v, None) for v in consts[0][1]))
        return None

    # -- statements --------------------------------------------------------

    def block(self, stmts, ctx):
        ret = brk = cont = None
        fall = EPS
        for stmt in stmts:
            if fall is None:
                break
            f = self.stmt(stmt, ctx)
            ret = alt(ret, seq(fall, f.ret))
            brk = alt(brk, seq(fall, f.brk))
            cont = alt(cont, seq(fall, f.cont))
            fall = seq(fall, f.fall)
            self._after(stmt, ctx)
        return Flow(ret, fall, brk, cont)

    def _after(self, node, ctx):
        """What a statement proves about the cursor for the ones after it."""
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name = node.targets[0].id
            offset = self._peek_offset(node.value, ctx)
            peekvars = dict(ctx["peekvars"])
            if offset is not None:
                peekvars[name] = offset
            else:
                peekvars.pop(name, None)
            ctx["peekvars"] = peekvars
        # `if <test>: raise/return` leaves the cursor where <test> is false
        if isinstance(node, ast.If) and not node.orelse and _ends(node.body):
            ctx["guard"] = _guard_meet(ctx["guard"], self.negated_guard(node.test, ctx))

    def negated_guard(self, test, ctx):
        """What the cursor is at when `test` is FALSE."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return self.guard_of(test.operand, ctx)
        if isinstance(test, ast.BoolOp):
            guards = [self.negated_guard(v, ctx) for v in test.values]
            if isinstance(test.op, ast.Or):          # not (a or b) = not a and not b
                out = None
                for g in guards:
                    out = _guard_meet(out, g)
                return out
            out = guards[0]                          # not (a and b) = not a or not b
            for g in guards[1:]:
                out = _guard_union(out, g)
            return out
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            flipped = {ast.NotEq: ast.Eq, ast.NotIn: ast.In}.get(type(test.ops[0]))
            if flipped is not None:
                return self._compare_guard(
                    ast.Compare(test.left, [flipped()], test.comparators), ctx)
        return None

    def stmt(self, node, ctx):
        if isinstance(node, ast.Return):
            return Flow(self.consume(node.value, ctx), None, None, None)
        if isinstance(node, ast.Raise):
            return DEAD
        if isinstance(node, ast.Break):
            return Flow(None, None, EPS, None)
        if isinstance(node, ast.Continue):
            return Flow(None, None, None, EPS)
        if isinstance(node, ast.If):
            pre = self.consume(node.test, ctx)
            guard = _guard_meet(ctx["guard"], self.guard_of(node.test, ctx))
            then = self.block(node.body, dict(ctx, guard=guard))
            other = self.block(node.orelse, dict(ctx))
            return Flow(seq(pre, alt(then.ret, other.ret)),
                        seq(pre, alt(then.fall, other.fall)),
                        seq(pre, alt(then.brk, other.brk)),
                        seq(pre, alt(then.cont, other.cont)))
        if isinstance(node, ast.While):
            guard = self.guard_of(node.test, ctx)
            body = self.block(node.body, dict(ctx, guard=guard))
            again = star(alt(body.fall, body.cont))
            exits = EPS
            if isinstance(node.test, ast.Constant) and node.test.value:
                exits = None           # `while True` leaves only by break/return
            return Flow(seq(again, body.ret),
                        alt(seq(again, exits), seq(again, body.brk)), None, None)
        if isinstance(node, ast.For):
            pre = self.consume(node.iter, ctx)
            inner = dict(ctx, guard=None)
            if (isinstance(node.iter, ast.Name) and isinstance(node.target, ast.Name)
                    and node.iter.id in ctx["env"]):
                inner["env"] = dict(ctx["env"], **{node.target.id: ctx["env"][node.iter.id]})
            body = self.block(node.body, inner)
            again = star(alt(body.fall, body.cont))
            rest = self.block(node.orelse, dict(ctx, guard=None))
            return Flow(seq(pre, again, body.ret),
                        alt(seq(pre, again, rest.fall), seq(pre, again, body.brk)),
                        None, None)
        if isinstance(node, ast.Try):
            body = self.block(node.body, dict(ctx))
            flows = [body] + [self.block(h.body, dict(ctx, guard=None))
                              for h in node.handlers]
            if node.handlers:
                self.note(ctx["method"], "try/except (backtracking)")
            orelse = self.block(node.orelse, dict(ctx, guard=None))
            final = self.block(node.finalbody, dict(ctx, guard=None))
            ret = alt(*(f.ret for f in flows), seq(body.fall, orelse.ret))
            fall = alt(seq(body.fall, orelse.fall), *(f.fall for f in flows[1:]))
            return Flow(seq(ret, final.fall), seq(fall, final.fall),
                        alt(*(f.brk for f in flows)), alt(*(f.cont for f in flows)))
        if isinstance(node, ast.With):
            pre = seq(*(self.consume(item.context_expr, ctx) for item in node.items))
            body = self.block(node.body, ctx)
            return Flow(seq(pre, body.ret), seq(pre, body.fall),
                        seq(pre, body.brk), seq(pre, body.cont))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(isinstance(n, ast.Call) and _self_method(n) in self.methods
                   for n in ast.walk(node)):
                self.note(ctx["method"], "a nested function that parses")
            return Flow(None, EPS, None, None)
        reads = [self.consume(child, ctx) for child in ast.iter_child_nodes(node)
                 if isinstance(child, ast.expr)]
        return Flow(None, seq(*reads) if reads else EPS, None, None)

    # -- rules -------------------------------------------------------------

    def compile_rule(self, rule):
        method, env = self.specs.get(rule, (rule, {}))
        ctx = {"method": method, "guard": self.entries.get(rule), "peekvars": {},
               "env": env}
        flow = self.block(self.methods[method].body, ctx)
        out = alt(flow.ret, flow.fall)
        return out if out is not None else EPS

    def reach(self, starts):
        todo = list(starts)
        while todo:
            rule = todo.pop()
            if rule in self.rules:
                continue
            self.rules[rule] = self.compile_rule(rule)
            todo.extend(n for n in nonterminals(self.rules[rule]) if n not in self.rules)
        return self.rules


def _is_predicate(fn):
    """A parse method annotated `-> bool` is lookahead: it answers a question
    about the tokens ahead (restoring the cursor if it moved it) and reads
    nothing. `_arrow_params_ahead`, `_at_stream_iter` and the rest."""
    return (fn is not None and isinstance(fn.returns, ast.Name)
            and fn.returns.id == "bool")


def _ends(stmts):
    """Whether a block always leaves by `raise` or `return` (syntactically)."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Raise, ast.Return)):
        return True
    if isinstance(last, ast.If):
        return _ends(last.body) and _ends(last.orelse)
    return False


def _constant_strings(node):
    """The strings a literal collection, or a `self.<NAME>` collection on the
    parser class (`self._DURATION_UNITS`), holds; None otherwise."""
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
            return tuple(e.value for e in node.elts)
        return None
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        from . import parser as _parser  # noqa: PLC0415
        value = getattr(_parser.Parser, node.attr, None)
        if isinstance(value, (tuple, list, set, frozenset, dict)) and all(
                isinstance(v, str) for v in value):
            return tuple(sorted(value))
    return None


def _self_method(call):
    func = call.func
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id == "self"):
        return func.attr
    return None


def _describe(arg, env):
    """A call argument as a specialisation descriptor, or None."""
    if (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
            and arg.value.id == "self"):
        return ("methods", (arg.attr,))
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return ("consts", (arg.value,))
    if (isinstance(arg, (ast.Tuple, ast.List)) and arg.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                    for e in arg.elts)):
        return ("consts", tuple(e.value for e in arg.elts))
    if isinstance(arg, ast.Name) and arg.id in env:
        return env[arg.id]
    return None


def _desc_tag(desc):
    if desc[0] == "methods":
        return "_".join(m.strip("_") for m in desc[1])
    return "ops" + hashlib.sha256("\0".join(desc[1]).encode()).hexdigest()[:6]


def nonterminals(expr):
    if expr is None:
        return
    if expr[0] == "n":
        yield expr[1]
    elif expr[0] in ("seq", "alt"):
        for sub in expr[1]:
            yield from nonterminals(sub)
    elif expr[0] == "star":
        yield from nonterminals(expr[1])


def parser_methods(source: str | None = None) -> dict:
    """`{name: FunctionDef}` for every method of `revl.parser.Parser`."""
    tree = ast.parse(source if source is not None else _parser_module_source())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Parser":
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise ValueError("revl.parser defines no Parser class")


# The rules every category starts from (`CATEGORIES` below names them).
_STARTS = ("_parse_program", "stmt", "fn_stmt", "pure_expr", "type_")


def derive(source: str | None = None):
    """`(rules, notes, unguarded)`: every reachable rule, where the compilation
    had to be loose, and each `self.next()` it read as any token.

    Two passes. The first records the guard every call site held; the second
    compiles each rule with the union of its call sites' guards as its entry
    guard, so a method that opens with `self.next()` reads what its callers
    tested for. A rule with any unguarded call site gets no entry guard."""
    methods = parser_methods(source)
    first = _Compiler(methods)
    first.reach(_STARTS)
    entries = {}
    for rule, sites in first.calls.items():
        if sites and all(g is not None for g in sites):
            merged = sites[0]
            for g in sites[1:]:
                merged = _guard_union(merged, g)
            if merged is not None:
                entries[rule] = merged
    second = _Compiler(methods, entries)
    second.specs.update(first.specs)
    second.reach(_STARTS)
    return second.rules, second.notes, second.unguarded


# --------------------------------------------------------------------------
# categories: what a hole can be scoped to
# --------------------------------------------------------------------------

# Each category is the rule a fill starts from. `program` is a whole document;
# the others are the slices a hole-filling decoder is constrained to.
CATEGORIES = {
    "program": ("n", "_parse_program"),
    "component-body": ("star", ("n", "stmt")),
    "statements": ("star", ("n", "fn_stmt")),
    "expression": ("n", "pure_expr"),
    "type": ("n", "type_"),
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _rule_name(name: str, style: str) -> str:
    base = "".join(c if c.isalnum() else "_" for c in name.lower())
    if style == "gbnf":
        return "r-" + base.replace("_", "-")
    return "r_" + base


# The lexical patterns for the token classes the lexer scans by hand. Each one
# is the lexer's rule restated as a pattern; the corpus test is what holds them
# to `revl.lexer`. `lark` is llguidance's regex dialect, `gbnf` a GBNF body.
_LEX = {
    "INT": (r"/0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*/",
            r'("0" [xX] [0-9a-fA-F_]+ | "0" [bB] [01_]+ | "0" [oO] [0-7_]+ | [0-9] [0-9_]*)'),
    "FLOAT": (r"/[0-9][0-9_]*\.[0-9][0-9_]*([eE][+-]?[0-9][0-9_]*)?|[0-9][0-9_]*[eE][+-]?[0-9][0-9_]*/",
              r'([0-9] [0-9_]* "." [0-9] [0-9_]* ([eE] [+-]? [0-9] [0-9_]*)? | [0-9] [0-9_]* [eE] [+-]? [0-9] [0-9_]*)'),
    "STRING": (r'''/"""([^"]|"[^"]|""[^"])*"""|"([^"\\\n]|\\[^\n])*"|'([^'\\\n]|\\[^\n])*'/''',
               r'''("\"\"\"" ([^"] | "\"" [^"] | "\"\"" [^"])* "\"\"\"" | "\"" ([^"\\\n] | "\\" [^\n])* "\"" | "'" ([^'\\\n] | "\\" [^\n])* "'")'''),
}


def _literal(text: str, style: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _term(term, style, used):
    """A token term in the target dialect, recording which shared rules it uses."""
    if term == ANY:
        used.add("any")
        return "any_token" if style != "gbnf" else "any-token"
    _, kind, value = term
    if not isinstance(value, str):
        value = None
    if kind is None or (kind in ("kw", "ident") and value is not None):
        return _literal(value, style)
    if kind == "kw":
        used.add("keyword")
        return "keyword"
    if kind == "ident":
        used.add("ident")
        return "IDENT" if style == "lark" else "ident"
    if kind == "float" and style == "lark":
        return "float"
    if kind in ("int", "float", "string"):
        used.add(kind)
        return kind.upper() if style == "lark" else kind
    if kind == "template":
        used.add("template")
        return "template"
    if kind == "hostbody":
        used.add("hostbody")
        return "host_body" if style != "gbnf" else "host-body"
    if kind == "arrow":
        return _literal("->", style)
    return _literal(kind, style)


def _expr(e, style, used, top=True):
    if e is None:
        return None
    tag = e[0]
    if tag in ("t", "any"):
        out = _term(e, style, used)
        return f"ws {out}" if style == "gbnf" else out
    if tag == "n":
        return _rule_name(e[1], style)
    if tag == "star":
        inner = _expr(e[1], style, used, top=False)
        return f"({inner})*" if style != "ebnf" else f"{{ {inner} }}"
    if tag == "seq":
        if not e[1]:
            return None
        parts = [_expr(x, style, used, top=False) for x in e[1]]
        parts = [p for p in parts if p is not None]
        return " ".join(parts) if parts else None
    if tag == "alt":
        has_eps = EPS in e[1]
        parts = [_expr(x, style, used, top=False) for x in e[1] if x != EPS]
        parts = [p for p in parts if p is not None]
        if not parts:
            return None
        body = " | ".join(parts)
        if has_eps:
            return f"({body})?" if style != "ebnf" else f"[ {body} ]"
        return body if top else f"({body})"
    raise ValueError(f"unknown grammar node {tag!r}")


def _render_rules(rules, start, style, used):
    out = []
    order = [r for r in _reachable(rules, start)]
    for name in order:
        body = _expr(rules[name], style, used)
        lhs = _rule_name(name, style)
        out.append(_production(lhs, body, style))
    return out


def _production(lhs, body, style):
    if body is None:
        body = '""'
    if style == "gbnf":
        return f"{lhs} ::= {body}"
    if style == "lark":
        return f"{lhs}: {body}"
    return f"{lhs} = {body} ;"


def _reachable(rules, start):
    seen = []
    todo = list(nonterminals(start))
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.append(name)
        todo.extend(nonterminals(rules[name]))
    return seen


def _keyword_alt(style):
    from . import lexer as _lexer  # noqa: PLC0415
    return " | ".join(_literal(k, style) for k in sorted(_lexer.KEYWORDS))


def _gbnf_ident_rules():
    """GBNF has no negation: an identifier that is not a keyword is built from
    the trie of keywords. Each trie node is a rule; leaving the trie (a prefix
    no keyword continues) reaches `ident-free`, and a node accepts unless its
    prefix is exactly a keyword."""
    from . import lexer as _lexer  # noqa: PLC0415
    keywords = sorted(_lexer.KEYWORDS)
    prefixes = {k[:i] for k in keywords for i in range(len(k) + 1)}
    cont = "a-zA-Z0-9_"
    lines = ['ident-free ::= [a-zA-Z0-9_]*']

    def node_name(prefix):
        return "ident-" + ("".join(f"{ord(c):02x}" for c in prefix) or "root")

    for prefix in sorted(prefixes):
        nexts = sorted({k[len(prefix)] for k in keywords
                        if k.startswith(prefix) and len(k) > len(prefix)})
        alts = [f'"{c}" {node_name(prefix + c)}' for c in nexts]
        chars = "a-zA-Z_" if prefix == "" else cont
        excluded = "".join(nexts)
        if excluded:
            alts.append(f"[{chars}] ident-free" if False else
                        f"{_gbnf_class_minus(chars, excluded)} ident-free")
        else:
            alts.append(f"[{chars}] ident-free")
        if prefix and prefix not in keywords:
            alts.append('""')
        lines.append(f"{node_name(prefix)} ::= " + " | ".join(alts))
    lines.append("ident ::= ident-root")
    return lines


def _gbnf_class_minus(ranges: str, excluded: str) -> str:
    """A GBNF character class: the characters of `ranges` minus `excluded`."""
    allowed = []
    for spec in ranges.replace("a-z", "abcdefghijklmnopqrstuvwxyz").replace(
            "A-Z", "ABCDEFGHIJKLMNOPQRSTUVWXYZ").replace("0-9", "0123456789"):
        if spec not in excluded and spec not in allowed:
            allowed.append(spec)
    return "[" + "".join(allowed) + "]"


def _shared(style, used):
    """The shared rules and terminals the rendered rules referred to."""
    out = []
    if style == "lark":
        from . import lexer as _lexer  # noqa: PLC0415
        kws = "|".join(sorted(_lexer.KEYWORDS))
        out.append(f"IDENT: /[A-Za-z_][A-Za-z0-9_]*/ & ~/({kws})/")
        out.append(f"keyword: {_keyword_alt(style)}")
        for name in ("INT", "STRING"):
            out.append(f"{name}: {_LEX[name][0]}")
        # A float is a rule over three lexemes, not one lexeme: llguidance's
        # lexer does not back off a longer candidate, so a FLOAT lexeme would
        # swallow the `10.` of `10.checked_div(3)` and then refuse the `c`.
        out.append('float: INT "." FRAC | EXPNUM')
        out.append(r"FRAC: /[0-9][0-9_]*([eE][+-]?[0-9][0-9_]*)?/")
        out.append(r"EXPNUM: /[0-9][0-9_]*[eE][+-]?[0-9][0-9_]*/")
        # A template and a host body are each ONE token to the lexer, so each is
        # one lexeme here: llguidance's lexer applies `%ignore` between lexemes
        # (which would read `//` inside a template or host body as a comment)
        # and never backs off a longer candidate. Brace nesting is bounded at
        # NESTING_DEPTH, which the corpus test holds to the corpus.
        out.append("template: TEMPLATE")
        out.append(f"TEMPLATE: /{_template_regex()}/")
        out.extend(_lark_host_bodies())
        out.append("any_token: IDENT | keyword | INT | float | STRING | template"
                   " | " + " | ".join(_literal(s, style) for s in _symbols()))
        # Whitespace and `//` line comments (revl has no block comments) are
        # ignored between tokens. llguidance applies %ignore only BETWEEN two
        # lexemes, so `start` also reads optional trivia at either end.
        # The alternation is deliberately not wrapped in `( ... )+`: llguidance's
        # lexer refuses `a / b` when the ignore pattern is a repetition that can
        # begin with `/` (measured), and `%ignore` repeats anyway.
        trivia = r"/[ \t\r\n\f]+|\/\/[^\n]*/"
        out.append(f"TRIVIA: {trivia}")
        out.append(f"%ignore {trivia}")
        return out
    if style == "gbnf":
        out.extend(_gbnf_ident_rules())
        out.append(f"keyword ::= {_keyword_alt(style)}")
        for name in ("INT", "FLOAT", "STRING"):
            out.append(f"{name.lower()} ::= {_LEX[name][1]}")
        out.append('template ::= "`" ([^`$] | "$" [^{`] | "${" (r-pure-expr-nw | r-pure-expr-nn) ws "}")* "$"? "`"')
        out.extend(_gbnf_host_bodies())
        out.append("any-word ::= ident | keyword | int | float")
        out.append("any-other ::= string | template | "
                   + " | ".join(_literal(s, style) for s in _symbols()))
        out.append('ws ::= ([ \\t\\r\\n] | "//" [^\\n]*)*')
        out.append('ws1 ::= ([ \\t\\r\\n] | "//" [^\\n]*)+')
        return out
    out.append("(* IDENT: an identifier that is not a keyword; INT, FLOAT, STRING, "
               "template and host_body: the lexer's token classes *)")
    out.append(f"keyword = {_keyword_alt(style)} ;")
    return out


# How deep a `{` nests inside one template interpolation or host body before
# the single-lexeme form below stops following it. The lexer is unbounded; the
# corpus test holds this bound to every document in the tree.
NESTING_DEPTH = 10


def _rx_escape(text: str) -> str:
    return "".join("\\" + c if c in r"\.^$|?*+()[]{}/-" else c for c in text)


def _rx_class_escape(chars: str) -> str:
    return "".join("\\" + c if c in r"\]^-[/" else c for c in chars)


def _trivia_atoms(tv) -> tuple:
    """`(atoms, first_chars)` for one `lexer._Trivia`: the regex alternatives
    for each string and comment form it skips, and the characters any of them
    can start with (which plain text must then exclude)."""
    atoms, first = [], set()
    for opener, closer in tv.block_comments:
        o, c = _rx_escape(opener), _rx_escape(closer)
        # anything up to the first closer; `closer` is two characters here
        body = f"([^{_rx_class_escape(closer[0])}]|{_rx_escape(closer[0])}+[^{_rx_class_escape(closer)}])*"
        atoms.append(f"{o}{body}{_rx_escape(closer[0])}+{_rx_escape(closer[1:])}")
        first.add(opener[0])
    for triple in tv.triples:
        q = _rx_escape(triple[0])
        atoms.append(f"{q}{q}{q}([^{_rx_class_escape(triple[0])}\\\\]|\\\\[\\s\\S]|{q}[^{_rx_class_escape(triple[0])}]|{q}{q}[^{_rx_class_escape(triple[0])}])*{q}{q}{q}")
        first.add(triple[0])
    for quote, mode in tv.strings.items():
        q, qc = _rx_escape(quote), _rx_class_escape(quote)
        # Each form is DETERMINISTIC, as `lexer._skip_trivia` is: a quote always
        # opens a string, which runs to its closing quote (an escaped string
        # also ends at a newline). llguidance never backs off a longer lexeme,
        # so a form that could also end early would leave a body that closes on
        # the same line as a string unable to end at its real `}`.
        if mode == "escape":
            atoms.append(f"{q}([^{qc}\\\\\\n]|\\\\[\\s\\S])*({q}|\\n)")
        elif mode == "raw":
            atoms.append(f"{q}[^{qc}]*{q}")
        else:                                   # "char": a literal, or plain text
            atoms.append(f"{q}(\\\\[^{qc}\\n]*|[^{qc}\\n]){q}")
            atoms.append(q)
        first.add(quote)
    for opener in tv.line_comments:
        atoms.append(f"{_rx_escape(opener)}[^\\n]*")
        first.add(opener[0])
    for c in sorted(first):                     # a comment opener's first char alone
        if c not in tv.strings:
            atoms.append(_rx_escape(c))
    return tuple(atoms), "".join(sorted(first))


def _balanced(tv, depth: int) -> str:
    """A regex for brace-balanced text up to `depth` levels, skipping the
    string and comment forms `tv` names (`lexer._match_brace`'s rule)."""
    atoms, first = _trivia_atoms(tv)
    text = f"[^{{}}{_rx_class_escape(first)}]"
    inner = "(" + "|".join((text,) + atoms) + ")*"
    level = inner
    for _ in range(depth):
        level = "(" + "|".join((text,) + atoms + ("\\{" + level + "\\}",)) + ")*"
    return level


def _template_regex() -> str:
    from . import lexer as _lexer  # noqa: PLC0415
    body = _balanced(_lexer._REVL_TRIVIA, NESTING_DEPTH)
    return f"`([^`$]|\\$[^{{`]|\\$\\{{{body}\\}})*\\$?`"


def _host_groups():
    """`[(names, trivia)]`: the `@backend` names that share one trivia, from
    `lexer._HOST_TRIVIA`, in a stable order."""
    from . import lexer as _lexer  # noqa: PLC0415
    groups: dict = {}
    for name, tv in _lexer._HOST_TRIVIA.items():
        groups.setdefault(id(tv), (tv, []))[1].append(name)
    return [(tuple(sorted(names)), tv) for tv, names in groups.values()]


def _lark_host_bodies():
    from . import lexer as _lexer  # noqa: PLC0415
    out, alts = [], []
    for i, (names, tv) in enumerate(_host_groups() + [((), _lexer._C_FAMILY)]):
        lexeme = f"HOST_BODY_{i}"
        out.append(f"{lexeme}: /\\{{{_balanced(tv, NESTING_DEPTH)}\\}}/")
        if names:
            alts.append('"@" (' + " | ".join(_literal(n, "lark") for n in names)
                        + f") {lexeme}")
        else:                                   # any other backend: C-family
            alts.append(f'"@" (IDENT | keyword) {lexeme}')
    return ["host_body: " + " | ".join(alts)] + out


def _gbnf_host_bodies():
    """GBNF is a character-level CFG, so a host body is a recursive rule there
    (no depth bound); its strings and comments are approximated by the same
    trivia, as plain alternatives."""
    from . import lexer as _lexer  # noqa: PLC0415
    out, alts = [], []
    for i, (names, tv) in enumerate(_host_groups() + [((), _lexer._C_FAMILY)]):
        rule = f"host-inner-{i}"
        quotes = "".join(sorted(tv.strings))
        parts = [f"[^{{}}{_gbnf_class_escape(quotes)}]"]
        for quote in sorted(tv.strings):
            parts.append(f'"{_gbnf_lit_escape(quote)}" [^{_gbnf_class_escape(quote)}]* "{_gbnf_lit_escape(quote)}"')
            parts.append(f'"{_gbnf_lit_escape(quote)}"')
        parts.append(f'"{{" {rule} "}}"')
        out.append(f"{rule} ::= (" + " | ".join(parts) + ")*")
        head = ('"@" (' + " | ".join(_literal(n, "gbnf") for n in names) + ")"
                if names else '"@" ident')
        alts.append(f'{head} [ \\t\\r\\n]* "{{" {rule} "}}"')
    return ["host-body ::= " + " | ".join(alts)] + out


def _gbnf_class_escape(chars: str) -> str:
    return "".join("\\" + c if c in "\\]^-[" else c for c in chars)


def _gbnf_lit_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _symbols():
    from . import lexer as _lexer  # noqa: PLC0415
    singles = [c for c in _lexer.SINGLE_OPERATORS]
    return sorted(set(_lexer.SYMBOLS) | set(_lexer.OPERATORS) | set(singles))


# --------------------------------------------------------------------------
# GBNF: word boundaries without a tokenizer
# --------------------------------------------------------------------------
#
# GBNF is a grammar over characters. With an optional `ws` between tokens,
# `requires` would also read as an identifier `requir` followed by `es`, and
# `componentC` as two words, because nothing forces a boundary between two
# words. So every rule is lowered in four variants, `R[c][l]`: the derivations
# of R written after a token of class `c` and ending in a token of class `l`,
# where a class is W (the token starts or ends with `[A-Za-z0-9_]`) or N. A
# token that starts with a word character, written after one that ended with
# a word character, is preceded by `ws1` (at least one space or comment); every
# other token by `ws`. An empty derivation ends in its context's class.

_WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _token_variants(term):
    """`[(rendered, first, last)]` for one token term in GBNF."""
    if term == ANY:
        return [("any-word", "W", "W"), ("any-other", "N", "N")]
    _, kind, value = term
    if not isinstance(value, str):
        value = None
    if kind is None or (kind in ("kw", "ident") and value is not None):
        text = value
    elif kind in ("kw", "ident"):
        return [({"kw": "keyword"}.get(kind, kind), "W", "W")]
    elif kind in ("int", "float"):
        # the lexer ends a number at the first non-digit, so `30s` is the
        # number 30 then the name `s`: a number needs no break after it. (Two
        # numbers run together are then read as two; that only loosens.)
        return [(kind, "W", "N")]
    elif kind == "string":
        return [("string", "N", "N")]
    elif kind == "template":
        return [("template", "N", "N")]
    elif kind == "hostbody":
        return [("host-body", "N", "N")]
    elif kind == "arrow":
        text = "->"
    else:
        text = kind
    first = "W" if text[0] in _WORD else "N"
    last = "W" if text[-1] in _WORD else "N"
    return [(_literal(text, "gbnf"), first, last)]


class _GbnfLowering:
    """Builds the GBNF rules. An alternative is a tuple of items, each
    ("ref", rule) or ("raw", text); a rule is a list of alternatives. Rules
    that derive nothing are pruned at the end, with every alternative that
    refers to one."""

    def __init__(self, rules):
        self.rules = rules
        self.out: dict = {}
        self.todo: list = []
        self.n = 0
        self._suffixes: dict = {}
        self._stars: dict = {}

    def variant(self, name, c, l):
        vname = f"{_rule_name(name, 'gbnf')}-{c}{l}".lower()
        if vname not in self.out:
            self.out[vname] = []
            self.todo.append((vname, self.rules[name], c, l))
        return vname

    def lower(self, e, c, l):
        """The alternatives for `e` after class `c`, ending in class `l`."""
        if e is None:
            return []
        tag = e[0]
        if tag in ("t", "any"):
            alts = []
            for rendered, first, last in _token_variants(e):
                if last == l:
                    sep = "ws1" if (c == "W" and first == "W") else "ws"
                    alts.append((("raw", sep), ("raw", rendered)))
            return alts
        if tag == "n":
            return [(("ref", self.variant(e[1], c, l)),)]
        if tag == "alt":
            alts = []
            for sub in e[1]:
                for alt_ in self.lower(sub, c, l):
                    if alt_ not in alts:
                        alts.append(alt_)
            return alts
        if tag == "seq":
            return self._seq(tuple(e[1]), c, l)
        if tag == "star":
            return [(("ref", self._star(e[1], c, l)),)]
        raise ValueError(tag)

    def _ref_to(self, alts):
        """One item standing for `alts`: the item itself when there is a single
        one-item alternative, else a helper rule."""
        if len(alts) == 1 and len(alts[0]) == 1:
            return alts[0][0]
        self.n += 1
        name = f"g-{self.n}"
        self.out[name] = alts
        return ("ref", name)

    def _seq(self, items, c, l):
        """A sequence, threaded left to right. Each suffix gets one memoised
        helper per (context, last) pair, so the lowering stays linear in the
        sequence length instead of doubling at every item."""
        if not items:
            return [()] if c == l else []
        head, rest = items[0], items[1:]
        if not rest:
            return self.lower(head, c, l)
        alts = []
        for m in ("W", "N"):
            first = self.lower(head, c, m)
            if not first:
                continue
            tail = self._suffix(rest, m, l)
            if tail is None:
                continue
            alts.append((self._ref_to(first), tail))
        return alts

    def _suffix(self, items, c, l):
        key = (items, c, l)
        if key not in self._suffixes:
            alts = self._seq(items, c, l)
            self._suffixes[key] = self._ref_to(alts) if alts else None
        return self._suffixes[key]

    def _star(self, body, c, l):
        """`(body)*` after `c` ending in `l`: four mutually recursive helpers,
        so the class threads through each repetition."""
        key = (body, c, l)
        if key in self._stars:
            return self._stars[key]
        self.n += 1
        names = {(cc, ll): f"s-{self.n}-{cc}{ll}".lower() for cc in "WN" for ll in "WN"}
        for (cc, ll), name in names.items():
            self._stars[(body, cc, ll)] = name
            self.out[name] = []
        for (cc, ll), name in names.items():
            alts = [()] if cc == ll else []
            for m in ("W", "N"):
                once = self.lower(body, cc, m)
                if once:
                    alts.append((self._ref_to(once), ("ref", names[(m, ll)])))
            self.out[name] = alts
        return self._stars[key]

    def run(self, start):
        root = []
        for l in ("W", "N"):
            root.extend(self.lower(start, "N", l))
        # a template's `${...}` is an expression, written after `${` (class N);
        # the shared `template` rule refers to these two variants by name
        self.keep = [self.variant("pure_expr", "N", "W"),
                     self.variant("pure_expr", "N", "N")]
        while self.todo:
            vname, expr, c, l = self.todo.pop()
            self.out[vname] = self.lower(expr, c, l)
        self.out["root"] = [alt_ + (("raw", "ws"),) for alt_ in root]
        self._prune()

    def _prune(self):
        live: set = set()
        changed = True
        while changed:
            changed = False
            for name, alts in self.out.items():
                if name in live:
                    continue
                if any(all(item[0] == "raw" or item[1] in live for item in alt_)
                       for alt_ in alts):
                    live.add(name)
                    changed = True
        for name in list(self.out):
            if name not in live:
                del self.out[name]
        for name, alts in self.out.items():
            self.out[name] = [alt_ for alt_ in alts
                              if all(item[0] == "raw" or item[1] in live for item in alt_)]
        # keep only what root reaches
        reach, todo = set(), ["root"] + [k for k in self.keep if k in self.out]
        while todo:
            name = todo.pop()
            if name in reach:
                continue
            reach.add(name)
            for alt_ in self.out[name]:
                todo.extend(item[1] for item in alt_ if item[0] == "ref")
        self.out = {k: v for k, v in self.out.items() if k in reach}


def _render_gbnf(rules, start):
    lowering = _GbnfLowering(rules)
    lowering.run(start)
    order = ["root"] + [n for n in lowering.out if n != "root"]
    lines = []
    for name in order:
        alts = [" ".join(item[1] for item in alt_) if alt_ else '""'
                for alt_ in lowering.out[name]]
        lines.append(f"{name} ::= " + " | ".join(alts))
    return lines


HEADER = {
    "lark": "// ", "gbnf": "# ", "ebnf": "(* ",
}


def render(style: str, category: str = "program", derived=None) -> str:
    """The grammar of `category` in `style` ("lark", "gbnf" or "ebnf")."""
    if style not in HEADER:
        raise ValueError(f"unknown grammar format {style!r} (lark, gbnf, ebnf)")
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r} "
                         f"({', '.join(CATEGORIES)})")
    rules, notes, unguarded = derived if derived is not None else derive()
    start = CATEGORIES[category]
    used: set = set()
    if style == "gbnf":
        body = _render_gbnf(rules, start)
        start_expr = None
    else:
        body = _render_rules(rules, start, style, used)
        start_expr = _expr(start, style, used)
    lead = HEADER[style]
    tail = " *)" if style == "ebnf" else ""
    lines = [
        f"{lead}revl source grammar, category `{category}`, format {style}.{tail}",
        f"{lead}GENERATED by `revl grammar` from src/revl/parser.py "
        f"(src/revl/source_grammar.py). Do not edit.{tail}",
        f"{lead}An over-approximation of the parser: every document it accepts "
        f"is in this language.{tail}",
        f"{lead}{len(unguarded)} parser reads are modelled as any token "
        f"(see `revl grammar --notes`).{tail}",
    ]
    if style == "gbnf":
        pass                                     # `root` is the first lowered rule
    elif style == "lark":
        lines.append(f"start: TRIVIA* {start_expr} TRIVIA*")
    else:
        lines.append(f"start = {start_expr} ;")
    lines.extend(body)
    lines.extend(_shared(style, used))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# the committed copies and the drift check
# --------------------------------------------------------------------------

FORMATS = ("lark", "gbnf", "ebnf")
COMMITTED_DIR = "grammar"


def committed_files(root) -> dict:
    """`{path: text}`: what `grammar/` must hold, freshly derived."""
    from pathlib import Path  # noqa: PLC0415
    derived = derive()
    return {Path(root) / COMMITTED_DIR / f"revl.{fmt}": render(fmt, derived=derived)
            for fmt in FORMATS}


def drifted(root) -> list:
    """The committed grammar files that differ from a fresh derivation."""
    return [str(path) for path, text in committed_files(root).items()
            if not path.exists() or path.read_text(encoding="utf-8") != text]


def write(root) -> list:
    out = []
    for path, text in committed_files(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            out.append(str(path))
    return out


def notes_text(derived=None) -> str:
    """Where the derivation is looser than the parser, for `--notes`."""
    rules, notes, unguarded = derived if derived is not None else derive()
    lines = [f"{len(rules)} rules derived from src/revl/parser.py."]
    if unguarded:
        lines.append(f"{len(unguarded)} reads take any token (the condition before "
                     f"them is not a shape the derivation reads):")
        lines.extend(f"  parser.py:{line}  {method}" for method, line in sorted(
            set(unguarded), key=lambda x: (x[1], x[0])))
    loose = sorted((m, w) for m, ws in notes.items() for w in ws
                   if w != "next() under no guard")
    if loose:
        lines.append("Other constructs modelled loosely:")
        lines.extend(f"  {method}: {what}" for method, what in loose)
    return "\n".join(lines) + "\n"
