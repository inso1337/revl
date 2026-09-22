"""The programs the flagship demo runs (roadmap item 525, issue #1200).

They live here as Python strings rather than as `.rvl` files in the tree, and
the reason is measured rather than stylistic. `demo/` is a census corpus root
(`tools/gate_reference_census.py` CORPUS_DIRS), and the self-host gate does not
yet parse `route model` (item 512 slice 3) or carry the computer-use registry
(item 521/522 self-host ports). Dropping these programs in as files was tried
and measured on this branch: the admitting agent entered the census as
`false-reject/BAD` and the item-522 teardown refusal as `false-admit/G4`, one
of each, on a corpus that is otherwise unchanged from its baseline. The same
reasoning is written down in `tests/test_model_placement_512.py`, which keeps
its programs inline for the same corpus.

`demo/legacy_enterprise/run_demo.py` writes each of these into a throwaway
temp dir and drives the real `revl` CLI over it.
"""

from __future__ import annotations

# --------------------------------------------------------------- the agent
#
# One component, three rungs of the review's fallback ladder, in one realm so
# the erasure report has a unit to answer for.
#
#   rung 1  the typed API          `emit ledger.adjust(...)`   [db.invoice]
#   rung 2  the peer service       `emit peer.adjust(...)`     [net.ledger]
#   rung 3  computer-use           the `screen.*` / `ui.*` family
#
# Every `@py` body is a placeholder that returns a constant. The demo never
# drives a desktop and never claims to: the computer-use substrate is a host
# obligation filed upstream (roadmap item 539, inso1337/revl-harness#11), and a
# body that pretended to drive one would be the stub this demo exists not to
# ship. What is exercised here is revl's side of that line.

LADDER = """
// A legacy billing desktop. The refund path exists as a typed service, as a
// peer service, and - when neither answers - as a window somebody clicks.

model role triage on_device
model role drafter off_device

service Ledger {
  emission[db.invoice] fn adjust(invoice: Str, cents: Int) -> Str
}

service PeerLedger {
  emission[net.ledger] fn adjust(invoice: Str, cents: Int) -> Str
}

// Item 521 slice 4's target record. A program declaring `ui.find` or any
// actuation verb must carry it, and every field is required: a field the
// registry names and the author omits is a binding no later reader can
// recover. `ui.find` RETURNS it and the three actuation verbs each TAKE one,
// so the actuation is bound to the observation that justified it rather than
// naming its target by a string that is re-resolved at every use.
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

// rung 3. `Untrusted[...]` is the author's declaration AND, since item 521
// slice 2, revl's own derivation: `screen` and `ui.find` are source classes,
// so removing the qualifier below changes nothing about whether the value is
// untrusted. See `docs/design/551-flagship-demo.md` section 4.
extern emission[screen.observe] fn read_pane(region: Str) -> Untrusted[Str]
  = @py { return "" }

extern emission[ui.find] fn locate(hint: Str) -> Untrusted[UiTarget]
  = @py { return None }

extern pure fn clear_amount_field()
  = @py { return None }

// `ui.text` is compensatable, so the registry REQUIRES an inverse (item 522).
extern emission[ui.text] fn type_amount(target: UiTarget, amount: Str)
  compensate clear_amount_field()
  = @py { return None }

// `ui.click` is unknown and `ui.download` is irreversible. Neither may carry
// a `compensate`; both therefore report UNCOMPENSATED in the erasure report.
extern emission[ui.click] fn actuate(target: UiTarget)
  = @py { return None }

extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str
  = @py { return "" }

service Refund {
  emission fn settle(invoice: Str, cents: Int) -> Str
}

component LegacyAgent
  requires ledger: Ledger
  requires peer: PeerLedger
  provides refund: Refund
{
  isolate refund in realm("billing")

  route model on settle {
    confidential -> triage,
    * -> drafter
  }

  provide refund {
    fn settle(invoice, cents) {
      let pane = emit read_pane("invoice-detail")
      let field = emit locate("amount")
      emit type_amount(field, invoice)
      emit actuate(field)
      let receipt = emit fetch_receipt(field)
      let posted = emit ledger.adjust(invoice, cents)
      let mirrored = emit peer.adjust(invoice, cents)
      return posted
    }
  }
}
"""

# The same task with rung 1 only. This is the `revl audit --diff` baseline: it
# is what the composition reached before anyone descended the ladder.
TYPED_ONLY = """
service Ledger {
  emission[db.invoice] fn adjust(invoice: Str, cents: Int) -> Str
}

service Refund {
  emission fn settle(invoice: Str, cents: Int) -> Str
}

component LegacyAgent requires ledger: Ledger provides refund: Refund {
  isolate refund in realm("billing")
  provide refund {
    fn settle(invoice, cents) {
      let posted = emit ledger.adjust(invoice, cents)
      return posted
    }
  }
}
"""

# ------------------------------------------------------------- the refusals

#: The root names the whole GUI surface (item 521, G8).
REFUSE_UI_ROOT = """
extern emission[ui] fn do_anything(target: Str) = @py { return None }
"""

#: The verb set is closed, so a verb an author invents is refused rather than
#: admitted into a namespace no policy rule selects (item 521, G8).
REFUSE_UNKNOWN_VERB = """
extern emission[ui.drag] fn drag(target: Str) = @py { return None }
"""

#: An irreversible step may not claim an inverse (item 522, G4).
REFUSE_DOWNLOAD_COMPENSATE = """
extern pure fn rm_receipt() = @py { return None }
extern emission[ui.download] fn fetch_receipt(target: Str) -> Str
  compensate rm_receipt()
  = @py { return "" }
"""

#: `ui.click` is `unknown`, and unknown is handled exactly as irreversible
#: (item 522, G4).
REFUSE_CLICK_COMPENSATE = """
extern pure fn undo_click() = @py { return None }
extern emission[ui.click] fn actuate(target: Str)
  compensate undo_click()
  = @py { return None }
"""

#: A compensatable step must carry the inverse only its author can write
#: (item 522, G4). The failure direction is the point: silence is refused.
REFUSE_TEXT_WITHOUT_COMPENSATE = """
extern emission[ui.text] fn type_amount(target: Str, amount: Str)
  = @py { return None }
"""

#: Screen content is text. It cannot become the authority of a command
#: (item 249, G9). This is the review's own framing case.
REFUSE_SCREEN_TO_SINK = """
extern emission[screen.observe] fn read_pane(region: Str) -> Untrusted[Str]
  = @py { return "" }

extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }

service Ops { emission fn go() }

component Reader provides ops: Ops {
  provide ops {
    fn go() {
      let pane = emit read_pane("invoice-detail")
      emit run(pane)
      return
    }
  }
}
"""

#: The SAME program with the author's qualifier removed. It is refused too,
#: and that is the point: item 521 slice 2 made `screen` a source class, so
#: the containment is revl's derivation rather than something the author has
#: to remember to write. Before slice 2 this program ADMITTED, which is the
#: measurement section 4 of the design doc was built on; the demo now asserts
#: the refusal for BOTH spellings, because a qualifier an author can remove to
#: turn the check off is not a containment.
REFUSE_SCREEN_TO_SINK_UNQUALIFIED = REFUSE_SCREEN_TO_SINK.replace(
    "-> Untrusted[Str]", "-> Str")

#: A confidential input may not be placed on an off-device model role
#: (item 512, G-MODEL-PLACE). Built from the agent so the refusal is about
#: the one clause that changed.
REFUSE_CONFIDENTIAL_OFF_DEVICE = LADDER.replace(
    "confidential -> triage", "confidential -> drafter")

#: The operator's authority floor. `ui.download` is the rung this composition
#: is not permitted, whatever its code says it does (item 33).
POLICY = "component LegacyAgent may not reach ui.download\n"
