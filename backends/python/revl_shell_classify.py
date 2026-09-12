"""Pure shell-to-witnessed classifier for `stdlib/shell.rvl` (roadmap item 252).

The terminal tool is the workload's largest all-emission surface: `sh -c` is one
opaque `emission` extern, so *every* command prompts. But most agent shell use is
filesystem traffic in disguise. This module is the pure, total, testable core
that decides — BEFORE execution — whether a command line is a recognizable,
provably-fs-local operation that can be *lowered* onto the witnessed catalog
(`stdlib/fs.rvl`, item 244) and executed through the witnessed path with a real
inverse, or whether it must stay ONE honest, irreversible `emission`.

# The contract (safety-critical)

`classify(cmd) -> Plan` is:

  * PURE — no IO. It never reads the filesystem, never resolves a realpath,
    never expands a glob, never touches the environment. It reasons only about
    the *text* of the command. (Confinement to the workspace root and any
    existence check are RUNTIME concerns, enforced by the witnessed `fs.rvl`
    bodies, which fail closed on an out-of-root or missing target.)
  * TOTAL — it never raises. Every input yields exactly one of two verdicts.
  * TWO-VALUED — the result is either a `witnessed` lowering plan (an ordered
    list of catalog ops, each with its real inverse) or an `emission` verdict
    carrying a human `reason`. "cannot classify" is a FIRST-CLASS verdict, never
    a fallthrough to witnessed.

# The one asymmetry that matters

A false `emission` (refusing to lower a command that *was* reversible) costs a
prompt — annoying, never dangerous. A false `witnessed` (lowering a command that
is actually irreversible) silently auto-approves an irreversible effect behind a
single commit — the dangerous failure. So every ambiguity resolves toward
`emission`. Unknown flag, shell metacharacter, unexpected operand count, a
command name we do not recognize: all `emission`. We lower a command ONLY when
its text alone *proves* it is a single, bare, fs-local operation drawn from the
recognized set.

# The lowering table (command -> catalog op -> inverse)

    mv SRC DST     -> fs.move(SRC, DST)   undo unmove          (rename; reversible)
    rm PATH...     -> fs.rm(PATH)         undo unrm            (garbage-dir park)
    mkdir PATH...  -> fs.mkdir(PATH)      undo rmdir_if_empty  (empty-dir create)
    cp SRC DST     -> fs.write(DST, <SRC>) undo restore        (create/overwrite DST)
    touch PATH...  -> fs.write(PATH, "")  undo restore         (create-if-absent *)

  (*) `touch` is the one command whose reversibility depends on runtime state the
  pure classifier cannot see: on an ABSENT target it creates an empty file
  (a witnessed create whose inverse deletes it — fully reversible); on an
  EXISTING target it only bumps mtime, which the catalog cannot restore. The
  plan therefore carries `op="touch"` with `create_only=True`, and the shell
  runtime honours the safety asymmetry: absent -> witnessed create, present ->
  the mtime bump routes to the honest `emission` (one prompt). The classifier's
  guarantee — "witnessed lowering only where it is provably reversible" — is kept
  by that runtime split, documented here so the delegation is explicit.

# What is refused (stays one emission)

Anything with an unquoted shell metacharacter (pipe, ampersand, semicolon, the
redirect angles, parens, braces, dollar, backtick, star, question mark, bracket,
tilde, bang, hash) or a newline — pipelines, redirects, command substitution,
subshells,
globs, variable expansion, comments, command sequences; a `$` or a backtick
INSIDE DOUBLE QUOTES (they are still expansions there — only `'...'` suppresses
them); a backslash-`$` or backslash-backtick inside double quotes (the shell
consumes the backslash, shlex does not, so the two name different words); a
backslash-newline
LINE CONTINUATION (the shell deletes the pair and joins the words around it, so
the operand the tokenizer would produce is not the word the command names — and
the join crosses quotes, which no tokenizer can express); any flag at all (so
`rm -rf`, `cp -a`, `mv -f`, `mkdir -p` are all refused — each changes the
semantics or the reversibility the bare form guarantees); a command name outside
the recognized set (including a pathful `/bin/mv`); an operand starting with `-`
(an ambiguous flag-or-file); the wrong operand count; unbalanced quotes; the
empty command.

A metacharacter INSIDE quotes is literal data, not a shell feature: `mv "a;b" c`
lowers (the `;` is part of a filename), while `mv a b ; c` does not. The quote
scanner below draws exactly that line — but the two quote kinds are NOT alike.
SINGLE quotes make every metacharacter literal; DOUBLE quotes do not, because
`$` and the backtick stay shell features inside them (`mv "$(id)" dst` runs a
command, `rm "$HOME/x"` expands a variable, `rm "`cat targets`"` substitutes a
command). A pure classifier cannot see what those expand to, so the text does
not prove an fs-local operand and both are refused inside double quotes too.
`mv "a;b" c` lowers; `mv "$(id)" c` does not.

Two spellings of that same boundary are refused for the sibling reason — the
tokenized word is not the word the command names:

  * a backslash-NEWLINE LINE CONTINUATION, anywhere (the shell deletes the pair
    and joins the words around it, and the join crosses quotes);
  * a backslash-`$` or backslash-backtick INSIDE double quotes: POSIX says a
    backslash escapes only `$`, backtick, `"` and the backslash itself there, so
    the shell CONSUMES it (`"a\\$b"` is the word `a$b`) while `shlex` keeps it
    (`a\\$b`). Naming either would auto-approve an op on a path the command
    never mentions.
"""

from __future__ import annotations

import shlex
from typing import Optional


# Unquoted occurrences of any of these mean the command is not a single, bare,
# simple fs command — a shell feature (pipeline, redirect, expansion, glob,
# subshell, comment, sequence) is in play. Their presence, outside quotes, is an
# immediate `emission` verdict. A backslash escape and a newline are handled
# specially by the scanner (see `_first_unquoted_metachar`).
_METACHARS = frozenset("|&;<>(){}$`*?[]~!#")

# The commands we know how to lower, and the exact operand arity each requires.
# `None` means "one or more" (a sequence of independent witnessed ops).
_ARITY = {
    "mv": 2,
    "cp": 2,
    "rm": None,
    "mkdir": None,
    "touch": None,
}


def _first_unquoted_metachar(cmd: str) -> Optional[str]:
    """Return the first shell metacharacter that appears OUTSIDE any quoted span
    (or a description of a newline / dangling escape / unbalanced quote / a
    double-quoted expansion), or `None` if the command is free of unquoted shell
    features.

    This is the load-bearing safety scan. It walks the raw string tracking
    single- and double-quote state and honouring backslash escapes, so a
    metacharacter that is part of a quoted or escaped *argument* (literal data,
    e.g. a filename containing `;`) is allowed through, while a metacharacter
    acting as a shell operator is caught. Being purely textual it cannot be
    fooled by anything the shell would expand — because it refuses everything the
    shell would expand, INCLUDING inside double quotes, where `$` and the
    backtick are still features (only `'...'` suppresses them).
    """
    i = 0
    n = len(cmd)
    in_single = False  # inside '...'  (no escapes, everything literal)
    in_double = False  # inside "..."  (backslash escapes a few chars)
    while i < n:
        c = cmd[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\":
                # In double quotes a backslash escapes the next char; skip both.
                # A backslash-NEWLINE is NOT an escaped newline: it is a line
                # continuation, and the shell DELETES the pair (`"a\<nl>b"` is
                # the word `ab`), while shlex keeps a literal newline in the
                # token. The tokenized operand would then not be the word the
                # command names. Refuse (see `classify`'s asymmetry note: what
                # the text does not prove stays an emission).
                if i + 1 < n and cmd[i + 1] == "\n":
                    return "\\n"
                # POSIX: inside double quotes a backslash escapes ONLY `$`,
                # backtick, `"` and `\` — and for `$`/backtick the shell
                # CONSUMES it, so `"a\$b"` is the word `a$b` while shlex keeps
                # the backslash and yields `a\$b`. The plan would name a path the
                # command never mentions. Refuse rather than name it.
                if i + 1 < n and cmd[i + 1] in "$`":
                    return "escaped-expansion"
                i += 2
                continue
            if c == '"':
                in_double = False
                i += 1
                continue
            # DOUBLE quotes do NOT make `$` or the backtick literal — only single
            # quotes do. Inside `"..."` the shell still performs command
            # substitution (`"$(id)"`, `` "`id`" ``), parameter expansion
            # (`"$HOME/x"`, `"${SRC}"`) and arithmetic, so the operand the
            # tokenizer records is not the path the command will use. Refusing
            # here is the whole point of the scan: `mv "$(id)" dst` runs a
            # command whose target the text does not name, and auto-approving it
            # as `witnessed` (reversible by construction) would lower an op on a
            # different path than the one that runs. Every OTHER metacharacter
            # IS literal inside double quotes, so `mv "a;b" c` still lowers.
            if c == "$" or c == "`":
                return "expansion"
            i += 1
            continue
        # unquoted context
        if c == "\\":
            # An escaped char is literal data (e.g. `mv a\ b c`). Skip the pair.
            # A trailing backslash (escape at end of string) is malformed.
            if i + 1 >= n:
                return "\\"
            # A backslash-NEWLINE is a line continuation, not an escaped
            # newline: the shell deletes the pair and joins the words (`mv
            # a\<nl>b c` runs `mv ab c`), while shlex puts a literal newline in
            # the operand — the plan would name a path the command never
            # mentions. The join also crosses quotes (`mv 'x'\<nl>'y' c` runs
            # `mv xy c`), which shlex cannot express at all. Refuse.
            if cmd[i + 1] == "\n":
                return "\\n"
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "\n" or c == "\r":
            return "\\n"
        if c in _METACHARS:
            return c
        i += 1
    if in_single or in_double:
        return "unbalanced-quote"
    return None


def _emission(cmd: str, reason: str) -> dict:
    """The 'cannot classify / irreversible' verdict — a first-class result. The
    command stays one honest `emission` (irreversible by default)."""
    return {"verdict": "emission", "command": cmd, "reason": reason}


def _op(op: str, witnessed: str, inverse: str, args: list) -> dict:
    """One lowered catalog operation: the recognized command form (`op`), the
    `stdlib/fs.rvl` witnessed extern it executes through (`witnessed`), the
    inverse that extern auto-registers as a teardown entry (`inverse`), and the
    path arguments. `create_only` (touch) is added by the caller."""
    return {"op": op, "witnessed": witnessed, "inverse": inverse, "args": list(args)}


def _witnessed(cmd: str, ops: list) -> dict:
    return {"verdict": "witnessed", "command": cmd, "ops": ops}


def classify(cmd: str) -> dict:
    """Classify one command line (what would otherwise go to `sh -c`).

    Returns a Plan dict — either
      {"verdict": "witnessed", "command": cmd, "ops": [ {op, witnessed, inverse,
        args, [create_only]}, ... ]}
    or
      {"verdict": "emission", "command": cmd, "reason": "..."}.

    Total and pure: never raises, never does IO. See the module docstring for the
    lowering table and the refusal rules.
    """
    if not isinstance(cmd, str):
        # Defensive: the extern contract passes a Str, but stay total regardless.
        return _emission(str(cmd), "command is not a string")

    stripped = cmd.strip()
    if not stripped:
        return _emission(cmd, "empty command")

    # (1) Safety scan FIRST: any unquoted shell metacharacter, newline, dangling
    # escape, unbalanced quote, or a double-quoted expansion => this is not a
    # single bare fs command.
    meta = _first_unquoted_metachar(cmd)
    if meta is not None:
        if meta == "unbalanced-quote":
            return _emission(cmd, "unbalanced quotes")
        if meta == "\\":
            return _emission(cmd, "dangling backslash escape")
        if meta == "\\n":
            return _emission(cmd, "command spans multiple lines")
        if meta == "expansion":
            return _emission(cmd, "shell expansion inside double quotes")
        if meta == "escaped-expansion":
            return _emission(cmd, "escaped expansion inside double quotes")
        return _emission(cmd, f"unquoted shell metacharacter {meta!r}")

    # (2) Tokenize. shlex in POSIX mode resolves the quoting/escaping the scanner
    # already proved contains no shell operators, yielding literal argv tokens.
    try:
        tokens = shlex.split(cmd, comments=False, posix=True)
    except ValueError as exc:  # unbalanced quote shlex catches that we did not
        return _emission(cmd, f"unparseable command ({exc})")
    if not tokens:
        return _emission(cmd, "empty command")

    name = tokens[0]
    if name not in _ARITY:
        return _emission(cmd, f"unrecognized command {name!r}")

    # (3) Split operands from flags. ANY token starting with '-' is a flag (or a
    # '--' end-of-options, or an ambiguous flag-or-file); every recognized flag
    # would change the semantics or the reversibility the bare form guarantees
    # (`rm -rf`, `cp -a`, `mv -f`, `mkdir -p`). Unknown flag => cannot classify.
    operands = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            return _emission(cmd, f"flag {tok!r} changes semantics; not classifiable")
        operands.append(tok)

    if not operands:
        return _emission(cmd, f"{name}: no operands")

    arity = _ARITY[name]
    if arity is not None and len(operands) != arity:
        return _emission(
            cmd, f"{name}: expected {arity} operand(s), got {len(operands)}"
        )

    # (4) Build the lowering plan against the witnessed catalog.
    if name == "mv":
        src, dst = operands
        return _witnessed(cmd, [_op("mv", "move", "unmove", [src, dst])])

    if name == "cp":
        src, dst = operands
        # cp creates-or-overwrites DST; lowered to a witnessed write whose
        # preimage witness makes the overwrite reversible (restore) and whose
        # `created` flag makes a fresh DST reversible (delete). The runtime reads
        # SRC's bytes (IO — not the classifier's job) and calls fs.write(DST, .).
        return _witnessed(cmd, [_op("cp", "write", "restore", [src, dst])])

    if name == "rm":
        # each operand is an independent witnessed rm (garbage-dir park); a
        # sequence, each registering its own unrm inverse.
        return _witnessed(cmd, [_op("rm", "rm", "unrm", [p]) for p in operands])

    if name == "mkdir":
        return _witnessed(
            cmd, [_op("mkdir", "mkdir", "rmdir_if_empty", [p]) for p in operands]
        )

    if name == "touch":
        # create-if-absent; the runtime witnessed-creates an absent target
        # (reversible delete) and routes an mtime bump on an EXISTING target to
        # the honest emission (see module docstring). `create_only` flags that.
        ops = []
        for p in operands:
            entry = _op("touch", "write", "restore", [p])
            entry["create_only"] = True
            ops.append(entry)
        return _witnessed(cmd, ops)

    # Unreachable: every name in _ARITY is handled above. Stay total anyway.
    return _emission(cmd, f"unhandled command {name!r}")  # pragma: no cover
