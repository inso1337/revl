"""The `host` row: roadmap item 457 S3.

`docs/design/457-endpoint-one-definition.md` §"The Cordis `ctx.server` binding:
a `host` row" (design note 530 Decision A). A `host` row is the SIBLING of a
`remote` row: where a remote row's provider is synthesized from a service
declaration plus a peer address and reaches a PEER over the wire, a host row's
provider is synthesized from a service declaration plus a shipped shim and
reaches the HOST PROCESS's own ambient service (Cordis `ctx.server`, `ctx.sso`)
through the reviewed `@ts ref` door — never a `globalThis` bridge.

What this slice lands is the DECLARATION, the SYNTHESIS and the ADMISSION facts:
the `host` row parses as a contextual keyword, resolves to an ordinary row whose
provider binds each service method to a `@ts ref` of the shipped shim, carries a
`host` admission fact into the IR, and refuses an unhostable service (no shim, a
plain `fn` method). The EMISSION half — compiling that provider to the ts tier
and registering each routed operation on `ctx.server.get(path, handler)` — is
item 457 S3's emitted-`ctx.server` half and is not exercised here (its concrete
handler contract is finalized against the exemplary app, item 462), exactly as
`test_424_remote_row.py` pins the remote declaration but leaves the runtime
withdrawal cascade to the bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.synthesize import HOST_SHIMS, synthesize_provider  # noqa: E402

# The shipped `Server` binding module, used verbatim so the test tracks the real
# stdlib contract rather than a private copy of it.
SERVER_RVL = (ROOT / "stdlib" / "server.rvl").read_text()

# A consumer written with no idea that its provider is the host runtime. Not one
# word of this changes between a host provider and a real revl one — the wiring
# is local (design note 530 Decision A, mirroring 424 D-424c.1).
CONSUMER = """
use "server.rvl" { Server }

service Frontend {
  emission fn mount() -> Unit
}

component FrontendSvc requires server: Server provides frontend: Frontend {
  provide frontend {
    fn mount() { emit server.get("/notes/{id}") }
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

def test_host_row_parses_with_a_contextual_keyword_only():
    """`host` heads a row only in this one position, so the lexer's KEYWORDS set
    is untouched and a program using `host` as an ordinary name still parses —
    the property `remote`/`seam`/`place` preserve, so the self-host lexer needs
    no sync."""
    program = Parser("""
composition Notes {
  host @server provides server: Server
  host @auth provides auth: Auth in realm("tenant_a")
}
""", "t.rvl").parse()
    hosts = program.compositions[0].hosts
    assert (hosts[0].label, hosts[0].key, hosts[0].service) == (
        "server", "server", "Server")
    assert hosts[0].realm is None
    assert (hosts[1].service, hosts[1].realm) == ("Auth", "tenant_a")

    # `host` as an ordinary parameter name, in ordinary code, still parses.
    ordinary = Parser("""
service S { emission fn go(host: Str) -> Str }
""", "t.rvl").parse()
    assert list(ordinary.services[0].methods["go"].params)[0][0] == "host"


def test_host_row_needs_a_service_after_the_key():
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition Notes {
  host @server provides server
}
""", "t.rvl").parse()
    assert "expected `: <Service>`" in str(excinfo.value)


def test_a_host_row_shares_the_one_label_namespace(tmp_path):
    """A consumer cannot tell a host row from a file row, so two rows that a
    consumer cannot tell apart must not share a name (426 §1.2)."""
    write(tmp_path, server=SERVER_RVL, base="""
composition Notes {
  use "server.rvl"
  row @server from "server.rvl" provides server
  host @server provides server: Server
}
""")
    with pytest.raises(RevlError) as excinfo:
        Parser((tmp_path / "base.rvl").read_text(), "base.rvl").parse()
    assert "duplicate row label `@server`" in str(excinfo.value)


# ------------------------------------------------ resolution + the admission fact

def test_host_row_resolves_to_a_synthesized_shim_provider(tmp_path):
    """The row resolves to an ORDINARY row (a synthesized provider), claims the
    key, requires nothing, and carries the `host` admission fact — the shipped
    shim and the Cordis package it wraps — into the IR."""
    write(tmp_path, server=SERVER_RVL, base="""
composition Notes {
  use "server.rvl"
  host @server provides server: Server
}
""")
    rt = resolve(tmp_path)
    row = next(r for r in rt.rows if r.label == "server")
    assert row.component == "HostServerProvider"
    assert row.claims == [("server", None)]
    assert row.requires == []
    assert row.host == {
        "service": "Server",
        "serviceSource": "server.rvl",
        "shim": "../backends/typescript/revl_server_ts.ts",
        "package": "@cordisjs/server",
    }
    # the admission fact reaches the IR under the additive `host` key
    ir = row.to_ir()
    assert ir["host"]["package"] == "@cordisjs/server"

    # the synthesized provider binds every Server method to a `@ts ref` of the
    # shim — the reviewed host-reference door, not a globalThis reach.
    src = rt.sources[row.source]
    for verb in ("get", "post", "put", "patch", "delete", "head"):
        assert (f'extern emission fn host_server_{verb}(path: Str) -> Unit '
                f'= @ts ref {verb} from '
                f'"../backends/typescript/revl_server_ts.ts"') in src
    # and it is ordinary, valid revl source (re-parses cleanly)
    reparsed = Parser(src, "syn.rvl").parse()
    assert [c.name for c in reparsed.components] == ["HostServerProvider"]
    assert len(reparsed.externs) == 6


def test_consumer_of_a_host_key_resolves_one_provider(tmp_path):
    """The consumer keeps `requires server: Server` and resolves exactly one
    provider — the host row is a wiring-local admission fact."""
    write(tmp_path, server=SERVER_RVL, consumer=CONSUMER, base="""
composition Notes {
  use "server.rvl"
  row @frontend from "consumer.rvl" provides frontend
  host @server provides server: Server
}
""")
    rt = resolve(tmp_path)
    providers = [r for r in rt.rows if ("server", None) in r.claims]
    assert len(providers) == 1
    assert providers[0].host is not None


def test_a_host_row_needs_its_service_in_scope(tmp_path):
    """A host row has no component header, so the service declaration IS its
    contract; a service the composition does not `use` is a refusal naming it."""
    write(tmp_path, server=SERVER_RVL, base="""
composition Notes {
  host @server provides server: Server
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "does not declare" in str(excinfo.value)


# ------------------------------------------------------- the hostability refusals

def test_a_service_with_no_shipped_shim_is_refused():
    plain = Parser("service Gadget { emission fn go() -> Int }", "t.rvl").parse()
    svc = plain.services[0]
    assert "Gadget" not in HOST_SHIMS
    with pytest.raises(RevlError) as excinfo:
        synthesize_provider(svc, "host", {
            "label": "g", "key": "g", "realm": None, "doc": "t.rvl", "line": 1})
    assert "no shipped host shim" in str(excinfo.value)


def test_a_plain_fn_host_service_is_unhostable(monkeypatch):
    """Reaching the host runtime is a boundary crossing, so a plain `fn` host
    service is refused naming the method — G4 read at the host boundary, the same
    rule `remote` reads at the client (424 D-424c.2)."""
    monkeypatch.setitem(HOST_SHIMS, "Gizmo",
                        {"module": "../backends/typescript/x.ts", "package": "@x"})
    plain = Parser("service Gizmo { fn tick() -> Int }", "t.rvl").parse()
    with pytest.raises(RevlError) as excinfo:
        synthesize_provider(plain.services[0], "host", {
            "label": "z", "key": "z", "realm": None, "doc": "t.rvl", "line": 1})
    assert "not hostable" in str(excinfo.value)
    assert "tick" in str(excinfo.value)


def test_shipped_server_service_is_hostable():
    """The shipped `stdlib/server.rvl` `Server` service resolves through the
    `host` kind without refusal — its verb set is all `emission` and it has a
    shim — so the binding module and the synthesizer agree."""
    prog = Parser(SERVER_RVL, "server.rvl").parse()
    server = next(s for s in prog.services if s.name == "Server")
    comp, text = synthesize_provider(server, "host", {
        "label": "server", "key": "server", "realm": None,
        "doc": "server.rvl", "line": 1})
    assert comp == "HostServerProvider"
    assert text.count("@ts ref") == 6 + 2  # 6 externs + 2 header mentions


def test_host_fact_is_absent_for_an_ordinary_composition(tmp_path):
    """A composition with no host row produces rows whose `host` fact is `None`
    and absent from the IR — the additive-key discipline (byte-identical)."""
    write(tmp_path, server=SERVER_RVL, consumer=CONSUMER, local="""
use "server.rvl" { Server }
component LocalServer provides server: Server {
  provide server {
    fn get(path) { return }
    fn post(path) { return }
    fn put(path) { return }
    fn patch(path) { return }
    fn delete(path) { return }
    fn head(path) { return }
  }
}
""", base="""
composition Notes {
  use "server.rvl"
  row @server from "local.rvl" provides server
}
""")
    rt = resolve(tmp_path)
    row = next(r for r in rt.rows if r.label == "server")
    assert row.host is None
    assert "host" not in row.to_ir()
