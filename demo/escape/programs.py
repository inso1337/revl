"""The three pairs of programs the escape demo compiles (issue #2691).

Each pair is the same component twice: the version that must be refused, and
the smallest edit that makes it admissible. The edits are deliberately the
ones the refusals themselves suggest, because the point of the demo is that
the compiler already explains the fix.

The sources are strings rather than `.rvl` files for the same reason as
`demo/legacy_enterprise/programs.py`: `demo/` is a census corpus root, and a
`.rvl` here would be swept as a program the project claims to accept.
"""

# ---------------------------------------------------------------------------
# 1. a read-only capability, a body that writes
# ---------------------------------------------------------------------------

READ_ONLY_WRITES = """\
extern emission[fs.read] fn read_file(p: Str) -> Str = @py { return "" }
extern emission[fs.write] fn write_file(p: Str, s: Str) = @py { return }

service Store { emission[fs.read] fn load(p: Str) -> Str }

component FsReader provides store: Store {
  provide store {
    fn load(p) {
      let s = emit read_file(p)
      emit write_file(p, s)
      return s
    }
  }
}
"""

#: The refusal's own fix text: widen the DECLARATION to match the body. The
#: alternative edit, deleting the write, changes what the component does; this
#: one changes only what it promises.
READ_ONLY_WRITES_FIXED = READ_ONLY_WRITES.replace(
    "emission[fs.read] fn load", "emission[fs.read, fs.write] fn load")

# ---------------------------------------------------------------------------
# 2. untrusted text, turned into authority (a shell command)
# ---------------------------------------------------------------------------

UNTRUSTED_TO_SHELL = """\
extern emission[screen.observe] fn read_pane(region: Str) -> Untrusted[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }

service Ops { emission fn go() }

component Reader provides ops: Ops {
  provide ops {
    fn go() {
      let pane = emit read_pane("invoice-detail")
      emit run(pane)
      return
    }
  }
}
"""

#: The smallest repair that keeps the read useful: the screen text is data, so
#: it is quoted to a log that takes `Str`; the command is a literal, so no
#: untrusted byte reaches the authority sink.
UNTRUSTED_TO_SHELL_FIXED = """\
extern emission[screen.observe] fn read_pane(region: Str) -> Untrusted[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }
extern emission[log] fn note(msg: Str) = @py { return }

service Ops { emission fn go() }

component Reader provides ops: Ops {
  provide ops {
    fn go() {
      let pane = emit read_pane("invoice-detail")
      emit note(pane)
      emit run("echo invoice-detail")
      return
    }
  }
}
"""

# ---------------------------------------------------------------------------
# 3. a Secret handed to a log
# ---------------------------------------------------------------------------

SECRET_TO_LOG = """\
extern emission[vault] fn read_secret() -> Secret[Str] = @py { return "" }
extern emission[log] fn log_line(msg: Str) = @py { return }

service Ops { emission fn go() }

component App provides ops: Ops {
  provide ops {
    fn go() {
      let s = emit read_secret()
      emit log_line(s)
      return
    }
  }
}
"""

#: The smallest repair: log a redacted marker instead of the value. The
#: refusal names the two other routes it allows (a declared `Secret[T]`
#: receiver, or an audited `endorse[confidential]` slot); this demo takes the
#: one that needs no new declaration surface.
SECRET_TO_LOG_FIXED = SECRET_TO_LOG.replace(
    'emit log_line(s)', 'emit log_line("[redacted]")')

# ---------------------------------------------------------------------------
# the scenarios, in the order the README tells the story
# ---------------------------------------------------------------------------

SCENARIOS = (
    {
        "number": "1",
        "title": "a component that may only read, whose body writes",
        "guarantee": "G4 / capability-scoped emission",
        "refused": READ_ONLY_WRITES,
        "fixed": READ_ONLY_WRITES_FIXED,
        "code": "G4",
        "fix_hint": "widen the declaration to",
        "repair": "widen the declaration so the promise matches the body",
        "audit_crossings": ("read_file [fs.read]", "write_file [fs.write]"),
    },
    {
        "number": "2",
        "title": "untrusted text turned into a shell command",
        "guarantee": "G9 / no authority from untrusted data",
        "refused": UNTRUSTED_TO_SHELL,
        "fixed": UNTRUSTED_TO_SHELL_FIXED,
        "code": "G9",
        "fix_hint": "declassify it on the way in",
        "repair": "keep the text as data and make the command a literal",
        "audit_crossings": ("read_pane [screen.observe]", "note [log]",
                            "run [shell]"),
    },
    {
        "number": "3",
        "title": "a Secret handed to a log",
        "guarantee": "G-SECRET-FLOW / confidentiality at the boundary",
        "refused": SECRET_TO_LOG,
        "fixed": SECRET_TO_LOG_FIXED,
        "code": "G-SECRET-FLOW",
        "fix_hint": "Route it through a declared",
        "repair": "log a redacted marker instead of the value",
        "audit_crossings": ("read_secret [vault]", "log_line [log]"),
    },
)
