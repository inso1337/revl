"""`python -m revl.truc` — the launcher, for parity with the console script."""
from ._launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
