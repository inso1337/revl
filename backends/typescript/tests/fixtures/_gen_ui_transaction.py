"""One-off generator for ui_transaction.ir.json. NOT part of the build.

The ts mirror of tests/test_ui_transaction_runtime_1369.py (issue #1369, item
522 slice 3): item 522's five-step UI transaction, the same program with its
last crossing in tail position, and an async unit, each over a fake desktop
whose host bodies `record` what they ran on the runtime's `hostLog`. A host
body throws when `globalThis.__revlUiFail` names its line and the occurrence
it has reached (`locate:Approve#2` is the second resolution of `Approve`), and
`clear_memo` throws when `globalThis.__revlUiCompFail` is set, so a test picks
the failure without a second program.

The whole source is compiled by `compile_source`, so the shapes are real
compiler output. Run once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_ui_transaction.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_UI_TARGET = """
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
"""

_TARGET = ("{ application: 'Billing', window: 'w', role: 'r', name, "
           "evidence: 'e', action: 'a', session: 's', bounds: 'b', "
           "expiry: 0n, confirm: false }")


def _host(line: str, ret: str = "", comp_fail: bool = False) -> str:
    """A fake-desktop @ts body: records `line`, throws on the configured
    occurrence, and returns `ret`."""
    fail_comp = ("  if (g.__revlUiCompFail) throw new Error('the memo field is gone')\n"
                 if comp_fail else "")
    returns = f"  return {ret}\n" if ret else ""
    return (
        "@ts {\n"
        "  const g = globalThis as any\n"
        f"  const line = {line}\n"
        "  record(line)\n"
        f"{fail_comp}"
        "  g.__revlUiSeen = g.__revlUiSeen ?? {}\n"
        "  g.__revlUiSeen[line] = (g.__revlUiSeen[line] ?? 0) + 1\n"
        "  const [want, nth] = String(g.__revlUiFail ?? '').split('#')\n"
        "  if (line === want && g.__revlUiSeen[line] === Number(nth || '1')) {\n"
        "    throw new Error('the substrate could not confirm ' + line)\n"
        "  }\n"
        f"{returns}"
        "}"
    )


_DECLARATIONS = f"""
extern emission[screen.observe] fn read_pane(region: Str) -> Str
  = {_host("'observe'", "'pane'")}
extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget
  = {_host("'locate:' + name", _TARGET)}
extern pure fn clear_amount() = {_host("'compensate:clear_amount'")}
extern pure fn clear_memo() = {_host("'compensate:clear_memo'", comp_fail=True)}
extern pure fn clear_note() = {_host("'compensate:clear_note'")}
extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = {_host("'type_amount'")}
extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = {_host("'type_memo'")}
extern emission[ui.click] fn actuate(target: UiTarget) = {_host("'actuate'")}
extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = {_host("'type_note'")}
extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str
  = {_host("'fetch_receipt'", "'receipt'")}
extern emission[ui.find] async fn locate_async(pane: Str, name: Str) -> UiTarget
  = {_host("'locate:' + name", _TARGET)}
extern emission[ui.click] async fn actuate_async(target: UiTarget)
  = {_host("'actuate'")}
"""

_SOURCE = _UI_TARGET + _DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
service TailOps { emission fn run(region: Str) }
service AsyncOps { async emission fn run(region: Str) -> Int }

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
      let typed = emit type_amount(amount, "10")
      let approve = emit locate(pane, "Approve")
      return emit actuate(approve)
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
"""


def build() -> dict:
    return compile_source(_SOURCE, "ui_transaction.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "ui_transaction.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
