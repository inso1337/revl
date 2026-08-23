"""CLI: run the TCK against a runtime adapter and print the conformance report.

    backends/python/.venv/bin/python -m tck.conformance --adapter py
    backends/python/.venv/bin/python -m tck.conformance --adapter py --json

Exit status is 0 when the run is OK (every case passed, is a pinned divergence,
or is honestly pending) and 1 when any case failed — including a pinned
divergence that started meeting the ideal, which fails on purpose so the report
changes only deliberately.
"""

from __future__ import annotations

import argparse
import importlib
import sys

from .report import to_json_str, to_text
from .runner import run_suite

# short name -> "module:factory" building a RuntimeAdapter
_ADAPTERS = {
    "py": "tck.adapters.py_adapter:build",
}


def _load_adapter(spec: str):
    if spec in _ADAPTERS:
        spec = _ADAPTERS[spec]
    module_name, _, factory = spec.partition(":")
    if not factory:
        factory = "build"
    module = importlib.import_module(module_name)
    return getattr(module, factory)()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="revl runtime TCK conformance runner")
    parser.add_argument(
        "--adapter", default="py",
        help="adapter to drive: a registered short name ('py') or "
             "'module:factory' for a candidate runtime's own adapter")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        adapter = _load_adapter(args.adapter)
    except Exception as exc:  # a runtime that will not even import is a hard stop
        print(f"could not load adapter {args.adapter!r}: {exc!r}", file=sys.stderr)
        return 2

    report = run_suite(adapter)
    print(to_json_str(report) if args.json else to_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
