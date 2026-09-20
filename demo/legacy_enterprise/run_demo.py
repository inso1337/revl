#!/usr/bin/env python
"""The flagship integrated demo: one legacy-enterprise agent, nine checks.

Roadmap item 525 (issue #1200), from the 2026-09-19 external review. The
review's closing recommendation is a priority rather than a feature: every
primitive this project argues for is individually demonstrable and nothing
demonstrates them together, so the thesis has to be assembled by a reader out
of separate documents. This is the assembly.

The scenario is the review's own: a billing desktop whose refund path has no
clean typed service, so the honest options are a typed API, a peer service,
and - when neither answers - a window somebody clicks. One composition
declares all three rungs, and each of the nine steps below crosses a different
guarantee and prints the artifact that shows it crossing.

    1  the boundary is enumerable          `revl audit`          G8
    2  descending the ladder WIDENS it     `revl audit --diff`   G8 / item 21
    3  the GUI surface is not a capability  refusal              G8 / item 521
    4  the verb set is closed               refusal              G8 / item 521
    5  screen content cannot become a command   refusal          G9 / item 249
    6  an irreversible step may not claim an inverse  refusals   G4 / item 522
    7  a confidential input stays on the device      refusal     item 512
    8  the operator's floor refuses a rung  `revl audit --policy` item 33
    9  the recovery status is honest        `revl erase-report`   G4 / item 546
    +  and the admitted whole is signed     `revl attest`         item 127

Run it:

    python demo/legacy_enterprise/run_demo.py
    python demo/legacy_enterprise/run_demo.py --verbose   # print every artifact

It needs the compiler and nothing else. There is no runtime, no desktop and no
network in this demo, which is the honest shape rather than a limitation
worked around: the computer-use substrate is a host obligation filed upstream
(roadmap item 539, inso1337/revl-harness#11), so every `@py` body in
`programs.py` returns a constant and the demo never claims to have driven a
window. `docs/design/551-flagship-demo.md` section 5 is the list of what this
demo therefore does NOT show, with the item that owns each one.

Every program is written into a throwaway temp dir; the checkout is untouched
and a second run passes as cleanly as the first. Exits nonzero on the first
failed check.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

sys.path.insert(0, str(HERE))
import programs  # noqa: E402

VERBOSE = False
_CHECKS = 0


# --------------------------------------------------------------- the plumbing

def _revl(args: list[str], cwd: Path, timeout: int = 180):
    """Drive the real CLI a reader would type.

    `-P` is PYTHONSAFEPATH: the `revl` console script is the documented happy
    path and this is the window-free `-m` form (issue #336,
    `tests/test_336_no_bare_python_m_revl.py`). `src/` goes on PYTHONPATH so
    the demo runs from a checkout with nothing installed.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.setdefault("REVL_ATTEST_KEY", "flagship-demo-key-not-a-secret")
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-P", "-m", "revl", *args],
        cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=timeout)


def _step(number: str, title: str, guarantee: str) -> None:
    print(f"\n[{number}] {title}")
    print(f"     guarantee: {guarantee}")


def _artifact(label: str, text: str, keep: int = 14) -> None:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    shown = lines if VERBOSE else lines[:keep]
    print(f"     --- {label} ---")
    for line in shown:
        print(f"     | {line if VERBOSE else line[:150]}")
    if len(shown) < len(lines):
        print(f"     | ... {len(lines) - len(shown)} more line(s); --verbose prints them")


def _check(condition: bool, what: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        print(f"\nFAILED CHECK: {what}")
        raise SystemExit(1)
    print(f"     ok: {what}")


def _refuses(name: str, filename: str, cwd: Path) -> str:
    """Compile a program that must NOT admit, and return the diagnostic."""
    proc = _revl(["compile", filename], cwd)
    _check(proc.returncode != 0, f"`revl compile` refuses {name}")
    return proc.stdout


# -------------------------------------------------------------- the nine steps

def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true",
                    help="print every artifact in full rather than its head")
    VERBOSE = ap.parse_args().verbose

    work = Path(tempfile.mkdtemp(prefix="revl-flagship-"))
    write = {
        "ladder.rvl": programs.LADDER,
        "typed_only.rvl": programs.TYPED_ONLY,
        "ui_root.rvl": programs.REFUSE_UI_ROOT,
        "unknown_verb.rvl": programs.REFUSE_UNKNOWN_VERB,
        "download_compensate.rvl": programs.REFUSE_DOWNLOAD_COMPENSATE,
        "click_compensate.rvl": programs.REFUSE_CLICK_COMPENSATE,
        "text_no_compensate.rvl": programs.REFUSE_TEXT_WITHOUT_COMPENSATE,
        "screen_to_sink.rvl": programs.REFUSE_SCREEN_TO_SINK,
        "screen_unqualified.rvl": programs.ADMITS_SCREEN_TO_SINK_UNQUALIFIED,
        "confidential_off_device.rvl": programs.REFUSE_CONFIDENTIAL_OFF_DEVICE,
        "floor.rvlpolicy": programs.POLICY,
    }
    for name, text in write.items():
        (work / name).write_text(text)

    print("THE FLAGSHIP DEMO - a legacy-enterprise agent, end to end")
    print("roadmap item 525, issue #1200, docs/design/551-flagship-demo.md")
    print(f"work dir: {work}")

    # ---------------------------------------------------------------- step 1
    _step("1", "the whole boundary is enumerable, one rung at a time",
          "G8 - a component's reach is declared, and `revl audit` prints it")
    proc = _revl(["audit", "ladder.rvl"], work)
    _check(proc.returncode == 0, "the three-rung agent ADMITS")
    surface = proc.stdout
    _artifact("revl audit ladder.rvl", surface, keep=20)
    for token in ("db.invoice", "net.ledger", "screen.observe",
                  "ui.find", "ui.text", "ui.click", "ui.download"):
        _check(token in surface, f"the audit names the crossing `{token}`")

    # The ORDER of the ladder is not on this surface, and saying so is part of
    # the demo. A route condition can check a plan's reach; what walks a ladder
    # is an agent loop, and the loop is not this repository's (item 539).
    print("     note: the audit enumerates the rungs. It does not order them -")
    print("           the descent order belongs to the loop (item 539), not here.")

    # ---------------------------------------------------------------- step 2
    _step("2", "descending the ladder WIDENS the boundary, and the gate says so",
          "G8 / item 21 - authority drift is a refusal, not a diff to read later")
    prev = work / "typed_only.audit.json"
    proc = _revl(["audit", "typed_only.rvl", "--json"], work)
    _check(proc.returncode == 0, "the typed-API-only agent ADMITS")
    prev.write_text(proc.stdout)
    proc = _revl(["audit", "ladder.rvl", "--diff", "typed_only.audit.json"], work)
    _check(proc.returncode != 0, "`revl audit --diff` FAILS on the widened reach")
    _artifact("revl audit ladder.rvl --diff typed_only.audit.json", proc.stdout)
    for crossing in ("host:LegacyAgent:actuate", "host:LegacyAgent:fetch_receipt",
                     "emit:LegacyAgent:peer.adjust"):
        _check(crossing in proc.stdout, f"the drift gate names `{crossing}`")

    # ---------------------------------------------------------------- step 3
    _step("3", "the GUI surface is not a capability anyone can hold",
          "G8 / item 521 - an act-on-anything verb is refused, not admitted as `*`")
    out = _refuses("`emission[ui]`", "ui_root.rvl", work)
    _artifact("revl compile ui_root.rvl", out)
    _check("not an enumerable boundary" in out,
           "the refusal says WHY: the root is not enumerable")

    # ---------------------------------------------------------------- step 4
    _step("4", "the computer-use verb set is closed",
          "G8 / item 521 - an invented verb escapes every policy written "
          "against the real one")
    out = _refuses("`emission[ui.drag]`", "unknown_verb.rvl", work)
    _artifact("revl compile unknown_verb.rvl", out)
    _check("ui.click" in out and "ui.download" in out,
           "the refusal ENUMERATES the admissible verbs rather than describing them")

    # ---------------------------------------------------------------- step 5
    _step("5", "screen content is text, and text does not mint a capability",
          "G9 / item 249 - the review's own framing case")
    out = _refuses("screen content reaching a shell sink",
                   work / "screen_to_sink.rvl", work)
    _artifact("revl compile screen_to_sink.rvl", out)
    _check("screen.observe" in out,
           "the refusal names the ORIGIN of the untrusted value")
    _check("endorse" in out or "verified fn" in out,
           "the refusal names the declared declassification points")

    # The honest half of this step. The qualifier above is the AUTHOR's, and
    # removing it admits the same program: `screen` is not yet a source class.
    proc = _revl(["compile", "screen_unqualified.rvl"], work)
    _check(proc.returncode == 0,
           "MEASURED GAP: the same program with `Untrusted[Str]` dropped ADMITS")
    print("     gap: on this tree the untrustedness of a screen read is")
    print("          AUTHOR-DECLARED, not derived from `screen.observe`.")
    print("          Deriving it is item 521 slice 2 (docs/design/532 section 5).")

    # ---------------------------------------------------------------- step 6
    _step("6", "an irreversible step may not claim an inverse",
          "G4 / item 522 - a transaction may not claim cleanliness it cannot have")
    out = _refuses("`ui.download` + `compensate`",
                   work / "download_compensate.rvl", work)
    _artifact("revl compile download_compensate.rvl", out)
    _check("is irreversible" in out, "the refusal states the verb's CLASS")
    _check("uncompensated" in out,
           "the refusal names what the transaction must report instead")

    out = _refuses("`ui.click` + `compensate`", "click_compensate.rvl", work)
    _check("is unknown" in out,
           "`ui.click` is `unknown`, and unknown is treated exactly as irreversible")

    out = _refuses("`ui.text` with no `compensate`",
                   work / "text_no_compensate.rvl", work)
    _check("must declare `compensate`" in out,
           "the compensatable verb's missing inverse is refused, not defaulted")

    # ---------------------------------------------------------------- step 7
    _step("7", "a confidential input does not leave the device",
          "item 512 / G-MODEL-PLACE - the placement is checked, not requested")
    out = _refuses("`confidential -> drafter` (an off-device role)",
                   work / "confidential_off_device.rvl", work)
    _artifact("revl compile confidential_off_device.rvl", out)
    for token in ("settle", "confidential", "drafter", "off_device"):
        _check(token in out, f"the refusal names `{token}`")

    # ---------------------------------------------------------------- step 8
    _step("8", "the operator's floor refuses a rung, whatever the code says",
          "item 33 - authority, evaluated over the same audit graph")
    proc = _revl(["audit", "ladder.rvl", "--policy", "floor.rvlpolicy"], work)
    _check(proc.returncode != 0, "the boundary policy REFUSES admission")
    _artifact("revl audit ladder.rvl --policy floor.rvlpolicy", proc.stdout)
    _check("fetch_receipt" in proc.stdout,
           "the why-trace names the host extern that reaches the forbidden rung")

    # ---------------------------------------------------------------- step 9
    _step("9", "the recovery status is honest about what was not undone",
          "G4 / item 546 - a revert RESTORES some layers and COMPENSATES others")
    proc = _revl(["erase-report", "ladder.rvl", "--realm", "billing",
                  "--no-residue-proof"], work)
    _check(proc.returncode == 0, "`revl erase-report` renders the realm")
    report = proc.stdout
    # keep must clear the crossings block AND item 522's residue split, which is
    # what this step is pointing at. The artifact is 61 lines, so `--verbose`
    # still has a job. It is 40 rather than 31 because the DOES NOT PROVE block
    # above it grows one line per residue state; the test below fails if a later
    # change pushes the split back out of the default render.
    _artifact("revl erase-report ladder.rvl --realm billing", report, keep=40)
    _check("[UNCOMPENSATED] LegacyAgent  host fetch_receipt()" in report,
           "the `ui.download` crossing reports UNCOMPENSATED - no inverse exists")
    _check("[UNCOMPENSATED] LegacyAgent  host actuate()" in report,
           "the `ui.click` crossing reports UNCOMPENSATED too, because unknown is not clean")
    _check("[restored]     LegacyAgent  host type_amount()" in report,
           "the `ui.text` crossing reports restored - its inverse puts the field back")
    _check("Compensation is not inversion" in report,
           "the report itself refuses to call a compensation a restore")
    print("     read it in item 546's vocabulary: `type_amount` was COMPENSATED,")
    print("     which is not restored; `actuate` and `fetch_receipt` were neither.")
    print("     Nothing here may be summarised as `it rolled back cleanly`.")

    # --------------------------------------------------------------- step 10
    _step("+", "the admitted whole is signed, and the signature is checkable",
          "item 127 - an attestation over this exact composition")
    att = work / "ladder.attestation.json"
    proc = _revl(["attest", "ladder.rvl", "--json"], work)
    _check(proc.returncode == 0, "`revl attest` signs the admitted composition")
    att.write_text(proc.stdout)
    record = json.loads(proc.stdout)
    _check(record["verdict"] == "admitted",
           f"the record carries the verdict and the hash "
           f"({record['composition_hash'][:16]}...)")
    proc = _revl(["attest", "ladder.attestation.json", "--verify",
                  "--against", "ladder.rvl"], work)
    _check(proc.returncode == 0, "`revl attest --verify --against` accepts it")
    _artifact("revl attest --verify --against ladder.rvl", proc.stdout)

    tampered = work / "tampered.rvl"
    tampered.write_text(programs.LADDER.replace('realm("billing")',
                                                'realm("payments")'))
    proc = _revl(["attest", "ladder.attestation.json", "--verify",
                  "--against", "tampered.rvl"], work)
    _check(proc.returncode != 0,
           "and REJECTS the same attestation against a changed composition")

    print(f"\nflagship demo OK - {_CHECKS} checks, 0 failures")
    print("what this demo does NOT show is listed in "
          "docs/design/551-flagship-demo.md section 5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
