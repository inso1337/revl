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
