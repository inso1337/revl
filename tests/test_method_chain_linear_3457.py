"""A long operator chain inside a provide method (issue #3457).

#3400 made the expression path linear (`infer_ast` through `_lower_pure_expr`).
A provide-method body takes three other passes over the same subtree:

* `emission_analysis._method_emissions` called `_calls_in` on the whole
  subtree at every node it walked;
* `lower._crosses_computer_use` asks `_method_emissions` again, so it paid the
  same square;
* the component-stratum operator arms of `lower._lower_component_pure_expr`
  asked `typecheck.infer_ir` for each operand's type after lowering it, and
  each answer walked the operand's subtree.

The counts are pinned, not the time: the work at 2n operands is at most about
twice the work at n.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
import revl.emission_analysis as emission_analysis  # noqa: E402
import revl.lower as lower  # noqa: E402
import revl.typecheck as typecheck  # noqa: E402


def _method_chain(n: int) -> str:
    return ("service Counter {\n  fn next() -> Int\n}\n\n"
            "component C provides c: Counter {\n  provide c {\n"
            "    fn next() = " + "1 + " * n + "1\n  }\n}\n")


def _counts(monkeypatch, src: str) -> dict:
    counts = {"calls_in": 0, "calls_in_ui": 0, "infer_ir": 0}
    inside_ui = [False]
    calls_in = emission_analysis._calls_in
    infer_ir = typecheck.infer_ir
    crosses = lower._crosses_computer_use

    def counting_calls_in(*args, **kwargs):
        counts["calls_in"] += 1
        if inside_ui[0]:
            counts["calls_in_ui"] += 1
        return calls_in(*args, **kwargs)

    def counting_infer_ir(*args, **kwargs):
        counts["infer_ir"] += 1
        return infer_ir(*args, **kwargs)

    def counting_crosses(*args, **kwargs):
        inside_ui[0] = True
        try:
            return crosses(*args, **kwargs)
        finally:
            inside_ui[0] = False

    # both functions recurse through their module global, and lower.py holds
    # its own imported `infer_ir`: patch every name so each call is counted
    # (undone on return, so a second count does not wrap the first)
    with monkeypatch.context() as patch:
        patch.setattr(emission_analysis, "_calls_in", counting_calls_in)
        patch.setattr(typecheck, "infer_ir", counting_infer_ir)
        patch.setattr(lower, "infer_ir", counting_infer_ir)
        patch.setattr(lower, "_crosses_computer_use", counting_crosses)
        compile_source(src, "g.rvl")
    return counts


def test_provide_method_chain_costs_linear_work(monkeypatch):
    n = 150
    small = _counts(monkeypatch, _method_chain(n))
    large = _counts(monkeypatch, _method_chain(2 * n))
    # base: 150 `+` cost 162,466 `_calls_in` (81,233 under the computer-use
    # question) and 23,553 `infer_ir`; 300 cost 639,916, 319,958 and 92,103
    assert small["calls_in_ui"] > 0, small  # the counter reached the pass
    for key in small:
        assert large[key] <= 2.2 * small[key], (key, small, large)
