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
from . import a2a_boundary, a2a_task
from .crossing_redirect import CROSSING_TIMEOUT, py_policy
from .errors import RevlError
from .import_openapi import _authority_host, _comment_safe
# Item 439: the A2A 1.0.0 version claim and the set of terminal task states live
# in the importer (`revl import a2a`), the sibling entry point onto the same
# protocol. They are imported here rather than re-declared so the version a
# `through a2a` row claims and the terminal states its body accepts cannot drift
# from the importer's. There is no import cycle: `import_a2a` imports the
# openapi helpers and the redirect policy, never this module.
from .import_a2a import A2A_VERSION, _HTTPJSON_SEND_PATH, _TERMINAL_STATES

#: The kinds `synthesize_provider` knows. `remote` (slice C2), `seam`
#: (slice B2) and `host` (item 457 S3) are the three this module builds;
#: `configure` and item 60's mock are the other two of §4's table.
KINDS = ("remote", "seam", "host")

#: item 457 S3: the shipped host shims, keyed by the service a `host` row hosts.
#: Each entry names the `@ts ref` module (a `.ts` file in the revl tree, jailed
#: to the install root as an install-origin ref, item 410) that exports one
#: symbol per service method, and the Cordis package that module wraps. The
#: `host` row's provider is synthesized to bind each method to a `@ts ref` of
#: that module — the reviewed host-reference door (item 396 option B), never a
#: `globalThis` bridge (design note 530's question, answered by the row table).
#: A service with no entry here has no shipped shim and a `host` row for it is
#: refused naming the service; a tier with no ref door (`hostref.EXTERN_REF_TIERS`
#: is `py`/`ts` only) refuses the synthesized ref at lower time, which is the
#: per-tier shim refusal design 457 S3 describes riding existing machinery.
#: `stdlib/auth.rvl`'s `Auth` shim over `@cordisjs/plugin-sso` lands with item
#: 457 S2 (the opaque `Principal` machinery); its entry is added there.
HOST_SHIMS: dict[str, dict[str, str]] = {
    "Server": {
        "module": "../backends/typescript/revl_server_ts.ts",
        "package": "@cordisjs/server",
    },
}

#: The seam kinds that carry an observer (D-424b.4). `rewrite` is DELIBERATELY
#: not here: it has no spelling anywhere in the grammar, because argument
#: substitution between describe and execute is roadmap 427 F2's approve-one-
#: run-another shape and F2 is still unfixed (D-424b.4). The parser refuses the
#: word before this set is ever consulted; it is named here so the refusal has
#: one place to point.
SEAM_KINDS = ("observe", "decide")

#: The two-armed decision an `observe`-vs-`decide` observer returns. A `decide`
#: seam is admitted only on a method returning `Result[T, E]` (D-424b.4): a
#: `Deny` becomes that method's `Err`, so a method with nowhere to put an `Err`
#: cannot carry one. `observe` returns nothing and the forwarder ignores it.
_DECISION = "Decision"

#: The one NAMED transport this slice binds: A2A 1.0.0's `message/send` (item
#: 439). The default wire — `through` omitted, `transport is None` — is the
#: canonical envelope over HTTPS (`{"key","method","args"}` ->
#: `{"ok","value"|"error"}`, the placement bridge's own), and `through a2a`
#: MAPS THAT SAME CANONICAL SEAM ENVELOPE onto the A2A message shape at the
#: boundary: the canonical `method` becomes the `revl.skill` reference, the
#: canonical `args[0]` becomes the message's one text `Part`, and the reply's
#: `value` is extracted from a TERMINAL A2A `Task`/`Message`. Every other
#: `through <name>` is still refused (`check_transport`).
#: The two NAMED A2A sub-transports this slice binds. Both are a POST of a JSON
#: body to the agent's endpoint, exactly the two JSON-body transports A2A 1.0.0
#: defines and `revl import a2a` already binds — so a `through a2a` row and a
#: card the importer reads speak the SAME wire, JSON-RPC or REST:
#:   * `through a2a`      -> A2A over JSON-RPC 2.0 `message/send` (the default);
#:   * `through a2a_rest` -> A2A over HTTP+JSON/REST `POST /v1/message:send`.
#: gRPC is the third transport A2A 1.0.0 defines and it is NOT bound: it is a
#: binary transport over HTTP/2 with protobuf framing, not the JSON POST this
#: synthesizer emits, so it cannot ship under either label (`check_transport`).
_A2A = "a2a"
_A2A_REST = "a2a_rest"
_A2A_TRANSPORTS: tuple[str, ...] = (_A2A, _A2A_REST)
BOUND_TRANSPORTS: tuple[str, ...] = (_A2A, _A2A_REST)

#: A2A 1.0.0 JSON-RPC posts `message/send` to the agent's endpoint. A remote row
#: carries a bare authority (`check_address` refuses a path or userinfo), so the
#: endpoint is that authority's HTTPS root. An agent served under a path — the
#: `url` an Agent Card carries — is the importer's case (`revl import a2a` reads
#: the full `url` from the card); a `through a2a` row binds the root-endpoint
#: subset and says so in its header. The REST binding appends A2A 1.0.0's REST
#: method path (`_HTTPJSON_SEND_PATH`, imported from the sibling so the two
#: entry points cannot drift) to that same root.
_A2A_ENDPOINT = "https://%s"

#: Scalars that cross the canonical encoding untagged and unchanged.
_SCALARS = ("Str", "Int", "Int32", "Float", "Bool")

#: The two A2A `Part` modalities this slice projects, and the revl type each is
#: carried as. A `Str` becomes a text `Part` (`{"kind":"text","text":...}`); a
#: `Bytes` becomes a file `Part` with INLINE base64 bytes
#: (`{"kind":"file","file":{"bytes":...}}`). A `DataPart` (arbitrary structured
#: JSON) has no revl spelling here — it needs the tagged half of the canonical
#: encoding, which lives in the placement bridge and is C1's (`revl export
#: client`) to project — so it is refused rather than flattened, the same
#: honesty line the transport and version checks keep.
_A2A_MODALITY = {"Str": "text", "Bytes": "file"}

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

def _transport_fault_class(label: str, op: str, indent: int = 4) -> str:
    """Source for the inline `TransportFault` class a synthesized `@py` remote
    body raises under `on_failure(withdraw)` (item 439 T0, issue #118).

    The emitted module cannot import the runtime's `TransportFault`, so — like
    `_RedirectRefused` in `crossing_redirect.py:py_policy` — the fault is a
    small class defined inline. It carries the `_revl_transport_fault` MARKER
    the activation runtime keys on (never on class identity, so the exec'd
    module and the runtime share one contract without an import) plus the row
    label and the crossing that failed, so the runtime knows which provider to
    withdraw and how to attribute the cascade. It subclasses `RuntimeError` so a
    caller catching `RuntimeError` still sees it — the settlement the terminal
    single-crossing wire has always had."""
    pad = " " * indent
    rq, oq = json.dumps(label), json.dumps(op)
    return (
        f"{pad}class TransportFault(RuntimeError):\n"
        f"{pad}    # item 439 T0: a crossing fault under `on_failure(withdraw)`\n"
        f"{pad}    # WITHDRAWS the provider (the runtime keys on the marker).\n"
        f"{pad}    _revl_transport_fault = True\n"
        f"{pad}    _revl_row = {rq}\n"
        f"{pad}    _revl_crossing = {oq}\n")


def _py_body(host: str, key: str, op: str, in_band: bool,
             follow_redirects: bool = False, *, label: str = "") -> str:
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
    fault_class = "" if in_band else _transport_fault_class(label, op)
    fail = (
        '        return Err("remote: transport failure")\n'
        if in_band else
        '        # `on_failure(withdraw)`: the failure is a FAULT, never a\n'
        '        # quietly-empty result. Nothing is retried and nothing is\n'
        '        # undone — a remote effect has no local inverse. Item 439 T0:\n'
        '        # the activation runtime WITHDRAWS the provider on this fault.\n'
        '        raise TransportFault("remote: transport failure") from _exc\n')
    err = ('        return Err("remote: peer error")\n' if in_band else
           '        raise TransportFault("remote: peer error")\n')
    ok = "    return Ok(_reply.get(\"value\"))\n" if in_band else \
         "    return _reply.get(\"value\")\n"
    policy = py_policy("remote", follow=follow_redirects)
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
{fault_class}    _payload = _json.dumps({{"key": {kj}, "method": {oj},
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
                      line: int, label: str, transport: str) -> tuple[str, str]:
    """`through a2a` binds A2A 1.0.0's `message/send`, which crosses ONE `Part`
    in and reads the `Part`s of one TERMINAL reply back (item 439; the same
    single-crossing subset `revl import a2a` binds). So a method remoted over
    this wire takes exactly one parameter and returns one value, and each is one
    of the two `Part` modalities this slice projects:

      * a `Str` is a TEXT `Part` (`{"kind":"text","text":...}`);
      * a `Bytes` is a FILE `Part` with INLINE base64 bytes
        (`{"kind":"file","file":{"bytes":...}}`).

    Under `on_failure(result)` the return is `Result[Str, Str]` or
    `Result[Bytes, Str]` — the transport diagnostic in the `Err` is always a
    `Str`. Anything else (more than one parameter, a `DataPart`'s structured
    JSON, a non-`Str` error) has no `Part` this slice projects and is refused
    naming the method rather than flattened — the honesty rule the transport,
    version and terminal-state checks already keep.

    Returns `(in_modality, out_modality)`, each `"text"` or `"file"`, so the
    body builder marshals the right `Part` on each side.
    """
    hint = ("A2A 1.0.0 `message/send` crosses ONE `Part`: `through a2a` binds a "
            "method of shape `emission fn <op>(m: Str|Bytes) -> Str|Bytes` (or "
            "`-> Result[Str|Bytes, Str]` under `on_failure(result)`). A `Str` "
            "is carried as a text `Part`, a `Bytes` as a file `Part` with inline "
            "base64 bytes. A `DataPart` (arbitrary structured JSON) needs the "
            "tagged half of the canonical encoding (424 slice C1) and is refused "
            "rather than flattened; more than one parameter has no single `Part` "
            "to become (item 439).")
    if len(method.params) != 1:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` over `through {transport}` needs method "
            f"`{op}` of service `{service.name}` to take exactly one message "
            f"parameter, and it takes {len(method.params)}",
            hint=hint)
    pname, ptype = method.params[0]
    if ptype not in _A2A_MODALITY:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` over `through {transport}`: method `{op}`'s "
            f"parameter `{pname}` is `{ptype}`, not a `Str` (a text `Part`) or a "
            f"`Bytes` (a file `Part`)",
            hint=hint)
    in_modality = _A2A_MODALITY[ptype]
    if in_band:
        head, args = _type_head(method.returns or "")
        payload = args[0] if len(args) == 2 else ""
        if head != "Result" or payload not in _A2A_MODALITY or args[1] != "Str":
            raise RevlError(
                doc, line,
                f"remote row `@{label}` over `through {transport}` with "
                f"`on_failure(result)` needs method `{op}` to return "
                f"`Result[Str, Str]` or `Result[Bytes, Str]`, not "
                f"{'nothing' if not method.returns else f'`{method.returns}`'}",
                hint=hint)
        out_modality = _A2A_MODALITY[payload]
    else:
        ret = method.returns or ""
        if ret not in _A2A_MODALITY:
            raise RevlError(
                doc, line,
                f"remote row `@{label}` over `through {transport}` needs method "
                f"`{op}` to return `Str` (the reply text) or `Bytes` (a reply "
                f"file `Part`), not {'nothing' if not ret else f'`{ret}`'}",
                hint=hint)
        out_modality = _A2A_MODALITY[ret]
    return in_modality, out_modality


def _task_hint() -> str:
    return (
        "a `long_running` A2A row projects the four-op Task lifecycle "
        "(docs/design/439-a2a-task-lifecycle.md T1), so its service declares one "
        "or more complete quadruples sharing a base name:\n"
        "  emission fn <base>_start(message: Str) -> TaskRef\n"
        "  emission fn <base>_poll(task: TaskRef) -> TaskEvent\n"
        "  emission fn <base>_reply(task: TaskRef, message: Str) -> TaskEvent\n"
        "  emission fn <base>_cancel(task: TaskRef) -> Unit\n"
        "with `TaskRef`/`TaskState`/`TaskEvent` from `stdlib/a2a.rvl`. Under the "
        "default `on_failure(withdraw)` the returns are the bare vocabulary "
        "types, exactly as the terminal wire returns a bare `Str`.")


def _check_task_method(method, base: str, suffix: str, *, doc: str, line: int,
                       label: str, service_name: str) -> None:
    """One method of a `long_running` service must match the four-op shape its
    suffix names (item 439 T1). The signature IS the contract the four bodies
    marshal, so a mismatch is refused naming the method rather than projected
    onto a wire it does not fit."""
    op = a2a_task.op_name(base, suffix)
    want_params = a2a_task.PARAMS[suffix]
    want_ret = a2a_task.RETURN_TYPE[suffix]
    got_params = [t for _n, t in method.params]
    if got_params != [t for _n, t in want_params]:
        want = ", ".join(f"{n}: {t}" for n, t in want_params)
        raise RevlError(
            doc, line,
            f"remote row `@{label}` (`long_running`): method `{op}` of service "
            f"`{service_name}` must take ({want}), not "
            f"({', '.join(got_params) or 'no parameters'})",
            hint=_task_hint())
    if (method.returns or "") != want_ret:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` (`long_running`): method `{op}` of service "
            f"`{service_name}` must return `{want_ret}`, not "
            f"{'nothing' if not method.returns else f'`{method.returns}`'}",
            hint=_task_hint())


def _classify_task_ops(service, *, doc: str, line: int, label: str) -> list:
    """Group a `long_running` service's methods into complete four-op quadruples
    (item 439 T1). Returns `[(base, {suffix: op_name}), ...]` in declaration
    order of each base's `_start`. Every method must belong to a quadruple that
    has all four suffixes with the right signatures; anything else is refused."""
    groups: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for op, method in service.methods.items():
        matched = None
        for suffix in a2a_task.SUFFIXES:
            tail = f"_{suffix}"
            if op.endswith(tail) and len(op) > len(tail):
                matched = (op[: -len(tail)], suffix)
                break
        if matched is None:
            raise RevlError(
                doc, line,
                f"remote row `@{label}` (`long_running`): method `{op}` of "
                f"service `{service.name}` is not one of the four Task-lifecycle "
                f"operations",
                hint=_task_hint())
        base, suffix = matched
        _check_task_method(method, base, suffix, doc=doc, line=line,
                           label=label, service_name=service.name)
        if base not in groups:
            groups[base] = {}
            if suffix == "start":
                order.append(base)
        if suffix in groups[base]:
            raise RevlError(
                doc, line,
                f"remote row `@{label}` (`long_running`): duplicate "
                f"`{suffix}` operation for base `{base}` of service "
                f"`{service.name}`",
                hint=_task_hint())
        groups[base][suffix] = op
    for base in groups:
        missing = [s for s in a2a_task.SUFFIXES if s not in groups[base]]
        if missing:
            raise RevlError(
                doc, line,
                f"remote row `@{label}` (`long_running`): the Task-lifecycle "
                f"base `{base}` of service `{service.name}` is missing "
                f"{', '.join('`' + a2a_task.op_name(base, s) + '`' for s in missing)}",
                hint=_task_hint())
        if base not in order:
            order.append(base)
    return [(base, groups[base]) for base in order]


def _py_body_a2a(host: str, op: str, in_band: bool,
                 follow_redirects: bool = False, *, rest: bool = False,
                 in_modality: str = "text", out_modality: str = "text",
                 label: str = "") -> str:
    """One crossing, Python tier, over A2A 1.0.0 `message/send`.

    This is the `through a2a` / `through a2a_rest` wire (item 439). It maps the
    canonical seam envelope onto the A2A message shape: the canonical `args[0]`
    becomes the one `Part` of a `message/send`, the canonical `method` (`op`)
    rides in the message metadata as the `revl.skill` reference the agent may
    route on, and the canonical reply `value` is read from the `Part`s of a
    TERMINAL `Task` or `Message`. Everything the default wire's `_py_body`
    decides — one crossing, redirect-refusing (`crossing_redirect`), time-bound
    (`CROSSING_TIMEOUT`), `on_failure` withdraw/result branching — it decides
    identically; only the payload built and the reply parsed differ.

    Two sub-transports, one difference between them and only one, the same one
    `revl import a2a` carries: JSON-RPC 2.0 wraps the message in an envelope and
    puts an A2A error in `error`, its reply under `result`; HTTP+JSON/REST
    (`rest`) POSTs the bare message to `<endpoint>/v1/message:send` and its reply
    IS the `Task`/`Message`, an A2A error arriving as a non-2xx status the
    transport branch already faults on.

    Two `Part` modalities: a `Str` argument/return is a TEXT `Part`, a `Bytes`
    is a FILE `Part` with INLINE base64 bytes. `in_modality`/`out_modality`
    (`"text"` or `"file"`) select which `Part` is built and how the reply is
    read back — a file reply is base64-decoded to `Bytes`; a file part that
    carried only a `uri` (a second crossing this slice does not make) is a
    fault, never a silently-empty answer.

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
    endpoint = _A2A_ENDPOINT % host
    if rest:
        endpoint = endpoint.rstrip("/") + _HTTPJSON_SEND_PATH
    url = json.dumps(endpoint)
    terminal = json.dumps(list(_TERMINAL_STATES))
    policy = py_policy(_A2A, follow=follow_redirects)
    wire = ("HTTP+JSON/REST `POST /v1/message:send`" if rest
            else "JSON-RPC 2.0 `message/send`")

    def fault(indent: int, expr: str, cause: str = "") -> str:
        pad = " " * indent
        if in_band:
            return f"{pad}return Err({expr})\n"
        tail = f" from {cause}" if cause else ""
        # item 439 T0: under `on_failure(withdraw)` a crossing fault is a
        # `TransportFault`, which the activation runtime maps to provider
        # withdrawal. The redirect refusal above is deliberately NOT one — it
        # is the peer declining to be the declared endpoint, re-raised as
        # `_RedirectRefused`, and never withdraws.
        return f"{pad}raise TransportFault({expr}){tail}\n"

    # The one `Part` SENT. A `Str` is a text part; a `Bytes` is a file part
    # with INLINE base64 bytes (A2A 1.0.0 `FileWithBytes`).
    if in_modality == "file":
        send_prep = ('    import base64 as _b64\n'
                     '    _sent_part = {"kind": "file", "file": '
                     '{"bytes": _b64.b64encode(_message).decode("ascii")}}\n')
    else:
        send_prep = '    _sent_part = {"kind": "text", "text": _message}\n'

    message_obj = (
        '{\n'
        '        "role": "user",\n'
        '        "messageId": str(_uuid.uuid4()),\n'
        '        "parts": [_sent_part],\n'
        f'        "metadata": {a2a_boundary.py_metadata(op)},\n'
        '    }')
    if rest:
        payload = f'{{"message": {message_obj}}}'
        unwrap = (f"        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) "
                  "as _resp:\n            _result = _json.loads(_resp.read())\n")
        error_branch = ""
    else:
        payload = ('{\n'
                   '        "jsonrpc": "2.0",\n'
                   '        "id": _corr,\n'
                   '        "method": "message/send",\n'
                   f'        "params": {{"message": {message_obj}}},\n'
                   '    }')
        unwrap = (f"        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) "
                  "as _resp:\n            _rpc = _json.loads(_resp.read())\n")
        error_branch = (
            # Item 439: nothing of the reply is read before the three gates
            # (`a2a_boundary.py_envelope_gates`) pass, and the peer's own
            # `error.code` is funnelled (F5) on the way out.
            a2a_boundary.py_envelope_gates(fault)
            + '    if _rpc.get("error"):\n'
            + fault(8, '_scrub("a2a: JSON-RPC error %s"'
                       ' % (_rpc["error"].get("code"),))')
            + '    _result = _rpc.get("result")\n')

    # The one `Part` READ BACK. A text reply joins the text parts; a file reply
    # base64-decodes the first inline-bytes file part, and a uri-only file part
    # is a fault (this binding does not make a second crossing to fetch it).
    if out_modality == "file":
        extract = (
            '    import base64 as _b64\n'
            '    _files = [p.get("file") for p in _parts\n'
            '              if p.get("kind") == "file"'
            ' and isinstance(p.get("file"), dict)]\n'
            '    _inline = [f for f in _files if isinstance(f.get("bytes"), str)]\n'
            '    if not _inline and _files:\n'
            + fault(8, '"a2a: reply file part carried a uri, not inline bytes - '
                       'this binding does not fetch it"')
            + '    _value = _b64.b64decode(_inline[0]["bytes"]) if _inline'
              ' else b""\n')
        empty_guard = ('    if not _value and _parts:\n'
                       + fault(8, '"a2a: reply carried no inline-bytes file part"'))
    else:
        extract = (
            '    _value = "".join(p.get("text", "") for p in _parts\n'
            '                    if p.get("kind") == "text"'
            ' and isinstance(p.get("text"), str))\n')
        empty_guard = ('    if not _value and _parts:\n'
                       + fault(8, '"a2a: reply carried only non-text parts"'))

    transport_fail = (
        '        # `on_failure(withdraw)`: a transport failure is a FAULT, never\n'
        '        # a quietly-empty result. Nothing is retried and nothing is\n'
        '        # undone — a remote effect has no local inverse.\n'
        if not in_band else "")
    ok = "    return Ok(_value)\n" if in_band else "    return _value\n"
    fault_class = "" if in_band else _transport_fault_class(label, op)
    # Item 439 (question (2), the fourth layer) and the correlation identity:
    # both are `a2a_boundary`'s, shared with the four-op wire and the importer
    # so an external peer cannot be read differently per entry point.
    funnel = a2a_boundary.py_funnel("_args")
    correlation = a2a_boundary.py_correlation()
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
    import uuid as _uuid
{fault_class}{funnel}{correlation}    # A2A {A2A_VERSION}, {wire}. ONE crossing. The one canonical arg is the
    # message `Part`; the method name rides as `revl.skill`.
    _message = _args[0]
{send_prep}    _payload = _json.dumps({payload}).encode()
    _r = _req.Request({url}, data=_payload,
                      headers={{"content-type": "application/json"}})
{policy}    try:
        # A crossing that never returns is not a crossing.
{unwrap}    except _RedirectRefused:
        # NOT a transport failure, and so NOT `on_failure`'s to classify: a
        # redirect is the peer declining to be the declared endpoint at all.
        raise
    except Exception as _exc:
{transport_fail}{fault(8, '"a2a: transport failure"', cause="_exc")}{error_branch}{a2a_boundary.py_result_gate(fault)}    _kind = _result.get("kind")
    if _kind == "task":
        _state = (_result.get("status") or {{}}).get("state")
        if _state not in {terminal}:
            # Item 439's open question: a task still in flight is a LIFECYCLE
            # this slice does not express. Fault; never poll, never resume.
{fault(12, '_scrub("a2a: task returned non-terminal state %r - this binding crosses once and does not poll" % (_state,))')}        if _state != "completed":
{fault(12, '_scrub("a2a: task ended %r" % (_state,))')}        _parts = [p for a in (_result.get("artifacts") or [])
                  for p in (a.get("parts") or [])]
    elif _kind == "message":
        _parts = _result.get("parts") or []
    else:
{fault(8, '_scrub("a2a: unexpected result kind %r" % (_kind,))')}{extract}{empty_guard}{ok}    """


def _check_long_running(is_a2a: bool, is_rest: bool, in_band: bool, *,
                        doc: str, line: int, label: str,
                        transport: str | None) -> None:
    """`long_running` is an A2A JSON-RPC Task-lifecycle shape (item 439 T1), so
    it is admitted only on `through a2a`. It is refused on the canonical wire
    (there is no `tasks/get` there), on `through a2a_rest` (the REST task paths
    are a distinct binding this slice does not build) and with
    `on_failure(result)` (the feed IS the failure settlement)."""
    if not is_a2a:
        where = ("the default (canonical) wire" if transport is None
                 else f"`through {transport}`")
        raise RevlError(
            doc, line,
            f"remote row `@{label}` writes `long_running`, but its transport is "
            f"{where}",
            hint="the A2A Task lifecycle (`_start`/`_poll`/`_reply`/`_cancel`) is "
                 "an A2A protocol shape spoken over `tasks/get` / `tasks/cancel` "
                 "/ `message/send`; write `through a2a` on the row (item 439 T1)")
    if is_rest:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` combines `long_running` with "
            f"`through a2a_rest`",
            hint="T1 binds the Task lifecycle over A2A 1.0.0 JSON-RPC 2.0 only. "
                 "The HTTP+JSON/REST task paths (`GET /v1/tasks/{id}`, "
                 "`POST /v1/tasks/{id}:cancel`) are a distinct binding not built "
                 "in this slice; write `through a2a` (item 439 T1)")
    if in_band:
        raise RevlError(
            doc, line,
            f"remote row `@{label}` combines `long_running` with "
            f"`on_failure(result)`",
            hint="the four-op Task lifecycle IS the failure settlement: a fault "
                 "on any crossing withdraws (T0), and a `Faulted`/terminal "
                 "`TaskEvent` closes the feed. `on_failure(result)` would fold "
                 "that back into one bare `Err` with no `TaskEvent` to carry it. "
                 "Drop the clause to get the default `on_failure(withdraw)` "
                 "(item 439 T1)")


def _task_ops(service, host: str, capability: str, redirect: str, *,
              doc: str, line: int, label: str) -> tuple[list[str], list[str]]:
    """The four-op projection's externs and provide methods (item 439 T1).

    Each op is ONE emission crossing exactly as the terminal wire is; the only
    new thing is that `_start` carries a `tasks/cancel` compensation keyed by the
    `TaskRef` it returns (item 247), and the returns are the `stdlib/a2a.rvl`
    vocabulary rather than `Str`/`Bytes`. Every return is `Untrusted[T]` (slice
    C3): a `Done` payload a consumer reads is tainted `net` exactly as a terminal
    reply is, so it cannot reach a `Trusted[T]` sink without an `endorse`.
    """
    quads = _classify_task_ops(service, doc=doc, line=line, label=label)
    externs: list[str] = []
    provides: list[str] = []
    follow = redirect == "same_origin"
    for base, ops in quads:
        for suffix in a2a_task.SUFFIXES:
            op = ops[suffix]
            method = service.methods[op]
            names = [n for n, _ in method.params]
            sig = ", ".join(f"{n}: {t}" for n, t in method.params)
            arrow = f" -> Untrusted[{method.returns}]"
            extern = f"remote_{label}_{op}"
            body_src = a2a_task.task_body(suffix, f"https://{host}", base,
                                          follow_redirects=follow, label=label)
            # The `tasks/cancel` COMPENSATION of `_start` (item 247) is the
            # consumer's to register, keyed by the `TaskRef` `_start` returns —
            # `_cancel` below is exactly that compensation as a first-class op
            # (design §T1: "the compensation of the start emission, AND an
            # explicit op the consumer may call"). It is NOT a synthesized
            # extern-level `compensate`, because an emission extern's compensate
            # slot binds no `result` (lower.py:_check_extern_undo) — the value to
            # cancel is only known after the crossing returns. Auto-registering
            # it from the service declaration is the one T1 piece that waits on a
            # declaration-site compensation binding (see the header).
            externs.append(
                f"extern emission[{capability}] fn {extern}({sig}){arrow}\n"
                f"  = @py {{\n    _args = [{', '.join(names)}]\n"
                f"{body_src}}}")
            provides.append(f"    fn {op}({', '.join(names)}) = "
                            f"{extern}({', '.join(names)})")
    return externs, provides


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
    is_a2a = transport in _A2A_TRANSPORTS
    is_rest = transport == _A2A_REST
    long_running = params.get("long_running", False)
    lr_line = params.get("long_running_line", line)

    component = f"Remote{_pascal(label)}Provider"
    externs: list[str] = []
    provides: list[str] = []
    if long_running:
        _check_long_running(is_a2a, is_rest, in_band, doc=doc, line=lr_line,
                            label=label, transport=transport)
        externs, provides = _task_ops(service, host, capability, redirect,
                                      doc=doc, line=line, label=label)
    for op, method in ([] if long_running else service.methods.items()):
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
        returns = method.returns
        # `through a2a[_rest]` binds A2A's own `message/send` `Part` subset —
        # one `Str` (text `Part`) or `Bytes` (file `Part`) in, one back — and
        # marshals those parts ITSELF at the boundary rather than through the
        # canonical `{"$kind","$value"}` encoding. So the A2A signature check
        # replaces the generic JSON-transparency projection here (a `Bytes`
        # crosses A2A as an inline-base64 file part but is NOT canonical-wire
        # projectable), and it also names the A2A modality on a refusal rather
        # than the canonical transparency the method would also fail.
        in_modality = out_modality = "text"
        if is_a2a:
            in_modality, out_modality = _check_a2a_method(
                service, op, method, in_band, doc=doc, line=line, label=label,
                transport=transport)
        else:
            for pname, ptype in method.params:
                if not _projectable(ptype):
                    _refuse_type(doc, line, label, op,
                                 f"parameter `{pname}`", ptype)
            if in_band:
                _head, args = _type_head(returns or "")
                payload, errtype = args[0], args[1]
                if payload not in ("", "Unit") and not _projectable(payload):
                    _refuse_type(doc, line, label, op, "the `Ok` payload",
                                 payload)
                if errtype != "Str":
                    raise RevlError(
                        doc, line,
                        f"remote row `@{label}` needs method `{op}` to return "
                        f"`Result[T, Str]`, not `{returns}`",
                        hint="the synthesized `Err` carries a transport "
                             "diagnostic, which is a `Str`. A richer error type "
                             "needs the tagged half of the canonical encoding "
                             "(424 slice C1)")
            elif returns and not _projectable(returns):
                _refuse_type(doc, line, label, op, "the return type", returns)

        sig = ", ".join(f"{n}: {t}" for n, t in method.params)
        names = [n for n, _ in method.params]
        # Slice C3 / D-424c.9: every value a remote provider returns is
        # `Untrusted[T]`. The extern's declared return is wrapped, which
        # `taint.extract_and_normalize` reads as a taint source whose origin is
        # the crossing's reach class (`net`). The provide method below returns
        # that source, so the flow walk taints it interprocedurally at every
        # consumer of this key — exactly as `revl import a2a` does by declaring
        # its service operation `-> Untrusted[Str]`. The qualifier is orthogonal
        # to the base type and stripped before base typing, so the synthesized
        # provider still satisfies the service's declared `-> T`; only the taint
        # verdict is new. `on_failure(result)` wraps the whole `Result[T, Str]`
        # (the reply object crossed the boundary), the fail-closed reading and
        # the one a top-level `Untrusted[...]` source registers. A method that
        # returns nothing has no value to taint, so `arrow` stays empty.
        arrow = f" -> Untrusted[{returns}]" if returns else ""
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
        body_src = (_py_body_a2a(host, op, in_band, redirect == 'same_origin',
                                 rest=is_rest, in_modality=in_modality,
                                 out_modality=out_modality, label=label)
                    if is_a2a else
                    _py_body(host, key, op, in_band, redirect == 'same_origin',
                             label=label))
        externs.append(
            f"extern emission[{capability}] fn {extern}({sig}){arrow}\n"
            f"  = @py {{\n    _args = [{', '.join(names)}]\n"
            f"{body_src}}}")
        provides.append(f"    fn {op}({', '.join(names)}) = "
                        f"{extern}({', '.join(names)})")

    isolate = f"  isolate {key} in realm(\"{realm}\")\n" if realm else ""
    header = _remote_header(service, label, key, host, capability, on_failure,
                            transport, realm, redirect,
                            long_running=long_running)
    body = (f"component {component} provides {key}: {service.name} {{\n"
            f"{isolate}  provide {key} {{\n" + "\n".join(provides) + "\n  }\n}")
    return component, "\n\n".join([header, *externs, body]) + "\n"


def _a2a_task_scope_lines() -> list[str]:
    """The SCOPE block for a `long_running` row: the four-op Task lifecycle
    (item 439 T1), which polls and resumes rather than crossing once."""
    return [
        "//   the one argument of `_start`/`_reply` becomes the message's single",
        "//   text `Part`; the method name rides as the `revl.skill` reference.",
        "//   Version is claimed EXACTLY, never as bare \"A2A\" (decision (3)).",
        "//   SCOPE: the FOUR-OP A2A Task LIFECYCLE (item 439 T1), the explicit-",
        "//   handle surface for a long-running Task:",
        "//     * `_start`  -> `message/send`, returns a `TaskRef` handle;",
        "//     * `_poll`   -> `tasks/get`, one `TaskEvent` per call (the consumer",
        "//       drives the loop; `is_terminal` decides when to stop);",
        "//     * `_reply`  -> `message/send` + `taskId`, answers an",
        "//       `input-required` / `auth-required` prompt;",
        "//     * `_cancel` -> `tasks/cancel`, the best-effort COMPENSATION of",
        "//       `_start` (item 247) AND an explicit op the consumer may call.",
        "//   `_cancel` is the compensation the consumer registers on `_start`,",
        "//   keyed by the returned `TaskRef`; a peer's claim to have cancelled is",
        "//   audit-grade, not a witness, so it is `compensate`, never an inverse.",
        "//   Each is ONE crossing, so redirect refusal, the deadline and the",
        "//   `Untrusted[T]` return hold unchanged. A deadline or transport error",
        "//   is a `TransportFault` the runtime maps to WITHDRAWAL (T0) — item",
        "//   130's \"provider death is a terminal, never silence\", at the adapter.",
        "//   The stream sugar (item 130, T2), gRPC and the REST task paths are",
        "//   NOT this wire (item 439 T1).",
    ]


def _a2a_header_lines(rest: bool = False, long_running: bool = False) -> list[str]:
    """The `through a2a` / `through a2a_rest` header block (item 439). It states
    the protocol exactly (A2A 1.0.0, never bare "A2A", decision (3)), which of
    the two JSON-body sub-transports the row crosses, the terminal `Part` subset
    this slice binds, and — the load-bearing one — that the peer is a CLAIM, not
    a checked composition (decision (2); item 329's untrusted-author case)."""
    if long_running:
        wire_lines = [
            f"// Transport: A2A {A2A_VERSION} over JSON-RPC 2.0, the four-op Task",
            "//   lifecycle. The peer authority above is the agent's HTTPS "
            "endpoint root.",
        ]
        return wire_lines + _a2a_task_scope_lines() + [
            "//",
            "// THE A2A PEER IS A CLAIM, NOT A CHECKED COMPOSITION. An external "
            "agent",
            "//   is not a revl composition, so nothing about it is verified: this "
            "row",
            "//   admits, verifies and re-admits NOTHING about the callee (a "
            "client is",
            "//   the SENDER, D-424c.8; item 337 requires the RECEIVER to "
            "re-compile",
            "//   from its own source). Every A2A provider is item 329's",
            "//   untrusted-author case BY CONSTRUCTION (item 439 decision (2)).",
        ]
    if rest:
        wire_lines = [
            f"// Transport: A2A {A2A_VERSION} over HTTP+JSON/REST "
            "(`POST /v1/message:send`).",
            "//   The peer authority above is the agent's HTTPS endpoint root and",
            "//   the REST method path `/v1/message:send` is appended to it. The",
            "//   canonical seam envelope is MAPPED onto the A2A message shape:",
        ]
    else:
        wire_lines = [
            f"// Transport: A2A {A2A_VERSION} over JSON-RPC 2.0 (`message/send`)."
            " The peer",
            "//   authority above is the agent's HTTPS endpoint root. The "
            "canonical",
            "//   seam envelope is MAPPED onto the A2A message shape:",
        ]
    return wire_lines + [
        "//   the one argument becomes the message's single `Part` — a `Str` a",
        "//   text `Part`, a `Bytes` a file `Part` with inline base64 bytes — the",
        "//   method name rides as the `revl.skill` metadata reference, and the",
        "//   reply `Part` is read back from a TERMINAL `Task`/`Message`.",
        "//   Version is claimed EXACTLY, never as bare \"A2A\": the protocol",
        "//   moves and a binding that followed it silently would assert a",
        "//   compatibility nobody checked (item 439 decision (3)).",
        "//   SCOPE: a `message/send` whose task reaches a TERMINAL state in that",
        "//   one crossing — the subset where \"does an A2A Task map to one",
        "//   emission, to a stream (item 130), or to a session (item 250)?\"",
        "//   does not arise. A non-terminal reply (`working`, `input-required`,",
        "//   `auth-required`) faults at the boundary; the body never polls,",
        "//   resumes, or guesses. Streaming, gRPC and a `DataPart` (structured",
        "//   JSON, which needs the canonical tagged encoding, slice C1) are not",
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
                   transport, realm, redirect="refuse", *,
                   long_running: bool = False) -> str:
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
    if transport in _A2A_TRANSPORTS:
        lines += _a2a_header_lines(rest=transport == _A2A_REST,
                                   long_running=long_running)
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
        "// EVERY RETURNED VALUE IS `Untrusted[T]` (item 424 D-424c.9, slice C3).",
        "//   A generated client looks exactly like a local provider at every call",
        "//   site, so without this a remote value could reach an outbound send",
        "//   invisibly. Each synthesized crossing below returns `Untrusted[<T>]`,",
        "//   which the checker propagates through the provide method to every",
        "//   consumer of this key: a remote result reaching a `Trusted[T]` sink is",
        "//   refused (G9) unless an `endorse[<origin>]` sits on the flow path. The",
        "//   origin is the reach class of the peer (`net`). The TAINT IS THE",
        "//   ADMISSION FACT of remoteness: a consumer of a LOCAL provider of the",
        "//   same service is unchanged and untainted (D-424c.1), so bringing this",
        "//   provider back in-process removes the qualifier with no source edit.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------- the seam
#
# Slice B2 (item 424 gap (b), `docs/design/424-dsh-language-gaps.md` §2). A seam
# is the third of §4's four kinds of one function: a SYNTHESIZED FORWARDING
# PROVIDER derived from the service declaration plus the observer's kind
# (D-424b.3). The seam author writes an OBSERVER, never a forwarder, so the
# whole "middleware forgot to call next()" failure class a waterfall has is
# gone — there is no `next()` to forget because the forwarder is derived, not
# written.
#
# What this slice lands, and what it does not
# -------------------------------------------
#
# B2 lands the SURFACE, the SYNTHESIS and the ADMISSION checks: a seam is a
# composition row (D-424b.2), it synthesizes a forwarder that observes then
# forwards (D-424b.3), the observer never holds the inner handle so a seam can
# SUPPRESS a call but never MINT one (D-424b.8), and `decide` is admitted only
# on a `Result` method while `rewrite` has no spelling (D-424b.4).
#
# Two things measured against the compiler bound what a forwarder can be, and
# both are exactly what §2.2 measured against `origin/main` — carried here as
# code rather than left in the prose:
#
# 1. **The interposition is DISTINCT-KEY, not same-key.** `isolate` binds ONE
#    realm per key (`lower.py`), so a single component cannot require `db` from
#    an inner realm and provide `db` in the parent realm — the two are the same
#    key and get one realm. The only same-key shape the language admits routes
#    through `realms(...)`, and that shape is `run.py:747`'s hole: it compiles,
#    admits, and its provide body is never plugged. So the synthesized forwarder
#    requires the wrapped provider under a DISTINCT inner key and provides the
#    outer key, which is §2.2's sanctioned wrapper with the forwarder now
#    DERIVED. The cost §2.2 named — the wrapped provider is re-keyed in its own
#    source — is unchanged, and it is the reason a seam over an EXISTING
#    file provider is not yet source-transparent for that provider.
#
# 2. **The forwarder reaches the inner and observer keys, so G4 bounds it.**
#    A forwarder whose `provide` body emits through `<inner>` and `<obs>` is
#    refused by G4 unless the WRAPPED SERVICE declares those in its
#    `emission[...]`. So a seam compiles only where the service already declares
#    a bound wide enough to cover the crossing, and refuses otherwise with G4's
#    own message — §2.4's FALLBACK, the no-rule-change floor. On top of that G4
#    check, the fallback's other half is now ENFORCED at resolution
#    (`composition._check_seam_through`): the seam's declared `through` reach
#    must be a SUBSET of the service's own `emission[...]` bound, so `through` is
#    an enforced reach rather than decorative. What is NOT made here is the
#    D-424b.5 WIDENING — checking the forwarder against `through` INSTEAD of the
#    service, letting the composition mint a bound the service did not declare —
#    which weakens plain-`fn` transitive purity across the edge and needs 426
#    S5's `seam:` token to stay auditable; it remains the architect's to take.
#    `test_424_seam_row.py` pins each half.


def check_seam_kind(kind: str, *, doc: str, line: int, label: str) -> None:
    """`observe` and `decide` are the two kinds a seam carries; `rewrite` is
    refused (D-424b.4).

    The parser already refuses anything but the three words, and refuses
    `rewrite` there with the F2 argument; this is the synthesizer's own guard so
    a caller that hand-builds params cannot slip an unknown kind past it.
    """
    if kind not in SEAM_KINDS:
        raise RevlError(
            doc, line,
            f"seam row `@{label}` names kind `{kind}`, which is not a seam kind",
            hint="a seam is `observe` (the forwarder calls the inner method and "
                 "returns it unchanged) or `decide` (a `Deny` suppresses the call "
                 "and becomes the method's `Err`). `rewrite` has no spelling: "
                 "argument substitution between describe and execute is roadmap "
                 "427 F2's approve-one-run-another shape, still unfixed "
                 "(424 D-424b.4)")


def check_decidable(service, *, doc: str, line: int, label: str) -> None:
    """D-424b.4: a `decide` seam is admitted only on a service whose every
    method returns `Result[T, E]`.

    A `Deny(msg)` becomes the method's `Err(msg)`, so a method with nowhere to
    put an `Err` cannot carry a decision. The refusal names the method, the same
    shape `on_failure(result)` uses for a non-`Result` remote method.
    """
    for op, method in service.methods.items():
        head, args = _type_head(method.returns or "")
        if head != "Result" or len(args) != 2:
            raise RevlError(
                doc, line,
                f"`decide` seam `@{label}` needs every method of "
                f"`{service.name}` to return `Result[T, E]`, and `{op}` returns "
                f"{'nothing' if not method.returns else f'`{method.returns}`'}",
                hint="a `decide` seam turns a `Deny` into the method's `Err`, so "
                     "the method needs somewhere to put it. Use `observe` (which "
                     "returns the inner result unchanged and needs no `Result`), "
                     "or declare the method `-> Result[T, E]` (424 D-424b.4)")


#: The observer's method name, per kind. `observe` calls `saw`; `decide` calls
#: `allow`. Named once so the synthesized forwarder and the observer-contract
#: check in `composition.py` cannot disagree on the spelling.
OBSERVER_METHOD = {"observe": "saw", "decide": "allow"}


def _seam_header(service, params: dict) -> str:
    label = params["label"]
    key = params["key"]
    inner_key = params["inner_key"]
    obs_key = params["observer_key"]
    obs_service = params["observer_service"]
    kind = params["kind"]
    realm = params.get("realm")
    through = params.get("through") or ()
    lines = [
        f"// SYNTHESIZED for seam row `@{label}` — this file is not on disk.",
        f"// Item 424 D-424b.1/.3, slice B2. Service `{service.name}`, "
        f"key `{_comment_safe(key)}`"
        + (f", realm `{_comment_safe(realm)}`." if realm else "."),
        f"// Kind: `{kind}`. Observer: `{_comment_safe(obs_service)}` under key "
        f"`{_comment_safe(obs_key)}`.",
        "//",
        "// A SEAM IS A SYNTHESIZED FORWARDING PROVIDER (D-424b.3). The observer",
        "//   author writes an observer, never a forwarder; the forwarder below is",
        "//   DERIVED from the service declaration, so there is no `next()` to",
        "//   forget. The observer NEVER receives the inner handle (D-424b.8): the",
        "//   forwarder holds it, so a seam can SUPPRESS a call (`decide`) but can",
        "//   never MINT one or call the inner with other arguments.",
        "//",
        "// THE INTERPOSITION IS DISTINCT-KEY. `isolate` binds one realm per key,",
        "//   so this forwarder requires the wrapped provider under the inner key",
        f"//   `{_comment_safe(inner_key)}` and provides the outer key "
        f"`{_comment_safe(key)}`.",
        "//   The wrapped provider is re-keyed in its own source — §2.2's measured",
        "//   cost, unchanged — because the same-key shape routes through",
        "//   `realms(...)` and is `run.py:747`'s hole (compiles, never runs).",
    ]
    if kind == "observe":
        lines += [
            "//",
            "// OBSERVE: the forwarder calls the inner method and returns its",
            "//   result unchanged. The observer sees the call and has NO effect on",
            "//   it.",
        ]
    else:
        lines += [
            "//",
            "// DECIDE: a `Deny` from the observer suppresses the call and becomes",
            "//   the method's `Err`; an `Allow` forwards to the inner method.",
            "//   Admitted only because every method returns `Result[T, E]`.",
        ]
    if through:
        lines += [
            "//",
            f"// THROUGH: {', '.join('`' + _comment_safe(c) + '`' for c in through)}."
            "  The composition declares the reach the",
            "//   forwarder may cross (D-424b.5). ENFORCED (§2.4's fallback): this",
            "//   `through` set is a SUBSET of the wrapped service's own",
            "//   `emission[...]` bound — a `through` reach the service does not",
            "//   grant is refused at resolution naming the capability. G4 still",
            "//   checks the forwarder against the service declaration, so a seam",
            "//   compiles only where that bound already covers the crossing. The",
            "//   WIDENING — checking the forwarder against `through` INSTEAD of",
            "//   the service, minting a bound the service did not declare — is",
            "//   the rule change reserved for the architect and needs 426 S5's",
            "//   `seam:` token; it is not made here.",
        ]
    return "\n".join(lines)


def _seam_source(service, params: dict) -> tuple[str, str]:
    """The synthesized forwarding provider for a seam row (D-424b.3).

    Requires the wrapped provider under a distinct inner key and the observer
    under its own key; provides the outer key. Each method emits the observer
    call (with the operation name — the full `Untrusted[Value]` record of
    D-424b.7 is B3) and then, for `observe`, forwards to the inner method and
    returns it unchanged; for `decide`, forwards only on an `Allow`.
    """
    label = params["label"]
    key = params["key"]
    inner_key = params["inner_key"]
    obs_key = params["observer_key"]
    kind = params["kind"]
    doc, line = params["doc"], params["line"]

    check_seam_kind(kind, doc=doc, line=line, label=label)
    if kind == "decide":
        check_decidable(service, doc=doc, line=line, label=label)

    saw = OBSERVER_METHOD[kind]
    component = f"Seam{_pascal(label)}Provider"
    provides: list[str] = []
    for op, method in service.methods.items():
        names = [n for n, _ in method.params]
        call_args = ", ".join(names)
        inner_call = (f"emit {inner_key}.{op}({call_args})"
                      if call_args else f"emit {inner_key}.{op}()")
        # The observer record: the operation name as a `Str`. D-424b.7's
        # `Untrusted[Value]` record — the typed args funnelled into one dynamic
        # Value under a fail-closed taint join — needs the Value marshalling B3
        # builds, so this slice passes the op name and says so.
        observe_call = f'emit {obs_key}.{saw}("{op}")'
        if kind == "observe":
            body = (f"      {observe_call}\n"
                    f"      return {inner_call}") if method.returns else (
                    f"      {observe_call}\n"
                    f"      {inner_call}")
        else:
            # `decide`: the observer's `allow` returns `Decision = Allow |
            # Deny(Str)`. A `Deny` becomes the method's `Err` (checked to be a
            # `Result` by `check_decidable`); an `Allow` forwards. The decision
            # is bound first (a bare `match` is not an effect statement, G6) and
            # returned, so the observer NEVER holds the inner handle (D-424b.8):
            # only the `Allow` arm names the forwarder's inner call.
            body = (f"      let _decision = {observe_call}\n"
                    f"      return match _decision {{\n"
                    f"        Allow => {inner_call},\n"
                    f"        Deny(_msg) => Err(_msg),\n"
                    f"      }}")
        sig = ", ".join(names)
        provides.append(f"    fn {op}({sig}) {{\n{body}\n    }}")

    header = _seam_header(service, params)
    requires = f"requires {inner_key}: {service.name}, {obs_key}: {params['observer_service']}"
    body = (f"component {component} {requires} provides {key}: {service.name} "
            f"{{\n  provide {key} {{\n" + "\n".join(provides) + "\n  }\n}")
    return component, "\n\n".join([header, body]) + "\n"


# -------------------------------------------------------------- the host kind
#
# item 457 S3, docs/design/457-endpoint-one-definition.md §"The Cordis
# `ctx.server` binding: a `host` row" (design note 530 Decision A). The fourth
# caller of §4's one function, and the sibling of the `remote` kind: where
# `remote` synthesizes a provider that reaches a PEER over the wire, `host`
# synthesizes a provider that reaches the HOST PROCESS's own ambient service
# (Cordis `ctx.server`, `ctx.sso`) through the reviewed `@ts ref` door
# (item 396 option B). Design note 530 asked how a revl component reaches an
# ambient host service without a `globalThis` bridge; the answer is the row
# table, not a new checker mode, so this is a row whose provider is synthesized
# exactly as a `remote` row's is — and remains an ordinary component `_link`
# runs G2/G3/G4 over, so a bug here can only over-refuse, never admit.


def check_hostable(service, shim, *, doc: str, line: int, label: str) -> None:
    """Is this service reachable as a host service at all (item 457 S3)?

    Two conditions, both G4 read at the host boundary:

      * a shipped shim exists for the service (`HOST_SHIMS`). A `host` row for a
        service with no shim is refused naming it, never bound to a module that
        does not exist;
      * every method declares an emission bound (or is `async`). Reaching the
        host runtime IS a boundary crossing — `ctx.server.get` registers a route,
        `ctx.sso.validateSession` performs I/O — so a plain `fn` host service is
        not hostable, exactly as a plain `fn` service is not remotable
        (D-424c.2). The refusal names the method.
    """
    if shim is None:
        known = ", ".join(f"`{n}`" for n in sorted(HOST_SHIMS)) or "<none>"
        raise RevlError(
            doc, line,
            f"host row `@{label}` hosts service `{service.name}`, which has no "
            f"shipped host shim",
            hint=f"services with a host shim: {known}. A `host` row binds the "
                 "service to the host runtime's own ambient service through a "
                 "reviewed `@ts ref` shim; a service with none cannot be hosted "
                 "(item 457 S3)")
    if not service.methods:
        raise RevlError(
            doc, line,
            f"host row `@{label}` hosts service `{service.name}`, which declares "
            "no methods",
            hint="a host row synthesizes one host-shim binding per method; a "
                 "service with none has nothing to host")
    for op, method in service.methods.items():
        if not method.emission and not method.async_:
            raise RevlError(
                doc, line,
                f"service `{service.name}` is not hostable: method `{op}` is a "
                f"plain `fn` (host row `@{label}`)",
                hint="every method of a host service declares an emission bound "
                     "— `emission fn` or `emission[cap] fn` — or is `async`. "
                     "Reaching the host runtime is a boundary crossing (a route "
                     "registration, an SSO check), so this is G4 read at the host "
                     "boundary, not a new rule (item 457 S3, mirroring D-424c.2)")


def _host_header(service, label, key, realm, shim) -> str:
    module = _comment_safe(shim["module"])
    package = _comment_safe(shim["package"])
    return "\n".join([
        f"// SYNTHESIZED for host row `@{label}` — this file is not on disk.",
        f"// Item 457 S3 (docs/design/457-endpoint-one-definition.md). "
        f"Service: `{service.name}`,",
        f"// key `{_comment_safe(key)}`"
        + (f", realm `{_comment_safe(realm)}`." if realm else "."),
        f"// Host shim: `{module}` over `{package}` (the ts tier).",
        "//",
        "// A HOST ROW IS THE SIBLING OF A REMOTE ROW. Its provider is not revl",
        "//   source but the HOST PROCESS's own ambient service — Cordis",
        "//   `ctx.server`/`ctx.sso` — reached through the REVIEWED `@ts ref`",
        "//   door (item 396 option B), never a `globalThis` bridge. Design note",
        "//   530 asked how a revl component reaches an ambient host service; the",
        "//   answer is the row table, not a new checker mode (Decision A).",
        "//",
        "// THE WIRING IS LOCAL. Every consumer keeps `requires "
        f"{_comment_safe(key)}: {service.name}`",
        "//   and G2/G3/G4 are unchanged. HOSTNESS is an ADMISSION fact — the",
        "//   provider is the host runtime, reached through a reviewed ref — and",
        "//   never a wiring fact, so bringing a real revl provider in for this",
        "//   key is a one-line composition edit and not a source edit across",
        "//   every consumer (mirroring 424 D-424c.1).",
        "//",
        "// TIER: `ts` only. The `@ts ref` door is native to `py`/`ts`",
        "//   (`hostref.EXTERN_REF_TIERS`) and refused on go/rust/java/wasm at",
        "//   lower time, so a host row on a tier with no shim is refused there —",
        "//   the per-tier shim refusal riding the existing reviewed-ref",
        "//   machinery rather than a new resolution special case.",
    ])


def _host_source(service, params: dict) -> tuple[str, str]:
    """The synthesized provider for a `host` row (item 457 S3).

    Provides the outer key, binding each service method to a `@ts ref` extern of
    the shipped host shim (one exported symbol per method). The provider is an
    ordinary component: `_link` runs G4 over it, so a method the shim cannot
    honour is caught by the ordinary gate, not trusted here.
    """
    label = params["label"]
    key = params["key"]
    realm = params.get("realm")
    doc, line = params["doc"], params["line"]
    shim = HOST_SHIMS.get(service.name)

    check_hostable(service, shim, doc=doc, line=line, label=label)

    module = shim["module"]
    component = f"Host{_pascal(label)}Provider"
    externs: list[str] = []
    provides: list[str] = []
    for op, method in service.methods.items():
        sig = ", ".join(f"{n}: {t}" for n, t in method.params)
        names = [n for n, _ in method.params]
        arrow = f" -> {method.returns}" if method.returns else ""
        extern = f"host_{label}_{op}"
        # ONE extern per method, each a `@ts ref` of the shipped shim's matching
        # export. The reviewed host-reference door pins the shim's bytes into the
        # IR and jails the module to the install root (item 396 option B / item
        # 410), so the binding is auditable rather than an opaque ambient reach.
        externs.append(
            f"extern emission fn {extern}({sig}){arrow} "
            f"= @ts ref {op} from \"{module}\"")
        call_args = ", ".join(names)
        provides.append(f"    fn {op}({call_args}) = {extern}({call_args})")

    isolate = f"  isolate {key} in realm(\"{realm}\")\n" if realm else ""
    header = _host_header(service, label, key, realm, shim)
    body = (f"component {component} provides {key}: {service.name} {{\n"
            f"{isolate}  provide {key} {{\n" + "\n".join(provides) + "\n  }\n}")
    return component, "\n\n".join([header, *externs, body]) + "\n"


def synthesize_provider(service, kind: str, params: dict) -> tuple[str, str]:
    """Synthesize a provider component for `service`.

    Returns `(component_name, source_text)`. The text is ordinary revl source
    and is compiled by the ordinary compiler, which is the whole soundness
    argument: nothing here is trusted. `_link` still runs G2, G3 and G4 over the
    result, so a bug in this module can only refuse something admissible or
    produce source the compiler then refuses — never admit something `_link`
    would not (the same argument 426 §3.3 makes for the resolver).
    """
    if kind == "remote":
        return _remote_source(service, params)
    if kind == "seam":
        return _seam_source(service, params)
    if kind == "host":
        return _host_source(service, params)
    raise ValueError(
        f"unknown provider kind {kind!r}; this module ships "
        f"{', '.join(repr(k) for k in KINDS)}")


def check_transport(transport: str | None, *, doc: str, line: int,
                    label: str) -> None:
    """D-424c.1's `through <wire>` names the transport a remote row crosses.

    The synthesizer speaks the default wire plus two NAMED A2A sub-transports.
    The default — `through` OMITTED — is the canonical envelope over HTTPS
    (`{"key","method","args"}` -> `{"ok","value"|"error"}`, the placement
    bridge's own). The named wires are `through a2a` (A2A 1.0.0's `message/send`
    over JSON-RPC 2.0) and `through a2a_rest` (the same over HTTP+JSON/REST,
    `POST /v1/message:send`), item 439 — each maps that same canonical envelope
    onto the A2A message shape at the boundary, and each is a POST of a JSON body
    a `urllib` host can carry. Any OTHER `through <name>` is refused naming the
    transport and the row rather than emitted as a wire the generated body would
    not actually speak.

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
             "docs/composition-rows.md) — or write `through a2a` (A2A 1.0.0 over "
             "JSON-RPC 2.0 `message/send`) or `through a2a_rest` (A2A 1.0.0 over "
             "HTTP+JSON/REST `POST /v1/message:send`), item 439. gRPC is the "
             "third A2A transport and is NOT bound: it is a binary transport over "
             "HTTP/2 with protobuf framing, not the JSON POST this synthesizer "
             "emits, so it cannot ship under any label. Refusing a named wire the "
             "generated body would not actually speak is the honesty rule the "
             "version and redirect checks already keep (424 D-424c.1, item 439)")


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
