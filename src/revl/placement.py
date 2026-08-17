"""`revl run --placement`: launch a composition split across processes.

The placement file (TOML/JSON) assigns each component to a named process:

    [config.PgDatabase]
    url = "postgres://primary:5432/app"

    [processes.provider]
    components = ["PgDatabase"]

    [processes.consumer]
    components = ["UserCache"]
    probe = ["cache.put('alice', '42')", "cache.get('alice')"]

The conductor compiles the `.rvl`, works out the seams from the IR (which key
each process provides, which it requires from another), assigns a Unix socket
per provider, and spawns one `revl._process_runner` per process wired so that a
key required across a process boundary is served on one side and proxied on the
other. This is the manifest-driven form of what demo/bridge_pypy.py did by hand
(docs/interop-bridge.md §5: the broker is a placement map plus generated
proxy/stub, no new grammar). cordis-py only, for now.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError


def _load_placement(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        return json.loads(text)
    import tomllib  # noqa: PLC0415; stdlib, py3.11+

    return tomllib.loads(text)


def _stop_all(children: dict) -> None:
    for proc in children.values():
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    for proc in children.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_placement(files, placement_path: str, once: bool = False) -> int:
    try:
        ir = compile_files(files)
    except RevlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    placement = _load_placement(placement_path)
    processes = placement.get("processes") or {}
    config = placement.get("config") or {}
    if not processes:
        print("error: placement has no [processes]", file=sys.stderr)
        return 1

    components = {c["name"]: c for c in ir.get("components") or []}

    # every component placed exactly once
    placed: dict[str, str] = {}
    for pname, pconf in processes.items():
        for cname in pconf.get("components") or []:
            if cname not in components:
                print(f"error: process {pname!r} lists unknown component {cname!r}", file=sys.stderr)
                return 1
            if cname in placed:
                print(f"error: component {cname!r} is placed in both {placed[cname]!r} and {pname!r}",
                      file=sys.stderr)
                return 1
            placed[cname] = pname
    unplaced = [c for c in components if c not in placed]
    if unplaced:
        print(f"error: components not placed in any process: {', '.join(unplaced)}", file=sys.stderr)
        return 1

    def merged(cnames, which):  # union of a set of components' provides/requires (key -> service)
        out: dict[str, str] = {}
        for cname in cnames:
            out.update(components[cname].get(which) or {})
        return out

    provides = {p: merged(pc.get("components") or [], "provides") for p, pc in processes.items()}
    requires = {p: merged(pc.get("components") or [], "requires") for p, pc in processes.items()}
    owner = {key: p for p, keys in provides.items() for key in keys}
    methods = {name: list((svc.get("methods") or {}).keys()) for name, svc in (ir.get("services") or {}).items()}
    load_order = (ir.get("manifest") or {}).get("loadOrder") or [c["name"] for c in ir["components"]]

    tmp = Path(tempfile.mkdtemp(prefix="revl_placement_"))
    sockets = {p: str(tmp / f"{p}.sock") for p in processes}

    specs: dict[str, dict] = {}
    for pname, pconf in processes.items():
        proxies: dict[str, dict] = {}
        for key, service in requires[pname].items():
            if key in provides[pname]:
                continue  # satisfied in-process; no seam
            host = owner.get(key)
            if host is None:
                print(f"error: key {key!r} required by {pname!r} is provided by no process", file=sys.stderr)
                return 1
            proxies[key] = {"socket": sockets[host], "methods": methods.get(service, [])}
        serve_keys = [k for k in provides[pname]
                      if any(k in requires[q] and q != pname for q in processes)]
        local = [c for c in load_order if placed.get(c) == pname]
        spec = {
            "name": pname,
            "files": [str(f) for f in files],
            "components": local,
            "config": config,
            "provides": list(provides[pname]),
            "proxies": proxies,
            "probe": pconf.get("probe") or [],
        }
        if serve_keys:
            spec["serve"] = {"socket": sockets[pname], "keys": serve_keys}
        specs[pname] = spec

    summary = "  ".join(f"{p}=[{','.join(specs[p]['components'])}]" for p in processes)
    print(f"placement: {summary}", flush=True)

    import revl  # noqa: PLC0415
    src_dir = str(Path(revl.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src_dir, os.environ.get("PYTHONPATH", "")])}

    children: dict[str, subprocess.Popen] = {}
    for pname, spec in specs.items():
        spec_file = tmp / f"{pname}.spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        children[pname] = subprocess.Popen(
            [sys.executable, "-m", "revl._process_runner", str(spec_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True,
        )

    up: set[str] = set()

    def pump(pname: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.strip() == f"[{pname}] UP":
                up.add(pname)

    threads = [threading.Thread(target=pump, args=(p, proc), daemon=True) for p, proc in children.items()]
    for thread in threads:
        thread.start()

    rc = 0
    try:
        if once:
            for _ in range(300):  # up to ~30s for every process to come up
                if len(up) == len(children):
                    break
                time.sleep(0.1)
            if len(up) != len(children):
                missing = ", ".join(p for p in children if p not in up)
                print(f"error: processes did not come up: {missing}", file=sys.stderr)
                rc = 1
            _stop_all(children)
        else:
            print("(placement up; Ctrl-C to tear down)", flush=True)
            for proc in children.values():
                proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_all(children)
        for thread in threads:
            thread.join(timeout=2)
    return rc
