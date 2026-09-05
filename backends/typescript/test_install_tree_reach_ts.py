"""Issue #278: a verbatim `@ts` extern body has no sanctioned door to an
install-tree host module, and the architect decision is Option 1 — NO door by
design. This is the exit test: an install-tree reach must produce a COMPILE
diagnostic that NAMES THE RULE (distinguishable from a genuine runtime
module-resolution error), while a `node:` builtin reach is left untouched for
#295's runtime gate to report.

Toolchain-free: this compiles rvl source and emits, asserting on the emitter's
own diagnostic — no node/vitest needed. The #295 runtime-gate behaviour itself
is asserted in `test_runtime_contract.py`, which must stay green and unchanged.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_278", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A relative-path reach: `../foo.ts` is an install-tree module.
_RELATIVE_REACH = """
pub extern pure fn load(s: Str) -> Str
  = @ts { return require("../foo.ts").f(s) }

test "relative reach" { assert load("x") == "x" }
"""

# A bare-package reach: `somepkg` resolves through node_modules in the install
# tree — no resolution root under plain node.
_PACKAGE_REACH = """
pub extern pure fn load(s: Str) -> Str
  = @ts { return require("somepkg").f(s) }

test "package reach" { assert load("x") == "x" }
"""

# The synthesized-loader form of the same reach.
_CREATE_REQUIRE_REACH = """
pub extern pure fn load(s: Str) -> Str
  = @ts {
      const req = process.getBuiltinModule("node:module").createRequire(import.meta.url)
      return req("somepkg").f(s)
    }

test "createRequire reach" { assert load("x") == "x" }
"""

# #295's case: a `node:` builtin. This must EMIT (the runtime gate reports it at
# run under plain node); the #278 diagnostic must NOT fire here.
_NODE_BUILTIN = """
pub extern pure fn base(s: Str) -> Str
  = @ts { return require("node:path").basename(s) }

test "node builtin" { assert base("/a/b.txt") == "b.txt" }
"""

# The sanctioned node-builtin door (#382). Plain text; must emit unbothered.
_GET_BUILTIN_MODULE = """
pub extern pure fn base(s: Str) -> Str
  = @ts { return process.getBuiltinModule("node:path").basename(s) }

test "getBuiltinModule" { assert base("/a/b.txt") == "b.txt" }
"""


def _assert_named_rule(exc: Exception) -> None:
    msg = str(exc)
    assert "install-tree host module" in msg, msg
    assert "@ts ref" in msg and "#396" in msg, msg          # option B door
    assert "getBuiltinModule" in msg and "#382" in msg, msg  # node-builtin door
    assert "#278" in msg, msg                                # the rule is named


@pytest.mark.parametrize("src", [_RELATIVE_REACH, _PACKAGE_REACH,
                                 _CREATE_REQUIRE_REACH])
def test_install_tree_reach_is_a_named_compile_diagnostic(src):
    m = _emit()
    ir = compile_source(src, "reach.rvl")
    with pytest.raises(m.EmitError) as ei:
        m.emit(ir)
    _assert_named_rule(ei.value)


@pytest.mark.parametrize("src", [_NODE_BUILTIN, _GET_BUILTIN_MODULE])
def test_node_builtin_reach_is_not_diagnosed(src):
    """A `node:` `require` is #295's runtime gate's, and `getBuiltinModule` is
    the sanctioned door — neither is an install-tree reach, so emit succeeds."""
    m = _emit()
    out = m.emit(compile_source(src, "ok.rvl"))
    assert "function base(" in out


def test_the_295_bare_require_document_still_emits():
    """The exact document `test_runtime_contract.py` relies on must still EMIT
    (its failure is the runtime gate's, not a compile diagnostic)."""
    m = _emit()
    src = (
        'pub extern pure fn base(s: Str) -> Str\n'
        '  = @ts { return require("node:path").basename(s) }\n\n'
        'test "a bare require in a @ts body" { assert base("/a/b.txt") == "b.txt" }\n'
    )
    out = m.emit(compile_source(src, "bare_require.rvl"))
    assert 'require("node:path")' in out
