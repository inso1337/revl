"""Provider synthesis: a provider component built from a service declaration.

Roadmap item 424, gap (c), slice C2 — the `remote` row
(`docs/design/424-dsh-language-gaps.md` §3.2 and §4).

§4 of that note makes a claim worth restating, because it is the reason this
module is small: revl's answers to "a config row", "a mock provider", "a seam
forwarder" and "a remote client" are FOUR KINDS OF ONE FUNCTION, not four
compiler projects. Each synthesizes a provider from a service declaration plus
some parameters, and puts the result in the row table 426 S1 built:

| kind        | synthesized from                          | status              |
|-------------|-------------------------------------------|---------------------|
| `configure` | the decl, the constants, the overrides    | designed, 426 S2    |
| mock        | the decl alone                            | SHIPPED (item 60)   |
| seam        | the decl plus the observer's kind         | designed, 424 B2    |
| `remote`    | the decl plus the peer address            | HERE                |

`synthesize_provider(service, kind, params)` is that function. `remote` is the
kind it ships with; the other three are callers to add, not rewrites.

What a `remote` row promises, and what it refuses to promise
------------------------------------------------------------

A `remote` row names a provider that is OUTSIDE the composition's trust
boundary by construction. So the surface is written to refuse rather than to
pretend, and three refusals matter more than any feature here.

**It synthesizes no inverse, and a remote effect survives unwind.** revl's
teardown stack has exactly three entry kinds
(`docs/design/teardown-contract.md`): `bracket` (an acquire's release,
proof-grade and INFALLIBLE by contract, G5), `transactional` (a witnessed
crossing with a HOST-LOCAL inverse plus a witness captured on the `Ok` branch,
item 243), and `compensation` (audit-grade, best-effort, abort-only, item 247).
A remote call can be neither of the first two: an inverse that travels over a
network is fallible by construction, and a peer's own claim that it undid
something is not a witness — it is one more assertion from the same unchecked
peer. So every synthesized operation is `emission` with NO `undo` and NO
`compensate`, which is G4's other branch, the one that means DECLARED
IRREVERSIBLE. When the local composition unwinds, the remote effect stays.

This is what keeps G7 intact rather than what strains it. G7 is LIFO-complete
over REGISTERED entries; a synthesized remote operation registers none, so
there is nothing for G7 to walk and nothing it can fail to walk. A
`compensation` is the only kind such a call could ever carry, it is
best-effort, and it stays the composing engineer's to write by hand, because
only they know which remote operation undoes which. This module will not guess.

**Withdrawal costs nothing, precisely because there is nothing to undo.**
426 §5.3 files R1 (activation of `replace`/`remove`) as blocked, and the reason
is teardown: withdrawing a wired row means disposing a fiber and replaying its
teardown in the correct LIFO position, which the partial-link path refuses to
do. A `remote` row DOES have a local fiber — the synthesized provider is an
ordinary component and is plugged like one — but that fiber holds no acquired
resource and registers no teardown entry, so disposing it is a pure unwiring:
the provision is withdrawn, consumers re-resolve and deactivate reactively
(R2/R3), and the LIFO replay that blocks R1 is VACUOUS. The expensive half of
R1 is exactly the half a remote row does not have. That is also why D-424c.3
routes transport failure into peer-death withdrawal rather than inventing a
second failure channel: it is the same operation, triggered by the transport
instead of by an operator.

**It re-admits nothing and says nothing about the peer.** Item 337 requires the
RECEIVER to derive both gate inputs from independently held state and re-compile
from its own source; a client sits on the SENDING side and holds no gate over
the callee. So the generated header states what is bounded — the reach, the
capability, the failure mode, all of them LOCAL — and states that nothing
whatever is claimed about what the peer runs. No "verified remote" badge
(D-424c.8).

What this slice projects, and what it refuses
---------------------------------------------

The wire is the CANONICAL one, not a second encoding: the request envelope is
`{"key", "method", "args"}` and the reply `{"ok", "value" | "error"}`, which is
the placement bridge's own envelope (`backends/python/bridge.py:19`) carried
over HTTP. D-424c.5 is a citation, not a new decision, and D-424c.6 says the
server face is a transport rather than a design.

Marshalling the tagged half of that encoding (`{"$kind", "$value"}` for an ADT
or a record) needs `_encode_value`/`_decode_value`, which live in the bridge
and are not reachable from a generated host body. So this slice projects the
JSON-TRANSPARENT SUBSET and refuses the rest naming the method and the type —
the same discipline `revl import a2a` uses for a non-text modality. Building
that projection is C1's job (`revl export client`, buildable today over the same
encoding); a remote row will use it when it lands rather than growing a second
copy.

The `py` tier is likewise the only one emitted, for a reason recorded on
`_py_body`: an `emission` method emits a SYNCHRONOUS function on the ts tier,
and a network round trip is not synchronous.
"""

from __future__ import annotations

import json
import re

# The audited authority helpers (items 416f and 421 F4). A peer address is the
# same class of value an importer's server URL is, so these are reused rather
# than re-derived.
from .crossing_redirect import CROSSING_TIMEOUT, py_policy
from .errors import RevlError
from .import_openapi import _authority_host, _comment_safe
# Item 439: the A2A 1.0.0 version claim and the set of terminal task states live
# in the importer (`revl import a2a`), the sibling entry point onto the same
# protocol. They are imported here rather than re-declared so the version a
# `through a2a` row claims and the terminal states its body accepts cannot drift
# from the importer's. There is no import cycle: `import_a2a` imports the
# openapi helpers and the redirect policy, never this module.
from .import_a2a import A2A_VERSION, _TERMINAL_STATES

#: The kinds `synthesize_provider` knows. `remote` is the one this slice
#: builds; the other three of §4's table are callers to add.
KINDS = ("remote",)

#: The one NAMED transport this slice binds: A2A 1.0.0's `message/send` (item
#: 439). The default wire — `through` omitted, `transport is None` — is the
#: canonical envelope over HTTPS (`{"key","method","args"}` ->
#: `{"ok","value"|"error"}`, the placement bridge's own), and `through a2a`
#: MAPS THAT SAME CANONICAL SEAM ENVELOPE onto the A2A message shape at the
#: boundary: the canonical `method` becomes the `revl.skill` reference, the
#: canonical `args[0]` becomes the message's one text `Part`, and the reply's
#: `value` is extracted from a TERMINAL A2A `Task`/`Message`. Every other
#: `through <name>` is still refused (`check_transport`).
_A2A = "a2a"
BOUND_TRANSPORTS: tuple[str, ...] = (_A2A,)

#: A2A 1.0.0 JSON-RPC posts `message/send` to the agent's endpoint. A remote row
#: carries a bare authority (`check_address` refuses a path or userinfo), so the
#: endpoint is that authority's HTTPS root. An agent served under a path — the
#: `url` an Agent Card carries — is the importer's case (`revl import a2a` reads
#: the full `url` from the card); a `through a2a` row binds the root-endpoint
#: subset and says so in its header.
_A2A_ENDPOINT = "https://%s"

#: Scalars that cross the canonical encoding untagged and unchanged.
_SCALARS = ("Str", "Int", "Int32", "Float", "Bool")

#: A conservative peer authority: `host` or `host:port`, optionally bracketed
#: for IPv6. No scheme, no path, no query, and — checked separately, with its
#: own refusal — no userinfo. The address is interpolated into a generated `//`
#: comment AND into a generated host body, so it is validated against a strict
#: character class up front rather than escaped afterwards (item 416f).
_AUTHORITY_RE = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9\-.]*[A-Za-z0-9])?)"
    r"(?::[0-9]{1,5})?$")

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _snake(name: str) -> str:
    out = re.sub(r"[^0-9A-Za-z]+", "_", str(name))
    return out.strip("_").lower()


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:]
                   for part in _snake(name).split("_") if part)


def cap_token(host: str) -> str:
    """The reach token for a peer host: `net.<token>` (D-424c.10).

    Folded from the HOST alone — never the port, never the userinfo — so two
    credentials against two hosts cannot collapse onto one token and a
    credential can never become part of a capability spelling. A bare IP
    literal folds to something starting with a digit, which the lexer reads as
    a NUMBER with digit separators rather than an identifier, so it is prefixed;
    an ordinary hostname keeps the readable token it had.
    """
    token = _snake(_authority_host(host))
    if not token:
        return "net.peer"
    if not _IDENT_RE.match(token):
        token = f"h_{token}"
    return f"net.{token}" if _IDENT_RE.match(token) else "net.peer"


# ------------------------------------------------------------- type projection

def _type_head(spelling: str) -> tuple[str, list[str]]:
    """`Result[Str, Str]` -> `("Result", ["Str", "Str"])`. Split at the top
    level only, so a nested argument stays whole."""
    text = spelling.strip()
    if not text.endswith("]") or "[" not in text:
        return text, []
    head, rest = text.split("[", 1)
    inner, depth, args = "", 0, []
    for ch in rest[:-1]:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(inner.strip())
            inner = ""
            continue
        inner += ch
    args.append(inner.strip())
    return head.strip(), [a for a in args if a]


def _projectable(spelling: str) -> bool:
    """True for the JSON-transparent subset this slice marshals: a scalar, an
    `Opt` of one, or a `List` of one. Everything else needs the tagged half of
    the canonical encoding, which C1 builds."""
    head, args = _type_head(spelling)
    if not args:
        return head in _SCALARS
    if head in ("Opt", "List") and len(args) == 1:
        return _projectable(args[0])
    return False


def _refuse_type(doc: str, line: int, label: str, op: str, what: str,
                 spelling: str) -> None:
    raise RevlError(
        doc, line,
        f"remote row `@{label}` cannot project {what} of method `{op}`: "
        f"`{spelling}` is not in the JSON-transparent subset",
        hint="this slice marshals scalars (Str, Int, Int32, Float, Bool) and "
             "`Opt`/`List` of them. A record or an ADT needs the tagged half of "
             "the canonical encoding (`{\"$kind\", \"$value\"}`, "
             "docs/interop-bridge.md), which lives in the placement bridge and "
             "is not reachable from a generated host body; `revl export client` "
             "(424 slice C1) builds that projection, and a remote row will use "
             "it rather than grow a second copy")


# ------------------------------------------------------------- admissibility

def check_remotable(service, *, doc: str, line: int, label: str,
                    on_failure: str, on_failure_line: int) -> None:
    """D-424c.2 and D-424c.3: is this service remotable at all?

    **D-424c.2** Every method must declare an emission bound (or be `async`).
    This is not a new rule — it is G4 read at the client. A network call IS a
    boundary crossing, and a provider may be purer than it declares but never
    less pure, so a plain `fn` service is not remotable and the refusal names
    the method.

    **D-424c.3** `on_failure(result)` is admitted only if EVERY method returns
    `Result[T, E]`, because the opt-in is "the failure comes back in band" and
    a method with nowhere to put it cannot honour that. The default,
    `on_failure(withdraw)`, reuses peer-death withdrawal (R2/R3) — the semantics
    the bridges already implement — rather than inventing a second failure
    channel. There is no third option: silently swallowing a transport failure
    has no spelling.
    """
    if not service.methods:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` remotes service `{service.name}`, which "
            "declares no methods",
            hint="a remote row synthesizes one crossing per method; a service "
                 "with none has nothing to remote")

    for op, method in service.methods.items():
        if not method.emission and not method.async_:
            raise RevlError(
                doc, line,
                f"service `{service.name}` is not remotable: method `{op}` is a "
                f"plain `fn` (remote row `@{label}`)",
                hint="every method of a remotable service declares an emission "
                     "bound — `emission fn` or `emission[cap] fn` — or is "
                     "`async`. This is G4 read at the client, not a new rule: a "
                     "network call is a boundary crossing, and a provider may "
                     "be purer than it declares but never less pure "
                     "(424 D-424c.2)")

    if on_failure != "result":
        return
    for op, method in service.methods.items():
        head, args = _type_head(method.returns or "")
        if head != "Result" or len(args) != 2:
            raise RevlError(
                doc, on_failure_line,
                f"`on_failure(result)` on remote row `@{label}` needs every "
                f"method of `{service.name}` to return `Result[T, E]`, and "
                f"`{op}` returns "
                f"{'nothing' if not method.returns else f'`{method.returns}`'}",
                hint="`on_failure(result)` means the transport failure comes "
                     "back IN BAND, so every method needs somewhere to put it. "
                     "Drop the clause to get the default, `on_failure(withdraw)`, "
                     "which reuses peer-death withdrawal (R2/R3); silently "
                     "swallowing a transport failure has no spelling "
                     "(424 D-424c.3)")


# ------------------------------------------------------------- the remote kind

def _py_body(host: str, key: str, op: str, in_band: bool,
             follow_redirects: bool = False) -> str:
    """One crossing, Python tier. The canonical envelope
    (`{"key","method","args"}` -> `{"ok","value"|"error"}`,
    `backends/python/bridge.py:19`) over HTTPS.

    The `py` tier is the only one this slice emits, and that is a refusal
    rather than an oversight. An `emission` method emits a SYNCHRONOUS function
    on the TypeScript tier (`export function f(...): boolean`), and a network
    round trip is not synchronous, so a `fetch`-based body would be `await`
    inside a non-`async` function — code that does not typecheck. Emitting it
    anyway would ship a body that only fails at the tier's own gate. The ts
    projection therefore waits on the async crossing, and a ts-target emit of a
    remote row refuses naming the extern instead of producing broken output.
    """
    url = json.dumps(f"https://{host}/{key}/{op}")
    kj, oj = json.dumps(key), json.dumps(op)
    fail = (
        '        return Err("remote: transport failure")\n'
        if in_band else
        '        # `on_failure(withdraw)`: the failure is a FAULT, never a\n'
        '        # quietly-empty result. Nothing is retried and nothing is\n'
        '        # undone — a remote effect has no local inverse.\n'
        '        raise RuntimeError("remote: transport failure") from _exc\n')
    err = ('        return Err("remote: peer error")\n' if in_band else
           '        raise RuntimeError("remote: peer error")\n')
    ok = "    return Ok(_reply.get(\"value\"))\n" if in_band else \
         "    return _reply.get(\"value\")\n"
    policy = py_policy("remote", follow=follow_redirects)
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
    _payload = _json.dumps({{"key": {kj}, "method": {oj},
                            "args": list(_args)}}).encode()
    _r = _req.Request({url}, data=_payload,
                      headers={{"content-type": "application/json"}})
{policy}    try:
        # A crossing that never returns is not a crossing.
        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) as _resp:
            _reply = _json.loads(_resp.read())
    except _RedirectRefused:
        # NOT a transport failure, and so NOT `on_failure`'s to classify:
        # `on_failure` says what happens when the DECLARED crossing fails, and
        # a redirect is the peer declining to be the declared endpoint at all.
        # Folding it into an in-band `Err` would lose the one diagnostic that
        # says the peer address was contradicted.
        raise
    except Exception as _exc:
{fail}    if not _reply.get("ok"):
{err}{ok}    """


def _check_a2a_method(service, op, method, in_band: bool, *, doc: str,
                      line: int, label: str) -> None:
    """`through a2a` binds A2A 1.0.0's `message/send`, which crosses ONE user
    message — text in, text out (item 439; the same modality `revl import a2a`
    projects, because it is the only one an Agent Card describes well enough to
    bind). So a method remoted over this wire takes exactly one `Str` parameter
    (the message text) and returns `Str` (`Result[Str, Str]` under
    `on_failure(result)`). A richer signature is refused naming the method
    rather than flattened onto the one text `Part` the crossing sends — the
    honesty rule the transport, version and modality checks already keep.
    """
    hint = ("A2A 1.0.0 `message/send` crosses ONE user message: a single text "
            "`Part` out, and text parts back. `through a2a` therefore binds a "
            "method of shape `emission fn <op>(message: Str) -> Str` (or "
            "`-> Result[Str, Str]` under `on_failure(result)`). A parameter or "
            "result that is not `Str` has no A2A `Part` this slice projects and "
            "is refused rather than flattened (item 439; the text-only subset "
            "`revl import a2a` also binds). A non-text modality is a later slice.")
    if len(method.params) != 1:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` over `through a2a` needs method `{op}` of "
            f"service `{service.name}` to take exactly one `Str` message "
            f"parameter, and it takes {len(method.params)}",
            hint=hint)
    pname, ptype = method.params[0]
    if ptype != "Str":
        raise RevlError(
            doc, line,
            f"remote row `@{label}` over `through a2a`: method `{op}`'s "
            f"parameter `{pname}` is `{ptype}`, not the `Str` the A2A message "
            f"text is carried as",
            hint=hint)
    if in_band:
        head, args = _type_head(method.returns or "")
        payload = args[0] if args else ""
        if head != "Result" or payload != "Str":
            raise RevlError(
                doc, line,
                f"remote row `@{label}` over `through a2a` with "
                f"`on_failure(result)` needs method `{op}` to return "
                f"`Result[Str, Str]`, not "
                f"{'nothing' if not method.returns else f'`{method.returns}`'}",
                hint=hint)
    elif (method.returns or "") != "Str":
        raise RevlError(
            doc, line,
            f"remote row `@{label}` over `through a2a` needs method `{op}` to "
            f"return `Str` (the reply text), not "
            f"{'nothing' if not method.returns else f'`{method.returns}`'}",
            hint=hint)


def _py_body_a2a(host: str, op: str, in_band: bool,
                 follow_redirects: bool = False) -> str:
    """One crossing, Python tier, over A2A 1.0.0 JSON-RPC 2.0 `message/send`.

    This is the `through a2a` wire (item 439). It maps the canonical seam
    envelope onto the A2A message shape: the canonical `args[0]` becomes the one
    text `Part` of a `message/send`, the canonical `method` (`op`) rides in the
    message metadata as the `revl.skill` reference the agent may route on, and
    the canonical reply `value` is the text extracted from a TERMINAL `Task` or
    `Message`. Everything the default wire's `_py_body` decides — one crossing,
    redirect-refusing (`crossing_redirect`), time-bound (`CROSSING_TIMEOUT`),
    `on_failure` withdraw/result branching — it decides identically; only the
    payload built and the reply parsed differ.

    The `py` tier is the only one emitted, for the reason `_py_body` states: an
    `emission` method emits a SYNCHRONOUS ts function, and a network round trip
    is not synchronous, so a `fetch` body would be `await` inside a non-`async`
    function. A2A's own async colour (item 80, issue #251) is the importer's
    to declare because it writes the service; a remote row must not recolour a
    `service` it did not write, so it emits the sync `@py` body and the ts
    projection waits on the async crossing.

    Terminal-only: a `message/send` whose task is still `working`,
    `input-required`, `auth-required` or `unknown` is a LIFECYCLE this slice
    does not express (item 439's open question), so the body faults rather than
    polls or resumes.
    """
    url = json.dumps(_A2A_ENDPOINT % host)
    oj = json.dumps(op)
    terminal = json.dumps(list(_TERMINAL_STATES))
    policy = py_policy(_A2A, follow=follow_redirects)

    def fault(indent: int, expr: str, cause: str = "") -> str:
        pad = " " * indent
        if in_band:
            return f"{pad}return Err({expr})\n"
        tail = f" from {cause}" if cause else ""
        return f"{pad}raise RuntimeError({expr}){tail}\n"

    transport_fail = (
        '        # `on_failure(withdraw)`: a transport failure is a FAULT, never\n'
        '        # a quietly-empty result. Nothing is retried and nothing is\n'
        '        # undone — a remote effect has no local inverse.\n'
        if not in_band else "")
    ok = "    return Ok(_text)\n" if in_band else "    return _text\n"
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
    import uuid as _uuid
    # A2A {A2A_VERSION}, JSON-RPC 2.0 `message/send`. ONE crossing. The one
    # canonical arg is the message text; the method name rides as `revl.skill`.
    _message = _args[0]
    _payload = _json.dumps({{
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "message/send",
        "params": {{"message": {{
            "role": "user",
            "messageId": str(_uuid.uuid4()),
            "parts": [{{"kind": "text", "text": _message}}],
            "metadata": {{"revl.skill": {oj}}},
        }}}},
    }}).encode()
    _r = _req.Request({url}, data=_payload,
                      headers={{"content-type": "application/json"}})
{policy}    try:
        # A crossing that never returns is not a crossing.
        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) as _resp:
            _rpc = _json.loads(_resp.read())
    except _RedirectRefused:
        # NOT a transport failure, and so NOT `on_failure`'s to classify: a
        # redirect is the peer declining to be the declared endpoint at all.
        raise
    except Exception as _exc:
{transport_fail}{fault(8, '"a2a: transport failure"', cause="_exc")}    if _rpc.get("error"):
{fault(8, '"a2a: JSON-RPC error %s" % (_rpc["error"].get("code"),)')}    _result = _rpc.get("result")
    if not _result:
{fault(8, '"a2a: response carried no result"')}    _kind = _result.get("kind")
    if _kind == "task":
        _state = (_result.get("status") or {{}}).get("state")
        if _state not in {terminal}:
            # Item 439's open question: a task still in flight is a LIFECYCLE
            # this slice does not express. Fault; never poll, never resume.
{fault(12, '"a2a: task returned non-terminal state %r - this binding crosses once and does not poll" % (_state,)')}        if _state != "completed":
{fault(12, '"a2a: task ended %r" % (_state,)')}        _parts = [p for a in (_result.get("artifacts") or [])
                  for p in (a.get("parts") or [])]
    elif _kind == "message":
        _parts = _result.get("parts") or []
    else:
{fault(8, '"a2a: unexpected result kind %r" % (_kind,)')}    _text = "".join(p.get("text", "") for p in _parts
                    if p.get("kind") == "text" and isinstance(p.get("text"), str))
    if not _text and _parts:
{fault(8, '"a2a: reply carried only non-text parts"')}{ok}    """


def _remote_source(service, params: dict) -> tuple[str, str]:
    label = params["label"]
    key = params["key"]
    host = params["host"]
    realm = params.get("realm")
    capability = params["capability"]
    on_failure = params["on_failure"]
    transport = params.get("transport")
    # `redirect(refuse | same_origin)`, default `refuse`. The peer address is
    # what the operator reads off the row, so a transport that follows a
    # `Location` elsewhere makes the row stop describing the crossing.
    redirect = params.get("redirect", "refuse")
    doc, line = params["doc"], params["line"]
    in_band = on_failure == "result"

    # A `through <wire>` naming an unbound transport is refused before a single
    # line is synthesized: a named wire the body would not speak must not ship
    # under its label (424 D-424c.1, item 439). `through a2a` IS bound and takes
    # the A2A branch below; the default (omitted) wire is the canonical envelope.
    check_transport(transport, doc=doc, line=line, label=label)
    is_a2a = transport == _A2A

    component = f"Remote{_pascal(label)}Provider"
    externs: list[str] = []
    provides: list[str] = []
    for op, method in service.methods.items():
        if method.async_ and not method.emission:
            raise RevlError(
                doc, line,
                f"remote row `@{label}` cannot project `async fn {op}` of "
                f"service `{service.name}`",
                hint="an `async` method IS remotable under 424 D-424c.2; this "
                     "slice's synthesizer projects the single-crossing "
                     "`emission` shape only, and refuses rather than "
                     "approximating an async one. Declare the method "
                     "`emission fn` if the crossing is single, or wait for the "
                     "async projection")
        # `through a2a` binds the text-in/text-out `message/send` subset (item
        # 439); a method that is not that shape is refused before the generic
        # projection checks, so its message names the A2A modality rather than
        # the canonical JSON-transparency it also fails.
        if is_a2a:
            _check_a2a_method(service, op, method, in_band,
                              doc=doc, line=line, label=label)
        for pname, ptype in method.params:
            if not _projectable(ptype):
                _refuse_type(doc, line, label, op, f"parameter `{pname}`", ptype)
        returns = method.returns
        if in_band:
            _head, args = _type_head(returns or "")
            payload, errtype = args[0], args[1]
            if payload not in ("", "Unit") and not _projectable(payload):
                _refuse_type(doc, line, label, op, "the `Ok` payload", payload)
            if errtype != "Str":
                raise RevlError(
                    doc, line,
                    f"remote row `@{label}` needs method `{op}` to return "
                    f"`Result[T, Str]`, not `{returns}`",
                    hint="the synthesized `Err` carries a transport diagnostic, "
                         "which is a `Str`. A richer error type needs the tagged "
                         "half of the canonical encoding (424 slice C1)")
        elif returns and not _projectable(returns):
            _refuse_type(doc, line, label, op, "the return type", returns)

        sig = ", ".join(f"{n}: {t}" for n, t in method.params)
        names = [n for n, _ in method.params]
        arrow = f" -> {returns}" if returns else ""
        extern = f"remote_{label}_{op}"
        # ONE extern per method, all of them carrying the SAME capability
        # token. D-424c.2 sketches one extern per SERVICE; that shape needs an
        # untyped argument vector at the boundary, and the property the decision
        # actually names — "the capability is a single name and the reach is a
        # single bound on the row" — holds either way. Per-method externs keep
        # every argument's declared type at the crossing, so the divergence buys
        # type fidelity and costs nothing the decision asked for. `_args` is the
        # marshalled argument list, bound in the body rather than interpolated
        # per parameter so the envelope is identical for every arity.
        body_src = (_py_body_a2a(host, op, in_band, redirect == 'same_origin')
                    if is_a2a else
                    _py_body(host, key, op, in_band, redirect == 'same_origin'))
        externs.append(
            f"extern emission[{capability}] fn {extern}({sig}){arrow}\n"
            f"  = @py {{\n    _args = [{', '.join(names)}]\n"
            f"{body_src}}}")
        provides.append(f"    fn {op}({', '.join(names)}) = "
                        f"{extern}({', '.join(names)})")

    isolate = f"  isolate {key} in realm(\"{realm}\")\n" if realm else ""
    header = _remote_header(service, label, key, host, capability, on_failure,
                            transport, realm, redirect)
    body = (f"component {component} provides {key}: {service.name} {{\n"
            f"{isolate}  provide {key} {{\n" + "\n".join(provides) + "\n  }\n}")
    return component, "\n\n".join([header, *externs, body]) + "\n"


def _a2a_header_lines() -> list[str]:
    """The `through a2a` header block (item 439). It states the protocol exactly
    (A2A 1.0.0, never bare "A2A", decision (3)), the wire, the terminal subset
    this slice binds, and — the load-bearing one — that the peer is a CLAIM, not
    a checked composition (decision (2); item 329's untrusted-author case)."""
    return [
        f"// Transport: A2A {A2A_VERSION} over JSON-RPC 2.0 (`message/send`)."
        " The peer",
        "//   authority above is the agent's HTTPS endpoint root. The canonical",
        "//   seam envelope is MAPPED onto the A2A message shape: the one `Str`",
        "//   argument becomes the message's single text `Part`, the method name",
        "//   rides as the `revl.skill` metadata reference, and the reply text is",
        "//   read back from a TERMINAL `Task`/`Message`.",
        "//   Version is claimed EXACTLY, never as bare \"A2A\": the protocol",
        "//   moves and a binding that followed it silently would assert a",
        "//   compatibility nobody checked (item 439 decision (3)).",
        "//   SCOPE: a `message/send` whose task reaches a TERMINAL state in that",
        "//   one crossing — the subset where \"does an A2A Task map to one",
        "//   emission, to a stream (item 130), or to a session (item 250)?\"",
        "//   does not arise. A non-terminal reply (`working`, `input-required`,",
        "//   `auth-required`) faults at the boundary; the body never polls,",
        "//   resumes, or guesses. Streaming, gRPC and non-text `Part`s are not",
        "//   this wire (item 439; `revl import a2a` binds the same subset).",
        "//",
        "// THE A2A PEER IS A CLAIM, NOT A CHECKED COMPOSITION. An external agent",
        "//   is not a revl composition, so nothing about it is verified: this row",
        "//   admits, verifies and re-admits NOTHING about the callee (a client is",
        "//   the SENDER, D-424c.8; item 337 requires the RECEIVER to re-compile",
        "//   from its own source). Every A2A provider is item 329's",
        "//   untrusted-author case BY CONSTRUCTION (item 439 decision (2)).",
    ]


def _remote_header(service, label, key, host, capability, on_failure,
                   transport, realm, redirect="refuse") -> str:
    safe_host = _comment_safe(host)
    lines = [
        f"// SYNTHESIZED for remote row `@{label}` — this file is not on disk.",
        f"// Item 424 D-424c.1, slice C2. Service: `{service.name}`, "
        f"key `{_comment_safe(key)}`"
        + (f", realm `{_comment_safe(realm)}`." if realm else "."),
        f"// Peer: {safe_host}   Reach: `{capability}`",
        "//   The capability token is folded from the HOST alone — never the",
        "//   port, never userinfo. A credential in an address is a live secret,",
        "//   not an identifier, and must never become part of a capability",
        "//   spelling (item 424 D-424c.10, roadmap 421 F4).",
    ]
    if transport == _A2A:
        lines += _a2a_header_lines()
    elif transport:
        lines.append(f"// Transport requested: `{_comment_safe(transport)}`.")
    lines += [
        f"// Redirect: `{redirect}`. THE PEER ADDRESS ABOVE IS THE ADDRESS.",
        "//   `urllib` follows a redirect by default and re-issues a 301/302/303",
        "//   POST as a GET with the body dropped, to whatever host `Location`",
        "//   names — so the address on this row would stop describing where the",
        "//   crossing goes, the declared emission would become a read, and every",
        "//   header on the request (a credential, a `Secret[T]`) would travel to",
        "//   an origin nothing declared. The body below refuses instead, naming",
        "//   the rule and reporting only the target's ORIGIN.",
        ("//   `redirect(same_origin)` is declared on this row: a 307 or 308 that "
         "stays"
         if redirect == "same_origin" else
         "//   Write `redirect(same_origin)` on the row to allow a 307 or 308 "
         "that stays"),
        "//   on the declared origin is followed with its method and body intact,",
        "//   at most five hops. A 301, 302 or 303 is refused either way, and so",
        "//   is any cross-origin hop.",
        "//   The refusal is a FAULT even under `on_failure(result)`: a redirect",
        "//   is the peer declining to be the declared endpoint, not a failure of",
        "//   the declared crossing.",
        f"// Timeout: {CROSSING_TIMEOUT}s. A peer that accepts the connection and "
        "then says",
        "//   nothing is a fault, not a wait.",
    ]
    lines.append("// Tier: `py` only — an `emission` method emits a SYNCHRONOUS "
                 "ts function,")
    lines.append("//   and a network round trip is not synchronous. The ts "
                 "projection waits")
    lines.append("//   on the async crossing rather than shipping a body that "
                 "does not typecheck.")
    lines += [
        "//",
        "// THE WIRING IS LOCAL. Every consumer keeps `requires "
        f"{_comment_safe(key)}: {service.name}`",
        "// and G2, G3 and G4 are unchanged. Remoteness is an ADMISSION fact — a",
        "// reach, a capability, a failure mode — and never a wiring fact, so",
        "// bringing this provider back in-process is a one-line composition edit",
        "// and not a source edit across every consumer (D-424c.1; the rule",
        "// docs/interop-bridge.md §3 already states as \"manifest data, not",
        "// source text\").",
        "//",
        "// THIS FILE MAKES NO CLAIM ABOUT WHAT THE PEER RUNS. A remote row does",
        "// not admit, verify or re-admit the callee, and there is no \"verified",
        "// remote\" badge to be had: item 337 requires the RECEIVER to re-compile",
        "// from its own independently held source, and a client is the SENDER",
        "// (D-424c.8). What IS bounded is local and only local: the reach, the",
        "// capability and the failure mode above. If both sides are revl and both",
        "// want a mutual guarantee, that is `revl contract export` / `revl",
        "// contract check`, or 337's seam — not this row.",
        "//",
        "// NO INVERSE IS SYNTHESIZED, AND A REMOTE EFFECT SURVIVES UNWIND.",
        "//   * `bracket` needs an INFALLIBLE inverse (G5). An inverse that",
        "//     travels over a network is fallible by construction: the peer may",
        "//     be unreachable, restarted, or gone by teardown time.",
        "//   * `transactional` (item 243) needs a HOST-LOCAL inverse and a",
        "//     witness captured on the `Ok` branch. The peer's state is not",
        "//     host-local, and the peer's own claim that it undid something is",
        "//     not a witness — it is one more assertion from the same peer.",
        "// So every operation below is `emission` with no `undo` and no",
        "// `compensate`: G4's other branch, DECLARED IRREVERSIBLE. G7 stays",
        "// LIFO-complete because a remote operation registers NO teardown entry,",
        "// so there is nothing for G7 to walk. A `compensation` (item 247,",
        "// audit-grade, best-effort) is the only kind such a call could carry and",
        "// it is the composing engineer's to write by hand: only they know which",
        "// remote operation undoes which, and this synthesizer will not guess.",
        "//",
    ]
    if on_failure == "result":
        lines += [
            "// ON FAILURE: `result`. A transport failure comes back IN BAND as",
            "// `Err`, admitted because every method returns `Result[T, Str]`.",
            "// The provider is NOT withdrawn, so a wedged peer stays wired and",
            "// every call keeps paying for the round trip — which is the cost of",
            "// the opt-in, and the reason it is not the default (D-424c.3).",
        ]
    else:
        lines += [
            "// ON FAILURE: `withdraw` (the default). A transport failure raises a",
            "// FAULT rather than a quietly-empty result. The intended settlement",
            "// is peer-death withdrawal — the provider is withdrawn and every",
            "// consumer deactivates reactively, R2/R3, the semantics the",
            "// placement bridges already implement (docs/network-path.md) — and",
            "// D-424c.3 reuses it deliberately rather than opening a second",
            "// failure channel. See docs/composition-rows.md for what is declared",
            "// here and what the runtime does not yet wire.",
        ]
    lines += [
        "//",
        "// Values are NOT tainted here. Item 424 D-424c.9 requires every value a",
        "// remote provider returns to be `Untrusted[T]`, and that is slice C3,",
        "// not this one. Until it lands, a value that crossed this boundary is",
        "// indistinguishable at a call site from a local one — which is exactly",
        "// the hole D-424c.9 exists to close. Treat it as such.",
    ]
    return "\n".join(lines)


def synthesize_provider(service, kind: str, params: dict) -> tuple[str, str]:
    """Synthesize a provider component for `service`.

    Returns `(component_name, source_text)`. The text is ordinary revl source
    and is compiled by the ordinary compiler, which is the whole soundness
    argument: nothing here is trusted. `_link` still runs G2, G3 and G4 over the
    result, so a bug in this module can only refuse something admissible or
    produce source the compiler then refuses — never admit something `_link`
    would not (the same argument 426 §3.3 makes for the resolver).
    """
    if kind != "remote":
        raise ValueError(
            f"unknown provider kind {kind!r}; this slice ships "
            f"{', '.join(repr(k) for k in KINDS)}")
    return _remote_source(service, params)


def check_transport(transport: str | None, *, doc: str, line: int,
                    label: str) -> None:
    """D-424c.1's `through <wire>` names the transport a remote row crosses.

    The synthesizer speaks two wires. The default — `through` OMITTED — is the
    canonical envelope over HTTPS (`{"key","method","args"}` ->
    `{"ok","value"|"error"}`, the placement bridge's own). The one NAMED wire is
    `through a2a` (item 439): A2A 1.0.0's `message/send`, which maps that same
    canonical envelope onto the A2A message shape at the boundary. Any OTHER
    `through <name>` is refused naming the transport and the row rather than
    emitted as a wire the generated body would not actually speak.

    This is the honesty rule the version, redirect and modality checks already
    keep, restated for the transport axis: a named wire the body would not speak
    must not ship under its label. `through a2a` is honoured because the body
    below now genuinely speaks it; `through grpc`, say, is refused because it
    does not — gRPC is a binary transport over HTTP/2 with protobuf framing, not
    the JSON POST this synthesizer emits.

    `through a2a` binds only the subset where item 439's load-bearing open
    question — does an A2A Task map to one emission, to a stream (item 130), or
    to a session (item 250)? — does not arise: a `message/send` whose task
    reaches a TERMINAL state in that one crossing. A non-terminal reply is a
    fault at the boundary; the generated body refuses to poll or resume (the
    same subset `revl import a2a` binds).
    """
    if transport is None or transport in BOUND_TRANSPORTS:
        return
    raise RevlError(
        doc, line,
        f"remote row `@{label}` names transport `{transport}` with `through`, "
        "but the synthesizer binds no transport by that name",
        hint="omit `through` for the default wire — the canonical envelope over "
             'HTTPS (`{"key","method","args"}` -> `{"ok","value"|"error"}`, '
             "docs/composition-rows.md) — or write `through a2a` for A2A 1.0.0's "
             "`message/send` (item 439). No other named transport is bound: "
             "refusing a named wire the generated body would not actually speak "
             "is the honesty rule the version and redirect checks already keep "
             "(424 D-424c.1, item 439)")


def check_address(host: str, *, doc: str, line: int, label: str) -> None:
    """A peer address is a bare authority: `host` or `host:port`, and never a
    URL carrying userinfo.

    D-424c.10 says the reach bound is derived from host and port only, never
    from a URL containing userinfo, and 421 F4 is the shape it is guarding
    against: a credential riding into a capability spelling. The fail-closed
    reading of that is to refuse the address outright rather than to accept it
    and quietly drop the credential — dropping it would leave a live secret
    written in a composition document, which is a worse place for it than a URL.
    """
    if "@" in host:
        raise RevlError(
            doc, line,
            f"the peer address of remote row `@{label}` carries userinfo",
            hint="a credential in an address is a live secret, not an "
                 "identifier: it must never reach a capability spelling, a "
                 "ticket, a WAL record or a composition document (424 D-424c.10, "
                 "roadmap 421 F4). Write the bare `host:port` and supply the "
                 "credential through a capability-bound secret (item 256)")
    if "://" in host or "/" in host:
        raise RevlError(
            doc, line,
            f"the peer address of remote row `@{label}` is a URL, not an "
            "authority",
            hint='write `at host("billing.internal:8443")` — the scheme and the '
                 "path are the transport's, and the reach bound is derived from "
                 "the host and port alone (424 D-424c.10)")
    if not _AUTHORITY_RE.match(host):
        raise RevlError(
            doc, line,
            f"`{_comment_safe(host)}` is not a usable peer address for remote "
            f"row `@{label}`",
            hint="a peer address is `host` or `host:port` (an IPv6 literal in "
                 "brackets). It is interpolated into generated source, so it is "
                 "validated up front rather than escaped afterwards")
