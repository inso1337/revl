"""`revl import openapi`: item 421 F4, URL credential vs capability token.

The defect: `_net_cap` derived the `net.<host>` capability token by splitting
the server URL's authority on `:` before `@`. For a URL whose userinfo carries
a bare API key with no password
(`https://sk_live_SEKRIT:@api.example.com/v1`), that ordering strips the
authority down to the key BEFORE the `@`-split ever runs, so the emitted
token was `net.sk_live_sekrit`, a live credential snake-cased just enough
that a reviewer scrubbing URLs for secrets does not recognize it. Second
half: because the token named the USERNAME and not the host, two different
credentials against two different hosts (`admin@bank.example.com`,
`admin@attacker.example.net`) collapsed onto the identical token `net.admin`,
so a capability grant for one authorized the other.

Verified end to end before the fix: the raw key landed in the generated
`.rvl` (as the capability token, in the header's `// Server:` comment, and in
every generated extern's host-call comment) and in the compiled IR (both as
the corrupted token and via the same comments, which ride into the IR as the
extern's host body text).

The fix (`_authority_host`) parses the authority properly instead of patching
the `:` split: userinfo is everything before the LAST `@` (so a password
containing `@` or `:` does not truncate early), the authority ends at the
first `/`, `?` or `#`, and a bracketed IPv6 literal is recognized so its
embedded `:` is not mistaken for a port separator. The token is derived from
host alone, never port, matching the pre-existing (buggy but intentional)
behaviour of dropping the port. A second, independent sink is closed by
`_redact_userinfo`: the raw server URL echoed into the header comment and
into each generated extern's host-call comment is stripped of userinfo
before being written, so the credential does not reach generated source
through that path either, even though it does not feed the capability token.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.import_openapi import (  # noqa: E402
    _authority_host,
    _redact_userinfo,
    import_openapi,
)

_STR = {"type": "string"}
_OK = {"200": {"description": "ok",
               "content": {"application/json": {"schema": _STR}}}}
_ID_PARAM = [{"name": "id", "in": "path", "required": True, "schema": _STR}]
_BODY = {"content": {"application/json": {"schema": _STR}}}

_CREDENTIAL_URL = "https://sk_live_SEKRIT:@api.example.com/v1"
_SECRET = "sk_live_SEKRIT"


def _doc(server_url: str) -> dict:
    """A GET-preimage + compensate-grade PUT document, server URL supplied by
    the caller: this is the shape that puts the server URL on every sink
    the finding names (capability token, header comment, host-call comment)."""
    get_op = {"operationId": "getConfig", "parameters": _ID_PARAM, "responses": _OK}
    put_op = {"operationId": "setConfig", "parameters": _ID_PARAM,
              "requestBody": _BODY, "responses": _OK,
              "x-revl-compensate": True, "x-revl-preimage": "getConfig",
              "x-revl-undo": "setConfig"}
    return {"openapi": "3.0.3", "info": {"title": "Config API", "version": "1.0.0"},
            "servers": [{"url": server_url}],
            "paths": {"/config/{id}": {"get": get_op, "put": put_op}}}


def _compile_src(src: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".rvl", delete=False) as handle:
        handle.write(src)
        path = handle.name
    return compile_files([path])


# ------------------------------------------------ the reproducer, end to end

def test_credential_does_not_reach_the_capability_token():
    """The crux: a bare-key userinfo must never become the token."""
    src = import_openapi(_doc(_CREDENTIAL_URL), backend="py")
    assert "net.api_example_com" in src
    assert "net.sk_live_sekrit" not in src.lower()


def test_credential_appears_in_no_generated_artifact():
    """Every sink the finding names: generated `.rvl` source AND the compiled
    IR (which carries the extern host bodies, so a credential embedded in a
    comment reaches the IR too). The secret must be absent, case-insensitively,
    from both, not merely absent from the capability token."""
    src = import_openapi(_doc(_CREDENTIAL_URL), backend="ts")
    assert _SECRET.lower() not in src.lower()

    ir = _compile_src(src)
    ir_text = json.dumps(ir)
    assert _SECRET.lower() not in ir_text.lower()

    # the capability scope enumerated in the IR is the fixed, host-derived
    # token on every extern that carries one
    caps = {cap for ext in ir["externs"] for cap in ext.get("capabilities", [])}
    assert caps == {"net.api_example_com"}


def test_header_comment_does_not_echo_the_credential():
    """A second sink, independent of the capability token: the generated
    header's `// Server:` line used to be a verbatim copy of the document's
    server URL, credential included."""
    src = import_openapi(_doc(_CREDENTIAL_URL), backend="ts")
    header_lines = [line for line in src.splitlines() if line.startswith("// Server:")]
    assert header_lines == ["// Server: https://api.example.com/v1"]


def test_host_call_comments_do_not_echo_the_credential():
    """A third sink: every generated extern's host-call comment interpolates
    the server URL against the operation path. The forward call, and the
    compensate reversal's comment, must both be scrubbed."""
    src = import_openapi(_doc(_CREDENTIAL_URL), backend="ts")
    for line in src.splitlines():
        if "GET" in line or "PUT" in line:
            assert "sk_live" not in line.lower(), line


# --------------------------------------------- second half: no token collapse

def test_different_hosts_never_collapse_to_the_same_token_via_username():
    """Before the fix, the token named the USERNAME, not the host, so two
    different credentials against two different hosts produced the identical
    token `net.admin`, so an operator policy scoped to one host would cover
    the other. Confirmed here at the `_authority_host` level (the direct
    cause) and through full import (the observable capability token)."""
    assert _authority_host("https://admin:pw1@bank.example.com") == "bank.example.com"
    assert _authority_host("https://admin:pw2@attacker.example.net") == "attacker.example.net"

    bank_src = import_openapi(_doc("https://admin:pw1@bank.example.com"), backend="py")
    attacker_src = import_openapi(_doc("https://admin:pw2@attacker.example.net"), backend="py")
    assert "net.bank_example_com" in bank_src
    assert "net.attacker_example_net" in attacker_src
    assert "net.admin" not in bank_src
    assert "net.admin" not in attacker_src


# ------------------------------------------------------------- authority edge cases

def test_authority_host_userinfo_containing_at_and_colon():
    """Userinfo is everything before the LAST `@`: a password legally
    containing `@` or `:` must not truncate the split early."""
    assert _authority_host("https://user:p@ss:w@host.example.com/x") == "host.example.com"


def test_authority_host_empty_userinfo():
    assert _authority_host("https://:@host.example.com/x") == "host.example.com"
    assert _authority_host("https://@host.example.com/x") == "host.example.com"


def test_authority_host_port_with_no_userinfo():
    assert _authority_host("https://host.example.com:8443/v1") == "host.example.com"


def test_authority_host_ipv6_literal():
    assert _authority_host("https://[::1]:8080/v1") == "::1"
    assert _authority_host("https://user:pass@[::1]:8080/v1") == "::1"
    assert _authority_host("https://[2001:db8::1]/v1") == "2001:db8::1"


def test_authority_host_no_server():
    assert _authority_host("") == ""


def test_redact_userinfo_preserves_path_query_and_fragment():
    assert (_redact_userinfo("https://u:p@host.example.com/a/b?x=1#frag")
            == "https://host.example.com/a/b?x=1#frag")
    assert _redact_userinfo("https://host.example.com/v1") == "https://host.example.com/v1"
    assert _redact_userinfo("") == ""
