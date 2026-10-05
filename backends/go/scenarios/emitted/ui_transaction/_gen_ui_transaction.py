"""One-off generator for ui_transaction.ir.json. NOT part of the build.

Issue #1369 (item 522 slice 3) on the go tier: the UI transaction unit. The
go mirror of tests/test_ui_transaction_runtime_1369.py and of the ts tier's
backends/typescript/tests/ui_transaction.test.ts, whose fixture generator
(backends/typescript/tests/fixtures/_gen_ui_transaction.py) this follows.

Item 522's five-step transaction (`Agent`), the same program with its last
crossing in tail position (`TailAgent`), and the async spelling
(`AsyncAgent`; go erases the async color, so it is the same unit), and a
unit holding every other kind of entry (`Mixed`), each over a fake desktop. Every host body goes through `uiStep` (or `uiCompensate` for
`clear_memo`), defined in exec_test.go: it records its line through the host
trace and panics on the occurrence a test configured, so a test picks the
failure without a second program.

The source lives here rather than in a `.rvl` file so it does not join the
scoring corpora (the census, the self-host oracles). The `lifecycle test` is
the smoke path that keeps the document on emit()'s live stc-go component
path. The whole source is compiled by `compile_source`, so the shapes are
real compiler output. Run once, by hand, to regenerate the checked-in IR:

    python3 backends/go/scenarios/emitted/ui_transaction/_gen_ui_transaction.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SOURCE = """\
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}

extern emission[screen.observe] fn read_pane(region: Str) -> Str = @go {
	uiStep("observe")
	return "pane"
}

extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget = @go {
	uiStep("locate:" + name)
	return UiTarget{Application: "Billing", Name: name}
}

extern pure fn clear_amount() = @go {
	uiStep("compensate:clear_amount")
}

extern pure fn clear_memo() = @go {
	uiCompensate("compensate:clear_memo")
}

extern pure fn clear_note() = @go {
	uiStep("compensate:clear_note")
}

extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = @go {
	uiStep("type_amount")
}

extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = @go {
	uiStep("type_memo")
}

extern emission[ui.click] fn actuate(target: UiTarget) = @go {
	uiStep("actuate")
}

extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = @go {
	uiStep("type_note")
}

extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str = @go {
	uiStep("fetch_receipt")
	return "receipt"
}

extern emission[ui.find] async fn locate_async(pane: Str, name: Str) -> UiTarget = @go {
	uiStep("locate:" + name)
	return UiTarget{Application: "Billing", Name: name}
}

extern emission[ui.click] async fn actuate_async(target: UiTarget) = @go {
	uiStep("actuate")
}

type Mark = { line: Str }
type MarkError = { code: Str }

extern pure fn unmark(w: Mark) -> Unit = @go {
	uiStep("undo:" + w.Line)
	return
}

extern witnessed[fs] fn mark(line: Str) -> Result[Mark, MarkError] undo unmark(result) = @go {
	uiStep("mark:" + line)
	return RevlOk[Mark, MarkError]{Value: Mark{Line: line}}
}

extern pure fn restore_row() = @go {
	uiStep("compensate:restore_row")
}

extern emission fn put_row(body: Str) -> Int compensate restore_row() = @go {
	uiStep("put:" + body)
	return 1
}

service Ops { emission fn run(region: Str) -> Int }
service TailOps { emission fn run(region: Str) }
service AsyncOps { async emission fn run(region: Str) -> Int }
service MixedOps { emission fn run(region: Str) }

component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      let pane1 = emit read_pane(region)
      let amount = emit locate(pane1, "Amount")
      emit type_amount(amount, "10")
      let pane2 = emit read_pane(region)
      let memo = emit locate(pane2, "Memo")
      emit type_memo(memo, "m")
      let pane3 = emit read_pane(region)
      let approve = emit locate(pane3, "Approve")
      emit actuate(approve)
      let pane4 = emit read_pane(region)
      let checked = emit locate(pane4, "Approve")
      let pane5 = emit read_pane(region)
      let note = emit locate(pane5, "Note")
      emit type_note(note, "n")
      let pane6 = emit read_pane(region)
      let receipt = emit locate(pane6, "Attach")
      let saved = emit fetch_receipt(receipt)
      return 1
    }
  }
}

component TailAgent provides tail: TailOps {
  provide tail {
    fn run(region) {
      let pane = emit read_pane(region)
      let amount = emit locate(pane, "Amount")
      emit type_amount(amount, "10")
      let approve = emit locate(pane, "Approve")
      emit actuate(approve)
    }
  }
}

component AsyncAgent provides async_ops: AsyncOps {
  provide async_ops {
    async fn run(region) {
      let amount = emit locate_async(region, "Amount")
      emit type_amount(amount, "10")
      let approve = emit locate_async(region, "Approve")
      emit actuate_async(approve)
      return 1
    }
  }
}

// the unit's other entries: a witnessed inverse (Phase 1), a non-computer-use
// extern's declared compensation in a value position, and a site-spelled
// compensation that replaces a computer-use extern's declared one
component Mixed provides mixed: MixedOps {
  provide mixed {
    fn run(region) {
      effect mark("a")
      let n = emit put_row("p")
      let target = emit locate(region, "Amount")
      emit type_amount(target, "10") compensate clear_note()
      emit actuate(target)
    }
  }
}

lifecycle test "clean unload of an un-called agent leaves no residue" {
  load Agent
  unload Agent
  assert no_residue
}
"""


def build() -> dict:
    return compile_source(SOURCE, "ui_transaction.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "ui_transaction.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
