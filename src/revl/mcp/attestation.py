"""A remote MCP server's identity, bound to a key and to its tool manifest.

The MCP proxy gates what a call MAY do. It said nothing about WHO answered: a
server was trusted because something listened at the address it was pointed at,
so an upstream swapped for another process (a different server, or the same
server with one more tool) was indistinguishable from the one the operator
meant to run. This module is the other half of that boundary.

What is signed
--------------
A **server attestation** is an asymmetric signature over a small record::

    {kind, version, server_id, server_version, session, issued_at,
     expires_at, tools, manifest_digest, capabilities,
     sign_alg, key_id, public_key, signature}

* ``server_id`` names the server, and ``session`` names one run of it: two
  records that claim the same session but different keys are two servers, and
  a verifier can notice.
* ``tools`` maps every tool name to the sha256 of that tool's canonical JSON
  (the whole object: description, schema, annotations, extensions included).
  ``manifest_digest`` is the sha256 of the canonical ``tools`` object, so the
  whole list has one value to compare and each tool has one value to NAME when
  it changed.
* ``capabilities`` is the server's own declared scope. It is signed so it can
  be read back honestly, not so a verifier believes it: the client's pin
  declares what IT allows, and the smaller of the two is enforced.
* ``expires_at`` bounds a record's life. An expired attestation is refused
  exactly as a bad signature is, because it makes the same claim ("this key
  answered for this manifest") about a moment nobody is checking now.

The signature is ``peer_identity.sign_record``: deterministic ECDSA over NIST
P-256, the tree's one asymmetric primitive (issue #1278, item 272). Nothing
here implements a curve, a hash or an encoding, and the record carries the
signer's ``public_key`` INSIDE the covered set so a swapped key is a signature
failure and not a successful verification under an attacker's key.

What a verified record proves, and under what assumption
--------------------------------------------------------
It proves that the holder of the private half of the pinned key produced these
tool digests, for this server id, at this session. That is all. It does not
prove the tools are safe, the code is correct, or the answer is true: it
proves the ANSWER CAME FROM THE SERVER THE CLIENT PINNED, and that the tool
list is the one that server signed. A server that lies about its tools still
signs its lie, which is why every response is labelled untrusted data
separately (see `origin`).

The deployment assumption is the one `peer_identity` already states: the
private half was generated on the server's own machine and the public half
reached the operator out of band. A pin taken from the server at the same
moment the server is first trusted proves continuity, not identity.

The pin
-------
An operator pins a server by writing a **pin** file (``revl.mcp.pin``): the
server id, the public key, and the tool digests the operator means to trust.
``revl mcp serve --attestation-pin-out PATH`` writes one; a hand-written one
is accepted if it has the same shape. The pin carries no private material and
is safe to commit beside the client configuration.

A pin with no ``tools`` member is refused rather than treated as "trust
whatever list the server sends": the manifest is what the pin is for.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import RevlError
from ..peer_identity import PeerIdentity, sign_record, verify_record

#: The attestation envelope. A verifier refuses a version it does not
#: implement rather than reading an unknown shape as this one.
ATTESTATION_KIND = "revl.mcp.attestation"
ATTESTATION_VERSION = "1.0"

#: The pin envelope.
PIN_KIND = "revl.mcp.pin"
PIN_VERSION = "1.0"

#: Domain separation for the server attestation. `peer_identity`'s records are
#: domain-tagged so an attestation cannot be replayed as another protocol's
#: record under the same key.
ATTESTATION_SIGN_DOMAIN = b"revl.mcp.server/v1\x00"

#: `_meta` key the attestation rides under in an `initialize` or `tools/list`
#: result. One spelling for both ends.
META_ATTESTATION = "revl/attestation"

#: The `_meta` key every proxied result carries its origin under.
META_ORIGIN = "revl/origin"

#: The `kind` of that origin label.
ORIGIN_KIND = "mcp"

#: A tool manifest larger than this is refused rather than digested: the digest
#: is computed over a JSON object, and an unbounded object is a memory request
#: an upstream chooses.
MAX_TOOLS = 4096


class AttestationError(RevlError):
    """An attestation or pin is missing, malformed or refused."""


# ---------------------------------------------------------------- the manifest

def canonical_tool(tool: dict) -> str:
    """The exact bytes a tool's digest covers: sorted keys, compact
    separators, UTF-8.

    The WHOLE tool object is covered, not a chosen subset (item 517's
    discipline): a member added later, an annotation changed, a description
    rewritten is a different digest without a field list to fall out of date.
    """
    return json.dumps(tool, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def tool_digests(tools) -> dict[str, str]:
    """`{name: sha256(canonical tool)}` for a `tools/list` result.

    A tool without a usable string name is skipped, not guessed at: the proxy
    refuses such a tool anyway, and a digest under a name nobody can be refused
    by is worse than no digest. A name repeated in one manifest is refused,
    because two tools under one name make every later comparison ambiguous.
    """
    if not isinstance(tools, list):
        raise AttestationError(
            "<mcp>", 0,
            f"the tool manifest is {type(tools).__name__}, not a list",
            code="MCP-ATTESTATION")
    if len(tools) > MAX_TOOLS:
        raise AttestationError(
            "<mcp>", 0,
            f"the tool manifest lists {len(tools)} tools, more than the "
            f"{MAX_TOOLS} an attestation will digest",
            code="MCP-ATTESTATION")
    digests: dict[str, str] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in digests:
            raise AttestationError(
                "<mcp>", 0,
                f"the tool manifest lists {name!r} twice, so no digest can "
                "name which of the two a client pinned",
                code="MCP-ATTESTATION")
        digests[name] = hashlib.sha256(
            canonical_tool(tool).encode("utf-8")).hexdigest()
    if not digests:
        raise AttestationError(
            "<mcp>", 0,
            "the tool manifest names no tool with a usable name, so there is "
            "nothing to attest",
            code="MCP-ATTESTATION")
    return digests


def manifest_digest(digests: dict[str, str]) -> str:
    """sha256 over the canonical `{name: digest}` object."""
    return hashlib.sha256(
        json.dumps(digests, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the record

def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _parse_iso(value, what: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AttestationError("<mcp>", 0, f"{what} is not an ISO timestamp",
                               code="MCP-ATTESTATION")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AttestationError(
            "<mcp>", 0, f"{what} is not an ISO timestamp: {value!r}",
            code="MCP-ATTESTATION") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_attestation(identity: PeerIdentity, *, server_id: str, tools,
                     session: str | None = None, ttl: float = 3600.0,
                     capabilities=None, now: datetime | None = None) -> dict:
    """Sign one manifest under `identity` and return the record.

    `tools` is the exact `tools/list` list the server is about to answer; its
    digests are computed here so the signed value and the served value cannot
    drift. `session` is generated when omitted, and a caller that re-attests a
    changed list passes the same one so a verifier can tell one run from two.
    """
    if not isinstance(server_id, str) or not server_id:
        raise AttestationError("<mcp>", 0, "a server attestation needs a "
                               "non-empty server_id", code="MCP-ATTESTATION")
    digests = tool_digests(tools)
    issued = now or datetime.now(timezone.utc)
    body = {
        "kind": ATTESTATION_KIND,
        "version": ATTESTATION_VERSION,
        "server_id": server_id,
        "session": session or secrets.token_hex(16),
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + timedelta(seconds=max(0.0, float(ttl)))),
        "tools": digests,
        "manifest_digest": manifest_digest(digests),
        "capabilities": sorted(str(c) for c in (capabilities or ())),
    }
    return sign_record(ATTESTATION_SIGN_DOMAIN, body, identity)


def verify_attestation(record, public_key: bytes, *, now: datetime | None = None
                       ) -> tuple[bool, str]:
    """`(ok, reason)` for one attestation under a public key the verifier
    pins. Never raises: a hostile record must not turn a refusal into a crash
    (the rule `peer_identity.verify_record` states).

    Expiry is ALWAYS checked; `now` exists so a test can name the instant
    rather than sleep. An expired record is refused exactly as a bad signature
    is, because it makes the same claim about a moment nobody is checking.
    """
    if not isinstance(record, dict):
        return False, "the server presented no attestation"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ok, reason = verify_record(ATTESTATION_SIGN_DOMAIN, record, public_key)
    if not ok:
        return False, reason
    if record.get("kind") != ATTESTATION_KIND:
        return False, (f"kind is {record.get('kind')!r}, expected "
                       f"{ATTESTATION_KIND!r}")
    if record.get("version") != ATTESTATION_VERSION:
        return False, (f"version is {record.get('version')!r}, which this "
                       f"verifier does not implement (expected "
                       f"{ATTESTATION_VERSION!r})")
    try:
        expires = _parse_iso(record.get("expires_at"), "expires_at")
        issued = _parse_iso(record.get("issued_at"), "issued_at")
    except AttestationError as error:
        return False, str(error)
    if issued > expires:
        return False, "the attestation expires before it was issued"
    if expires <= current:
        return False, f"the attestation expired at {record.get('expires_at')}"
    digests = record.get("tools")
    if not isinstance(digests, dict) or not digests:
        return False, "the attestation covers no tool manifest"
    try:
        if manifest_digest(digests) != record.get("manifest_digest"):
            return False, ("manifest_digest does not cover the attested tool "
                           "list")
    except (TypeError, ValueError):
        return False, "the attested tool digests are not a string map"
    return True, "valid: the pinned key signed this tool manifest"


# ---------------------------------------------------------------- the pin

def make_pin(identity: PeerIdentity, *, server_id: str, tools,
             capabilities=None) -> dict:
    """The public half of an attestation: what a client pins."""
    digests = tool_digests(tools)
    public = identity.public()
    return {
        "kind": PIN_KIND,
        "version": PIN_VERSION,
        "server_id": server_id,
        "key_id": identity.key_id,
        "public_key": public.public_key_hex,
        "tools": digests,
        "manifest_digest": manifest_digest(digests),
        "capabilities": sorted(str(c) for c in (capabilities or ())),
    }


def load_pin(path) -> dict:
    """Read and validate a pin file, or raise `AttestationError` naming the
    member that is wrong."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise AttestationError(
            "<mcp>", 0, f"cannot read the attestation pin {path}: {error}",
            code="MCP-ATTESTATION") from error
    except json.JSONDecodeError as error:
        raise AttestationError(
            "<mcp>", 0, f"the attestation pin {path} is not JSON: {error}",
            code="MCP-ATTESTATION") from error
    if not isinstance(document, dict):
        raise AttestationError("<mcp>", 0,
                               f"the attestation pin {path} is not an object",
                               code="MCP-ATTESTATION")
    if document.get("kind") != PIN_KIND:
        raise AttestationError(
            "<mcp>", 0,
            f"{path} is not a revl MCP pin: kind is {document.get('kind')!r}, "
            f"expected {PIN_KIND!r}", code="MCP-ATTESTATION")
    if document.get("version") != PIN_VERSION:
        raise AttestationError(
            "<mcp>", 0,
            f"{path} has pin version {document.get('version')!r}, which this "
            f"reader does not implement (expected {PIN_VERSION!r})",
            code="MCP-ATTESTATION")
    for member in ("server_id", "public_key", "tools"):
        if not document.get(member):
            raise AttestationError(
                "<mcp>", 0,
                f"the attestation pin {path} carries no {member}; a pin without "
                "it verifies nothing", code="MCP-ATTESTATION")
    try:
        bytes.fromhex(document["public_key"])
    except (TypeError, ValueError):
        raise AttestationError(
            "<mcp>", 0,
            f"the attestation pin {path} has a public_key that is not hex",
            code="MCP-ATTESTATION") from None
    tools = document["tools"]
    if not isinstance(tools, dict) or not tools:
        raise AttestationError(
            "<mcp>", 0,
            f"the attestation pin {path} carries no tool digest map; a pin "
            "that covers no manifest cannot refuse a changed tool",
            code="MCP-ATTESTATION")
    for name, digest in tools.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise AttestationError(
                "<mcp>", 0,
                f"the attestation pin {path} has a non-string tool digest",
                code="MCP-ATTESTATION")
    # `capabilities` scopes what a pinned proxy serves by tool NAME. A string
    # in its place would match by substring (`"list" in "list_notes"`), which
    # widens the scope, so anything but a list of strings is refused.
    capabilities = document.get("capabilities")
    if capabilities is not None and (
            not isinstance(capabilities, list)
            or not all(isinstance(c, str) for c in capabilities)):
        raise AttestationError(
            "<mcp>", 0,
            f"the attestation pin {path} has capabilities that are not a list "
            f"of tool names: found {capabilities!r}",
            code="MCP-ATTESTATION")
    return document


def write_pin(path, pin: dict) -> None:
    Path(path).write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def verify_pin(record, pin: dict, *, now: datetime | None = None
               ) -> dict:
    """Verify one attestation against one pin.

    Returns ``{"ok", "reason", "changed"}``; ``changed`` names every tool whose
    digest differs from the pin (added, removed or edited), so a refusal can
    name the tool instead of only saying the manifest moved. Ordering is
    deterministic, which keeps a refusal text stable enough to assert.
    """
    public_key = bytes.fromhex(pin["public_key"])
    ok, reason = verify_attestation(record, public_key, now=now)
    if not ok:
        return {"ok": False, "reason": reason, "changed": []}
    if record.get("server_id") != pin["server_id"]:
        return {"ok": False, "reason": (f"server_id: pinned "
                                        f"{pin['server_id']!r}, server claims "
                                        f"{record.get('server_id')!r}"),
                "changed": []}
    pinned = pin["tools"]
    attested = record.get("tools") or {}
    changed = sorted(set(pinned) ^ set(attested) |
                     {name for name in set(pinned) & set(attested)
                      if pinned[name] != attested[name]})
    if changed:
        return {"ok": False, "changed": changed,
                "reason": ("the tool manifest does not match the pin: "
                           + ", ".join(changed))}
    # `pin["capabilities"]` is not checked here: it narrows what the proxy
    # serves (an attested tool outside it is excluded, never proxied), so a
    # narrowed pin must still verify against the whole attested manifest.
    return {"ok": True, "reason": "valid: the pinned key signed this tool "
                                  "manifest", "changed": []}


def record_expired(record, *, now: datetime | None = None) -> bool:
    """Whether a record's expiry window has closed.

    Fail closed: a record a reader cannot date is treated as expired, because
    an expiry a parser cannot read is an expiry no one can rely on. This is the
    cheap re-check a long-lived holder runs on every use; `verify_attestation`
    remains the full check (signature, kind, version and window).
    """
    if not isinstance(record, dict):
        return True
    try:
        expires = _parse_iso(record.get("expires_at"), "expires_at")
    except AttestationError:
        return True
    return expires <= (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# ---------------------------------------------------------------- the origin

def origin(record: dict | None = None, *, server_name: str | None = None
           ) -> dict:
    """The untrusted-origin label a result from this server carries.

    It says where the answer came from and that the answer is DATA: an
    attested origin is a statement about the server, never about the truth of
    what it returned. ``attested`` is false for a server no pin was declared
    for, which is exactly the trust-by-address case this module removes when a
    pin is supplied.
    """
    label = {
        "kind": ORIGIN_KIND,
        "trust": "untrusted",
        "attested": record is not None,
    }
    if record is not None:
        label["server"] = record.get("server_id")
        label["keyId"] = record.get("key_id")
        label["session"] = record.get("session")
    elif server_name:
        label["server"] = server_name
    return label
