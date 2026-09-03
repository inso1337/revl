"""`python -m revl.truc` — the launcher, for parity with the console script."""
# issue #317: see revl._safepath — `-m` puts the working directory at
# sys.path[0], which the console script this mirrors does not.
from .._safepath import drop_cwd_entry

drop_cwd_entry()

from ._launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
