"""Issue #3531: `revl emit --backend ts --runtime-import PATH` names the import
specifier the emitted module writes for the runtime (the value the Python
`emit(runtime_import=...)` already takes). Default stays '../runtime.ts'; the
flag is refused for non-typescript backends.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402

MODULE = """\
service S { fn f() -> Int }
component C provides s: S {
  provide s { fn f() = 1 }
}
"""


def _emit(tmp_path, *extra):
    src = tmp_path / "module.rvl"
    src.write_text(MODULE, encoding="utf-8")
    out = tmp_path / "out" / "module.ts"
    out.parent.mkdir(exist_ok=True)
    rc = main(["emit", "--backend", "ts", str(src), "-o", str(out), *extra])
    return rc, out


def _runtime_import_line(out: Path) -> str:
    (line,) = [ln for ln in out.read_text(encoding="utf-8").splitlines()
               if ln.startswith("import ") and "runtime" in ln]
    return line


def test_the_default_runtime_import_is_unchanged(tmp_path):
    rc, out = _emit(tmp_path)
    assert rc == 0
    assert "from '../runtime.ts'" in _runtime_import_line(out)


def test_runtime_import_names_the_specifier(tmp_path):
    rc, out = _emit(tmp_path, "--runtime-import", "./runtime.ts")
    assert rc == 0
    head = _runtime_import_line(out)
    assert "from './runtime.ts'" in head
    assert "../runtime.ts" not in head


def test_runtime_import_is_refused_for_other_backends(tmp_path):
    src = tmp_path / "module.rvl"
    src.write_text(MODULE, encoding="utf-8")
    rc = main(["emit", "--backend", "python", str(src),
               "--runtime-import", "./runtime.ts"])
    assert rc == 2
