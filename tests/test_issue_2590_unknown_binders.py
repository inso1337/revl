"""Unknown local types must not fall through to private functions in other files.

Issue #2590: `types[FNS_KEY]` is project-wide, while function visibility is
file-scoped. An untyped local or loop binder must therefore remain unknown
when a different file declares a function with the same name.
"""

from pathlib import Path

from revl import compile_files


_OTHER_FILE = "fn shadow() -> Int { return 1 }\n"


def _compile_pair(tmp_path: Path, local_source: str) -> dict:
    local = tmp_path / "local.rvl"
    sibling = tmp_path / "sibling.rvl"
    local.write_text(local_source, encoding="utf-8")
    sibling.write_text(_OTHER_FILE, encoding="utf-8")
    # The source is valid by itself; adding a private same-named function in a
    # separate file must not change how its unknown local is inferred.
    compile_files([str(local)])
    return compile_files([str(local), str(sibling)])


def test_unknown_let_binding_stays_unknown_across_files(tmp_path):
    source = """\
fn no_declared_return() {}
fn probe() -> Str {
  let shadow = no_declared_return()
  return shadow
}
"""
    _compile_pair(tmp_path, source)


def test_unknown_for_binding_stays_unknown_across_files(tmp_path):
    source = """\
fn unknown_iterable() {}
fn probe() -> Str {
  for (shadow of unknown_iterable()) {
    return shadow
  }
  return "done"
}
"""
    _compile_pair(tmp_path, source)


def test_component_match_arm_unknown_payload_does_not_fall_through(tmp_path):
    """The component IR inference path is a safe neighboring case.

    `_lower_component_pure_expr` uses `infer_ir` for match arms, so an unknown
    payload stays unknown even when another file has a same-named function.
    This pins the path the issue called out for investigation; it passed before
    the fix and remains out of the production diff.
    """
    source = """\
service S { fn get() -> Str }
fn unknown_value() {}
component C provides s: S {
  provide s { fn get() -> Str {
    return match unknown_value() {
      Some(shadow) => shadow,
      None => "ok"
    }
  } }
}
"""
    _compile_pair(tmp_path, source)
