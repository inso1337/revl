"""`revl context` / `revl_context`: the declarations-only grounding pack
(docs/context.md).

The pack is what an agent reads instead of the composition before it writes a
component, so these pin the four things that make it worth reading: it carries
every signature the component is held to, it carries no body, it builds from a
component whose body does not check yet, and its hash moves on a signature
edit and never on a doc edit. The examples bar holds the size claim against the
real tree.
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
from pathlib import Path

import pytest

from revl.context import context, count_tokens
from revl.lexer import lex
from revl.mcp import server
from revl.parser import Parser

ROOT = Path(__file__).resolve().parents[1]
MEDIATOR = str(ROOT / "examples" / "mediator.rvl")

sys.path.insert(0, str(ROOT / "bench"))
import tokens as bench_tokens  # noqa: E402

BROKEN_BODY = """\
service S {
  // the doc of f
  emission fn f(x: Int) -> Int
}
service P {
  fn g() -> Int
}
component C requires s: S provides p: P {
  provide p {
    fn g() = s.nope()
  }
}
"""


def _body_keywords(pack: str) -> list[str]:
    return [t.value for t in lex(pack, "<pack>")
            if t.kind == "kw" and t.value in ("effect", "undo", "provide")]


def _tool(arguments: dict) -> dict:
    tool = next(t for t in server.TOOLS if t["name"] == "revl_context")
    return tool["handler"](arguments)


def test_pack_carries_every_signature_and_no_body():
    record = context([MEDIATOR], component="NotesConsumer")
    assert record["ok"]
    assert record["header"] == (
        "component NotesConsumer requires writer: AppendWriter provides notes: Notes")
    roles = {(e["role"], e["service"]) for e in record["services"]}
    assert roles == {("provides", "Notes"), ("requires", "AppendWriter")}
    assert "emission fn append(path_under_dir: Str, data: Str) -> Int" in record["pack"]
    assert "emission fn save(data: Str) -> Int" in record["pack"]
    assert _body_keywords(record["pack"]) == []
    writer = next(e for e in record["services"] if e["service"] == "AppendWriter")
    assert writer["clause"] == "requires writer: AppendWriter"
    assert writer["providers"] == [{"component": "FsMediator", "key": "writer"}]


def test_a_body_that_does_not_check_still_grounds():
    from revl.compiler import compile_source
    from revl.errors import RevlError
    with pytest.raises(RevlError, match="not a method of service S"):
        compile_source(BROKEN_BODY)
    record = _tool({"source": BROKEN_BODY, "component": "C"})
    assert record["ok"]
    assert "emission fn f(x: Int) -> Int" in record["pack"]
    assert "s.nope" not in record["pack"]


def test_a_parse_error_is_a_structured_refusal():
    record = _tool({"source": "service S {\n  fn f( -> Int\n}\n"})
    assert record["ok"] is False
    assert record["diagnostics"][0]["line"] == 2


def test_hash_ignores_docs_and_layout_and_moves_on_a_signature():
    def hash_of(src: str) -> str:
        return context(["s.rvl"], sources={"s.rvl": src})["services"][0]["hash"]

    plain = hash_of("service S {\n  fn f(x: Int) -> Int\n}\n")
    documented = hash_of("// what S is for\nservice S {\n  // f's doc\n"
                         "  fn f(x: Int)   ->   Int  // trailing\n}\n")
    retyped = hash_of("service S {\n  fn f(x: Str) -> Int\n}\n")
    marked = hash_of("service S {\n  emission fn f(x: Int) -> Int\n}\n")
    assert plain == documented
    assert len({plain, retyped, marked}) == 3


def test_a_service_from_another_module_carries_its_use_line(tmp_path):
    (tmp_path / "lib.rvl").write_text(
        "pub service Clock {\n  fn now() -> Int\n}\n")
    app = tmp_path / "app.rvl"
    app.write_text('use "lib.rvl" { Clock }\n\n'
                   "service Tick {\n  fn tick() -> Int\n}\n\n"
                   "component Ticker requires clock: Clock provides tick: Tick {\n"
                   "  provide tick {\n    fn tick() = clock.now()\n  }\n}\n")
    record = context([str(app)], component="Ticker")
    clock = next(e for e in record["services"] if e["service"] == "Clock")
    tick = next(e for e in record["services"] if e["service"] == "Tick")
    assert clock["use"] == 'use "lib.rvl" { Clock }'
    assert tick["use"] is None
    assert record["pack"].index('use "lib.rvl" { Clock }') < record["pack"].index(
        "pub service Clock")
    assert sorted(Path(p).name for p in record["tokens"]["rawFiles"]) == [
        "app.rvl", "lib.rvl"]


def test_without_a_component_every_service_is_named_with_its_provider_key():
    record = context([MEDIATOR])
    by_name = {e["service"]: e for e in record["services"]}
    assert set(by_name) == {"FileStore", "AppendWriter", "Notes"}
    assert by_name["AppendWriter"]["clause"] == "requires writer: AppendWriter"
    assert by_name["FileStore"]["clause"] is None  # nothing in the file provides it


def test_budget_sheds_docs_then_collapses_from_the_last_service_back():
    path = str(ROOT / "examples" / "app" / "notes.rvl")
    full = context([path], component="NotesConsole")
    order = [e["service"] for e in full["services"]]
    assert order == ["NotesConsoleRpc", "WebUI", "Ranker"]

    record = context([path], component="NotesConsole", budget=200)
    budget = record["budget"]
    assert budget["fits"] and record["tokens"]["pack"] <= 200
    assert budget["steps"] == ["docs off: Ranker", "docs off: NotesConsoleRpc",
                               "collapsed: Ranker", "collapsed: WebUI",
                               "collapsed: NotesConsoleRpc"]
    for name in order:  # every service stays named, with its clause and hash
        assert f"service {name} " in record["pack"]
    assert all(e["hash"] == f["hash"] for e, f in
               zip(record["services"], full["services"]))

    floor = context([path], component="NotesConsole", budget=50)
    assert floor["budget"]["fits"] is False  # said, not truncated past a name
    assert all(f"service {name} " in floor["pack"] for name in order)

    roomy = context([path], component="NotesConsole", budget=10_000)
    assert roomy["budget"]["steps"] == [] and roomy["pack"] == full["pack"]


def test_an_unknown_component_names_the_ones_there_are():
    record = context([MEDIATOR], component="Nope")
    assert record["ok"] is False
    assert record["diagnostics"][0]["available"] == ["FsMediator", "NotesConsumer"]


def test_the_cli_prints_the_pack_and_exits_on_its_verdict():
    run = [sys.executable, "-P", "-m", "revl", "context", MEDIATOR]
    ok = subprocess.run(run + ["--component", "NotesConsumer", "--json"],
                        capture_output=True, text=True)
    assert ok.returncode == 0
    assert json.loads(ok.stdout)["header"].startswith("component NotesConsumer")
    text = subprocess.run(run + ["--component", "NotesConsumer"],
                          capture_output=True, text=True)
    assert text.stdout == context([MEDIATOR], component="NotesConsumer")["pack"]
    bad = subprocess.run(run + ["--component", "Nope"], capture_output=True, text=True)
    assert bad.returncode == 1 and "FsMediator, NotesConsumer" in bad.stderr


def test_the_token_proxy_is_the_one_tokens_to_green_counts_with():
    for text in ("", "emission fn f(x: Int) -> Int\n", BROKEN_BODY,
                 Path(MEDIATOR).read_text(encoding="utf-8")):
        assert count_tokens(text) == bench_tokens.count_tokens(text)


def test_examples_bar_the_pack_is_under_seventy_percent_and_bodiless():
    """The bar in docs/context.md, set before this change was measured:
    across every component of every example, the pack costs under 70% of the
    files it was cut from, and no pack carries a body keyword."""
    pack = raw = 0
    for path in sorted(glob.glob(str(ROOT / "examples" / "**" / "*.rvl"),
                                 recursive=True)):
        if "rejections" in Path(path).parts:
            continue
        for comp in Parser(Path(path).read_text(encoding="utf-8"), path).parse().components:
            record = context([path], component=comp.name)
            assert _body_keywords(record["pack"]) == [], (path, comp.name)
            pack += record["tokens"]["pack"]
            raw += record["tokens"]["raw"]
    assert raw and pack / raw < 0.70


# ----- each cut is the declaration's own text, found by its tokens

def _one(src: str, **kwargs) -> dict:
    return context(["one.rvl"], sources={"one.rvl": src}, **kwargs)


def test_two_services_on_one_line_each_get_their_own_text_and_hash():
    both = _one("service B { fn b() -> Int } service C { fn c() -> Str }\n")
    texts = {e["service"]: e["text"] for e in both["services"]}
    assert texts == {"B": "service B { fn b() -> Int }",
                     "C": "service C { fn c() -> Str }"}
    moved = _one("service B { fn b() -> Int } service C { fn c() -> Int }\n")
    hashes = lambda r: {e["service"]: e["hash"] for e in r["services"]}  # noqa: E731
    assert hashes(both)["B"] == hashes(moved)["B"]
    assert hashes(both)["C"] != hashes(moved)["C"]


def test_a_line_separator_in_a_comment_does_not_shift_later_cuts():
    record = _one("// a b\nservice A {\n  fn a() -> Int\n}\n"
                  "service B {\n  fn b() -> Int\n}\n")
    texts = {e["service"]: e["text"] for e in record["services"]}
    assert texts["A"].endswith("service A {\n  fn a() -> Int\n}")
    assert texts["B"] == "service B {\n  fn b() -> Int\n}"


def test_a_prefix_on_its_own_line_is_part_of_the_cut_and_pub_not_of_the_hash():
    record = _one("pub\nservice S {\n  fn f() -> Int\n}\n"
                  "commutative service T {\n  fn g() -> Int\n}\n")
    texts = {e["service"]: e["text"] for e in record["services"]}
    assert texts["S"].startswith("pub\nservice S {")
    assert texts["T"].startswith("commutative service T {")
    bare = _one("service S {\n  fn f() -> Int\n}\n")["services"][0]["hash"]
    assert record["services"][0]["hash"] == bare


def test_the_header_stops_at_the_brace_that_opens_the_body():
    one_line = ("service S { fn f() -> Int }\nservice P { fn g() -> Int }\n"
                "component C requires s: S provides p: P { provide p { fn g() = 1 } }\n")
    assert _one(one_line, component="C")["header"] == (
        "component C requires s: S provides p: P")
    commented = ("service P { fn g() -> Int }\n"
                 "boot component C provides p: P { // {x}\n"
                 "  provide p { fn g() = 1 }\n}\n")
    assert _one(commented, component="C")["header"] == "boot component C provides p: P"


def test_a_bare_name_resolves_through_the_use_that_names_it(tmp_path):
    (tmp_path / "a.rvl").write_text("pub service Clock {\n  fn wrong() -> Int\n}\n",
                                    encoding="utf-8")
    (tmp_path / "b.rvl").write_text("pub service Clock {\n  fn now() -> Int\n}\n",
                                    encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text('use "a.rvl" as A\nuse "b.rvl" { Clock }\n\n'
                   "service Tick {\n  fn tick() -> Int\n}\n\n"
                   "component Ticker requires clock: Clock provides tick: Tick {\n"
                   "  provide tick {\n    fn tick() = clock.now()\n  }\n}\n",
                   encoding="utf-8")
    clock = next(e for e in context([str(app)], component="Ticker")["services"]
                 if e["service"] == "Clock")
    assert "fn now() -> Int" in clock["text"]
    assert Path(clock["module"]).name == "b.rvl"
    assert clock["use"] == 'use "b.rvl" { Clock }'


# ----- the budget measures the pack it will print, and only ever shrinks it

def test_the_budget_counts_the_header_it_prints():
    name = "AVeryLongComponentNameThatCostsManyTokensWhenSpelled"
    src = ("service S {\n  // a doc block that costs a few tokens to carry\n"
           "  fn f(x: Int) -> Int\n}\n"
           f"component {name} provides s: S {{\n  provide s {{ fn f(x) = x }}\n}}\n")
    full = _one(src, component=name)["tokens"]["pack"]
    record = _one(src, component=name, budget=full - 1)
    assert record["budget"]["steps"] == ["docs off: S"]
    assert record["budget"]["fits"] and record["tokens"]["pack"] < full


def test_collapse_is_skipped_when_it_would_not_shorten_the_service():
    src = "service S { fn f() -> Int }\ncomponent C provides s: S {\n  provide s { fn f() = 1 }\n}\n"
    full = _one(src, component="C")
    record = _one(src, component="C", budget=1)
    assert record["budget"]["steps"] == []
    assert record["budget"]["fits"] is False
    assert record["pack"] == full["pack"]


# ----- inputs

def test_a_bare_source_reads_nothing_from_the_disk(tmp_path, monkeypatch):
    (tmp_path / "lib.rvl").write_text("pub service L {\n  fn l() -> Int\n}\n",
                                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    src = 'use "lib.rvl" { L }\ncomponent C requires l: L {\n}\n'
    refused = _tool({"source": src, "component": "C"})
    assert refused["ok"] is False and "`modules`" in refused["diagnostics"][0]["message"]
    given = _tool({"source": src, "component": "C",
                   "modules": {"lib.rvl": "pub service L {\n  fn m() -> Str\n}\n"}})
    assert given["ok"] and "fn m() -> Str" in given["pack"]


@pytest.mark.parametrize("budget", ["10", 0, -3, True, 2.5])
def test_a_budget_that_is_not_a_positive_integer_is_a_usage_refusal(budget):
    record = _tool({"source": BROKEN_BODY, "component": "C", "budget": budget})
    assert record["ok"] is False
    assert record["diagnostics"][0]["category"] == "usage"


def test_the_cli_refuses_a_non_positive_budget():
    run = subprocess.run([sys.executable, "-P", "-m", "revl", "context", MEDIATOR,
                          "--budget", "0"], capture_output=True, text=True)
    assert run.returncode == 2 and "at least 1" in run.stderr


def test_many_declarations_on_one_line_cost_a_lex_or_two_each(monkeypatch):
    # every service on one line, each operation named after the keyword and a
    # trailing comment holding a brace: each cut is still its first candidate,
    # where trying every pair of the line's matches grew about cubically
    import revl.context as module

    n, lexes, real = 200, 0, module._spelled

    def counted(text):
        nonlocal lexes
        lexes += 1
        assert lexes <= 3 * n, f"more than {3 * n} lexes for {n} declarations"
        return real(text)

    monkeypatch.setattr(module, "_spelled", counted)
    decls = [f"service S{i} {{ fn service{i}() -> Int }}" for i in range(n)]
    source = " ".join(decls) + " // } service }\n"
    result = context(["many.rvl"], sources={"many.rvl": source})
    assert [s["text"] for s in result["services"]] == decls


@pytest.mark.parametrize("tail", [
    "liveness 30s {\n  provide p {\n    fn g() = s.f()\n  }\n}\n",
    "liveness 30s { provide p { fn g() = s.f() } }\n",
    "liveness 1_000ms {\n  provide p {\n    fn g() = s.f()\n  }\n}\n",
    # many clauses on the line: every `s` in them is a bare-text lookalike
    " ".join(f"requires sss{i}: S" for i in range(25))
    + " liveness 30s { provide p { fn g() = s.f() } }\n",
])
def test_a_header_ending_in_a_duration_is_cut_at_its_suffix(tail):
    # `30s` lexes as `30` then `s`: the suffix is glued to the digits, so a
    # word-boundary match never finds it and the cut has to look again
    source = ("service S {\n  fn f() -> Int\n}\nservice P {\n  fn g() -> Int\n}\n"
              "component C requires s: S provides p: P " + tail)
    header = context(["d.rvl"], sources={"d.rvl": source}, component="C")["header"]
    assert header == "component C requires s: S provides p: P " + tail.split(" {")[0]
