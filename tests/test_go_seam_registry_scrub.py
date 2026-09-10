"""Stage 2 of the go tier's seam funnel (roadmap item 421 F5).

The seam error channel must not become the reverse of a forward crossing the
checker refuses statically. Stage 1 replaced the caller's own argument values
inside a provider-side failure; stage 2 replaces the values a declared
`Secret[T]` marking registered. A failure that quotes a registered credential it
was *not* called with -- an expired token in a driver message, a held handle in a
connection string -- is in no argument list, so stage 1 cannot see it.

The registry belongs to the composition (`revlRedactText`, emitted into the
generated `emitted` package), and `emitted` imports `bridge`, not the other way
round, so the module cannot call it directly. The emitter installs it as a hook
from the generated bridge half's `init`; these tests pin both that wiring and
what it does when it runs.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import placement  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("go") is None,
                                reason="no go toolchain on PATH")

CANARY = "SEKRIT-CANARY-421-F5F6"
REDACTED_SECRET = "<redacted:secret>"

_RUNNER = ROOT / "backends" / "go" / "placement_runner"

# A component holding a credential as a declared secret, whose provide method
# returns a plain Str: the secret reaches the placement's registry and nothing
# else, so anything quoting it can only be the seam.
_SECRET_DOC = f'''
service Vault {{ fn show(user: Str) -> Str }}

component Impl provides vault: Vault {{
  config {{ token: Secret[Str] = "{CANARY}" }}

  provide vault {{
    fn show(user) {{ return "shown " + user }}
  }}
}}
'''

_PLAIN_DOC = '''
service Vault { fn show(user: Str) -> Str }

component Impl provides vault: Vault {
  config { token: Str = "not-a-secret" }

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
'''


def _go_emit():
    spec = importlib.util.spec_from_file_location("revl_go_emit",
                                                  ROOT / "backends" / "go" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_mode_installs_the_hook_in_the_generated_bridge_half():
    source = _go_emit().emit_placement(compile_source(_SECRET_DOC), "emitted")
    module_src, bridge_src = source.split("\f", 1)
    # The registry itself is the composition's, so it is emitted with the module.
    assert "func revlRedactText" in module_src
    # The hook has to be installed by the half that already imports `bridge`,
    # and it has to be the bridge half: the module half cannot reference the
    # package it is imported by.
    assert "bridge.SecretScrub = revlRedactText" in bridge_src
    assert "bridge.SecretScrub = revlRedactText" not in module_src


def test_marking_free_placement_installs_no_hook():
    source = _go_emit().emit_placement(compile_source(_PLAIN_DOC), "emitted")
    # No marking anywhere means no registry to install: the funnels stay the
    # identity function, and a placement that declares no secret behaves exactly
    # as it did before the hook existed.
    assert "revlRedactText" not in source
    assert "SecretScrub" not in source


def test_bridge_funnel_suite_passes():
    # The native suite asserts both stages on the funnel itself, including over a
    # real socket, so it is the authority for the redaction semantics.
    done = subprocess.run(["go", "test", "./bridge/"], cwd=str(_RUNNER),
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_probe_error_channel_is_scrubbed_end_to_end():
    # A probe names a method the process does not provide. The dispatch failure
    # quotes the requested method, and a probe is dispatched in-process, so the
    # line is rendered by the runner's log choke point and reaches neither the
    # trace funnel nor `bridge.SeamFailure`. The probe's method is the value the
    # composition holds as a declared secret, which is the shape this covers: the
    # console must not become the channel that prints it.
    tmp = Path(tempfile.mkdtemp(prefix="rvlgo_scrub_"))
    binary = placement._build_go(compile_source(_SECRET_DOC), tmp)
    spec = {
        "name": "vault", "backend": "go", "components": ["Impl"], "config": {},
        "provides": ["vault"], "proxies": {},
        "probe": [{"key": "vault", "method": CANARY, "args": []},
                  {"key": "vault", "method": "show", "args": ["u1"]}],
        "once": True,
    }
    spec_path = tmp / "spec.json"
    spec_path.write_text(json.dumps(spec))
    done = subprocess.run([binary, str(spec_path)], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert CANARY not in done.stdout
    assert REDACTED_SECRET in done.stdout
    # The diagnostic survives redaction: the channel still says what went wrong.
    assert "is not exported for service Vault" in done.stdout
    # False-positive control: a result carrying no secret is printed verbatim.
    assert '"shown u1"' in done.stdout
