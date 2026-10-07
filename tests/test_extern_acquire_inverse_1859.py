"""An `extern acquire`'s site `undo` is the inverse its declaration names.

Issue #1859 (slice 3). `extern acquire fn open_h() -> H undo close_h(result)`
names the one call that releases what it acquires, with `result` for the
handle. Before this, any site `undo` compiled beside such an acquisition:
`undo noop()`, a literal, another extern, a helper fn, and the unbound
`effect open_h() undo noop()`. Each ran at teardown, the handle stayed open,
and the teardown report still read clean.

The rule shares slice 1's code path (`_check_site_release` in
src/revl/lower.py): the site `undo` must call the declared inverse with the
same arity and pass the bound handle wherever the declaration passes
`result`. It runs after G5 and O1/B1, so the O1 double-close refusal of a
sibling handle keeps its own message. What it proves is the CHOICE of inverse;
that the host body reverts stays the declaration's assertion.

Each refusal is mirrored in selfhost/lower.rvl, byte for byte; the census
holds the gate to the fixture examples/rejections/g4_extern_undo_not_declared.rvl.
"""

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

_PRELUDE = (
    "type H = Opaque\n"
    "extern pure fn close_h(h: H) -> Unit = @py { return None }\n"
    "extern pure fn close_two(h: H, why: Str) -> Unit = @py { return None }\n"
    "extern pure fn noop() -> Unit = @py { return None }\n"
    "extern pure fn unlock() -> Unit = @py { return None }\n"
    "extern acquire fn open_h() -> H undo close_h(result) = @py { return 1 }\n"
    "extern acquire fn open_two() -> H undo close_two(result, \"done\")"
    " = @py { return 1 }\n"
    "extern acquire fn lock() -> H undo unlock() = @py { return 1 }\n"
    "fn helper() -> Unit { return noop() }\n"
    "service S { fn go(n: Int) -> Int }\n"
)
_HEAD = "component C provides s: S {\n"
_TAIL = "  provide s { fn go(n) = n }\n}\n"


def _src(body: str) -> str:
    return _PRELUDE + _HEAD + body + "\n" + _TAIL


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return excinfo.value


def _mismatch(bind: str, fn: str, form: str) -> str:
    return (f"the `undo` of `let {bind} = effect {fn}(...)` must be the inverse "
            f"`{fn}` declares, on THAT handle: write {form}")


_OPEN_H = _mismatch("h", "open_h", "`undo close_h(h)`")

# the issue's pairings of `let h = effect open_h()` with a site `undo`: only the
# declared inverse on the bound handle compiles
PAIRINGS = [
    ("  let h = effect open_h() undo close_h(h)", None),
    ("  let h = effect open_h undo close_h(h)", None),
    ("  let h = effect open_h() undo noop()", _OPEN_H),
    ("  let h = effect open_h() undo unlock()", _OPEN_H),
    ("  let h = effect open_h() undo helper()", _OPEN_H),
    ("  let h = effect open_h() undo 1", _OPEN_H),
    ("  let h = effect open_h undo noop()", _OPEN_H),
]


@pytest.mark.parametrize("body, expected", PAIRINGS)
def test_a_site_undo_must_be_the_declared_inverse_on_the_bound_handle(body, expected):
    if expected is None:
        assert compile_source(_src(body), "t.rvl")["components"]
        return
    err = _refusal(_src(body))
    assert err.message == expected
    assert err.code == "G4"
    # the hint says what is proven and what stays asserted
    assert "what the declaration asserts" in err.hint


def test_an_unbound_acquisition_whose_inverse_takes_the_handle_is_refused():
    err = _refusal(_src("  effect open_h() undo noop()"))
    assert err.message == (
        "`effect open_h(...)` must bind its handle so its `undo` can release "
        "it: write `let <name> = effect open_h(...)` with `undo close_h(<name>)`")
    assert err.code == "G4"


def test_a_declared_inverse_with_other_arguments_pins_only_the_result_slot():
    # the `result` slot must be the bound handle; the other arguments are the
    # author's, already checked against the inverse's signature
    assert compile_source(_src('  let h = effect open_two() undo close_two(h, "x")'),
                          "t.rvl")["components"]
    err = _refusal(_src("  let h = effect open_two() undo close_h(h)"))
    assert err.message == _mismatch(
        "h", "open_two",
        "`undo close_two(...)` with `h` where the declaration passes `result`")


def test_a_declared_inverse_that_takes_no_handle_pins_the_callee():
    assert compile_source(_src("  effect lock() undo unlock()"), "t.rvl")["components"]
    assert compile_source(_src("  let k = effect lock() undo unlock()"), "t.rvl")["components"]
    err = _refusal(_src("  effect lock() undo noop()"))
    assert err.message == (
        "the `undo` of `effect lock(...)` must be the inverse `lock` declares: "
        "write `undo unlock(...)` as the declaration calls it")


def test_a_sibling_handle_keeps_the_o1_double_close_refusal():
    # the extern rule runs after O1: closing ANOTHER bracket's handle is a
    # double-close, and that is the message the author gets
    err = _refusal(_src("  let a = effect open_h() undo close_h(a)\n"
                        "  let b = effect open_h() undo close_h(a)"))
    assert err.code == "G7"
    assert "would double-close" in err.message


def test_a_provide_method_acquisition_names_both_legal_spellings():
    err = _refusal(_PRELUDE.replace("service S { fn go(n: Int) -> Int }\n", "")
                   + "service S { fn go(n: Int) -> Int }\n"
                   "component C provides s: S {\n"
                   "  provide s {\n"
                   "    fn go(n) {\n"
                   "      effect open_h() undo noop()\n"
                   "      return n\n"
                   "    }\n"
                   "  }\n"
                   "}\n")
    assert err.message == (
        "`effect open_h(...)` in a provide method cannot name its handle, so its "
        "declared `undo close_h(...)` cannot be written at the site: write a site "
        "`undo` naming the values this acquisition was given, or declare `open_h` "
        "`witnessed` and drop the site `undo`, and its declared `undo close_h(...)` "
        "releases each acquisition")
    # the hint says which spelling reverts on a clean unload and which does not
    assert "a site `undo` replays on a clean unload AND on abort" in err.hint


# -- issue #2102: the seam's NAME-addressed release -------------------------
#
# At a provide-method seam the acquisition cannot be bound and `result` is not
# in scope, so an `extern acquire` whose declared inverse takes `result` has no
# spelling that names the handle. What the author CAN write is a release naming
# the values the acquisition was given: that release is the teardown that
# actually runs, on abort AND on a clean unload, which is why `witnessed` --
# the refusal's other spelling, discharged on commit -- is not a substitute for
# it. The refusals below pin the property the relaxation turns on, the VALUES;
# each was confirmed to bite by reverting the relaxation and watching it go red
# (the PR body records the run).

_SEAM_PRELUDE = (
    "type Handle = Opaque\n"
    "extern acquire fn open_h(owner: Str, id: Str) -> Handle undo close_h(result)"
    ' = @py { return owner + "/" + id }\n'
    "extern pure fn close_h(h: Handle) -> Str = @py { return h }\n"
    "extern pure fn close_named(owner: Str, id: Str) -> Str"
    ' = @py { return owner + "/" + id }\n'
    "extern pure fn close_owner(owner: Str) -> Str = @py { return owner }\n"
    "extern pure fn close_all() -> Str = @py { return \"all\" }\n"
    "service Ops { emission fn start(id: Str) -> Str }\n"
)
_SEAM_HEAD = ("component OpsC provides ops: Ops {\n"
              '  config { owner: Str = "default", other: Str = "o2" }\n'
              "  provide ops {\n"
              "    fn start(id) {\n")
_SEAM_TAIL = ("      return id\n"
              "    }\n"
              "  }\n"
              "}\n")


def _seam_src(acquire: str, undo: str | None) -> str:
    body = f"      effect {acquire}\n"
    if undo is not None:
        body += f"      undo   {undo}\n"
    return _SEAM_PRELUDE + _SEAM_HEAD + body + _SEAM_TAIL


def _seam_refusal(acquire: str, undo: str | None) -> RevlError:
    return _refusal(_seam_src(acquire, undo))


def test_2102_a_seam_release_naming_the_acquisitions_values_is_admitted():
    # the issue's reproducer: `undo close_named(config.owner, id)` beside
    # `effect open_h(config.owner, id)`, whose declared inverse is
    # `close_h(result)` and therefore unnameable at the site.
    ir = compile_source(_seam_src("open_h(config.owner, id)",
                                  "close_named(config.owner, id)"), "t.rvl")
    step = ir["components"][0]["body"][0]["methods"][0]["body"][0]
    assert step["step"] == "effect"
    assert step["acquire"]["args"] == [{"kind": "config", "field": "owner"},
                                       {"kind": "name", "id": "id"}]
    assert step["undo"] == {"kind": "fn", "name": "close_named",
                            "args": [{"kind": "config", "field": "owner"},
                                     {"kind": "name", "id": "id"}]}


def test_2102_a_seam_release_may_take_a_prefix_of_the_acquisitions_values():
    # a release that needs only the acquisition's first value
    assert compile_source(_seam_src("open_h(config.owner, id)",
                                    "close_owner(config.owner)"), "t.rvl")["components"]


SEAM_VALUE_MISMATCHES = [
    # a literal where the acquisition was given a value
    ("open_h(config.owner, id)", 'close_named("other", id)'),
    # the acquisition's values swapped
    ("open_h(config.owner, id)", "close_named(id, config.owner)"),
    # another config field
    ("open_h(config.owner, id)", "close_named(config.other, id)"),
    # a release naming none of the acquisition's values
    ("open_h(config.owner, id)", 'close_named("a", "b")'),
    # a literal acquisition released by a different literal
    ('open_h("a", "b")', 'close_named("a", "c")'),
]


@pytest.mark.parametrize("acquire, undo", SEAM_VALUE_MISMATCHES)
def test_2102_a_seam_release_not_naming_the_acquisitions_values_is_refused(acquire, undo):
    err = _seam_refusal(acquire, undo)
    assert err.code == "G4"
    assert err.category == "inverse"
    assert "in a provide method cannot name its handle" in err.message


def test_2102_a_seam_release_called_with_no_arguments_is_refused():
    # a declared zero-argument release: admitted by the argument judgment, and
    # refused here because it names none of the acquisition's values
    err = _seam_refusal("open_h(config.owner, id)", "close_all()")
    assert err.code == "G4"
    assert "in a provide method cannot name its handle" in err.message


def test_2102_a_seam_acquisition_with_no_site_undo_is_still_refused():
    err = _seam_refusal("open_h(config.owner, id)", None)
    assert "has no site `undo`" in err.message


def test_the_rejection_fixture_is_refused_with_the_rule():
    from pathlib import Path
    path = (Path(__file__).resolve().parents[1] / "examples" / "rejections"
            / "g4_extern_undo_not_declared.rvl")
    err = _refusal(path.read_text(encoding="utf-8"))
    assert err.message == _mismatch("log", "log_open", "`undo log_close(log)`")


def test_a_typed_hole_in_the_site_undo_is_not_refused():
    # an unfilled obligation, as in the host half: admission refuses the draft
    # until it is filled, and the fill is judged by this rule
    assert compile_source(_src('  let h = effect open_h() undo hole[Unit] "release"'),
                          "t.rvl")["components"]
