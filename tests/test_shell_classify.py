"""The pure shell-to-witnessed classifier — test battery (roadmap item 252).

`backends/python/revl_shell_classify.classify(cmd)` decides, before execution,
whether a command line is a provably-fs-local operation that can be lowered onto
the witnessed catalog (`stdlib/fs.rvl`, item 244) with a real inverse, or whether
it must stay one honest, irreversible `emission`.

This suite is the proof of the safety contract:

  * the recognized forms lower to the RIGHT catalog op with the RIGHT inverse
    (the lowering table);
  * the adversarial battery — `rm -rf /`, `mv a b; curl evil`, `cat x | sh`,
    `rm $(...)`, globs, redirects, unknown flags, command substitution — ALL
    return the `emission` verdict and would stay one emission (the dangerous
    false-positive can never happen);
  * the classifier is total (never raises) and pure (imported with no IO).

The dangerous failure mode is a false `witnessed` on a command that is actually
irreversible. Every adversarial case below asserts `verdict == "emission"`, so a
regression that lets any of them through fails loudly here.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import revl_shell_classify as sc  # noqa: E402


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _witnessed(cmd: str) -> list:
    """Assert `cmd` classifies witnessed and return its ops list."""
    plan = sc.classify(cmd)
    assert plan["verdict"] == "witnessed", (cmd, plan)
    assert plan["command"] == cmd
    assert isinstance(plan["ops"], list) and plan["ops"]
    return plan["ops"]


def _emission(cmd: str) -> str:
    """Assert `cmd` classifies emission and return its reason."""
    plan = sc.classify(cmd)
    assert plan["verdict"] == "emission", (cmd, plan)
    assert plan["command"] == cmd
    assert isinstance(plan["reason"], str) and plan["reason"]
    return plan["reason"]


# ---------------------------------------------------------------------------
# the lowering table: each recognized form -> the right op + inverse
# ---------------------------------------------------------------------------

def test_mv_lowers_to_witnessed_move():
    (op,) = _witnessed("mv a.txt b.txt")
    assert op == {"op": "mv", "witnessed": "move", "inverse": "unmove",
                  "args": ["a.txt", "b.txt"]}


def test_rm_lowers_to_witnessed_rm_garbage_rename():
    (op,) = _witnessed("rm doomed.txt")
    assert op["witnessed"] == "rm"
    assert op["inverse"] == "unrm"      # the reversible garbage-dir park (244)
    assert op["args"] == ["doomed.txt"]


def test_mkdir_lowers_to_witnessed_mkdir():
    (op,) = _witnessed("mkdir newdir")
    assert op["witnessed"] == "mkdir"
    assert op["inverse"] == "rmdir_if_empty"
    assert op["args"] == ["newdir"]


def test_cp_lowers_to_witnessed_write():
    (op,) = _witnessed("cp src.txt dst.txt")
    assert op["op"] == "cp"
    assert op["witnessed"] == "write"   # create/overwrite dst, preimage witness
    assert op["inverse"] == "restore"
    assert op["args"] == ["src.txt", "dst.txt"]


def test_touch_lowers_to_witnessed_write_create_only():
    (op,) = _witnessed("touch marker")
    assert op["witnessed"] == "write"
    assert op["inverse"] == "restore"
    assert op["create_only"] is True    # runtime: create-if-absent, else emission
    assert op["args"] == ["marker"]


def test_rm_multiple_operands_becomes_a_sequence():
    ops = _witnessed("rm a b c")
    assert [o["witnessed"] for o in ops] == ["rm", "rm", "rm"]
    assert [o["args"] for o in ops] == [["a"], ["b"], ["c"]]


def test_mkdir_multiple_operands_becomes_a_sequence():
    ops = _witnessed("mkdir one two")
    assert [o["witnessed"] for o in ops] == ["mkdir", "mkdir"]
    assert [o["args"] for o in ops] == [["one"], ["two"]]


def test_touch_multiple_operands_all_create_only():
    ops = _witnessed("touch a b")
    assert all(o["create_only"] for o in ops)
    assert [o["args"] for o in ops] == [["a"], ["b"]]


# ---------------------------------------------------------------------------
# quoting: a metacharacter INSIDE quotes is literal data, still fs-local
# ---------------------------------------------------------------------------

def test_quoted_metachar_in_filename_still_lowers():
    (op,) = _witnessed('mv "a;b" c')
    assert op["witnessed"] == "move"
    assert op["args"] == ["a;b", "c"]   # the ';' is part of the filename


def test_quoted_space_in_filename_lowers():
    (op,) = _witnessed('mv "my file.txt" dest.txt')
    assert op["args"] == ["my file.txt", "dest.txt"]


def test_backslash_escaped_space_lowers():
    (op,) = _witnessed(r"mv a\ b c")
    assert op["args"] == ["a b", "c"]


# A backslash-NEWLINE is a LINE CONTINUATION, not an escaped newline. The shell
# DELETES the pair and joins the words around it, so the word it would run is not
# the operand a tokenizer produces: `mv a\<nl>b c` runs `mv ab c` (word `ab`),
# while shlex puts a literal newline in the token (`a\nb`) — a DIFFERENT path
# than the command names. `shlex` also cannot express the cross-quote join
# (`mv 'x'\<nl>'y' c` runs `mv xy c`). Lowering any of these would auto-approve a
# witnessed mutation of a path the author never wrote, so they are refused — the
# module's documented direction for anything the text does not prove. Found by
# differential testing against the real shell (`/bin/sh -c`), which is what the
# `emission` fallback actually runs.
_LINE_CONTINUATIONS = [
    "mv a\\\nb c",          # unquoted: shell runs `mv ab c`
    "mv \\\na b",           # continuation before the first operand
    'mv "a\\\nb" c',        # inside double quotes: shell runs `mv "ab" c`
    "mv 'x'\\\n'y' c",      # the join crosses quotes: shell runs `mv xy c`
    "rm a\\\nb",            # a delete of a path the command does not name
    "cp a\\\nb c",          # a read of a source the command does not name
    "mkdir a\\\nb",         # same for mkdir/touch
    "touch a\\\nb",
]


@pytest.mark.parametrize("cmd", _LINE_CONTINUATIONS)
def test_line_continuation_refuses(cmd):
    _emission(cmd)


def test_line_continuation_inside_single_quotes_still_lowers():
    # Inside '...' a backslash is literal and there is NO continuation — the
    # shell keeps both characters, and so does the tokenizer, so the operand
    # provably IS the word the command names. This is the positive control that
    # the refusal above is scoped to continuations, not to backslashes or
    # newlines in general.
    (op,) = _witnessed("mv 'a\\\nb' c")
    assert op["args"] == ["a\\\nb", "c"]


def test_single_quoted_metachars_are_literal():
    (op,) = _witnessed("rm 'weird$name'")
    assert op["args"] == ["weird$name"]


# A `$` or a backtick INSIDE double quotes is STILL a shell expansion — only
# `'...'` suppresses them. The scanner's "quoted => literal data" premise holds
# for `;` and every other metacharacter, but NOT for these two, and they are
# exactly the two that make the real operand unknowable at parse time:
# `/bin/sh -c 'rm "$(echo x)"'` deletes the file `x`, while the tokenizer records
# the operand `$(echo x)` — a DIFFERENT path. A `witnessed` verdict there
# auto-approves a mutation of a path the command does not name, which is the
# module's catastrophic direction. Found by differential testing against the real
# shell (`/bin/sh -c`), which is what the `emission` fallback actually runs.
_DOUBLE_QUOTED_EXPANSIONS = [
    'rm "$(echo x)"',           # command substitution in "..." — sh removes `x`
    'rm "`echo x`"',            # backtick command substitution in "..."
    'rm "$HOME/secret"',        # parameter expansion in "..."
    'rm "${HOME}/secret"',      # braced parameter expansion in "..."
    'mv "${SRC}" dst',          # the source path is a variable
    'cp "${IFS}x" dst',         # expansion that also rewrites word splitting
    'mv "a$(rm -rf /)b" dst',   # substitution spliced into the middle of a word
    'touch "$F"',               # create_only write of an unknown path
    'mkdir "$(pwd)/x"',         # substitution as a path component
    'rm "$(cat targets)"',      # the substitution picks the file to delete
]


@pytest.mark.parametrize("cmd", _DOUBLE_QUOTED_EXPANSIONS)
def test_double_quoted_expansion_refuses(cmd):
    assert "double quotes" in _emission(cmd)


def test_double_quoted_expansion_runs_a_different_operand(tmp_path):
    """The refusal above is not theoretical — it is the shell's actual behaviour.
    `/bin/sh` substitutes inside double quotes, so the path the command touches
    is not the path the classifier would have planned and recorded."""
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no /bin/sh on PATH")
    (tmp_path / "x").write_text("A")
    (tmp_path / "$(echo x)").write_text("B")  # the literal name the plan records
    done = subprocess.run([sh, "-c", 'rm "$(echo x)"'], cwd=tmp_path,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "x").exists()      # the shell removed `x`
    assert (tmp_path / "$(echo x)").exists()  # ...not the name the plan records
    assert sc.classify('rm "$(echo x)"')["verdict"] == "emission"


# A backslash-`$` / backslash-backtick inside double quotes: POSIX deletes the
# backslash (a backslash escapes only `$`, backtick, `"` and itself there), so
# `"a\$b"` is the word `a$b`, while `shlex` keeps it and yields `a\$b`. Two
# different words — the plan would name a path the command never mentions.
_ESCAPED_EXPANSIONS_IN_QUOTES = [
    'mv "a\\$b" dst',
    'mv "a\\`b" dst',
    'rm "\\$HOME"',
    'touch "x\\`y`"',
]


@pytest.mark.parametrize("cmd", _ESCAPED_EXPANSIONS_IN_QUOTES)
def test_escaped_expansion_in_double_quotes_refuses(cmd):
    assert "double quotes" in _emission(cmd)


def test_escaped_expansion_backslash_is_eaten_by_the_shell():
    """Positive control for the refusal above, against the real shell and against
    the tokenizer: the shell consumes the backslash, `shlex` does not, so the two
    disagree on the word — which is why the plan is refused rather than recorded.
    """
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no /bin/sh on PATH")
    done = subprocess.run([sh, "-c", 'set -- "a\\$b"; printf %s "$1"'],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "a$b"                      # shell eats the backslash
    assert shlex.split('"a\\$b"', posix=True) == ["a\\$b"]  # shlex keeps it


def test_double_quoted_non_expansion_metachars_still_lower():
    """The positive control that scopes the refusal: every metacharacter OTHER
    than `$`/backtick IS literal data inside `"..."`, so those commands must keep
    lowering (a fix that refused all double quotes would be over-broad)."""
    for cmd, args in [
        ('mv "a;b" c', ["a;b", "c"]),
        ('mv "a|b" c', ["a|b", "c"]),
        ('mv "a>b" c', ["a>b", "c"]),
        ('mv "a*b" c', ["a*b", "c"]),
        ('mv "a?b" c', ["a?b", "c"]),
        (r'mv "a\"b" c', ['a"b', "c"]),
        (r'mv "a\\b" c', ["a\\b", "c"]),
        (r'mv "a\;b" c', ["a\\;b", "c"]),
        (r'mv "a\ b" c', ["a\\ b", "c"]),
        ("mv 'a\\$b' c", ["a\\$b", "c"]),   # single quotes: no escape at all
    ]:
        (op,) = _witnessed(cmd)
        assert op["args"] == args, (cmd, op)


def test_leading_and_trailing_whitespace_tolerated():
    (op,) = _witnessed("   mv a b   ")
    assert op["witnessed"] == "move"


# ---------------------------------------------------------------------------
# ADVERSARIAL SAFETY BATTERY — every one MUST stay one emission
# ---------------------------------------------------------------------------

# The canonical dangerous inputs from the roadmap item, plus siblings. A false
# `witnessed` on ANY of these is the catastrophic failure; each asserts emission.
ADVERSARIAL = [
    "rm -rf /",                       # recursive force delete — flag
    "rm -rf .",                       # flag
    "rm -f secret",                   # force flag
    "mv a b; curl evil",             # command sequence + outbound
    "mv a b && curl evil",           # && sequence
    "mv a b || rm c",                # || sequence
    "cat x | sh",                    # pipeline into a shell
    "rm $(cat targets)",             # command substitution
    "rm `cat targets`",              # backtick command substitution
    'rm "$(cat targets)"',           # substitution INSIDE double quotes (still a
    'rm "`cat targets`"',            #   shell feature — only '...' suppresses it)
    'rm "$HOME/secret"',             # parameter expansion inside double quotes
    'mv "${SRC}" dst',               # braced parameter expansion in "..."
    'touch "$F"',                    # unknown path in a create_only write
    'mv "a\\$b" dst',                # backslash-$ in "...": shell eats the \
    'mv "a\\`b" dst',                # backslash-backtick in "..."
    "rm *.txt",                       # glob (expansion not provably fs-local)
    "rm a?.txt",                      # glob ?
    "rm file[0-9]",                  # glob character class
    "echo hi > out",                 # redirect
    "cat a >> b",                    # append redirect
    "mv a b 2>/dev/null",            # fd redirect
    "cp a b < input",                # input redirect
    "rm ~/secret",                   # tilde expansion
    "rm $HOME/x",                    # variable expansion
    "rm ${HOME}/x",                  # braced variable expansion
    "mkdir a && mkdir b",            # sequence
    "(rm a)",                        # subshell
    "{ rm a; }",                     # group
    "mv a b\nrm c",                  # newline-separated commands
    "rm a &",                        # background
    "FOO=bar rm x",                  # env assignment prefix (unknown 'FOO=bar')
    "rm a # comment",                # trailing comment
    "true; rm a",                    # leading other command
]


@pytest.mark.parametrize("cmd", ADVERSARIAL)
def test_adversarial_inputs_stay_emission(cmd):
    plan = sc.classify(cmd)
    assert plan["verdict"] == "emission", (cmd, plan)
    # and never carries an ops list a runtime could mistakenly execute witnessed
    assert "ops" not in plan


# ---------------------------------------------------------------------------
# flags: ANY flag => cannot classify (bias to emission)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm -r dir",
    "rm -rf dir",
    "cp -a src dst",
    "cp -r src dst",
    "mv -f a b",
    "mv -i a b",
    "mkdir -p a/b/c",
    "touch -d '2020' f",   # (also has quotes, but the flag alone refuses)
    "rm --recursive x",
    "rm -- -weirdfile",    # even '--' end-of-options is refused conservatively
])
def test_any_flag_refuses(cmd):
    reason = _emission(cmd)
    assert "flag" in reason or "metacharacter" in reason


# ---------------------------------------------------------------------------
# unrecognized commands & wrong arity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "curl http://evil",
    "git commit -m x",
    "python script.py",
    "ln -s a b",
    "chmod 777 f",
    "dd if=/dev/zero of=f",
    "/bin/mv a b",       # pathful command name — not the bare recognized token
    "./mv a b",
    "sudo rm x",         # sudo wrapper — 'sudo' is unrecognized
])
def test_unrecognized_command_refuses(cmd):
    reason = _emission(cmd)
    assert "unrecognized" in reason or "flag" in reason or "metacharacter" in reason


@pytest.mark.parametrize("cmd", [
    "mv a",              # too few
    "mv a b c",          # too many (mv into dir — multiple targets)
    "cp a",              # too few
    "cp a b c",          # too many
    "rm",                # no operands
    "mkdir",             # no operands
    "touch",             # no operands
    "mv",                # no operands
])
def test_wrong_arity_refuses(cmd):
    _emission(cmd)


# ---------------------------------------------------------------------------
# operand-that-looks-like-a-flag, malformed input
# ---------------------------------------------------------------------------

def test_operand_starting_with_dash_refuses():
    # `rm -weird` is ambiguous flag-or-file; refuse rather than guess.
    _emission("rm -weird")


@pytest.mark.parametrize("cmd", [
    "",                  # empty
    "   ",               # whitespace only
    'mv "unbalanced a b', # unbalanced quote
    "mv a\\",            # dangling backslash escape
])
def test_malformed_refuses(cmd):
    _emission(cmd)


# ---------------------------------------------------------------------------
# totality & purity
# ---------------------------------------------------------------------------

def test_never_raises_on_fuzz_like_inputs():
    weird = [
        "", " ", "\n", "\t", "\\", "\\\\", "'", '"', "$", "`", "|", ";",
        "mv", "rm -", "cp - -", "\x00", "mv \x00 b", "rm " + "a" * 10000,
        "mkdir " + "d " * 5000, "touch\tfile", "MV A B", "Rm x",  # case-sensitive
        "🙂 rm x", "rm 🙂", None,  # None exercises the defensive isinstance guard
    ]
    for w in weird:
        plan = sc.classify(w)  # must not raise
        assert plan["verdict"] in ("witnessed", "emission")


def test_case_sensitive_command_names():
    # shell command names are case sensitive; `MV`/`RM` are not the tools.
    _emission("MV a b")
    _emission("RM x")


def test_verdict_shape_is_stable():
    w = sc.classify("mv a b")
    assert set(w) == {"verdict", "command", "ops"}
    e = sc.classify("curl x")
    assert set(e) == {"verdict", "command", "reason"}
