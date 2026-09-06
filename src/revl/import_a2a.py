"""A2A 1.0.0 Agent Card -> revl source.

`revl import a2a` is the fifth member of the import codegen family
(docs/v2.0-roadmap.md §14), after `revl mcp import`, `revl import wit`,
`revl import openapi` and `revl import cordis`. It reads an **A2A 1.0.0 Agent
Card** and emits revl: a `service` for the agent's skills, one `extern` per
skill, and a provider component — so a revl composition can consume an external
agent as an ordinary coeffect.

This is slice 1 of roadmap item 439 (the A2A transport binding). It binds the
Agent Card and the single-crossing `message/send` call, and nothing else. What
it deliberately does not do is listed under "Not in this slice" below.

Why this member is the harshest of the family
---------------------------------------------

WIT and OpenAPI describe *specified* interfaces. A Cordis plugin is untyped
TypeScript whose surface can at least be read out of source I hold. An A2A
agent is **none of those**: it is a process someone else runs, and its Agent
Card is a document that process serves about itself. Nothing in it is checked
by anybody. So:

    **An Agent Card is a CLAIM, not a specification.** Every fact this
    importer transcribes — the skills, the modalities, the protocol version —
    is the remote agent's own assertion about itself, and this importer's job
    is to turn that assertion into a *bounded* local declaration, never into a
    guarantee. Item 439 decision (2); item 329's untrusted-author case, by
    construction rather than by policy.

Three consequences, each of them executable rather than prose:

  * **Every returned value is `Untrusted[T]`** (item 424 D-424c.9). The
    checker then does the work: a card result reaching an authority-granting
    sink is refused (G9) until it is parsed by a `verified fn` or endorsed at
    a declared point. A remote value must not be able to reach an outbound
    send invisibly, and a generated client is exactly the construct that would
    otherwise let it, because it looks like a local provider at every call
    site.
  * **The reach bound is derived from host and port only, never from
    userinfo** (D-424c.10). A credential riding in a card's `url` is a live
    secret, not an identifier; it is redacted everywhere it would otherwise be
    echoed, and it never becomes part of a capability spelling.
  * **The version claim is exact.** The card must say `"1.0.0"`, and the
    generated header says "A2A 1.0.0", never "A2A" (item 439 decision (3)).
    A card claiming any other protocol version is refused naming the version
    it claimed. The protocol moves; the binding does not silently move with it.

Teardown: a remote A2A call cannot participate in G7, and that is a decision
--------------------------------------------------------------------------

revl's teardown stack has exactly three entry kinds
(`docs/design/teardown-contract.md`): `bracket` (an acquire's release,
proof-grade and infallible by contract, G5), `transactional` (a witnessed
crossing with a HOST-LOCAL inverse, item 243), and `compensation` (audit-grade,
best-effort, abort-only, may fail into residue, item 247).

An A2A skill can be neither of the first two:

  * `bracket` requires an infallible inverse. An inverse that travels over a
    network is fallible by construction — the peer may be unreachable,
    restarted, or simply gone by teardown time — so it cannot carry G5.
  * `transactional` requires a host-local inverse and a witness captured on the
    `Ok` branch. An A2A agent's state is not host-local, and any witness it
    returns is one more claim from the same unchecked peer.

A2A 1.0.0's `tasks/cancel` is not an inverse either: it asks a *running* task
to stop, the agent is permitted to refuse it, and it says nothing whatever
about a task that already reached a terminal state. So the generated externs
never call it at teardown, and this importer **never synthesizes an inverse**.

Every operation is therefore `emission` with no inverse — G4's other branch,
the one that means "declared irreversible". When the local composition unwinds,
the remote effect stays. A `compensation` entry is the only kind an A2A call
could ever carry, it is audit-grade and best-effort, and it is the importing
engineer's to write by hand, because only they know which remote operation
undoes which: the Agent Card does not say, and this importer will not guess.

An A2A crossing suspends, so on a coloured tier the operation is `async`
--------------------------------------------------------------------

A JSON-RPC round trip to another process is not a synchronous call, and the ts
tier has no blocking fetch to pretend otherwise with. So on a tier whose host
body suspends, every declaration this importer writes — the service operation,
the extern and the provide method — carries `async` (roadmap item 80,
`docs/design/async-extern.md`). Issue #251: it used to carry none of them and
hand the ts tier a body that `await`s, which emits `await` inside a
non-`async` function and typechecks nowhere.

PR #250's `remote` row refuses the ts tier over this same sentence, and both
answers are right, because the two own different amounts of the surface. A
`remote` row synthesizes only a PROVIDER for a service somebody else wrote, and
its whole point is that a consumer does not change by one character when a
local provider becomes a remote one; colouring a `service` declaration it did
not write would break the property it exists to preserve, so it refuses. This
importer writes the service, the extern and the provider together out of one
card. Nothing it would have to recolour predates it, so it declares the colour
the crossing actually has.

For a caller that means the async-ness is DECLARED and readable: a consumer
binds `emission async fn` off the generated service and calls it from an async
context. That is A1's rule, not an extra one — asynchrony crosses a component
boundary only by declaration and is never smuggled in by a provider
(async-extern.md §3) — and the value type is untouched: `-> Untrusted[Str]`
still means `Untrusted[Str]`, and the `Promise` exists only in the emitted
TypeScript (§2).

The `@py` binding of the same card stays SYNC. A generated file carries exactly
one host body, and the colour it declares is the colour of that body: `urllib`
blocks rather than suspends, and py is a coloured tier, so declaring it `async`
would wrap a blocking call in an `async def`, stall the caller's loop, and
colour every py caller for a suspension that never happens.

Not in this slice, and deliberately
-----------------------------------

  * **The `remote` row.** Item 424(c)'s C2 (a row whose provider is
    synthesized, `on_failure`, per-realm peers) is not built, so a card imports
    as an ordinary provider component, exactly like the rest of the family.
    Withdrawal-on-transport-failure (D-424c.3) arrives with that row; here a
    transport failure is a fault raised out of the host body.
  * **The Task lifecycle.** Item 439's load-bearing open question — does an
    A2A Task map to one emission, to a stream (item 130), or to a session
    (item 250)? — is not answered here and is not pre-empted. This slice binds
    only the subset where the question does not arise: a `message/send` whose
    task reaches a TERMINAL state in that one crossing. A non-terminal
    response (`working`, `input-required`, `auth-required`, `unknown`) is a
    fault at the boundary; the generated body refuses to poll, refuses to
    resume, and refuses to guess.
  * **Streaming and push notifications.** A card's `capabilities.streaming` /
    `pushNotifications` are recorded in the header as NOT PROJECTED rather
    than quietly ignored. `message/stream`, `tasks/resubscribe` and webhook
    delivery need the lifecycle answer above first.
  * **Capability extensions.** An OPTIONAL `capabilities.extensions` entry is
    recorded as NOT PROJECTED, like streaming. A REQUIRED one is refused: A2A
    1.0.0's `required` means a client must comply with the extension to
    interact, and this slice projects no extension, so it cannot comply.
    Recording it and emitting a plain provider anyway would drop a contract the
    agent declared mandatory — the honesty rule the version and transport
    checks already enforce.
  * **Transports other than JSON-RPC 2.0.** gRPC and HTTP+JSON are refused
    naming the transport; `additionalInterfaces` are recorded, not projected.
  * **Non-text modalities.** A `FilePart` or a `DataPart` has no transcription
    this slice defines, so a skill whose modes are not all `text/*` is refused
    naming the skill and the mode.
"""

from __future__ import annotations

import json
import re

from .crossing_redirect import CROSSING_TIMEOUT, py_policy, ts_policy
from .errors import RevlError
from .lexer import KEYWORDS
# The OpenAPI importer's authority helpers are the audited ones (items 416f and
# 421 F4) and an Agent Card is the same class of attacker-supplied document, so
# they are reused rather than re-derived: `_comment_safe` for every
# card-derived string that reaches a `//` comment, `_authority_host` for the
# capability token, `_redact_userinfo` for every echo of the endpoint.
from .import_openapi import (
    _authority_host,
    _comment_safe,
    _line_of,
    _pointer,
    _redact_userinfo,
)

#: The one protocol version this binding claims. Item 439 decision (3): the
#: claim is "A2A 1.0.0", never "A2A", because the protocol moves and a binding
#: that silently follows it is claiming something it has not checked.
A2A_VERSION = "1.0.0"

#: The one transport this slice binds. A2A 1.0.0 also defines gRPC and
#: HTTP+JSON; both are refused rather than approximated.
_TRANSPORT = "JSONRPC"

#: The task states A2A 1.0.0 calls terminal. Anything else coming back from a
#: single `message/send` is a lifecycle this slice does not express.
_TERMINAL_STATES = ("completed", "failed", "canceled", "rejected")

#: A conservative absolute-URL shape. The endpoint is interpolated into a
#: generated `//` comment AND into a generated host body, so it is validated
#: against a strict character class up front rather than escaped afterwards:
#: no quotes, no braces, no backslash, no whitespace, no control characters.
#: Refusing an odd URL costs an import; accepting one costs a codegen hole.
_URL_RE = re.compile(
    r"^(?P<scheme>https?)://"
    r"[A-Za-z0-9\-._~%!$&'()*+,;=:@\[\]]+"
    r"(?P<path>/[A-Za-z0-9\-._~%!$&'()*+,;=:@/]*)?$"
)

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _cap_token(host: str) -> str:
    """The reach token for a host: `net.<token>`, item 424 D-424c.10.

    Folded from the HOST alone — never the port, never the userinfo — so two
    credentials against two hosts cannot collapse onto one token and a
    credential can never become part of a capability spelling.

    A bare IP literal folds to something that starts with a digit
    (`127.0.0.1` -> `127_0_0_1`), which the lexer reads as a NUMBER with digit
    separators rather than as an identifier, so it is prefixed to `h_...`. The
    prefix is applied only when the fold is not already a usable identifier, so
    an ordinary hostname keeps the readable token it had.
    """
    token = _snake(host)
    if not token:
        return "a2a"
    if not _IDENT_RE.match(token):
        token = f"h_{token}"
    return token if _IDENT_RE.match(token) else "a2a"


# ---------------------------------------------------------------- naming

def _snake(name: str) -> str:
    """A card-supplied name as a revl identifier candidate. Never trusted to
    be one: `_ident` validates the result and refuses rather than repairing."""
    out = re.sub(r"[^0-9A-Za-z]+", "_", str(name))
    out = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", out)
    return out.strip("_").lower()


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in _snake(name).split("_") if part)


# ---------------------------------------------------------------- refusals

class _Card:
    """One Agent Card, validated. Every accessor either returns a checked
    value or raises; nothing is defaulted into existence."""

    def __init__(self, doc: object, filename: str, *, allow_plaintext: bool,
                 source: str = ""):
        self.filename = filename
        self.source = source
        self.allow_plaintext = allow_plaintext
        if not isinstance(doc, dict):
            self._refuse("an Agent Card must be a JSON object",
                         hint="`revl import a2a` reads an A2A 1.0.0 Agent Card "
                              "(the document an agent serves at "
                              "`/.well-known/agent-card.json`)")
        self.doc: dict = doc
        self.notes: list[str] = []
        self.unprojected: list[str] = []
        self._version()
        self._endpoint()
        self._transport()
        self._capabilities()
        self.skills = self._skills()

    # -- refusal helper ---------------------------------------------------

    def _refuse(self, message: str, *, pointer: str = "#", hint: str | None = None):
        """The family's refusal shape: an Agent Card has no line numbers that
        survive `json.loads`, so the JSON pointer is the authoritative "where"
        and `_line_of` adds a best-effort line on top of it
        (`import_openapi._Reporter`)."""
        raise RevlError(self.filename, _line_of(self.source, pointer),
                        f"{message} at {pointer}", hint=hint)

    # -- item 439 decision (3): version honesty ---------------------------

    def _version(self) -> None:
        claimed = self.doc.get("protocolVersion")
        if claimed is None:
            self._refuse(
                "this Agent Card declares no `protocolVersion`",
                pointer=_pointer("protocolVersion"),
                hint=f"`revl import a2a` binds A2A {A2A_VERSION} exactly. A card "
                     "that does not say which protocol it speaks is not a card "
                     "this importer can bind; nothing here is inferred from the "
                     "shape of the document")
        if not isinstance(claimed, str) or claimed != A2A_VERSION:
            self._refuse(
                f"this Agent Card claims protocol version "
                f"{_comment_safe(json.dumps(claimed))}, and this importer binds "
                f"A2A {A2A_VERSION} exactly",
                pointer=_pointer("protocolVersion"),
                hint="item 439 decision (3): the claim a generated boundary "
                     f"carries is \"A2A {A2A_VERSION}\", never \"A2A\". The "
                     "protocol moves, and a binding that follows it silently "
                     "would be asserting a compatibility nobody checked. If the "
                     "agent really does speak "
                     f"{A2A_VERSION}, fix the card; otherwise this needs a "
                     "binding for the version it does speak")

    # -- the endpoint, and the reach it derives (D-424c.10) ---------------

    def _endpoint(self) -> None:
        url = self.doc.get("url")
        if not isinstance(url, str) or not url:
            self._refuse("this Agent Card declares no `url` to reach the agent at",
                         pointer=_pointer("url"),
                         hint="the endpoint is where the reach bound comes from; "
                              "without one there is no capability to declare and "
                              "no address to call")
        match = _URL_RE.match(url)
        if match is None:
            # The refusal never echoes the offending URL: it is card-supplied
            # text and the whole reason for the check is that such text reaches
            # generated source.
            self._refuse(
                "the Agent Card's `url` is not a plain absolute http(s) URL",
                pointer=_pointer("url"),
                hint="the endpoint is interpolated into generated source (a "
                     "comment and a host body), so it is held to a strict "
                     "character class: scheme `http` or `https`, then only "
                     "RFC 3986 authority and path characters. Quotes, braces, "
                     "backslashes, whitespace and control characters are "
                     "refused rather than escaped")
        if match.group("scheme") != "https" and not self.allow_plaintext:
            self._refuse(
                "the Agent Card's `url` is plaintext `http`",
                pointer=_pointer("url"),
                hint="an A2A peer sits outside this composition's trust "
                     "boundary and everything crossing to it is authority "
                     "leaving the process. Pass `--allow-plaintext` to import "
                     "it anyway (a loopback development agent, say); the "
                     "generated header records that you did")
        self.url = url
        self.host = _authority_host(url)
        if not self.host:
            self._refuse("the Agent Card's `url` has no host to derive a reach from",
                         pointer=_pointer("url"))
        if _redact_userinfo(url) != url:
            # Item 421 F4: a credential in the authority is a live secret. It
            # is dropped here — from the comment, from the host body and from
            # the capability token — and its presence is reported without
            # reproducing any part of it.
            self.notes.append(
                "the Agent Card's `url` carried authority userinfo (a "
                "credential). It has been REDACTED from this file entirely: it "
                "is absent from the endpoint comment, from the host bodies and "
                "from the capability token. Supply the agent's credential "
                "out-of-band as a capability-bound secret, never in the URL")
        self.endpoint = _redact_userinfo(url)
        self.net_cap = f"net.{_cap_token(self.host)}"

    def _transport(self) -> None:
        preferred = self.doc.get("preferredTransport", _TRANSPORT)
        if preferred != _TRANSPORT:
            self._refuse(
                f"this Agent Card prefers the "
                f"{_comment_safe(json.dumps(preferred))} transport, and slice 1 "
                f"of the A2A binding speaks JSON-RPC 2.0 only",
                pointer=_pointer("preferredTransport"),
                hint="A2A 1.0.0 defines JSONRPC, GRPC and HTTP+JSON. The other "
                     "two are a transport each, not an approximation of this "
                     "one, so they are refused rather than guessed at")
        extra = self.doc.get("additionalInterfaces")
        if isinstance(extra, list) and extra:
            transports = sorted({
                str(entry.get("transport")) for entry in extra
                if isinstance(entry, dict) and entry.get("transport")
            })
            if transports:
                self.unprojected.append(
                    "`additionalInterfaces` advertising "
                    f"{_comment_safe(', '.join(transports))} — recorded, not "
                    "projected; this file speaks JSON-RPC 2.0 to `url` only")

    def _capabilities(self) -> None:
        caps = self.doc.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}
        if caps.get("streaming"):
            self.unprojected.append(
                "`capabilities.streaming` — `message/stream` and "
                "`tasks/resubscribe` are NOT projected. Mapping an A2A Task's "
                "lifecycle onto revl (one emission? a stream, item 130? a "
                "session, item 250?) is item 439's open question and this "
                "slice does not pre-empt it")
        if caps.get("pushNotifications"):
            self.unprojected.append(
                "`capabilities.pushNotifications` — webhook delivery is NOT "
                "projected. An inbound callback is a provision, not a client "
                "call, and it needs the lifecycle answer above first")
        if caps.get("stateTransitionHistory"):
            self.unprojected.append(
                "`capabilities.stateTransitionHistory` — NOT projected; this "
                "slice observes one terminal crossing and no history")
        extensions = caps.get("extensions")
        if isinstance(extensions, list) and extensions:
            optional: set[str] = set()
            for index, ext in enumerate(extensions):
                if not isinstance(ext, dict):
                    continue
                # A2A 1.0.0 AgentExtension.required: the client MUST understand
                # and comply with the extension to interact with the agent. This
                # slice projects NO extension, so it cannot comply with a
                # mandatory one. Recording it as "not projected" and emitting a
                # plain provider anyway would drop a contract the agent declared
                # mandatory — the same dishonesty a non-JSON-RPC transport or a
                # non-terminal task is refused for. So a required extension is a
                # refusal (naming it), and only an optional one is recorded.
                if ext.get("required"):
                    raw_uri = ext.get("uri")
                    named = (_comment_safe(json.dumps(raw_uri))
                             if isinstance(raw_uri, str) and raw_uri
                             else f"the extension at index {index}")
                    self._refuse(
                        f"this Agent Card declares the REQUIRED capability "
                        f"extension {named}, and this binding projects no "
                        f"extension",
                        pointer=_pointer("capabilities", "extensions", index),
                        hint="A2A 1.0.0's `required` means a client must "
                             "understand and comply with the extension to "
                             "interact with the agent. Slice 1 binds only the "
                             "plain `message/send` crossing and projects no "
                             "extension, so it cannot comply; generating a "
                             "provider anyway would silently drop a contract "
                             "the agent declared mandatory. Mark the extension "
                             "optional on the agent, or this needs a binding "
                             "that implements it")
                uri = ext.get("uri")
                if isinstance(uri, str) and uri:
                    optional.add(uri)
            if optional:
                self.unprojected.append(
                    "`capabilities.extensions` declaring "
                    f"{_comment_safe(', '.join(sorted(optional)))} — NOT "
                    "projected. An optional extension is one more unchecked "
                    "claim by the same peer; no extension can grant this "
                    "boundary a property revl would otherwise have had to "
                    "check")
        if self.doc.get("securitySchemes") or self.doc.get("security"):
            self.notes.append(
                "the card declares a security scheme. No credential is "
                "generated into this file — the extern bodies send none. Wire "
                "the agent's credential through a capability-bound secret "
                "(item 256) so it is never a literal in source and never part "
                "of a capability spelling")

    # -- the skills -------------------------------------------------------

    def _modes(self, skill: dict, index: int) -> None:
        for field in ("inputModes", "outputModes"):
            declared = skill.get(field)
            if declared is None:
                declared = self.doc.get(
                    "defaultInputModes" if field == "inputModes" else "defaultOutputModes")
            if declared is None:
                continue
            if not isinstance(declared, list):
                self._refuse(f"`{field}` must be a list of media types",
                             pointer=_pointer("skills", index, field))
            for mode in declared:
                if not isinstance(mode, str) or not mode.split(";")[0].strip().startswith("text/"):
                    self._refuse(
                        f"skill {_comment_safe(json.dumps(skill.get('id')))} "
                        f"declares the non-text modality "
                        f"{_comment_safe(json.dumps(mode))} in `{field}`",
                        pointer=_pointer("skills", index, field),
                        hint="slice 1 of the A2A binding transcribes a skill as "
                             "text in, text out, because that is the only "
                             "modality an Agent Card describes well enough to "
                             "project. A `FilePart` or a `DataPart` has no "
                             "revl spelling here and is refused rather than "
                             "flattened into a string")

    def _skills(self) -> list[tuple[str, str, dict]]:
        skills = self.doc.get("skills")
        if not isinstance(skills, list) or not skills:
            self._refuse(
                "this Agent Card advertises no skills",
                pointer=_pointer("skills"),
                hint="a card with no skills has no callable surface to "
                     "generate. There is nothing to import and nothing to "
                     "invent")
        out: list[tuple[str, str, dict]] = []
        seen: dict[str, str] = {}
        for index, skill in enumerate(skills):
            if not isinstance(skill, dict):
                self._refuse("each entry of `skills` must be an object",
                             pointer=_pointer("skills", index))
            raw_id = skill.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                self._refuse("this skill declares no `id`",
                             pointer=_pointer("skills", index, "id"),
                             hint="the id is what the generated operation is "
                                  "named after and what the call carries as its "
                                  "skill reference; it is not derivable from "
                                  "anything else on the card")
            op = _snake(raw_id)
            if not _IDENT_RE.match(op) or op in KEYWORDS:
                self._refuse(
                    f"skill id {_comment_safe(json.dumps(raw_id))} does not "
                    "yield a usable revl operation name",
                    pointer=_pointer("skills", index, "id"),
                    hint="an operation name must be `[a-z][a-z0-9_]*` after "
                         "case folding and must not be a revl keyword. The id "
                         "is repaired no further than case and separators: a "
                         "silently renamed operation is a boundary nobody can "
                         "match back to the card")
            if op in seen:
                self._refuse(
                    f"skill id {_comment_safe(json.dumps(raw_id))} collides "
                    f"with {_comment_safe(json.dumps(seen[op]))} on the "
                    f"operation name `{op}`",
                    pointer=_pointer("skills", index, "id"),
                    hint="two skills that fold onto one operation cannot both "
                         "be called; rename one on the agent")
            seen[op] = raw_id
            self._modes(skill, index)
            out.append((op, raw_id, skill))
        return out


# ---------------------------------------------------------------- host bodies

def _ts_body(endpoint: str, skill_id: str, *, follow_redirects: bool) -> str:
    """The JSON-RPC 2.0 `message/send` crossing, TypeScript.

    Both interpolations are JSON-encoded, and both have already been validated
    (`_URL_RE`) or folded (`_snake`) upstream: the endpoint cannot contain a
    quote, a brace or a newline, and the skill id is re-encoded here anyway so
    the encoding is right even if the caller changes.

    `follow_redirects` is the `--follow-redirects` opt-in and is same-origin
    even when it is on (`revl.crossing_redirect`). The request is bound as a
    reusable `a2aSend` rather than written inline because a declared follow
    re-issues it, and re-issuing has to send the SAME method and the SAME body
    — which is exactly what the default `fetch` does not do.
    """
    url = json.dumps(endpoint)
    sid = json.dumps(skill_id)
    terminal = json.dumps(list(_TERMINAL_STATES))
    policy = ts_policy("a2a", follow=follow_redirects, url_expr=url,
                       send="a2aSend")
    return f"""
      // A2A {A2A_VERSION}, JSON-RPC 2.0 `message/send`. ONE crossing.
      const a2aPayload = JSON.stringify({{
        jsonrpc: "2.0",
        id: crypto.randomUUID(),
        method: "message/send",
        params: {{
          message: {{
            role: "user",
            messageId: crypto.randomUUID(),
            parts: [{{ kind: "text", text: message }}],
            metadata: {{ "revl.skill": {sid} }},
          }},
        }},
      }});
      const a2aSend = (u: string) => fetch(u, {{
        method: "POST",
        // Nothing is followed by the runtime; see the policy below.
        redirect: "manual",
        // A crossing that never returns is not a crossing.
        signal: AbortSignal.timeout({CROSSING_TIMEOUT} * 1000),
        headers: {{ "content-type": "application/json" }},
        body: a2aPayload,
      }});
{policy}      if (!res.ok) {{
        // A transport failure is a FAULT, never a quietly-empty result.
        throw new Error(`a2a: transport failure (HTTP ${{res.status}})`);
      }}
      const rpc = await res.json();
      if (rpc && rpc.error) {{
        throw new Error(`a2a: JSON-RPC error ${{rpc.error.code}}`);
      }}
      const result = rpc ? rpc.result : undefined;
      if (!result) {{
        throw new Error("a2a: response carried no result");
      }}
      // A direct Message reply is already terminal; a Task is terminal only in
      // the states A2A {A2A_VERSION} says are.
      const kind = result.kind;
      const terminal = {terminal};
      if (kind === "task") {{
        const state = result.status ? result.status.state : undefined;
        if (!terminal.includes(state)) {{
          // Item 439's open question: a task still in flight is a LIFECYCLE
          // this slice does not express. Refuse; never poll, never resume.
          throw new Error(
            `a2a: task returned non-terminal state '${{state}}' — this binding \
crosses once and does not poll`);
        }}
        if (state !== "completed") {{
          throw new Error(`a2a: task ended '${{state}}'`);
        }}
      }} else if (kind !== "message") {{
        throw new Error(`a2a: unexpected result kind '${{kind}}'`);
      }}
      // The reply is the peer's JSON and nothing about it is typed: `res.json()`
      // is `any` on every ts lib, so an unannotated `(p) => ...` here is an
      // implicit `any` parameter and `tsc --strict` refuses the module (issue
      // #251, the second defect the a2a-to-ts fixture found). The shape below
      // is a CLAIM being checked, not a type being trusted — every field stays
      // optional and the `kind`/`typeof` guards below do the real work.
      type A2aPart = {{ kind?: unknown; text?: unknown }};
      type A2aArtifact = {{ parts?: A2aPart[] }};
      const parts: A2aPart[] = kind === "task"
        ? ((result.artifacts as A2aArtifact[] | undefined) || [])
            .flatMap((a: A2aArtifact) => a.parts || [])
        : ((result.parts as A2aPart[] | undefined) || []);
      const text = parts
        .filter((p: A2aPart) => p && p.kind === "text" && typeof p.text === "string")
        .map((p: A2aPart) => p.text as string)
        .join("");
      if (text.length === 0 && parts.length > 0) {{
        throw new Error("a2a: reply carried only non-text parts");
      }}
      return text;
    """


def _py_body(endpoint: str, skill_id: str, *, follow_redirects: bool) -> str:
    """The same crossing, Python. Same interpolation discipline.

    `follow_redirects` is the `--follow-redirects` opt-in and is same-origin
    even when it is on (`revl.crossing_redirect`).
    """
    url = json.dumps(endpoint)
    sid = json.dumps(skill_id)
    terminal = json.dumps(list(_TERMINAL_STATES))
    policy = py_policy("a2a", follow=follow_redirects)
    return f"""
    import json as _json, urllib.request as _req, urllib.parse as _urlp
    import uuid as _uuid
    # A2A {A2A_VERSION}, JSON-RPC 2.0 `message/send`. ONE crossing.
    _payload = _json.dumps({{
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "message/send",
        "params": {{"message": {{
            "role": "user",
            "messageId": str(_uuid.uuid4()),
            "parts": [{{"kind": "text", "text": message}}],
            "metadata": {{"revl.skill": {sid}}},
        }}}},
    }}).encode()
    _r = _req.Request({url}, data=_payload,
                      headers={{"content-type": "application/json"}})
{policy}    try:
        # A crossing that never returns is not a crossing.
        with _opener.open(_r, timeout={CROSSING_TIMEOUT}) as _resp:
            _rpc = _json.loads(_resp.read())
    except _RedirectRefused:
        # The refusal names the rule. It is NOT a transport failure and must
        # not be flattened into one.
        raise
    except Exception as _exc:
        # A transport failure is a FAULT, never a quietly-empty result.
        raise RuntimeError("a2a: transport failure") from _exc
    if _rpc.get("error"):
        raise RuntimeError("a2a: JSON-RPC error %s" % _rpc["error"].get("code"))
    _result = _rpc.get("result")
    if not _result:
        raise RuntimeError("a2a: response carried no result")
    _kind = _result.get("kind")
    if _kind == "task":
        _state = (_result.get("status") or {{}}).get("state")
        if _state not in {terminal}:
            # Item 439's open question: a task still in flight is a LIFECYCLE
            # this slice does not express. Refuse; never poll, never resume.
            raise RuntimeError(
                "a2a: task returned non-terminal state %r - this binding "
                "crosses once and does not poll" % (_state,))
        if _state != "completed":
            raise RuntimeError("a2a: task ended %r" % (_state,))
        _parts = [p for a in (_result.get("artifacts") or [])
                  for p in (a.get("parts") or [])]
    elif _kind == "message":
        _parts = _result.get("parts") or []
    else:
        raise RuntimeError("a2a: unexpected result kind %r" % (_kind,))
    _text = "".join(p.get("text", "") for p in _parts
                    if p.get("kind") == "text" and isinstance(p.get("text"), str))
    if not _text and _parts:
        raise RuntimeError("a2a: reply carried only non-text parts")
    return _text
    """


_BODIES = {"ts": _ts_body, "py": _py_body}

#: The backends whose crossing SUSPENDS, and whose generated operation is
#: therefore declared `async` (roadmap item 80, docs/design/async-extern.md).
#:
#: Issue #251: this importer used to declare every operation synchronous and
#: hand the ts tier a body that `await`s `fetch`, which emits `await` inside a
#: non-`async` function — output no `tsc` will accept. JavaScript has no
#: blocking fetch, so "write the body synchronously" is not available on that
#: tier (async-extern.md, opening paragraph); the only two answers are to
#: declare the operation `async`, or to refuse the tier.
#:
#: **This importer declares it `async`, and PR #250's `remote` row refuses the
#: same tier, and both are right** — because they own different amounts of the
#: surface. A `remote` row synthesizes only a PROVIDER for a service somebody
#: else already wrote; its whole point is that "the consumer does not change by
#: one character" when a local provider becomes a remote one, so it cannot
#: colour a `service` declaration it did not write without breaking the
#: property it exists to preserve. `revl import a2a` writes the service, the
#: extern and the provider in one file from one card. Nothing it would have to
#: recolour predates it, so it declares the operation the colour the crossing
#: actually has instead of refusing the tier.
#:
#: What that means for a caller, stated plainly rather than left to be
#: discovered: the generated `service` declares `emission async fn`, so a
#: consumer binding it must call the operation from an async context — an
#: `async fn` provide method or a lifecycle `call`. It is a DECLARED property
#: of the service, which is exactly A1's rule: asynchrony crosses a component
#: boundary only by declaration and is never smuggled in by a provider
#: (async-extern.md §3). The colour is propagated, not asserted: the checker's
#: own async-propagation rule refuses a sync provide method that reaches an
#: async extern, so the generated provide method carries `async fn` too and the
#: ts emitter's `await` insertion is the frontend-admitted one, not a second
#: opinion. Nothing in revl's type language changes — `-> Untrusted[Str]` still
#: means `Untrusted[Str]`, and the `Promise` lives only in the emitted ts
#: (async-extern.md §2).
#:
#: `py` stays SYNC, and that is the same rule rather than an exception. The
#: importer emits a SINGLE-TIER file: the extern carries exactly one host body,
#: and the colour it declares is the colour of THAT body. The `@py` body is
#: `urllib.request.urlopen`, which blocks rather than suspends — py is a
#: coloured tier, so declaring it `async` would wrap a blocking call in an
#: `async def`, stall the caller's event loop, and colour every py caller for a
#: suspension that never happens. That is §2's own three-family split (colour
#: where the tier suspends, blocking where it blocks) read off the one body
#: present. If a future slice ever ships both bodies in one file, the colour
#: has to be `async` for both and the `@py` body has to stop blocking; it does
#: not ship both today.
_ASYNC_BACKENDS = frozenset({"ts"})


# ---------------------------------------------------------------- generation

class _Generator:
    def __init__(self, card: _Card, filename: str, backend: str,
                 service: str | None, *, follow_redirects: bool = False):
        self.card = card
        self.filename = filename
        self.backend = backend
        # `--follow-redirects`, off unless the operator declared it, and
        # same-origin even then (`revl.crossing_redirect`). Recorded in the
        # header the way `--allow-plaintext` is: a generated file states the
        # policy it was generated under.
        self.follow_redirects = follow_redirects
        # issue #251: the crossing's colour, from the one host body this file
        # ships (`_ASYNC_BACKENDS`). Rendered into all three declarations at
        # once — the service operation, the extern and the provide method — so
        # the async-propagation rule (A1) is satisfied by construction rather
        # than by the checker catching a half-coloured file.
        self.async_kw = "async " if backend in _ASYNC_BACKENDS else ""
        name = service or card.doc.get("name") or "agent"
        self.service = _pascal(name)
        self.key = _snake(name)
        if not self.service or not _IDENT_RE.match(self.key) or self.key in KEYWORDS:
            raise RevlError(
                filename, 0,
                f"the agent name {_comment_safe(json.dumps(name))} does not "
                "yield a usable revl service name",
                hint="pass `--service NAME` to name the generated service "
                     "yourself; nothing is invented from the card's other "
                     "fields")

    def _operation(self, op: str, skill_id: str, skill: dict) -> tuple[list[str], str, str]:
        summary = skill.get("name") or skill.get("description")
        lines = [f"  // skill `{_comment_safe(skill_id)}`"
                 + (f" — {_comment_safe(summary)}" if summary else "")]
        tags = skill.get("tags")
        if isinstance(tags, list) and tags:
            lines.append("  // tags (the agent's own claim): "
                         + _comment_safe(", ".join(str(t) for t in tags)))
        lines.append("  // `emission` with NO inverse: a remote effect has no "
                     "local undo (see the header).")
        lines.append("  // The result is `Untrusted[Str]` — the peer is not "
                     "this composition's trust domain.")
        if self.async_kw:
            lines.append("  // `async`: a network round trip SUSPENDS on this "
                         "tier, so callers need an")
            lines.append("  // async context (see the header).")
        lines.append(f"  emission {self.async_kw}fn {op}(message: Str) -> Untrusted[Str]")

        extern = f"a2a_{self.key}_{op}"
        body = _BODIES[self.backend](self.card.endpoint, skill_id,
                                     follow_redirects=self.follow_redirects)
        extern_decl = (
            f"extern emission[{self.card.net_cap}] {self.async_kw}fn "
            f"{extern}(message: Str) -> Untrusted[Str]\n"
            f"  = @{self.backend} {{{body}}}"
        )
        provide = f"    {self.async_kw}fn {op}(message) = {extern}(message)"
        return lines, extern_decl, provide

    def emit(self) -> str:
        op_lines: list[str] = []
        externs: list[str] = []
        provides: list[str] = []
        for op, skill_id, skill in self.card.skills:
            lines, extern, provide = self._operation(op, skill_id, skill)
            op_lines.extend(lines)
            externs.append(extern)
            provides.append(provide)

        parts = [self._header()]
        parts.append(f"service {self.service} {{\n" + "\n".join(op_lines) + "\n}")
        parts.extend(externs)
        parts.append(
            f"component {self.service}Provider provides {self.key}: {self.service} {{\n"
            f"  provide {self.key} {{\n" + "\n".join(provides) + "\n  }\n}")
        return "\n\n".join(parts) + "\n"

    def _header(self) -> str:
        doc = self.card.doc
        # Every card-derived value on these lines is `_comment_safe`d: a
        # newline in any of them would end the `//` comment and drop whatever
        # follows onto its own line of compiled source (item 416f, found in the
        # sibling importer and the same hole here).
        lines = [
            "// Generated by `revl import a2a` — do not edit by hand.",
            f"// Source Agent Card: {_comment_safe(self.filename)}",
            f"// Protocol: A2A {A2A_VERSION} over JSON-RPC 2.0 (`message/send`).",
        ]
        if doc.get("name"):
            lines.append(f"// Agent: {_comment_safe(doc.get('name'))}"
                         + (f" {_comment_safe(doc.get('version'))}"
                            if doc.get("version") else ""))
        if doc.get("description"):
            lines.append(f"// Described (by itself) as: {_comment_safe(doc.get('description'))}")
        lines += [
            f"// Endpoint: {_comment_safe(self.card.endpoint)}",
            f"// Reach: `{self.card.net_cap}` — derived from the endpoint's HOST "
            "only, never",
            "//   from a port and never from userinfo (item 424 D-424c.10).",
            "//",
            "// THE AGENT CARD IS A CLAIM, NOT A SPECIFICATION.",
            "//",
            "// This boundary is TRUSTED, NOT CHECKED (G8), and it is trusted",
            "// differently from the rest of the import family. WIT and OpenAPI",
            "// describe specified interfaces; a Cordis plugin is at least source I",
            "// hold. An A2A agent is a process someone else runs, and this card is",
            "// what that process says about itself. Nothing on it is checked by",
            "// anyone. Every skill, modality and version below is the peer's own",
            "// assertion (item 439 decision (2); item 329's untrusted-author case,",
            "// by construction rather than by policy).",
            "//",
            "// THIS FILE MAKES NO CLAIM ABOUT WHAT THE PEER RUNS. Importing a card",
            "// does not admit, verify or re-admit the agent, and there is no",
            "// \"verified remote\" badge to be had: item 337 requires the RECEIVER to",
            "// re-compile from its own independently held source, and a client is the",
            "// sender (item 424 D-424c.8). What IS bounded is local and only local:",
            "// the reach, the capability, the taint and the failure mode below.",
            "//",
            "// Every result is `Untrusted[Str]` (item 424 D-424c.9), so the checker",
            "// refuses a card result that reaches an authority-granting sink (G9)",
            "// until a `verified fn` parses it or an `endorse[...]` declassifies it at",
            "// a declared, auditable point. A generated client looks exactly like a",
            "// local provider at every call site; the taint is what keeps that from",
            "// being invisible.",
            "//",
            "// TEARDOWN: A REMOTE A2A CALL CANNOT PARTICIPATE IN G7, AND THAT IS A",
            "// DECISION, NOT A GAP.",
            "//",
            "// revl's teardown stack has three entry kinds",
            "// (docs/design/teardown-contract.md), and an A2A skill can be neither of",
            "// the first two:",
            "//   * `bracket` (an acquire's release) must be INFALLIBLE by contract",
            "//     (G5). An inverse that travels over a network is fallible by",
            "//     construction — the peer may be unreachable, restarted or gone by",
            "//     teardown time.",
            "//   * `transactional` (item 243) needs a HOST-LOCAL inverse and a witness",
            "//     captured on the `Ok` branch. The agent's state is not host-local,",
            "//     and any witness it returns is one more claim from the same peer.",
            "//",
            "// A2A's `tasks/cancel` is not an inverse either: it asks a RUNNING task to",
            "// stop, the agent may refuse it, and it says nothing about a task already",
            "// in a terminal state. So nothing below calls it at teardown and no",
            "// inverse is synthesized anywhere in this file.",
            "//",
            "// Every operation is therefore `emission` with NO inverse — G4's other",
            "// branch, the one that means `declared irreversible`. When your",
            "// composition unwinds, the remote effect STAYS: a clean unload replays",
            "// nothing here, and an abort replays nothing either unless you attach a",
            "// `compensate` BY HAND. That third kind is the only one an A2A call can",
            "// ever carry; it is audit-grade and best-effort (it may fail into",
            "// `compensation-residue`), and it is yours to write, because only you",
            "// know which remote operation undoes which. The card does not say, and",
            "// this importer will not guess.",
            "//",
            "// THE ENDPOINT ABOVE IS THE ENDPOINT. A REDIRECT IS REFUSED.",
            "//",
            "// The reach bound is derived from the endpoint's host, so a",
            "// transport that followed a `Location` to another host would make",
            "// that bound stop describing where this crossing can reach — and a",
            "// 301/302/303 re-issues the POST as a GET with the body dropped, so",
            "// the declared emission would become a read. Both tiers' clients do",
            "// that by DEFAULT (`urllib` follows; `fetch` defaults to",
            "// `redirect: \"follow\"`), so the generated body installs a policy",
            "// that refuses instead, naming the rule and reporting only the",
            "// target's ORIGIN (a `Location`'s userinfo would be a live",
            "// credential). Nothing on the original request — no header, no",
            "// credential, no `Secret[T]` — travels to an origin this file did",
            "// not name.",
            "//",
            ("// FOLLOWING IS DECLARED HERE (`--follow-redirects`), and is still "
             "bounded:"
             if self.follow_redirects else
             "// Following is NOT declared here. Pass `--follow-redirects` to "
             "allow it, bounded to:"),
            "//   a SAME-ORIGIN 307 or 308, at most five hops. A 301, 302 or 303",
            "//   is refused whatever the flag says, because it changes the",
            "//   method; a cross-origin hop is refused because the endpoint is",
            "//   the reach bound.",
            "//",
            f"// The crossing is bounded in time at {CROSSING_TIMEOUT}s. A peer that "
            "accepts the",
            "// connection and then says nothing is a fault, not a wait.",
            "//",
            "// A transport failure is a FAULT raised out of the host body, never a",
            "// quietly-empty result. Turning it into provider WITHDRAWAL, and the",
            "// `on_failure(result)` opt-in, arrive with item 424(c)'s `remote` row",
            "// (C2), which is not built; until then this imports as an ordinary",
            "// provider component like the rest of the family.",
            "//",
            "// SCOPE OF THIS SLICE (roadmap item 439, slice 1). Bound: the Agent Card,",
            "// and a `message/send` whose task reaches a TERMINAL state in that one",
            "// crossing — the subset where \"does an A2A Task map to one emission, to a",
            "// stream (item 130), or to a session (item 250)?\" does not arise. A",
            "// non-terminal reply (`working`, `input-required`, `auth-required`) is a",
            "// fault at the boundary; the generated body refuses to poll, refuses to",
            "// resume, and refuses to guess.",
            "//",
            "// A skill is transcribed as text in, text out, because that is all an",
            "// Agent Card describes. It carries no parameter or result schema, so",
            "// nothing richer is available to transcribe and nothing richer is",
            "// invented.",
        ]
        if self.async_kw:
            lines += [
                "//",
                "// COLOUR: EVERY OPERATION BELOW IS `async`, AND THAT IS PART OF THE",
                "// SERVICE, NOT AN IMPLEMENTATION DETAIL.",
                "//",
                "// A JSON-RPC round trip to another process suspends the calling task,",
                "// and this tier has no blocking fetch to hide that behind. So the",
                "// operation is DECLARED `async` (roadmap item 80,",
                "// docs/design/async-extern.md), on all three of the service operation,",
                "// the extern and the provide method. Asynchrony crosses a component",
                "// boundary only by declaration (§3): a consumer reads `async fn` off",
                "// the service and calls it from an async context — an `async fn`",
                "// provide method, or a lifecycle `call`. It is never smuggled in by a",
                "// provider, and flipping it is a breaking change to the service.",
                "//",
                "// Nothing about the VALUE changes: `-> Untrusted[Str]` still means",
                "// `Untrusted[Str]` (§2). The `Promise` is a tier artifact of the",
                "// emitted TypeScript and appears nowhere in revl's type language.",
                "//",
                "// The `@py` binding of the same card is SYNC, because its body blocks",
                "// rather than suspends. The colour a generated file declares is the",
                "// colour of the one host body it carries.",
            ]
        if self.card.unprojected:
            lines.append("//")
            lines.append("// NOT PROJECTED (recorded so it is visible, not silently dropped):")
            for item in self.card.unprojected:
                lines.append(f"//   * {item}")
        for note in self.card.notes:
            lines.append("//")
            lines.append(f"// note: {note}")
        return "\n".join(lines)


# ----------------------------------------------------------------- public API

def import_a2a(document: object, *, filename: str = "<agent-card>",
               backend: str = "ts", service: str | None = None,
               allow_plaintext: bool = False, source: str = "",
               follow_redirects: bool = False) -> str:
    """An A2A 1.0.0 Agent Card (already parsed) -> revl source.

    `source` is the card's raw text when the caller has it, used only to put a
    best-effort line number on a refusal; the JSON pointer in the message is
    the authoritative location either way.
    """
    if backend not in _BODIES:
        raise RevlError(filename, 0,
                        f"unsupported backend `{backend}`",
                        hint=f"pick one of: {', '.join(sorted(_BODIES))}")
    card = _Card(document, filename, allow_plaintext=allow_plaintext,
                 source=source)
    return _Generator(card, filename, backend, service,
                      follow_redirects=follow_redirects).emit()


def load_card(text: str, *, filename: str = "<agent-card>") -> object:
    try:
        return json.loads(text)
    except ValueError as error:
        raise RevlError(filename, 0, f"not valid JSON: {error}",
                        hint="an Agent Card is a JSON document") from None


def import_a2a_file(path: str, *, backend: str = "ts",
                    service: str | None = None,
                    allow_plaintext: bool = False,
                    follow_redirects: bool = False) -> str:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    return import_a2a(load_card(text, filename=path), filename=path,
                      backend=backend, service=service,
                      allow_plaintext=allow_plaintext, source=text,
                      follow_redirects=follow_redirects)
