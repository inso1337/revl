"""`retention.declared_policies` walks the import closure with the compiler's
module loader, so it must mark the roots the way `compile_files` does.

Without `mark_roots` the loader has no root directories, so an `asset` handle in
a root module's `pub fn` has no jail to resolve inside, and a file sitting next
to the root is refused as "resolves OUTSIDE the root compile tree". The
retention surface would then refuse a composition `compile_files` accepts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import retention as R  # noqa: E402
from revl.compiler import compile_files  # noqa: E402
from revl.parser import Parser  # noqa: E402

APP = '''retention customer_pii {
  until: "2099-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo, subject
  derivatives: summary, index
}

type Asset = { path: Str, sha256: Str }

pub fn entry() -> Asset { return asset "./e.ts" }
'''


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    (app / "e.ts").write_text("export default 1\n", encoding="utf-8")
    root = app / "a.rvl"
    root.write_text(APP, encoding="utf-8")
    return root


def test_declared_policies_resolves_a_root_asset_inside_the_root_tree(tmp_path):
    root = _app(tmp_path)
    policies = R.declared_policies([str(root)])
    assert set(policies) == {"customer_pii"}


def test_declared_policies_agrees_with_compile_files(tmp_path):
    """The compiler accepts the composition, and the closure walk reads the
    same policy the root's own text declares."""
    root = _app(tmp_path)
    compile_files([str(root)])
    expected = R.policies(Parser(APP, str(root)).parse(), str(root))
    assert R.declared_policies([str(root)]) == expected
