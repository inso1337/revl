"""A value-position emission in a provide method reaches the WAL (issue #1603).

`return emit file_host(t)` and `let r = emit file_host(t)` in a provide-method
body used to render as a bare `file_host(t)`: the `emit` marker leaves no trace
on the IR node, and the py emitter routed only `emit` STATEMENTS through the
recording seam (`_revl_extern_emit`, item 414). The host body ran, the crossing
happened, and the recorder never saw it, so the WAL held nothing for `revl
recover` to report or compensate: recovery failed open. The emitter now reads
the extern's class and fires an `emission` extern in a method's value position
through the seam, and leaves compensations, undos and pure externs alone.

The selfhost port (`selfhost/emit_py.rvl::method_value`) is held to the same
bytes by tests/test_selfhost_emit_py.py over
tests/fixtures/emit_py_corpus/services_value_emission.rvl.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")

SEAM = "_revl_extern_emit(_revl_ctx, 'file_host', file_host, "


def _emit(tmp_path: Path, source: str) -> str:
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    from revl._paths import python_backend_emitter  # noqa: PLC0415
    (tmp_path / "c.rvl").write_text(source, encoding="utf-8")
    return python_backend_emitter().emit(compile_files([str(tmp_path / "c.rvl")]))


def _method(src: str, name: str) -> str:
    start = src.index(f"def {name}(self")
    end = src.find("\n\n", start)
    return src[start:end]


HOSTS = """
extern emission fn file_host(t: Str) -> Str = @py {
    return 'id-' + t
}
extern emission fn unfile_host(t: Str) -> Unit = @py {
    return None
}
extern pure fn shout(t: Str) -> Str = @py {
    return t.upper()
}
"""

SHAPES = HOSTS + """
service Tickets {
  emission fn file(t: Str) -> Str
  emission fn bind(t: Str) -> Str
  emission fn nest(t: Str) -> Str
  emission fn keep(t: Str)
  fn fold(t: Str) -> Str
}
component Desk provides tickets: Tickets {
  provide tickets {
    fn file(t) { return emit file_host(t) }
    fn bind(t) {
      let r = emit file_host(t)
      return r
    }
    fn nest(t) { return shout(emit file_host(t)) }
    fn keep(t) { emit file_host(t) compensate unfile_host(t) }
    fn fold(t) { return shout(t) }
  }
}
"""


def test_a_returned_emission_fires_through_the_recording_seam(tmp_path):
    src = _emit(tmp_path, SHAPES)
    assert f"return {SEAM}(t,))" in _method(src, "file")


def test_a_bound_emission_fires_through_the_recording_seam(tmp_path):
    src = _emit(tmp_path, SHAPES)
    assert f"r = {SEAM}(t,))" in _method(src, "bind")


def test_a_nested_value_emission_fires_through_the_recording_seam(tmp_path):
    src = _emit(tmp_path, SHAPES)
    assert f"return shout({SEAM}(t,)))" in _method(src, "nest")


def test_a_compensation_and_a_pure_extern_are_not_routed(tmp_path):
    """A method's compensation runs at abort, not as the method's own
    crossing; a pure extern crosses nothing."""
    src = _emit(tmp_path, SHAPES)
    keep = _method(src, "keep")
    assert "compensation_method(lambda: unfile_host(t)" in keep
    assert "_revl_extern_emit(_revl_ctx, 'unfile_host'" not in keep
    assert _method(src, "fold").endswith("return shout(t)")


#: Agent calls `tickets.file("T1")`; Desk's method answers it with
#: `return emit file_host(t)`. Then the process exits mid-activation.
CRASH = HOSTS + """
service Tickets {
  emission fn file(t: Str) -> Str
}
component Desk provides tickets: Tickets {
  provide tickets {
    fn file(t) { return emit file_host(t) }
  }
}
extern emission fn crash() -> Unit = @py {
    import os
    os._exit(3)
}
component Agent requires tickets: Tickets {
  emit tickets.file("T1")
  emit crash()
}
"""


@needs_cordis
def test_a_returned_emission_is_on_the_wal_after_a_crash(tmp_path):
    (tmp_path / "app.rvl").write_text(CRASH, encoding="utf-8")
    code = ("import sys\nfrom revl.__main__ import main\n"
            "sys.exit(main(['run', 'app.rvl', '--wal', 'run.wal']))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                          env=dict(os.environ, PYTHONPATH=path),
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 3, proc.stdout[-3000:] + proc.stderr[-3000:]

    records = [json.loads(line) for line in
               (tmp_path / "run.wal").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    effects = [(r["component"], r["label"], r["boundary"]["class"])
               for r in records if r.get("record") == "effect"
               and r["boundary"].get("class") == "emission"]
    # the crossing Desk's method made is on the log, as Desk's emission
    assert ("Desk", "file_host", "emission") in effects
