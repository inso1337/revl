"""Issue #2856: behavior comment blocks derived from the IR.

A managed block between `// revl:behavior begin` and `// revl:behavior end`
carries the facts a component's behavior comment used to keep by hand: its
effect summary, the capabilities its emissions cross, its realm placement, its
extern surface and the sha256 of the IR they were derived from. `revl docs
gen` writes or refreshes it; `revl docs check` refuses a block whose digest no
longer matches the IR. Hand-written prose outside the markers is untouched,
and a component with no block is not an error, so adoption is incremental.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from revl.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples" / "user_cache.rvl"

DIGEST_RE = re.compile(r"^// digest: sha256:([0-9a-f]{64})$", re.MULTILINE)

COMPONENT = """\
service Db {
  emission[db] fn execute(sql: Str) -> Int
}

service Cache {
  fn get(key: Str) -> Opt[Str]
  emission[db] fn put(key: Str, value: Str)
}

// hand-written prose above the managed block
component Store requires db: Db provides cache: Cache {
  let store = effect Map.new() undo store.drop()

  provide cache {
    fn get(key) = store.get(key)
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO cache_log VALUES (${key})`)
    }
  }
}
"""

TEMPLATE = """\
service Db {
  emission[db] fn execute(sql: Str) -> Int
}

service Work {
  emission[db] fn run() -> Int
}

component Spawner requires db: Db {
  let worker = effect spawn Worker with { } undo worker.dispose()
}

component Worker requires db: Db provides work: Work {
  provide work {
    fn run() -> Int {
      emit db.execute(`row`)
      return 0
    }
  }
}
"""

EXTERN_SCOPE = """\
extern emission[net] fn fetch(url: Str) -> Int = @py { return 0 }

service Ping {
  emission[net] fn ping() -> Int
}

component Pinger provides ping: Ping {
  provide ping {
    fn ping() -> Int {
      emit fetch("x")
      return 0
    }
  }
}
"""

PROVENANCE_NAMED_KEY = """\
service Db {
  fn get() -> Int
}

service Cache {
  fn get() -> Int
}

service Own {
  fn value() -> Int
}

component Store requires %(key)s: Db provides own: Own {
  provide own {
    fn value() -> Int = %(key)s.get()
  }
}
"""


REACHED_FUNCTIONS = """\
fn base_workers() -> Int { return 4 }

fn default_workers() -> Int { return base_workers() }

fn unrelated() -> Int { return 1 }

service Pool {
  fn size() -> Int
}

component Sized provides pool: Pool {
  provide pool {
    fn size() -> Int = default_workers()
  }
}
"""

MARKERS_AS_DATA = """\
fn banner() -> Str {
  return \"\"\"
// revl:behavior end
// revl:behavior begin (managed)
\"\"\"
}

fn note() -> Str {
  return `
// revl:behavior end
`
}

extern pure fn host_note() -> Str = @py {
  text = \"\"\"
// revl:behavior begin
\"\"\"
  return text
}

service Doc {
  fn text() -> Str
}

component Writer provides doc: Doc {
  provide doc {
    fn text() -> Str = banner()
  }
}
"""


def _write(tmp_path: Path, text: str, name: str = "program.rvl") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _declaration_line(lines: list[str], name: str) -> int:
    pattern = re.compile(rf"\s*(?:boot\s+)?component\s+{re.escape(name)}\b")
    for index, line in enumerate(lines):
        if pattern.match(line) and not line.lstrip().startswith("//"):
            return index
    raise AssertionError(f"no declaration of {name}")


def _block(lines: list[str], name: str) -> list[str]:
    """The managed block directly above `name`'s declaration."""
    decl = _declaration_line(lines, name)
    assert lines[decl - 1].strip() == "// revl:behavior end", \
        f"no managed block above {name}"
    start = decl - 1
    while lines[start].strip() != "// revl:behavior begin":
        start -= 1
        assert start >= 0
    return lines[start:decl]


def _facts(block: list[str]) -> dict[str, str]:
    out = {}
    for line in block:
        match = re.match(r"^// ([a-z]+): (.*)$", line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def _digest(block: list[str]) -> str:
    match = DIGEST_RE.search("\n".join(block))
    assert match is not None, "block carries no sha256 digest"
    return match.group(1)


def test_gen_writes_a_component_block_with_the_ir_facts(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)

    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    block = _block(text.split("\n"), "Store")
    facts = _facts(block)

    assert facts["kind"] == "component"
    assert facts["effects"] == "2 reversible effects, 1 emission"
    assert facts["capabilities"] == "db"
    assert facts["realms"] == "shared"
    assert facts["externs"] == "Map.new"
    assert len(_digest(block)) == 64
    # the hand-written prose outside the markers is untouched
    assert "// hand-written prose above the managed block" in text
    # the managed block compiles like any other comment
    assert main(["compile", str(path)]) == 0
    assert main(["docs", "check", str(path)]) == 0


def test_gen_writes_a_block_for_a_template_and_its_spawner(tmp_path):
    path = _write(tmp_path, TEMPLATE)

    assert main(["docs", "gen", str(path)]) == 0
    lines = path.read_text(encoding="utf-8").split("\n")

    assert _facts(_block(lines, "Worker"))["kind"] == "template"
    assert _facts(_block(lines, "Spawner"))["kind"] == "component"
    assert main(["docs", "check", str(path)]) == 0


def test_capabilities_include_a_reached_externs_declared_scope(tmp_path):
    path = _write(tmp_path, EXTERN_SCOPE, "extern_scope.rvl")

    assert main(["docs", "gen", str(path)]) == 0
    facts = _facts(_block(path.read_text(encoding="utf-8").split("\n"), "Pinger"))

    assert facts["capabilities"] == "net"
    assert facts["externs"] == "fetch"
    assert facts["effects"] == "0 reversible effects, 1 emission"
    assert main(["docs", "check", str(path)]) == 0


def test_gen_is_idempotent(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)

    assert main(["docs", "gen", str(path)]) == 0
    first = path.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["docs", "gen", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == first
    assert "up to date" in capsys.readouterr().out


def test_check_refuses_a_changed_component_and_gen_updates_the_block(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    before = _digest(_block(path.read_text(encoding="utf-8").split("\n"), "Store"))

    changed = path.read_text(encoding="utf-8").replace(
        "INSERT INTO cache_log", "INSERT INTO cache_log_v2")
    path.write_text(changed, encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    errors = capsys.readouterr().err
    assert "outdated" in errors
    assert "revl docs gen" in errors
    assert str(path) in errors

    assert main(["docs", "gen", str(path)]) == 0
    assert main(["docs", "check", str(path)]) == 0
    after = _digest(_block(path.read_text(encoding="utf-8").split("\n"), "Store"))
    assert after != before


def test_check_refuses_a_hand_edited_fact_line(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    assert "// effects: 2 reversible effects, 1 emission" in text
    path.write_text(
        text.replace("// effects: 2 reversible effects, 1 emission",
                     "// effects: 99 reversible effects, 0 emissions"),
        encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "does not match the facts derived from the IR" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["source", "line", "file"])
def test_a_capability_key_named_like_provenance_still_stales_the_block(
        tmp_path, capsys, key):
    """The digest must not erase a semantic map entry keyed `source`/`line`/`file`."""
    path = _write(tmp_path, PROVENANCE_NAMED_KEY % {"key": key}, "named_key.rvl")
    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    before = _digest(_block(text.split("\n"), "Store"))

    changed = text.replace(f"requires {key}: Db", f"requires {key}: Cache")
    assert changed != text
    path.write_text(changed, encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "outdated" in capsys.readouterr().err
    assert main(["docs", "gen", str(path)]) == 0
    assert main(["docs", "check", str(path)]) == 0
    after = _digest(_block(path.read_text(encoding="utf-8").split("\n"), "Store"))
    assert after != before


def test_check_is_quiet_for_a_component_with_no_block(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 0
    assert capsys.readouterr().err == ""
    assert "revl:behavior begin" not in path.read_text(encoding="utf-8")


def test_prose_outside_the_block_is_free_and_position_independent(tmp_path):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")

    # a hand-written sentence above the block is not part of the digest, and
    # moving the component down the file does not change the IR either
    rewritten = text.replace("// hand-written prose above the managed block",
                             "// rewritten prose above the managed block")
    path.write_text("\n\n" + rewritten, encoding="utf-8")

    assert main(["docs", "check", str(path)]) == 0
    assert "// rewritten prose above the managed block" in path.read_text(
        encoding="utf-8")


def test_check_refuses_a_malformed_marker_pair(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    lines = path.read_text(encoding="utf-8").split("\n")
    decl = _declaration_line(lines, "Store")
    lines.insert(decl, "// revl:behavior begin")
    path.write_text("\n".join(lines), encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "pair directly above the component" in capsys.readouterr().err


def test_check_refuses_a_block_that_is_above_no_component(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    lines = path.read_text(encoding="utf-8").split("\n")
    orphan = _block(lines, "Store")
    assert orphan[0].strip() == "// revl:behavior begin"
    lines[0:0] = orphan + [""]
    path.write_text("\n".join(lines), encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "not directly above a component declaration" in capsys.readouterr().err


def test_check_refuses_an_orphaned_block_in_a_file_with_no_component(tmp_path, capsys):
    path = _write(tmp_path, "// revl:behavior begin\n"
                            "// kind: component\n"
                            "// revl:behavior end\n", "orphan.rvl")

    assert main(["docs", "check", str(path)]) == 1
    assert "not directly above a component declaration" in capsys.readouterr().err


def test_check_refuses_a_stray_end_marker(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT + "\n// revl:behavior end\n")

    assert main(["docs", "check", str(path)]) == 1
    assert "not directly above a component declaration" in capsys.readouterr().err


def test_check_refuses_a_marker_with_trailing_text(tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("// revl:behavior begin",
                                 "// revl:behavior begin (managed)", 1),
                    encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "pair directly above the component" in capsys.readouterr().err


def test_realms_are_rendered_from_the_ir(tmp_path):
    source = (ROOT / "examples" / "tenants.rvl").read_text(encoding="utf-8")
    path = _write(tmp_path, source, "tenants.rvl")
    assert main(["docs", "gen", str(path)]) == 0
    lines = path.read_text(encoding="utf-8").split("\n")

    assert _facts(_block(lines, "TenantAStore"))["realms"] == "isolate(kv in tenant_a)"
    assert _facts(_block(lines, "TenantBApp"))["realms"] == "isolate(kv in tenant_b)"
    assert main(["docs", "check", str(path)]) == 0


def test_the_corpus_component_carries_a_current_block(capsys):
    text = CORPUS.read_text(encoding="utf-8")
    assert "// revl:behavior begin" in text
    assert _facts(_block(text.split("\n"), "UserCache"))["capabilities"] == "db"

    assert main(["docs", "check", str(CORPUS)]) == 0
    assert capsys.readouterr().err == ""


def test_a_change_in_a_called_function_stales_the_block(tmp_path, capsys):
    path = _write(tmp_path, REACHED_FUNCTIONS, "reached.rvl")
    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    before = _digest(_block(text.split("\n"), "Sized"))

    # a function the component does not reach is not part of its behavior
    path.write_text(text.replace("fn unrelated() -> Int { return 1 }",
                                 "fn unrelated() -> Int { return 2 }"),
                    encoding="utf-8")
    capsys.readouterr()
    assert main(["docs", "check", str(path)]) == 0

    # the function the component calls, and the one that function calls
    for old, new in (("{ return base_workers() }", "{ return base_workers() + 1 }"),
                     ("{ return 4 }", "{ return 9 }")):
        current = path.read_text(encoding="utf-8")
        assert old in current
        path.write_text(current.replace(old, new), encoding="utf-8")
        capsys.readouterr()

        assert main(["docs", "check", str(path)]) == 1
        assert "outdated" in capsys.readouterr().err
        assert main(["docs", "gen", str(path)]) == 0
        assert main(["docs", "check", str(path)]) == 0

    after = _digest(_block(path.read_text(encoding="utf-8").split("\n"), "Sized"))
    assert after != before


def test_a_change_in_a_reached_externs_host_body_stales_the_block(tmp_path, capsys):
    path = _write(tmp_path, EXTERN_SCOPE, "extern_body.rvl")
    assert main(["docs", "gen", str(path)]) == 0
    path.write_text(path.read_text(encoding="utf-8").replace(
        "@py { return 0 }", "@py { return 1 }"), encoding="utf-8")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "outdated" in capsys.readouterr().err


def test_a_component_that_reaches_nothing_keeps_its_digest():
    # the corpus blocks were written before the reach existed and must not move
    assert main(["docs", "check", str(CORPUS)]) == 0


def test_gen_keeps_crlf_line_endings_and_other_lines_byte_for_byte(tmp_path, capsys):
    path = tmp_path / "crlf.rvl"
    path.write_bytes(COMPONENT.replace("\n", "\r\n").encode("utf-8"))

    assert main(["docs", "gen", str(path)]) == 0
    data = path.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") > 0
    assert b"// revl:behavior begin\r\n" in data
    capsys.readouterr()
    assert main(["docs", "check", str(path)]) == 0
    assert capsys.readouterr().err == ""

    # refreshing a stale block leaves the file CRLF too, and gen is idempotent
    path.write_bytes(data.replace(b"cache_log", b"cache_log_v2"))
    assert main(["docs", "check", str(path)]) == 1
    assert main(["docs", "gen", str(path)]) == 0
    refreshed = path.read_bytes()
    assert refreshed.count(b"\n") == refreshed.count(b"\r\n")
    assert main(["docs", "gen", str(path)]) == 0
    assert path.read_bytes() == refreshed


def test_gen_rewrites_only_the_block_in_a_file_with_mixed_endings(tmp_path):
    lines = COMPONENT.split("\n")
    mixed = "\n".join(line + "\r" if index % 2 == 0 else line
                      for index, line in enumerate(lines))
    path = tmp_path / "mixed.rvl"
    path.write_bytes(mixed.encode("utf-8"))

    assert main(["docs", "gen", str(path)]) == 0
    written = path.read_bytes().decode("utf-8")
    block_start = written.index("// revl:behavior begin")
    block_end = written.index("// revl:behavior end") + len("// revl:behavior end")
    before_block = mixed[:mixed.index("component Store")]
    assert written.startswith(before_block)
    assert written[block_end:].lstrip("\r\n").startswith("component Store")
    assert written.endswith(mixed[mixed.index("component Store"):])
    assert block_start >= len(before_block)


def test_gen_writes_through_a_temporary_file_and_keeps_the_mode(tmp_path):
    path = _write(tmp_path, COMPONENT)
    path.chmod(0o640)

    assert main(["docs", "gen", str(path)]) == 0

    assert path.stat().st_mode & 0o777 == 0o640
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name]


def test_a_failed_write_leaves_the_source_untouched(tmp_path, monkeypatch, capsys):
    path = _write(tmp_path, COMPONENT)
    original = path.read_bytes()

    def refuse(source, target):
        raise OSError("disk is full")

    monkeypatch.setattr("os.replace", refuse)
    assert main(["docs", "gen", str(path)]) == 1

    assert "cannot write" in capsys.readouterr().err
    assert path.read_bytes() == original
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("verb", ["gen", "check"])
def test_a_directory_argument_is_refused_with_one_line_and_exit_2(tmp_path, capsys, verb):
    capsys.readouterr()

    assert main(["docs", verb, str(tmp_path)]) == 2

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "is a directory" in err[0]


def test_marker_text_inside_a_string_template_or_host_body_is_data(tmp_path, capsys):
    path = _write(tmp_path, MARKERS_AS_DATA, "data.rvl")
    assert main(["compile", str(path)]) == 0
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 0
    assert capsys.readouterr().err == ""

    assert main(["docs", "gen", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    # the data lines are untouched and the one real block sits above Writer
    assert text.count("// revl:behavior end") == 3
    assert text.count("// revl:behavior begin") == 3
    assert _facts(_block(text.split("\n"), "Writer"))["kind"] == "component"
    assert main(["docs", "check", str(path)]) == 0


def test_a_real_stray_marker_is_still_refused_beside_marker_shaped_data(tmp_path, capsys):
    path = _write(tmp_path, MARKERS_AS_DATA + "\n// revl:behavior end\n", "data.rvl")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1
    assert "not directly above a component declaration" in capsys.readouterr().err


def test_a_leading_byte_order_mark_stays_first_when_a_block_goes_above_line_one(
        tmp_path, capsys):
    path = tmp_path / "bom.rvl"
    path.write_bytes(b"\xef\xbb\xbf" + COMPONENT.encode("utf-8"))
    one_line = tmp_path / "bom_first_line.rvl"
    one_line.write_bytes("\ufeffcomponent Lone {\n}\n".encode("utf-8"))

    for target in (path, one_line):
        assert main(["docs", "gen", str(target)]) == 0
        data = target.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf")
        assert data.count(b"\xef\xbb\xbf") == 1
        capsys.readouterr()
        assert main(["compile", str(target)]) == 0
        assert main(["docs", "check", str(target)]) == 0
        assert capsys.readouterr().err == ""


def test_an_up_to_date_block_is_not_rewritten_in_a_file_with_mixed_endings(
        tmp_path, capsys):
    path = _write(tmp_path, COMPONENT)
    assert main(["docs", "gen", str(path)]) == 0
    lines = path.read_text(encoding="utf-8").split("\n")
    # alternate the endings over the whole file, the current block included
    mixed = "\n".join(line + "\r" if index % 2 else line
                      for index, line in enumerate(lines)).encode("utf-8")
    path.write_bytes(mixed)
    capsys.readouterr()

    assert main(["docs", "gen", str(path)]) == 0

    assert path.read_bytes() == mixed
    assert "up to date" in capsys.readouterr().out
    assert main(["docs", "check", str(path)]) == 0


def test_a_declaration_on_the_last_line_with_no_final_newline_takes_the_files_ending(
        tmp_path):
    source = ("service Db {\r\n  emission[db] fn execute(sql: Str) -> Int\r\n}\r\n\r\n"
              "component One requires db: Db {\r\n  emit db.execute(\"x\")\r\n}")
    path = tmp_path / "no_final_newline.rvl"
    path.write_bytes(source.encode("utf-8"))

    assert main(["docs", "gen", str(path)]) == 0

    data = path.read_bytes()
    assert not data.endswith(b"\n")
    assert data.count(b"\n") == data.count(b"\r\n")
    assert main(["docs", "check", str(path)]) == 0


def test_several_data_markers_among_real_ones_are_classified_one_by_one(tmp_path, capsys):
    # three marker-shaped lines inside a string, one real stray marker after it
    source = ('fn banner() -> Str {\n  return """\n'
              "// revl:behavior end\n// revl:behavior end\n"
              '// revl:behavior begin (managed)\n"""\n}\n'
              "// revl:behavior end\n"
              "service Doc {\n  fn text() -> Str\n}\n"
              "component Writer provides doc: Doc {\n"
              "  provide doc {\n    fn text() -> Str = banner()\n  }\n}\n")
    path = _write(tmp_path, source, "classify.rvl")
    capsys.readouterr()

    assert main(["docs", "check", str(path)]) == 1

    errors = capsys.readouterr().err
    # the one real comment is refused, at its own line; the data lines are not
    assert errors.count("not directly above a component declaration") == 1
    assert "classify.rvl:8:" in errors
