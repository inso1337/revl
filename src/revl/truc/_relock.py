"""Regenerate truc's own stage-0 lock, `src/revl/truc/truc.lock`.

`python -P -m revl.truc._relock` rewrites the per-file `sha256` pins from the
component sources on disk and leaves the file LIST alone — which components
make up truc is a decision recorded in the lock, never something discovered by
globbing `components/`. The `-P` is the PYTHONSAFEPATH safety bit; without
it, `-m` puts the CWD at `sys.path[0]` and a sibling `truc.py` would shadow
truc's own launcher (issue #317). Run the relock after an intended edit to a
`src/revl/truc/components/*.rvl` and commit the result; the regenerate-or-red
test in `tests/test_truc_bootstrap.py` is what turns CI red if you do not.
"""

from __future__ import annotations

import json
import sys

from ._launcher import _HERE, lock_document


def main(argv: list[str] | None = None) -> int:
    del argv
    path = _HERE / "truc.lock"
    text = json.dumps(lock_document(), indent=2) + "\n"
    before = path.read_text(encoding="utf-8")
    if before == text:
        print(f"truc.lock is already current: {path}")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"rewrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover — console entry
    raise SystemExit(main(sys.argv[1:]))
