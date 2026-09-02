"""The single-choke-point claim, maintained as a test rather than a docstring.

`backends/python/revl_fs_workspace.py` used to say `resolve_within` was "the
single choke point every forward op AND every inverse routes through". It was
not, and the gap was invisible because every existing test asserted the choke
point on exactly the paths that DID route through it. Three families reached a
syscall around it — an inverse's SOURCE endpoint, the sidecar DIRECTORIES, and
(what realpath cannot see at all) hardlink aliases — and a fourth window sat
between the check and the syscall.

The claim is now an enumeration, `ws.PATH_FAMILIES`, and this suite is what
keeps it true:

  * `test_path_families_are_enumerated` pins the family set and the entry points
    each family names, so widening the guard's surface is a deliberate edit in
    two places rather than a quiet import;
  * `test_no_py_body_performs_a_raw_filesystem_mutation` and
    `test_every_syscall_argument_came_from_a_guard` scan the `@py` bodies of
    `stdlib/fs.rvl` and refuse anything that is not a listed helper fed a
    guarded path. A fifth path family cannot be added silently: it has to be a
    `_ws.` call, and an unlisted `_ws.` call fails the scan.

The scan reads the real `stdlib/fs.rvl`, not a fixture, so it fails the moment a
body regresses.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_files

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import revl_fs_workspace as ws  # noqa: E402

_FS_RVL = _ROOT / "stdlib" / "fs.rvl"

#: The `@py` body of every extern in the module, keyed by extern name. Bodies
#: are already indented one level, so they parse as a function body verbatim.
_BODY_RE = re.compile(
    r"^pub extern [^\n]*?\bfn (?P<name>\w+)\(.*?= @py \{\n(?P<body>.*?)\n\}",
    re.S | re.M,
)


def _bodies() -> dict[str, ast.Module]:
    text = _FS_RVL.read_text(encoding="utf-8")
    out: dict[str, ast.Module] = {}
    for m in _BODY_RE.finditer(text):
        out[m.group("name")] = ast.parse("def _body():\n" + m.group("body"))
    return out


@pytest.fixture(scope="module")
def bodies() -> dict[str, ast.Module]:
    parsed = _bodies()
    # every extern in the module has a @py body; if the regex ever stops
    # matching one, the scan below would silently cover less than it claims.
    ir = compile_files([str(_FS_RVL)])
    assert set(parsed) == {e["name"] for e in ir["externs"]}, \
        "the @py body scan did not reach every extern in stdlib/fs.rvl"
    return parsed


def _ws_calls(tree: ast.Module):
    """Every `_ws.<name>(...)` call in a body, as (name, call node)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                and fn.value.id == "_ws":
            yield fn.attr, node


# ===========================================================================
# The enumeration itself
# ===========================================================================

def test_path_families_are_enumerated():
    """The families are a closed set with a named guard each. Adding one means
    editing `PATH_FAMILIES` and this assertion together — which is the point:
    the fourth family was added to the code (hardlinks, the sidecar dirs, an
    inverse's source) without anyone editing a table, because there was none."""
    assert set(ws.PATH_FAMILIES) == {
        "named-endpoint",       # a path an op was handed
        "sidecar-directory",    # the reversal machinery's own directories
        "inverse-source",       # what an inverse renames FROM
        "syscall-time",         # the mutation, through a directory fd
    }
    for family, entries in ws.PATH_FAMILIES.items():
        assert entries, f"family {family} names no guard"
        for entry in entries:
            assert callable(getattr(ws, entry, None)), \
                f"{family} names {entry}, which is not a callable in the guard"

    # every syscall-time entry declares which of its arguments are paths, so the
    # argument scan below can never silently skip one
    assert set(ws.SYSCALL_PATH_ARGS) == set(ws.PATH_FAMILIES["syscall-time"])


def test_no_py_body_performs_a_raw_filesystem_mutation(bodies):
    """A body may import the guard and nothing else. The old bodies imported
    `os` and called `os.replace` / `open(...)` directly, which is exactly how
    the inverse-source and sidecar-directory families reached a syscall without
    passing a guard."""
    for name, tree in bodies.items():
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert imported <= {"revl_fs_workspace"}, \
            f"`{name}`'s @py body imports {sorted(imported - {'revl_fs_workspace'})}; " \
            "a body may reach the filesystem only through the guard"

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("open", "exec", "eval", "__import__"), \
                    f"`{name}`'s @py body calls the builtin `{node.func.id}`"


def test_every_ws_call_is_a_listed_guard_or_read_helper(bodies):
    """No unlisted helper. A new `_ws.` entry point is a new way to reach the
    filesystem, so it has to be declared in a family (or as a read helper)
    before a body may use it."""
    listed = {e for entries in ws.PATH_FAMILIES.values() for e in entries}
    listed |= set(ws.READ_HELPERS)
    for name, tree in bodies.items():
        for called, _ in _ws_calls(tree):
            assert called in listed, \
                f"`{name}`'s @py body calls unlisted guard helper `_ws.{called}`"


def test_every_syscall_argument_came_from_a_guard(bodies):
    """The load-bearing one: every path handed to a mutation was BOUND from a
    family 1-3 guard in the same body.

    This is the assertion the old code could not have passed:
    `os.replace(w["preimage"], target)` feeds a mutation a raw witness field.
    Written as a data-flow check rather than a call-name check, it stays true
    for a helper that has not been invented yet."""
    guards = set(ws.PATH_FAMILIES["named-endpoint"])
    guards |= set(ws.PATH_FAMILIES["sidecar-directory"])
    guards |= set(ws.PATH_FAMILIES["inverse-source"])

    for name, tree in bodies.items():
        # names bound from a guard call: `target = _ws.resolve_within(path)`
        guarded: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                    and fn.value.id == "_ws" and fn.attr in guards:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        guarded.add(target.id)

        for called, call in _ws_calls(tree):
            for index in ws.SYSCALL_PATH_ARGS.get(called, ()):
                assert index < len(call.args), \
                    f"`{name}` calls `_ws.{called}` without its path argument {index}"
                arg = call.args[index]
                assert isinstance(arg, ast.Name) and arg.id in guarded, (
                    f"`{name}`'s @py body passes an UNGUARDED path to "
                    f"`_ws.{called}` at argument {index} "
                    f"({ast.dump(arg)[:80]}); every path reaching a syscall must "
                    f"be bound from one of {sorted(guards)}"
                )


# ===========================================================================
# The classification decision, pinned
# ===========================================================================

def test_the_inverses_are_pure_and_the_mutations_are_capability_scoped():
    """The inverses stay `pure`, so they carry no capability. That is forced,
    not chosen: item 243 rule 3 (`_check_witnessed_inverse`, src/revl/lower.py)
    requires a witnessed extern's declared inverse to be non-emitting and
    non-witnessed, and the parser refuses a `[caps]` bracket on anything that is
    not `witnessed`/`emission`, so there is no capability-scoped spelling of an
    inverse in the surface at all.

    What makes that safe is not the classification but the shrunken authority:
    `resolve_sidecar` limits an inverse's SOURCE to a sidecar this workspace
    produced, and `resolve_within` limits its target to the root, so a
    capability-free inverse cannot name anything outside the jail — including
    when `revl recover` reconstructs one from a WAL witness with no revl source
    in play. This test pins both halves of the shape so a future edit to either
    is deliberate."""
    ir = compile_files([str(_FS_RVL)])
    classes = {e["name"]: (e["class"], tuple(e.get("capabilities", ())))
               for e in ir["externs"]}
    for inverse in ("restore", "unrm", "unmove", "rmdir_if_empty"):
        assert classes[inverse] == ("pure", ()), \
            f"{inverse} changed classification; see the guard module's " \
            "'why the inverses stay pure' section before accepting this"
    for mutation in ("write", "rm", "move", "mkdir"):
        assert classes[mutation] == ("witnessed", ("fs",))
