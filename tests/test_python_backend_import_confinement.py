"""User-origin Python host bodies cannot reach backend helper modules."""

from __future__ import annotations

import pytest

from revl.compiler import compile_files, compile_source
from revl.errors import RevlError


BODY = """
extern pure fn steal(x: Str) -> Str = @py {
    import revl_fs_workspace
    return x
}
"""


def test_user_py_backend_import_is_refused(tmp_path):
    source = tmp_path / "main.rvl"
    source.write_text(BODY, encoding="utf-8")

    with pytest.raises(RevlError, match="user-origin @py body.*backend module"):
        compile_files([str(source)])


def test_user_py_backend_import_refusal_names_the_rule():
    with pytest.raises(RevlError, match="user @py backend-import rule"):
        compile_source(BODY)


def test_refusal_hint_points_at_the_stdlib_door_not_py_ref():
    """The hint must send the reader to the sanctioned stdlib surface, not to
    `@py ref` — which cannot reach an install-tree backend module, so the old
    hint cost a round of debugging before the real question surfaced (#460)."""
    try:
        compile_source(BODY)
    except RevlError as exc:
        hint = exc.hint or ""
        # `revl_fs_workspace` is exposed as the fs observation surface.
        assert 'use "stdlib/fs.rvl"' in hint, hint
        assert "resolve_within" in hint, hint
        # and the old misdirection is explicitly corrected.
        assert "`@py ref` cannot reach" in hint, hint
    else:
        raise AssertionError("expected a RevlError")


def test_refusal_hint_for_shell_classify_names_shell_door():
    """A body reaching `revl_shell_classify` is pointed at `stdlib/shell.rvl`'s
    `classify`, the door the harness's terminal toolbox uses (#460)."""
    body = """
extern pure fn verdict(cmd: Str) -> Str = @py {
    import revl_shell_classify
    return revl_shell_classify.classify(cmd)["verdict"]
}
"""
    try:
        compile_source(body)
    except RevlError as exc:
        assert 'use "stdlib/shell.rvl" { classify }' in (exc.hint or ""), exc.hint
    else:
        raise AssertionError("expected a RevlError")


def test_user_py_standard_library_import_remains_allowed(tmp_path):
    source = tmp_path / "main.rvl"
    source.write_text(
        """
extern pure fn identity(x: Str) -> Str = @py {
    import json
    return json.loads(json.dumps(x))
}
""",
        encoding="utf-8",
    )

    assert compile_files([str(source)])["externs"][0]["name"] == "identity"
