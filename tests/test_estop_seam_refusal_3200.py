"""Issue #3200: a single-process `revl run` on a tier whose runtime does not
read the E-Stop latch at its crossing seams refuses an armed latch instead of
booting and running every crossing unwatched.

The py tier is the reference: with a halted latch it refuses the first plug
(tests/test_estop_443.py). No other tier reads the latch on its single-process
path: ts and wasm have no seam, and the rust, go and java runs do not forward
the latch to their runner (their placement runners honour it, which is why they
are in `revl.estop.TIERS_WITH_ESTOP`). Before this fix
`revl run --backend ts --estop-latch L` ran the composition to `DOWN` and
exited 0 with the operator's halt written to L. These tests need no toolchain:
the refusal happens before a runtime is imported, the same way `--attestation`
is refused off the py tier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import estop  # noqa: E402

EXAMPLE = str(ROOT / "examples" / "counter_pair.rvl")


def _cli(*argv) -> int:
    from revl.__main__ import main  # noqa: PLC0415
    return main(list(argv))


@pytest.fixture
def halted_latch(tmp_path, monkeypatch):
    monkeypatch.delenv(estop.LATCH_ENV, raising=False)
    latch = tmp_path / "run.estop"
    assert _cli("estop", "--latch", str(latch), "--reason", "test halt") == 1
    assert estop.read_latch(str(latch)) is not None
    return latch


@pytest.mark.parametrize("backend", ["ts", "wasm", "rust", "go", "java"])
def test_the_flag_is_refused_on_a_tier_without_a_seam(backend, halted_latch,
                                                      capsys):
    capsys.readouterr()
    rc = _cli("run", EXAMPLE, "--backend", backend, "--once",
              "--estop-latch", str(halted_latch))
    err = capsys.readouterr().err
    assert rc != 0
    assert f"--backend {backend} has no E-Stop seam" in err
    assert "--estop-latch" in err


@pytest.mark.parametrize("backend", ["rust", "go", "java"])
def test_the_ambient_latch_is_refused_off_py(backend, halted_latch,
                                             monkeypatch, capsys):
    monkeypatch.setenv(estop.LATCH_ENV, str(halted_latch))
    capsys.readouterr()
    rc = _cli("run", EXAMPLE, "--backend", backend, "--once")
    err = capsys.readouterr().err
    assert rc != 0
    assert f"--backend {backend} has no E-Stop seam" in err


def test_the_ambient_latch_is_refused_on_ts(halted_latch, monkeypatch, capsys):
    monkeypatch.setenv(estop.LATCH_ENV, str(halted_latch))
    capsys.readouterr()
    rc = _cli("run", EXAMPLE, "--backend", "ts", "--once")
    err = capsys.readouterr().err
    assert rc != 0
    assert estop.LATCH_ENV in err
    assert "--backend ts has no E-Stop seam" in err


def test_the_refusal_also_covers_plan(halted_latch, capsys):
    capsys.readouterr()
    assert _cli("run", EXAMPLE, "--backend", "ts", "--plan",
                "--estop-latch", str(halted_latch)) != 0
    assert "has no E-Stop seam" in capsys.readouterr().err


def test_a_ts_run_without_a_latch_is_unchanged(monkeypatch, capsys):
    monkeypatch.delenv(estop.LATCH_ENV, raising=False)
    capsys.readouterr()
    assert _cli("run", EXAMPLE, "--backend", "ts", "--plan") == 0
    assert "E-Stop" not in capsys.readouterr().err


def test_every_tier_outside_the_honouring_set_refuses(monkeypatch):
    monkeypatch.delenv(estop.LATCH_ENV, raising=False)
    from revl.run import KNOWN_BACKENDS, _estop_seam_problem  # noqa: PLC0415
    for backend in KNOWN_BACKENDS:
        problem = _estop_seam_problem(backend, "some.estop")
        if backend == "py":
            assert problem is None
        else:
            assert problem and f"--backend {backend}" in problem, backend
        assert _estop_seam_problem(backend, None) is None, backend
    monkeypatch.setenv(estop.LATCH_ENV, "ambient.estop")
    assert _estop_seam_problem("py", None) is None
    assert "ambient.estop" in _estop_seam_problem("ts", None)


def test_the_placement_honouring_set_is_unchanged():
    assert estop.TIERS_WITH_ESTOP == frozenset({"py", "go", "rust", "java"})
