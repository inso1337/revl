"""What a competent Python developer writes BY HAND for the same semantics as
each `programs/*.rvl` benchmark.

This is the yardstick of the codegen audit. Emitter waste is the gap between
the emitted module and this file, not an absolute number, so every function
here must keep the revl semantics exactly:

  * `Int` is bounded 64-bit and overflow TRAPS (docs/arithmetic.md), so the
    hand-written arithmetic keeps a bound check. It is written INLINE, as a
    comparison against two module constants, rather than as a helper call.
  * `%` is the TRUNCATED remainder (sign of the dividend), `div_trunc`
    truncates and `div_euclid` is Euclidean. These are written as module-level
    `def`s, defined once, not as lambdas rebuilt at each evaluation.
  * `List` and `Map` have VALUE semantics. Where a function's result is a fresh
    collection that no caller can alias into the input, a local mutable buffer
    is the ordinary Python spelling of the same value, so `append` replaces
    copy-on-push. Where a value really is shared (`drop_keys` returns a map the
    caller may hold alongside the input), the hand-written form copies too.
  * A record is one representation, not two: the hand-written code reads fields
    off dicts with `[]` because the benchmark data are record literals.

Each function pairs with the identically named function in the emitted module.
"""

I64_MIN = -(2 ** 63)
I64_MAX = 2 ** 63 - 1


def _trap(v):
    raise OverflowError('revl: Int overflow')


# --------------------------------------------------------------- list_build
def build(n):
    out = []
    i = 0
    while i < n:
        out.append(i)
        i = i + 1 if I64_MIN <= i + 1 <= I64_MAX else _trap(i)
    return out


# --------------------------------------------------------------- transforms
def doubled(xs):
    return [v * 2 if I64_MIN <= v * 2 <= I64_MAX else _trap(v) for v in xs]


def _rem_trunc(a, b):
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def evens(xs):
    return [v for v in xs if _rem_trunc(v, 2) == 0]


def total_reduce(xs):
    acc = 0
    for v in xs:
        acc = acc + v
        if not (I64_MIN <= acc <= I64_MAX):
            _trap(acc)
    return acc


def pipeline(xs):
    acc = 0
    for v in xs:
        w = v + 1
        if not (I64_MIN <= w <= I64_MAX):
            _trap(w)
        if w > 2:
            acc = acc + w
            if not (I64_MIN <= acc <= I64_MAX):
                _trap(acc)
    return acc


def _str_lt(a, b):
    la, lb = len(a), len(b)
    i = 0
    while i < la and i < lb:
        ca, cb = ord(a[i]), ord(b[i])
        if ca < cb:
            return True
        if ca > cb:
            return False
        i += 1
    return la < lb


def list_sort(xs):
    # Same algorithm as stdlib/list.rvl's insertion-by-first-greater sort, with
    # the per-step list REBUILD replaced by an in-place insert. The revl source
    # is quadratic by construction; the emitted `res = res + [y]` makes it cubic.
    out = []
    for x in xs:
        placed = False
        res = []
        for y in out:
            if not placed and _str_lt(x, y):
                res.append(x)
                placed = True
            res.append(y)
        if not placed:
            res.append(x)
        out = res
    return out


# -------------------------------------------------------------------- arith
def sum_squares(n):
    total = 0
    i = 0
    while i < n:
        sq = i * i
        if not (I64_MIN <= sq <= I64_MAX):
            _trap(sq)
        d = sq - i
        if not (I64_MIN <= d <= I64_MAX):
            _trap(d)
        total = total + d
        if not (I64_MIN <= total <= I64_MAX):
            _trap(total)
        i += 1
        if not (I64_MIN <= i <= I64_MAX):
            _trap(i)
    return total


# ------------------------------------------------------------------- divmod
def _div_trunc(a, b):
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _div_euclid(a, b):
    return a // b if b > 0 else -(a // -b)


def churn(n):
    acc = 0
    i = 1
    while i < n:
        acc += _rem_trunc(i, 7)
        acc += _div_trunc(i, 3)
        acc += _div_euclid(i, 5)
        if not (I64_MIN <= acc <= I64_MAX):
            _trap(acc)
        i += 1
    return acc


# ------------------------------------------------------------------ records
def total(ps):
    t = 0
    for p in ps:
        t = t + p['x'] + p['y']
        if not (I64_MIN <= t <= I64_MAX):
            _trap(t)
    return t


def shift_all(ps, d):
    out = []
    for p in ps:
        q = dict(p)
        x = p['x'] + d
        if not (I64_MIN <= x <= I64_MAX):
            _trap(x)
        q['x'] = x
        out.append(q)
    return out


# --------------------------------------------------------------------- maps
def fill(ks):
    m = {}
    for i, k in enumerate(ks):
        m[k] = i
    return m


def drop_keys(m, ks):
    # Value semantics kept: the caller's `m` is never mutated. One copy, then
    # in-place deletes, instead of one full rebuild per key.
    out = dict(m)
    for k in ks:
        out.pop(k, None)
    return out


def probe(m, ks):
    acc = 0
    for k in ks:
        v = m.get(k)
        acc += 0 if v is None else v
    return acc


# ------------------------------------------------------------------ strings
def _index_of(v, n):
    return v.find(n) if isinstance(v, str) else (v.index(n) if n in v else -1)


def scan(hay, needle, n):
    acc = 0
    i = 0
    while i < n:
        acc += _index_of(hay, needle)
        i += 1
    return acc


def split_join(csv, n):
    acc = 0
    i = 0
    while i < n:
        acc += len(csv.split(','))
        i += 1
    return acc


# ---------------------------------------------------------------- matching
# `Opt[T]` is `T | None` at runtime, so a two-arm match over Some/None is an
# ordinary conditional. No closure is built and none is called.
def unwrap_or(o, d):
    return o if o is not None else d


def sum_present(xs):
    t = 0
    for o in xs:
        t = t + (o if o is not None else 0)
        if not (I64_MIN <= t <= I64_MAX):
            _trap(t)
    return t


def classify(o):
    if o is None:
        return 0
    v = o + 1
    if not (I64_MIN <= v <= I64_MAX):
        _trap(v)
    return v
