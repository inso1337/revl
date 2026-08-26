"""`use` gets a stdlib search path (roadmap item 319, F-H1.3).

`use` resolves primarily relative to the importing file
(compiler.py's `_ModuleLoader.resolve_use`). That leaves a consumer outside
the revl tree — the harness, say — unable to `use "stdlib/fs.rvl"` without
vendoring a byte-copy of the stdlib into its own tree. This suite proves the
fallback: when the relative path does not resolve, the loader searches
`REVL_IMPORT_PATH` (if set) and then the revl stdlib, so the same `use`
literal a module inside the checkout writes also resolves for a module that
has never been anywhere near it.

Covered:
  * a consumer OUTSIDE the stdlib tree resolves + compiles a real stdlib
    module through the plain `use "stdlib/..."` path, no vendored copy;
  * relative `use "./local.rvl"` is unchanged: it still resolves relative to
    the importer, even when a same-named file also sits on the search path;
  * REVL_IMPORT_PATH is checked before the packaged stdlib default;
  * a genuine local file is never shadowed by the search path;
  * a missing module still errors clearly, and now names the search path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402
from revl._paths import stdlib_root  # noqa: E402

#: a consumer using the real stdlib/str.rvl — pure revl, no @py bodies, so it
#: compiles on every tier without a backend-specific fixture
CONSUMER = """\
use "stdlib/str.rvl" { trim }

pub fn clean(s: Str) -> Str { return trim(s) }
"""


def test_stdlib_module_resolves_outside_the_tree(tmp_path):
    # tmp_path has no `stdlib/` subdirectory and nothing was vendored into
    # it — the ONLY way `use "stdlib/str.rvl"` resolves here is the roadmap
    # 319 search-path fallback landing on the real stdlib/str.rvl.
    assert not (tmp_path / "stdlib").exists()
    main = tmp_path / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")

    ir = compile_files([str(main)])

    names = {fn["name"] for fn in ir["functions"]}
    assert {"clean", "trim"} <= names


def test_relative_use_still_resolves_relative_to_the_importer(tmp_path):
    # unchanged behavior: a genuine relative import never touches the search
    # path at all, stdlib module or not.
    (tmp_path / "local.rvl").write_text(
        "pub fn helper() -> Int { return 41 }\n", encoding="utf-8"
    )
    main = tmp_path / "main.rvl"
    main.write_text(
        'use "./local.rvl" { helper }\n'
        "pub fn call_it() -> Int { return helper() + 1 }\n",
        encoding="utf-8",
    )

    ir = compile_files([str(main)])

    assert {fn["name"] for fn in ir["functions"]} == {"call_it", "helper"}


def test_search_path_never_shadows_a_genuine_local_file(tmp_path, monkeypatch):
    # a local `stdlib/str.rvl` that does NOT define `trim` sits right where
    # the real module's relative path would land. If the search path ever
    # shadowed a resolved local file, this would still succeed (wrongly,
    # against the packaged module); instead it must fail on the LOCAL file's
    # missing `trim`, proving relative resolution won unconditionally.
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "stdlib" / "str.rvl").write_text(
        "pub fn not_trim(s: Str) -> Str { return s }\n", encoding="utf-8"
    )
    main = tmp_path / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")

    with pytest.raises(RevlError, match="`trim` is not a public declaration"):
        compile_files([str(main)])


def test_revl_import_path_is_checked_before_the_stdlib_default(tmp_path, monkeypatch):
    # a stand-in module on REVL_IMPORT_PATH wins over the packaged stdlib —
    # the fallback list is a real search path, not a single hardcoded dir.
    override_root = tmp_path / "override"
    (override_root / "stdlib").mkdir(parents=True)
    (override_root / "stdlib" / "str.rvl").write_text(
        "pub fn trim(s: Str) -> Str { return \"OVERRIDDEN\" }\n", encoding="utf-8"
    )
    consumer_dir = tmp_path / "consumer"
    consumer_dir.mkdir()
    main = consumer_dir / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")

    monkeypatch.setenv("REVL_IMPORT_PATH", str(override_root))
    ir = compile_files([str(main)])

    assert [fn["name"] for fn in ir["functions"]] == ["clean", "trim"]
    body = ir["functions"][1]["body"]
    assert "OVERRIDDEN" in str(body)


def test_missing_module_still_errors_clearly_and_names_the_search_path(tmp_path):
    main = tmp_path / "main.rvl"
    main.write_text('use "stdlib/no_such_module.rvl" { anything }\n', encoding="utf-8")

    with pytest.raises(RevlError) as exc:
        compile_files([str(main)])

    message = str(exc.value)
    assert "cannot find imported module `stdlib/no_such_module.rvl`" in message
    assert "search path" in message
    assert str(stdlib_root().parent) in message


def test_stdlib_root_directory_is_real():
    # sanity: the resolver's default fallback base actually has stdlib/ as a
    # child, so `os.path.join(base, "stdlib/str.rvl")` lands on a real file —
    # guards against the parent-vs-self mixup this join depends on.
    assert (stdlib_root() / "str.rvl").is_file()
    assert (stdlib_root().parent / "stdlib" / "str.rvl").is_file()
