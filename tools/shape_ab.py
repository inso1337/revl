#!/usr/bin/env python3
"""A/B of the token-shaped MCP answers over the committed corpus (issue #2847).

The token economy's house rule is "measured, not assumed" (docs/token-economy.md).
Three answer shapes are already in the tree, each justified so far by a
structural argument, not by a number:

  * a compound verb: `revl_ship` runs check -> admit -> plan in one call;
  * a content handle: `revl_fmt {handle: true}` names the formatted text and
    `revl_check {artifactId}` checks it, so the text is not echoed and not
    re-sent.

(The third candidate, a diff-shaped answer for a swap or an edit, needs a live
runtime to measure at all, so it is not in this table.)

This tool replays the same briefs through the current shape (arm A) and the
proposed shape (arm B) and reports the delta. A brief is one admitted component
of the corpus `bench/tokens.py` already measures (the attempt that compiled, one
per cell of the committed `typed-` and `baseline-` runs). "Admitted" there means
it compiled when the runs were scored: the admission gate of today may refuse
it, and the table reports those briefs as their own bucket. For every brief and
arm it records, from the real dispatcher (`revl.mcp.server.handle`, in process,
no model, no transport, no runtime):

  * **turns** - tool calls the agent makes (each one is a model turn);
  * **emitted tokens** - the arguments of those calls, the tokens the model has
    to generate, through `bench/tokens.py`'s proxy tokenizer;
  * **received tokens** - the answers, the tokens that enter the context and are
    paid again on every later turn, through the same tokenizer.

The two arms must reach the same verdict for a brief, with the same first
diagnostic when they stop; a brief where they differ is counted and fails the
guard test, so a saving cannot be bought by changing the answer.

Experiments:

  compound   check, admit, plan against a fixed running composition (three
             separate verbs, stopping at the first one that fails) versus one
             `revl_ship` call. Both arms send the running manifest, so the table
             is a lower bound on the saving: the session-held default, which
             spares the manifest, needs a live load and is not measured here.
             `manifest tokens` below gives the size of what it would remove.
  handle     `revl_fmt` then `revl_check` of the formatted text, with the text
             sent back to check (A) or named by its `artifactId` (B).

Usage:
  python3 tools/shape_ab.py            # print the table
  python3 tools/shape_ab.py --write    # rewrite bench/results/shape-ab.md
  python3 tools/shape_ab.py --compare  # reproduce it; exit 1 on any difference
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _import_root in (ROOT / "src", ROOT / "bench"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

import rescore  # noqa: E402
import tokens as token_proxy  # noqa: E402  (bench/tokens.py: the one tokenizer)
from revl.mcp import identity, repeat, server  # noqa: E402
from revl.mcp.server import Session, handle  # noqa: E402

DEFAULT_DOC = ROOT / "bench" / "results" / "shape-ab.md"

#: The committed runs the corpus is read from (the ones tokens-to-green scores).
RUNS = tuple(rescore.MODEL_RUNS)

#: The running composition the compound experiment admits against. A component
#: of its own, so a brief never collides with it by name.
RUNNING = """\
service Anchor { fn ping() -> Int }
component Anchored provides anchor: Anchor {
  provide anchor { fn ping() = 1 }
}
"""

#: The authoring profile every replay runs under: the server's default (closed:
#: no agent-authored host code). The refusals in the table are its refusals.
def _default_authoring():
    return server.AuthoringTrust()


PINNED_REVISION = "0" * 40
PINNED_SOURCE_DIGEST = "0" * 64
_MISSING = object()

EXPERIMENTS = ("compound", "handle")

#: Below this many source characters a handle costs more than the text it
#: replaces; the per-brief guard for the handle experiment starts here.
MIN_HANDLE_SOURCE_CHARS = 64


@dataclass(frozen=True)
class Brief:
    """One admitted component of the committed corpus."""

    id: str
    source: str


@dataclass
class Arm:
    """What one arm spent on one brief."""

    turns: int = 0
    emitted: int = 0
    received: int = 0
    verdict: str = ""
    extra: dict = field(default_factory=dict)


# ------------------------------------------------------------------ corpus


def corpus() -> list[Brief]:
    """The admitted component of every cell of the committed runs, in order."""
    briefs = []
    for run in RUNS:
        for cell in token_proxy.compute_run(run, token_proxy.rescore.ROOT):
            if not cell["admitted"]:
                continue
            path = (rescore.RESULTS / run / cell["spec"] / cell["variant"]
                    / f"attempt-{cell['green_at']}.rvl")
            briefs.append(Brief(f"{run}/{cell['spec']}/{cell['variant']}",
                                path.read_text(encoding="utf-8")))
    return briefs


# ------------------------------------------------------------------ replay


class _Replay:
    """One fresh, pinned session and a meter for the calls made through it."""

    def __init__(self) -> None:
        server.SESSION = Session()
        repeat.forget()
        server.UNDO_STACK.clear()
        self.arm = Arm()

    def stop(self, verdict: str, structured: dict) -> Arm:
        """End the replay with `verdict`, recording the first diagnostic code."""
        diagnostics = structured.get("diagnostics") or [{}]
        self.arm.verdict = verdict
        self.arm.extra["code"] = str(diagnostics[0].get("code"))
        return self.arm

    def call(self, verb: str, arguments: dict) -> dict:
        response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": verb, "arguments": arguments}})
        result = response["result"]
        text = result["content"][0]["text"]
        self.arm.turns += 1
        self.arm.emitted += token_proxy.count_tokens(json.dumps(arguments))
        self.arm.received += token_proxy.count_tokens(text)
        return result["structuredContent"]


def _compound_a(brief: Brief, manifest: dict) -> Arm:
    r = _Replay()
    checked = r.call("revl_check", {"source": brief.source})
    if not checked.get("ok") or checked.get("holes"):
        return r.stop("stopped:check", checked)
    args = {"source": brief.source, "manifest": manifest}
    admitted = r.call("revl_admit", args)
    if not (admitted.get("ok") and admitted.get("admitted")):
        return r.stop("stopped:admit", admitted)
    planned = r.call("revl_plan", args)
    # the predicate `ship.py` uses: a plan payload may omit `ok` on success
    if planned.get("ok", True) is False:
        return r.stop("stopped:plan", planned)
    r.arm.verdict = "planned"
    return r.arm


def _compound_b(brief: Brief, manifest: dict) -> Arm:
    r = _Replay()
    shipped = r.call("revl_ship", {"source": brief.source, "manifest": manifest})
    if shipped.get("ok"):
        r.arm.verdict = "planned"
        return r.arm
    # A refusal the server raises before `revl_ship` runs carries no
    # `stoppedAt`. It is labelled `stopped:gate`, never as a stage, and the
    # parity check below accepts it as arm A's `stopped:check` only when it
    # carries the same first diagnostic.
    return r.stop(f"stopped:{shipped.get('stoppedAt') or 'gate'}", shipped)


def _formatted(structured: dict) -> str | None:
    text = structured.get("formatted")
    return text if isinstance(text, str) else None


def _handle_a(brief: Brief) -> Arm:
    r = _Replay()
    formatted = r.call("revl_fmt", {"source": brief.source})
    text = _formatted(formatted)
    if not formatted.get("ok") or text is None:
        return r.stop("stopped:fmt", formatted)
    checked = r.call("revl_check", {"source": text})
    if not checked.get("ok"):
        return r.stop("checked:refused", checked)
    r.arm.verdict = "checked:ok"
    return r.arm


def _handle_b(brief: Brief) -> Arm:
    r = _Replay()
    formatted = r.call("revl_fmt", {"source": brief.source, "handle": True})
    artifact = formatted.get("artifactId")
    if not formatted.get("ok") or not isinstance(artifact, str):
        return r.stop("stopped:fmt", formatted)
    checked = r.call("revl_check", {"artifactId": artifact})
    if not checked.get("ok"):
        return r.stop("checked:refused", checked)
    r.arm.verdict = "checked:ok"
    return r.arm


# ------------------------------------------------------------------ measuring


@dataclass(frozen=True)
class Result:
    """One experiment: per-brief arms for A and B, in corpus order."""

    name: str
    a: tuple
    b: tuple
    manifest_tokens: int = 0


def measure(briefs: list[Brief]) -> list[Result]:
    """Replay every brief through both arms of every experiment."""
    from revl import compile_source  # noqa: PLC0415

    root = identity.package_root()
    previous_session = server.SESSION
    previous_identity = identity._CACHE.get(root, _MISSING)
    previous_repeat = dict(repeat._LAST)
    previous_undo = list(server.UNDO_STACK.entries)
    previous_authoring = server.AUTHORING
    server.AUTHORING = _default_authoring()
    identity._CACHE[root] = {"revision": PINNED_REVISION,
                             "source_digest": PINNED_SOURCE_DIGEST}
    try:
        manifest = compile_source(RUNNING)
        manifest_tokens = token_proxy.count_tokens(json.dumps(manifest))
        compound_a = tuple(_compound_a(b, manifest) for b in briefs)
        compound_b = tuple(_compound_b(b, manifest) for b in briefs)
        handle_a = tuple(_handle_a(b) for b in briefs)
        handle_b = tuple(_handle_b(b) for b in briefs)
        return [Result("compound", compound_a, compound_b, manifest_tokens),
                Result("handle", handle_a, handle_b)]
    finally:
        server.SESSION = previous_session
        server.AUTHORING = previous_authoring
        server.UNDO_STACK.entries[:] = previous_undo
        if previous_identity is _MISSING:
            identity._CACHE.pop(root, None)
        else:
            identity._CACHE[root] = previous_identity
        with repeat._LOCK:
            repeat._LAST.clear()
            repeat._LAST.update(previous_repeat)


def _parity_key(arm: Arm) -> tuple:
    """What two arms must agree on: the outcome and, when they stopped, the
    first diagnostic. A refusal raised before `revl_ship` ran (`stopped:gate`)
    is the same outcome as arm A's `stopped:check` when the diagnostic matches."""
    verdict = "stopped:check" if arm.verdict == "stopped:gate" else arm.verdict
    return verdict, arm.extra.get("code")


def verdict_mismatches(result: Result) -> list[int]:
    """Indices of briefs where the two arms did not reach the same outcome."""
    return [i for i, (a, b) in enumerate(zip(result.a, result.b))
            if _parity_key(a) != _parity_key(b)]


#: The verdicts that mean the exchange ran to its end.
_THROUGH = ("planned", "checked:ok")


def through(arm: Arm) -> bool:
    return arm.verdict in _THROUGH


def split(result: Result, want_through: bool) -> Result:
    """The briefs of `result` whose exchange ran to its end (or did not)."""
    keep = [i for i, b in enumerate(result.b) if through(b) == want_through]
    return Result(result.name, tuple(result.a[i] for i in keep),
                  tuple(result.b[i] for i in keep), result.manifest_tokens)


def _sum(arms, key: str) -> int:
    return sum(getattr(a, key) for a in arms)


def _median(arms, key: str) -> float:
    return statistics.median(getattr(a, key) for a in arms)


def _pct(a: float, b: float) -> str:
    if a == 0:
        return "n/a"
    return f"{100.0 * (b - a) / a:+.1f}%"


# ------------------------------------------------------------------ rendering


def _delta_table(result: Result) -> list[str]:
    lines = ["| metric | A total | B total | delta | A median | B median |",
             "|---|---:|---:|---:|---:|---:|"]
    for label, key in (("turns", "turns"), ("emitted tokens", "emitted"),
                       ("received tokens", "received")):
        a, b = _sum(result.a, key), _sum(result.b, key)
        lines.append(f"| {label} | {a} | {b} | {_pct(a, b)} | "
                     f"{_median(result.a, key):g} | {_median(result.b, key):g} |")
    a = _sum(result.a, "emitted") + _sum(result.a, "received")
    b = _sum(result.b, "emitted") + _sum(result.b, "received")
    lines.append(f"| **emitted + received** | **{a}** | **{b}** | "
                 f"**{_pct(a, b)}** | | |")
    return lines


def _verdict_split(result: Result) -> str:
    counts: dict[str, int] = {}
    for arm in result.b:
        counts[arm.verdict] = counts.get(arm.verdict, 0) + 1
    return ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))


def _experiment(result: Result, ran: str, stopped: str) -> list[str]:
    """The whole table, then the briefs that ran to the end and those that did
    not, so a saving on one is not carried by the other."""
    done, refused = split(result, True), split(result, False)
    briefs = len(result.a)
    mismatched = len(verdict_mismatches(result))
    dearer = sum(1 for a, b in zip(result.a, result.b)
                 if b.emitted > a.emitted or b.received > a.received)
    lines = [
        *_delta_table(result),
        "",
        f"Over all {briefs} briefs: {_verdict_split(result)}; "
        f"{mismatched} where the arms disagree on the outcome. B emits or "
        f"receives more than A on {dearer} of them.",
        "",
        f"### {ran} ({len(done.a)} briefs)",
        "",
        *(_delta_table(done) if done.a else ["none"]),
        "",
        f"### {stopped} ({len(refused.a)} briefs)",
        "",
        *(_delta_table(refused) if refused.a else ["none"]),
        "",
    ]
    return lines


def render(results: list[Result]) -> str:
    """The committed markdown, byte-for-byte reproducible."""
    by_name = {r.name: r for r in results}
    briefs = len(by_name["compound"].a)
    compound, handle = by_name["compound"], by_name["handle"]
    lines = [
        "# MCP answer shapes, measured A/B",
        "",
        "The same briefs through the current shape (A) and the token-shaped one",
        "(B), so a shape is kept on a number and not on a claim (#2847). Produced",
        "by `tools/shape_ab.py` from the answers a client actually receives, and",
        "reproduced in CI by `tests/test_shape_ab_2847.py`.",
        "",
        f"**Briefs:** {briefs}, the compiled component of every cell of the",
        "committed runs `bench/tokens.py` scores (the admitted components of",
        "`bench/results/tokens-to-green.md`). \"Admitted\" there means it compiled",
        "when the run was scored. Replayed today, under the server's default",
        "authoring profile, the admission gate refuses some of them (G8: the",
        "capability ceiling against the admission kernel, and host blocks under",
        "the untrusted profile). Those briefs are their own bucket below, because",
        "they spend differently from the ones that run to the end.",
        "",
        "Tokens are `bench/tokens.py`'s `count_tokens`. *Emitted* is the arguments",
        "of the calls (what the model generates), *received* is the answers (what",
        "enters the context and is paid again on every later turn), *turns* is the",
        "number of calls.",
        "",
        "## compound: check, admit, plan versus `revl_ship`",
        "",
        "A: `revl_check`, then `revl_admit`, then `revl_plan`, stopping at the",
        "first that fails. B: one `revl_ship`. Both send the running manifest",
        f"({compound.manifest_tokens} emitted tokens each time it is sent): B once per",
        "brief, A up to twice. A brief refused at check costs A one call with no",
        "manifest and B one call that carries it, so B emits more there. A",
        "session-held manifest default would spare that; it needs a live load and",
        "is not measured here.",
        "",
        *_experiment(compound, "ran to the end (planned)",
                     "refused before planning"),
        "## handle: `revl_fmt` then `revl_check`",
        "",
        "This exchange is constructed: format a candidate, then check the",
        "formatted text. A: `revl_fmt` returns the formatted text and",
        "`revl_check` is sent that text back. B: `revl_fmt {handle: true}` returns",
        "an `artifactId` and `revl_check {artifactId}` checks it. Both are two",
        "turns. A handle has a fixed cost (the field and the extra argument), so",
        "it does not pay on a text of a few tokens.",
        "",
        *_experiment(handle, "checked ok", "refused or not formattable"),
        "## Reading it",
        "",
        "- In total, B must not spend more than A on turns, emitted tokens or",
        "  received tokens, and on every compound brief that runs to the end, and",
        "  every handle brief whose text is at least "
        f"{MIN_HANDLE_SOURCE_CHARS} characters, B must not",
        "  spend more than A on any of them. Both arms must reach the same",
        "  outcome, with the same first diagnostic, on every brief.",
        "  `tests/test_shape_ab_2847.py` fails otherwise, and fails when this",
        "  file no longer reproduces, so an answer that grows shows up as a diff.",
        "- Not measured: a diff-shaped answer for `revl_swap` and `revl_edit`",
        "  (it needs a live runtime), the session-held manifest default, and a",
        "  real model's tokenizer (the proxy ranks and tracks, it is not a bill).",
        "",
        "## Re-run",
        "",
        "```",
        "python3 tools/shape_ab.py            # print the table",
        "python3 tools/shape_ab.py --write    # rewrite this file",
        "python3 tools/shape_ab.py --compare  # reproduce it; exit 1 on drift",
        "```",
        "",
    ]
    return "\n".join(lines)


def compare(results: list[Result], path: Path) -> int:
    """0 when `path` is byte-identical to a fresh render, 1 when it is not."""
    rendered = render(results)
    try:
        committed = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"shape A/B: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if rendered == committed:
        print(f"shape A/B: reproduced {path}")
        return 0
    print(f"shape A/B: {path} does not match a fresh run", file=sys.stderr)
    expected = rendered.splitlines()
    actual = committed.splitlines()
    for line_no in range(max(len(expected), len(actual))):
        want = expected[line_no] if line_no < len(expected) else "<missing>"
        got = actual[line_no] if line_no < len(actual) else "<missing>"
        if want != got:
            print(f"  first difference at line {line_no + 1}:", file=sys.stderr)
            print(f"  committed: {got}", file=sys.stderr)
            print(f"  fresh:     {want}", file=sys.stderr)
            break
    print("  run `python3 tools/shape_ab.py --write` and review the diff",
          file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                      help=f"rewrite {DEFAULT_DOC.relative_to(ROOT)}")
    mode.add_argument("--compare", nargs="?", const=str(DEFAULT_DOC),
                      metavar="PATH",
                      help="reproduce the committed table; exit 1 on difference")
    args = ap.parse_args(argv)

    results = measure(corpus())
    print(render(results), end="")
    if args.write:
        DEFAULT_DOC.write_text(render(results), encoding="utf-8")
        print(f"\nshape A/B: wrote {DEFAULT_DOC.relative_to(ROOT)}")
        return 0
    if args.compare is not None:
        return compare(results, Path(args.compare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
