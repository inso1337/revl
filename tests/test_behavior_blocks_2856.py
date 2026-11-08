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
