"""The `--once` residue proof, authenticated so program output cannot forge it
(issue #1621).

A `--once` runner (`run_go`, `run_rust`, `run_java`, `run_ts`, `run_wasm`)
boots a composition as a child process and reads the child's stdout for four
proof lines: `UP`, `NO-RESIDUE`, `RESIDUE-LEFT` and `DOWN`. Extern bodies run in
that child and write to the same stdout, so before this a program could print
`[run] NO-RESIDUE` itself, exit 0 before teardown, and be reported clean.

The fix is a per-run token the program has no way to learn:

* the runner draws `secrets.token_hex(16)`, sets `proofOnStdin` in the spec (a
  flag, not the token: the spec is a file the program could read), writes the
  token as the first line of the child's stdin, and closes stdin;
* the child's placement runner reads that line before it loads any component,
  so before any host code runs, and keeps it in a local. It prints its proof
  lines as `[<name>#<token>] UP` and so on;
* the runner accepts only token-tagged lines as proof and shows each one as
  `[<name>] ...`, exactly the line a person saw before. Every other line is
  program output, even one that spells `[run] NO-RESIDUE`.

Why stdin and not the alternatives: the spec file and argv are readable by the
program, and the environment is readable both by the program and, on macOS,
by `ps eww`. A second inherited file descriptor is writable by any program
that guesses its number. A token read off stdin before the first component
loads is consumed by the runtime and lives only in a local of the runner's
`main`. One channel every tier already has (all five runners open stdin as a
pipe and close it in once mode), and one line of reading in each.

What this does not stop: host code that reads the runner's own memory. That is
not output, and an in-process checker cannot defend against it.
"""

from __future__ import annotations

import secrets

#: The spec key that tells a placement runner to read the token off stdin.
SPEC_FLAG = "proofOnStdin"


class OnceProof:
    """One run's token and the proof lines verified against it."""

    def __init__(self, name: str = "run", token: str | None = None):
        self.name = name
        self.token = token or secrets.token_hex(16)
        self._tag = f"[{name}#{self.token}] "
        self.up = self.down = self.no_residue = self.residue_left = False

    def send(self, stdin) -> None:
        """Hand the token to the child as its first stdin line, then close
        stdin (once mode reads nothing else from it). A child that died before
        reading is not an error here: it will not print a tagged proof."""
        if stdin is None:
            return
        try:
            stdin.write(self.token + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def line(self, raw: str) -> str:
        """The line as a person should see it. A token-tagged line is the
        runtime's proof: it is recorded and shown as `[<name>] ...`. Any other
        line is shown as it came and recorded as nothing."""
        if not raw.startswith(self._tag):
            return raw
        rest = raw[len(self._tag):]
        word = rest.split(None, 1)[0] if rest.strip() else ""
        if word == "UP":
            self.up = True
        elif word == "DOWN":
            self.down = True
        elif word == "NO-RESIDUE":
            self.no_residue = True
        elif word == "RESIDUE-LEFT":
            self.residue_left = True
        return f"[{self.name}] {rest}"

    def verdict(self) -> dict:
        """The verified proof, for a caller that must not parse the text (the
        fault sweep)."""
        return {"up": self.up, "down": self.down,
                "noResidue": self.no_residue, "residueLeft": self.residue_left}

    def record(self, out: dict | None) -> None:
        """Copy the verdict into a caller's `proof` dict, when one was given."""
        if out is not None:
            out.update(self.verdict())
