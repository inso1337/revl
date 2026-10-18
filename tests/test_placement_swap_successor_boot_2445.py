"""Issue #2445: the swap successor wait fails fast on a successor that cannot boot.

The conductor's boot wait ends as soon as a child that has not said `UP` prints
a FATAL line or exits. The swap path waited a full minute for its successor
regardless. It now applies the same rule: the swap names the successor with its
error and exit code, tears it back down, and the old provider keeps serving.

Real conductor, scripted children (the pattern of tests/test_swap.py). The
conductor's clock runs 10x so the 60 second wait the old code spent costs six
real seconds in a failing run instead of a minute.
"""

from __future__ import annotations

import builtins
import json
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement  # noqa: E402

SPEED = 10.0
# What the conductor's clock measures: under the 60 s the old wait cost on its
# own. Teardown of the dead successor adds about 2 real seconds (20 here).
FAST = 50.0

_APP = """
service Cache {
  async fn get(k: Str) -> Opt[Str]
  async fn put(k: Str, v: Str)
}
service App { async fn run() -> Opt[Str] }

component MemCache provides cache: Cache {
  let m = effect Map.new() undo m.drop()
  provide cache {
    async fn get(k) = m.get(k)
    async fn put(k, v) = v
  }
}
component Consumer requires cache: Cache provides app: App {
  provide app { async fn run() = cache.get("k") }
}
"""

_TOML = """
[processes.provider]
components = ["MemCache"]

[processes.consumer]
components = ["Consumer"]
"""


class _Stdout:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def push(self, line):
        self._q.put(line if line.endswith("\n") else line + "\n")

    def close(self):
        self._q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _Stdin:
    def __init__(self, proc):
        self._proc = proc
        self.written: list[str] = []

    def write(self, text):
        self.written.append(text)
        for line in text.splitlines():
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cmd.get("op") == "repoint":
                self._proc.stdout.push(
                    f"[{self._proc.name}] REPOINTED {cmd['key']} -> {cmd['socket']}")

    def flush(self):
        pass

    def close(self):
        pass


class _Child:
    """A placement child. Healthy by default: says `UP`, acknowledges repoints
    and says `DOWN` when asked to stop. With `lines`/`code` it instead prints
    those lines and exits with `code`, never saying `UP`."""

    def __init__(self, name, lines=None, code=None):
        self.name = name
        self.stdout = _Stdout()
        self.stdin = _Stdin(self)
        self.terminated = False
        self._code = None
        if lines is None and code is None:
            self.stdout.push(f"[{name}] UP")
        else:
            for line in lines or []:
                self.stdout.push(f"[{name}] {line}")
            self._code = code
            self.stdout.close()

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        return self._code if self._code is not None else 0

    def terminate(self):
        self.terminated = True
        if self._code is None:
            self._code = 0
            self.stdout.push(f"[{self.name}] residue no residue | "
                             f"registry=0 provisions=[] disposables=0/0")
            self.stdout.push(f"[{self.name}] DOWN")
            self.stdout.close()

    kill = terminate


class _FastClock:
    """`placement.time` with a clock that runs SPEED times faster."""

    def __init__(self):
        self._start = time.time()

    def time(self):
        return self._start + (time.time() - self._start) * SPEED

    def __getattr__(self, name):
        return getattr(time, name)


def _swap(tmp_path, monkeypatch, capsys, successor):
    """Run the placement, swap MemCache onto py with `successor(name)` standing
    in for the new child, and return (rc, perceived seconds, out, err, procs)."""
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    plc = tmp_path / "app.toml"
    plc.write_text(_TOML, encoding="utf-8")
    procs: dict = {}

    def fake_popen(cmd, **kwargs):
        name = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))["name"]
        procs[name] = successor(name) if name.startswith("MemCache__t") \
            else _Child(name)
        return procs[name]

    monkeypatch.setattr(placement, "_cordis_py_installed", lambda: True)
    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(placement, "_interactive", lambda: True)
    feed = iter(["swap MemCache --to py", ":q"])

    def fake_input(prompt=""):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(placement, "time", _FastClock())
    started = placement.time.time()
    rc = placement.run_placement([str(app)], str(plc), once=False)
    elapsed = placement.time.time() - started
    captured = capsys.readouterr()
    return rc, elapsed, captured.out, captured.err, procs


def _assert_old_provider_keeps_serving(procs, out):
    """The refused swap left the composition as it was: the consumer was never
    re-pointed and the old provider was stopped only by the final teardown
    (after the REPL ended). The return code is not asserted: the dead
    successor is reported by the teardown as exited before DOWN (rc 1), exactly
    as it was after the old 60 second wait."""
    consumer_ops = [json.loads(w) for w in procs["consumer"].stdin.written
                    if w.strip()]
    assert not any(c.get("op") == "repoint" for c in consumer_ops), consumer_ops
    assert "swap refused: successor MemCache__t1 failed to boot" in out, out
    assert "running composition untouched" in out, out
    # the old provider unwound only at the final teardown, after the refusal
    assert out.index("swap refused") < out.index("[provider] DOWN"), out
    assert "the old provider" not in out, out
    assert "now on py" not in out, out


def test_a_successor_that_prints_fatal_and_exits_ends_the_wait_and_is_named(
        tmp_path, monkeypatch, capsys):
    rc, elapsed, out, err, procs = _swap(
        tmp_path, monkeypatch, capsys,
        lambda name: _Child(name, ["FATAL RuntimeError: boot failed loading "
                                   "MemCache"], 3))
    assert elapsed < FAST, elapsed
    assert ("error: process MemCache__t1 failed to boot: "
            "RuntimeError: boot failed loading MemCache (exit code 3)") in err, err
    assert "did not come up" not in out + err, out + err
    _assert_old_provider_keeps_serving(procs, out)


def test_a_successor_that_exits_before_up_with_no_fatal_line_is_named(
        tmp_path, monkeypatch, capsys):
    rc, elapsed, out, err, procs = _swap(
        tmp_path, monkeypatch, capsys, lambda name: _Child(name, [], 5))
    assert elapsed < FAST, elapsed
    assert ("error: process MemCache__t1 failed to boot: "
            "exited with code 5 before it reported up") in err, err
    assert "did not come up" not in out + err, out + err
    _assert_old_provider_keeps_serving(procs, out)


def test_a_successor_that_boots_normally_still_swaps(
        tmp_path, monkeypatch, capsys):
    rc, _, out, err, procs = _swap(
        tmp_path, monkeypatch, capsys, lambda name: _Child(name))
    assert rc == 0, err
    assert "failed to boot" not in err, err
    assert "swap refused" not in out, out
    ops = [json.loads(w) for w in procs["consumer"].stdin.written if w.strip()]
    assert any(c.get("op") == "repoint" and "MemCache__t1" in c.get("socket", "")
               for c in ops), ops
    assert procs["provider"].terminated
    assert "now on py (MemCache__t1)" in out, out
