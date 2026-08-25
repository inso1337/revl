"""A standalone py `Gate` provider that serves over **TCP + mutual TLS** on
loopback — the network-path (item 149) counterpart to the local-UDS provider in
tests/test_gate_bridge_service.py. Run as its own process (so a test can SIGKILL
it to prove reactive withdrawal on a dropped connection). Config JSON, argv[1]:

    {"host","port","cert","key","ca","identity", "wedge"?: bool}

`wedge=true` accepts the TLS handshake but never answers a request — a hung
remote provider, to prove the consumer's seam deadline / reactive withdrawal.
Prints `PROVIDER-UP <port>` once listening so the driver can proceed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _bridge():
    name = "revl_netgate_provider_bridge"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "backends" / "python" / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bridge = _bridge()

from revl.gate_service import admit, admit_case  # noqa: E402


class _GateService:
    """The py provider object — `service Gate`'s two operations, delegating to
    revl's in-process admission gate (src/revl/gate_service.py)."""

    def admit(self, sources_json, manifest_json):
        return admit(sources_json, manifest_json)

    def admit_case(self, case_name):
        return admit_case(case_name)


class _Ctx:
    def __init__(self, service):
        self._service = service

    def get(self, key):
        return self._service if key == "gate" else None


async def _main() -> None:
    cfg = json.loads(Path(sys.argv[1]).read_text())
    tls = bridge.TlsConfig(cfg["cert"], cfg["key"], cfg["ca"],
                           identity=cfg["identity"],
                           server_hostname=cfg.get("server_hostname", cfg["host"]))

    if cfg.get("wedge"):
        # Accept the mTLS handshake, read requests, and deliberately never reply
        # — a wedged remote provider. The consumer's deadline must bound this.
        async def handle(reader, writer):
            try:
                while await reader.readline():
                    pass
            except (ConnectionResetError, BrokenPipeError):
                pass
        server = await asyncio.start_server(
            handle, host=cfg["host"], port=cfg["port"], ssl=tls.server_context())
    else:
        endpoint = bridge.Endpoint(host=cfg["host"], port=cfg["port"], tls=tls)
        server = await bridge.serve(
            _Ctx(_GateService()), {"gate": ["admit", "admit_case"]}, endpoint)

    print(f"PROVIDER-UP {cfg['port']}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
