"""`truc <verb>` (the documented happy path) — `python -P -m revl.truc`
(the `-P` is the PYTHONSAFEPATH safety bit, issue #317) is the
absolute-interpreter fallback, kept in lockstep with the console script
because both spellings are real entry points for a `truc` invocation.
"""
# issue #317: see revl._safepath — `-m` puts the working directory at
# sys.path[0], which the console script this mirrors does not.
from .._safepath import drop_cwd_entry

drop_cwd_entry()

from ._launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
