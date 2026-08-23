"""revl runtime TCK — the published compatibility kit for the R1-R5 + A1-A8/G7
runtime contract. See tck/README.md."""

from .adapter import Observation, RuntimeAdapter
from .runner import Outcome, Report, run_suite
from .spec import CATALOG, Case, cases

__all__ = [
    "Observation", "RuntimeAdapter", "Outcome", "Report", "run_suite",
    "CATALOG", "Case", "cases",
]
