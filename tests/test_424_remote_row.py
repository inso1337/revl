"""The `remote` row: item 424 gap (c), slice C2.

`docs/design/424-dsh-language-gaps.md` §3.2 (D-424c.1 through D-424c.4) and
§3.3, which names the exit test this file executes:

    Every consumer of a remoted key compiles unchanged and resolves one
    provider; a plain `fn` service is refused as unremotable naming the method;
    two peers of one service in two realms admit and do not collide; a transport
    failure withdraws the provider and the consumer deactivates reactively (the
    R2/R3 path the bridges already test); `on_failure(result)` on a non-`Result`
    method is refused.

Four of those five are covered here in full. The fifth — the RUNTIME half of
`on_failure(withdraw)`, where a transport failure becomes a provider withdrawal
that cascades through R2/R3 — is not, and the reason is measured rather than
assumed: the withdrawal cascade is armed by the placement bridge's monitor
connection (`backends/python/bridge.py:723` `watch(on_lost)`, fired on monitor
EOF at `:747`), which is a seam-client mechanism. A synthesized `remote` row is
not a seam client, so it does not join that path for free. What C2 lands is the
DECLARATION and the contract: `on_failure` is parsed, checked against the
service's return types, carried into the IR and the manifest, and the generated
body raises a fault rather than returning a quietly-empty result. Wiring that
fault into the withdrawal cascade is the runtime half, and `test_on_failure_*`
below pins the declaration so the day the runtime lands, something says so.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import compile_composition, resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.synthesize import cap_token, synthesize_provider  # noqa: E402

BILLING = """
service Billing {
  emission fn charge(account: Str, amount: Int) -> Bool
  emission fn refund(account: Str, amount: Int)
}
"""

LEDGER = """
service Ledger {
  emission fn post(account: Str, amount: Int) -> Result[Str, Str]
}
"""

PLAIN = """
service Metrics {
  fn tick() -> Int
}
"""

# A consumer written with no idea that its provider is remote. Not one word of
# this changes between the local and the remote composition, which is the whole
# of D-424c.1.
CONSUMER = """
service Checkout {
  emission fn pay(account: Str) -> Bool
}

component CheckoutSvc requires billing: Billing provides checkout: Checkout {
  provide checkout {
    fn pay(account) = emit billing.charge(account, 100)
  }
}
"""

LOCAL_PROVIDER = """
component LocalBilling provides billing: Billing {
  provide billing {
    fn charge(account, amount) = true
    fn refund(account, amount) { return }
  }
}
"""


def write(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    return tmp_path


def resolve(tmp_path: Path, doc: str = "base"):
    return resolve_file(str(tmp_path / f"{doc}.rvl"), str(tmp_path))


# ------------------------------------------------------------------ the surface

def test_remote_row_parses_with_contextual_keywords_only():
    """`remote`, `at`, `host`, `through` and `on_failure` head a clause only in
    this one position, so the lexer's KEYWORDS set is untouched and a program
    using any of them as an ordinary name still parses.

    This is the property 426 S2 chose `configure @db with { ... }` to preserve:
    the lexer stays context-free, so the self-host lexer needs no sync.
    """
    program = Parser("""
composition Shop {
  remote @billing provides billing: Billing
    in realm("tenant_a")
    at host("billing.internal:8443")
    through jsonrpc
    on_failure(result)
}
""", "t.rvl").parse()
    row = program.compositions[0].remotes[0]
    assert (row.label, row.key, row.service) == ("billing", "billing", "Billing")
    assert (row.host, row.realm) == ("billing.internal:8443", "tenant_a")
    assert (row.transport, row.on_failure) == ("jsonrpc", "result")

    # The same words as ordinary names, in ordinary code. If `remote` were
    # promoted to a real keyword this would stop parsing, every backend that
    # re-derives the reserved-word set would need the same edit, and the
    # self-host lexer would need a sync. It is not worth that.
    ordinary = Parser("""
service S {
  emission fn go(remote: Str, host: Str, through: Str, on_failure: Str) -> Str
}
""", "t.rvl").parse()
    assert list(ordinary.services[0].methods["go"].params)[0][0] == "remote"


def test_a_remote_row_shares_the_one_label_namespace(tmp_path):
    """A consumer cannot tell a remote row from a file row, so two rows that a
    consumer cannot tell apart must not share a name."""
    write(tmp_path, services=BILLING, local=LOCAL_PROVIDER, base="""
composition Shop {
  use "services.rvl"
  row @billing from "local.rvl" provides billing
  remote @billing provides billing: Billing at host("b.internal:8443")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "duplicate row label `@billing`" in str(excinfo.value)


def test_a_remote_row_needs_a_peer_address():
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition Shop {
  remote @billing provides billing: Billing
}
""", "t.rvl").parse()
    assert "names no peer address" in str(excinfo.value)


# ------------------------------------ exit test 1: the consumer is unchanged

def test_consumer_compiles_unchanged_and_resolves_one_provider(tmp_path):
    """The exit test's first clause, executed against BOTH compositions.

    The consumer source is byte-identical in the two cases; only the
    composition document differs. That is D-424c.1: the wiring is local, every
    consumer keeps `requires billing: Billing`, and remoteness is an admission
    fact rather than a wiring fact.
    """
    write(tmp_path, services=BILLING, consumer=CONSUMER, local=LOCAL_PROVIDER,
          localbase="""
composition Shop {
  use "services.rvl"
  row @checkout from "consumer.rvl" provides checkout
  row @billing from "local.rvl" provides billing
}
""", base="""
composition Shop {
  use "services.rvl"
  row @checkout from "consumer.rvl" provides checkout
  remote @billing provides billing: Billing at host("billing.internal:8443")
}
""")
    local = compile_composition(str(tmp_path / "localbase.rvl"), str(tmp_path))
    remote = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))

    for document in (local, remote):
        assert "CheckoutSvc" in document["manifest"]["loadOrder"]

    # The WIRING projection — the rename-invariant one — is byte-identical
    # across the two, because `component` is provenance and the peer is not
    # wiring at all. Swapping a local provider for a remote one produces an
    # EMPTY wiring diff, which is the property that makes "bring it back
    # in-process" a one-line composition edit.
    assert resolve(tmp_path, "localbase").wiring() == resolve(tmp_path).wiring()

    row = next(r for r in resolve(tmp_path).rows if r.label == "billing")
    assert row.claims == [("billing", None)]
    assert row.remote["peer"] == "billing.internal:8443"
    assert row.remote["capability"] == "net.billing_internal"


def test_the_synthesized_provider_is_ordinary_source_on_no_disk(tmp_path):
    """Nothing is written to the filesystem: the provider is handed to
    `compile_files` through the in-memory `sources` map it already takes, so
    `_link` runs G2/G3/G4 over it exactly as over a file row."""
    write(tmp_path, services=BILLING, base="""
composition Shop {
  use "services.rvl"
  remote @billing provides billing: Billing at host("billing.internal:8443")
}
""")
    table = resolve(tmp_path)
    (rel, text), = table.sources.items()
    assert rel == ".revl/synthesized/_project/billing.remote.rvl"
    assert not (tmp_path / rel).exists()
    assert rel in table.paths()

    # The provenance path is derived from the origin and the label alone, so
    # two machines resolving the same composition produce byte-identical rows
    # (426 exit test 18, which a remote row must not break).
    assert json.dumps(table.to_ir()) == json.dumps(
        resolve_file(str(tmp_path / "base.rvl"), str(tmp_path)).to_ir())

    assert "component RemoteBillingProvider provides billing: Billing" in text
    assert "extern emission[net.billing_internal] fn remote_billing_charge" in text


# --------------------------------- exit test 2: a plain `fn` is unremotable

def test_a_plain_fn_service_is_refused_naming_the_method(tmp_path):
    """D-424c.2, which is G4 read at the client rather than a new rule: a
    network call is a boundary crossing, and a provider may be purer than it
    declares but never less pure."""
    write(tmp_path, plain=PLAIN, base="""
composition A {
  use "plain.rvl"
  remote @m provides metrics: Metrics at host("m.internal:80")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "not remotable" in message
    assert "method `tick` is a plain `fn`" in message


# ------------------------ exit test 3: two peers, two realms, no collision

def test_two_peers_of_one_service_in_two_realms_admit(tmp_path):
    """D-424c.4: `@RemoteScope` is a REALM and there is nothing to build. Two
    peers are two `(key, realm)` addresses and 426 §2.3's rule keeps them
    apart with no new machinery."""
    write(tmp_path, services=BILLING, base="""
composition C {
  use "services.rvl"
  remote @a provides billing: Billing in realm("tenant_a") at host("a.internal:8443")
  remote @b provides billing: Billing in realm("tenant_b") at host("b.internal:8443")
}
""")
    document = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    order = document["manifest"]["loadOrder"]
    assert {"RemoteAProvider", "RemoteBProvider"} <= set(order)

    rows = {r.label: r for r in resolve(tmp_path).rows}
    assert rows["a"].claims == [("billing", "tenant_a")]
    assert rows["b"].claims == [("billing", "tenant_b")]
    # Two peers, two reach bounds: the tokens do not collapse onto one another.
    assert rows["a"].remote["capability"] != rows["b"].remote["capability"]


def test_two_peers_in_one_realm_collide_naming_both_rows(tmp_path):
    write(tmp_path, services=BILLING, base="""
composition D {
  use "services.rvl"
  remote @a provides billing: Billing at host("a.internal:8443")
  remote @b provides billing: Billing at host("b.internal:8443")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert 'key("billing") is claimed by both row `.::@a`' in message
    assert "`.::@b`" in message


# ------------------------------------------- exit test 5: `on_failure(result)`

def test_on_failure_result_is_refused_on_a_non_result_method(tmp_path):
    write(tmp_path, services=BILLING, base="""
composition B {
  use "services.rvl"
  remote @b provides billing: Billing at host("b.internal:8443") on_failure(result)
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "`on_failure(result)`" in message
    assert "`charge` returns `Bool`" in message


def test_on_failure_result_admits_when_every_method_returns_result(tmp_path):
    write(tmp_path, ledger=LEDGER, base="""
composition F {
  use "ledger.rvl"
  remote @ledger provides ledger: Ledger
    at host("ledger.internal:8443") on_failure(result)
}
""")
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote["onFailure"] == "result"
    text = table.sources[row.source]
    # In band: the failure becomes an `Err`, and the provider is NOT withdrawn.
    assert 'return Err("remote: transport failure")' in text
    assert "raise RuntimeError" not in text


def test_on_failure_withdraw_is_the_default_and_raises_a_fault(tmp_path):
    """D-424c.3's default. The declaration is what C2 lands; the runtime half —
    turning this fault into a provider withdrawal that cascades through R2/R3 —
    is the seam-client mechanism at `backends/python/bridge.py:723`, which a
    synthesized row does not join. This pins the contract so the day the
    runtime wires it, something says so."""
    write(tmp_path, services=BILLING, base="""
composition G {
  use "services.rvl"
  remote @billing provides billing: Billing at host("billing.internal:8443")
}
""")
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote["onFailure"] == "withdraw"
    text = table.sources[row.source]
    assert 'raise RuntimeError("remote: transport failure") from _exc' in text
    # There is no third option: swallowing a transport failure has no spelling.
    assert "return None" not in text


def test_on_failure_takes_only_the_two_spellings():
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition H {
  remote @b provides billing: Billing at host("b:1") on_failure(ignore)
}
""", "t.rvl").parse()
    assert "takes `withdraw` or `result`" in str(excinfo.value)


# ------------------------------------------------- teardown: no inverse, ever

def test_no_inverse_is_synthesized_and_the_row_says_so(tmp_path):
    """The teardown answer, executed. A remote call is neither a `bracket` (an
    acquire's release is infallible by contract; a network inverse is fallible
    by construction) nor `transactional` (item 243 needs a host-local inverse
    plus a witness, and the peer's own claim is not a witness). So no inverse
    exists, a remote effect survives unwind, and G7 stays LIFO-complete because
    a remote operation is not an entry it walks."""
    write(tmp_path, services=BILLING, base="""
composition Shop {
  use "services.rvl"
  remote @billing provides billing: Billing at host("billing.internal:8443")
}
""")
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote["inverse"] is None
    text = table.sources[row.source]
    assert " undo " not in text
    assert "compensate" not in text.split("// NO INVERSE")[1].split("extern")[1]
    assert "NO INVERSE IS SYNTHESIZED, AND A REMOTE EFFECT SURVIVES UNWIND" in text
    # D-424c.8: no "verified remote" badge, and the generated source says so.
    assert "MAKES NO CLAIM ABOUT WHAT THE PEER RUNS" in text


# ----------------------------------------------- the address and the reach

def test_a_peer_address_carrying_userinfo_is_refused(tmp_path):
    """D-424c.10 and roadmap 421 F4. The fail-closed reading is to refuse the
    address outright rather than to accept it and quietly drop the credential:
    dropping it would leave a live secret written in a composition document,
    which is a worse place for it than a URL."""
    write(tmp_path, services=BILLING, base="""
composition E {
  use "services.rvl"
  remote @a provides billing: Billing at host("user:s3cret@a.internal:8443")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "carries userinfo" in message
    assert "s3cret" not in message


def test_a_url_is_refused_and_a_bad_authority_is_refused(tmp_path):
    for address in ("https://a.internal:8443", "a.internal:8443/api", "a b"):
        write(tmp_path, services=BILLING, base="""
composition E {
  use "services.rvl"
  remote @a provides billing: Billing at host("%s")
}
""" % address)
        with pytest.raises(RevlError):
            resolve(tmp_path)


def test_the_reach_token_is_folded_from_the_host_alone():
    """Never the port and never userinfo, so two credentials against two hosts
    cannot collapse onto one token."""
    assert cap_token("billing.internal:8443") == "net.billing_internal"
    assert cap_token("billing.internal:9000") == cap_token("billing.internal")
    assert cap_token("a.internal") != cap_token("b.internal")
    # A bare IP folds to something starting with a digit, which the lexer would
    # read as a number with digit separators; it is prefixed to stay an ident.
    assert cap_token("127.0.0.1:80") == "net.h_127_0_0_1"


# ----------------------------------------------- the transport (`through`)

def test_a_named_through_transport_is_refused(tmp_path):
    """`through <wire>` names the transport a remote row crosses (D-424c.1's
    `through billing_wire`). The synthesizer speaks the canonical envelope
    (selected by OMITTING `through`) and one NAMED wire, `through a2a` (item
    439). Any OTHER named transport is refused naming the transport and the row
    rather than emitted as a wire the body would not actually speak — the
    honesty rule the version, redirect and modality checks keep, on the
    transport axis.

    `through a2a` is bound (see `test_439_a2a_transport.py`); `jsonrpc` and
    `grpc` are not — gRPC in particular is a binary HTTP/2 transport, not the
    JSON POST this synthesizer emits — so both still refuse here.
    """
    for wire in ("jsonrpc", "grpc"):
        write(tmp_path, services=BILLING, base="""
composition Shop {
  use "services.rvl"
  remote @billing provides billing: Billing
    at host("billing.internal:8443")
    through %s
}
""" % wire)
        with pytest.raises(RevlError) as excinfo:
            resolve(tmp_path)
        message = str(excinfo.value)
        assert f"names transport `{wire}`" in message
        assert "`@billing`" in message
        assert "binds no transport by that name" in message


def test_the_default_wire_needs_no_through_and_still_compiles(tmp_path):
    """Omitting `through` is the one wire this slice speaks, so the row that a
    named transport is refused on compiles cleanly without it. This pins the
    refusal to the NAMED case rather than to the clause's existence."""
    write(tmp_path, services=BILLING, base="""
composition Shop {
  use "services.rvl"
  remote @billing provides billing: Billing at host("billing.internal:8443")
}
""")
    table = resolve(tmp_path)
    row, = table.rows
    assert row.remote.get("transport") is None
    # The synthesized wire is the canonical envelope, not a named binding.
    assert "\"key\"" in table.sources[row.source]


# --------------------------------------------- what the projection refuses

def test_a_record_return_is_refused_naming_the_method_and_the_type(tmp_path):
    """The JSON-transparent subset. A record or an ADT needs the tagged half of
    the canonical encoding, which lives in the placement bridge and is not
    reachable from a generated host body; `revl export client` (slice C1)
    builds that projection."""
    (tmp_path / "svc.rvl").write_text("""
type Invoice = { id: Str, total: Int }

service Billing {
  emission fn fetch(id: Str) -> Invoice
}
""")
    (tmp_path / "base.rvl").write_text("""
composition Shop {
  use "svc.rvl"
  remote @billing provides billing: Billing at host("b.internal:8443")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "cannot project the return type of method `fetch`" in message
    assert "`Invoice`" in message


def test_an_unknown_service_is_refused_listing_what_is_in_scope(tmp_path):
    write(tmp_path, services=BILLING, base="""
composition Shop {
  use "services.rvl"
  remote @x provides x: Nowhere at host("x.internal:80")
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "`Nowhere`, which composition Shop does not declare" in message
    assert "`Billing`" in message


def test_synthesize_provider_refuses_an_unknown_kind():
    """§4's claim is that four constructs are four KINDS of one function. The
    function says which kinds it has rather than silently doing the wrong one."""
    program = Parser(BILLING, "t.rvl").parse()
    with pytest.raises(ValueError) as excinfo:
        synthesize_provider(program.services[0], "seam", {})
    assert "'remote'" in str(excinfo.value)
