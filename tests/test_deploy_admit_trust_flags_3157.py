"""The `revl deploy-admit` runner can set the receiver's own trust rules (#3157).

The far host's :class:`deploy.TrustStore` was built from `--key`, `--host-key`
and the two `--require-*` switches only. `evidence_ttl_seconds`, `not_before`,
`capability_ceiling`, `revoked` and `recheck_source` all existed on the trust
store with "off" defaults and no way to turn them on from the runner's command
line, so a host accepted any past attested bundle a trusted key had ever
signed: it could not bound the age or the reach of what it was handed.

These tests drive the real CLI entry point, `deploy.deploy_admit_command` over
the parser `revl deploy-admit` is built from, so the flag spelling is the
contract under test and not an internal keyword argument. Every rule is checked
against the same bundle twice where it matters: once with the rule set, and once
without, so a refusal is the rule biting and not a bundle that never admitted.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from revl import deploy  # noqa: E402


SOURCE = """\
service Mail { emission[smtp] fn send(to: Str) }

extern emission fn smtp(line: Str) = @py { pass }

component Smtp provides mail: Mail {
  provide mail {
    fn send(to) = emit smtp(to)
  }
}

component Notifier requires mail: Mail {
  emit mail.send("a@b")
}
"""

#: The signer key the conductor attested with, held by the host in its trust
#: store as a `--key` file.
SIGNER_KEY = b"issue-3157-signer-key"
#: The host's own receipt-signing key (design R5), a `--host-key` file. Distinct
#: from the signer so a revoked SIGNER key never disables the host's own
#: receipts.
HOST_KEY = b"issue-3157-host-receipt-key"

CHALLENGE = "issue-3157-challenge"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    """A real admitted bundle with its deploy attestation, built once: the
    compile + emit is the expensive part and no test rewrites these bytes."""
    from revl.bundle import build_bundle

    root = tmp_path_factory.mktemp("bundle-3157")
    src = root / "mailer.rvl"
    src.write_text(SOURCE, encoding="utf-8")
    out = root / "app.revlbundle"
    build_bundle([str(src)], str(out), backends=("python",), env={})
    attestation = deploy.make_deploy_attestation(out, SIGNER_KEY, signer="ci")
    (out / deploy.ATTESTATION_NAME).write_text(
        json.dumps(attestation, sort_keys=True), encoding="utf-8")
    return out


@pytest.fixture
def stale_bundle(bundle, tmp_path):
    """The SAME admitted bundle, re-signed thirty days ago.

    A signature proves authenticity, never current validity; the signed
    timestamp is the signer's, and replaying an old attestation over an old
    bundle is exactly the rollback the freshness rules exist to bound."""
    stale = tmp_path / "stale.revlbundle"
    shutil.copytree(bundle, stale)
    signed = datetime.now(timezone.utc) - timedelta(days=30)
    attestation = deploy.make_deploy_attestation(
        stale, SIGNER_KEY, now=signed, signer="ci")
    (stale / deploy.ATTESTATION_NAME).write_text(
        json.dumps(attestation, sort_keys=True), encoding="utf-8")
    return stale


@pytest.fixture
def keys(tmp_path):
    """The host's own trust files: the signer key it verifies with, and the key
    it signs its verdicts with."""
    signer = tmp_path / "signer.key"
    signer.write_bytes(SIGNER_KEY)
    host = tmp_path / "host.key"
    host.write_bytes(HOST_KEY)
    return signer, host


def _request(bundle_path, challenge=CHALLENGE):
    return deploy.AdmitRequest(bundle=str(bundle_path), backend="python",
                               challenge=challenge).to_wire()


def _run_cli(monkeypatch, argv, request_wire):
    """Parse `argv` exactly as `revl deploy-admit` does, serve ONE request line
    off a replaced stdin, and return the reply the runner wrote."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(["deploy-admit", *argv])
    out = io.StringIO()
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(request_wire) + "\n"))
    with contextlib.redirect_stdout(out):
        rc = deploy.deploy_admit_command(args)
    assert rc == 0, out.getvalue()
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, lines
    # The runner writes the response WIRE dict; parse it the way the conductor
    # does so the receipt under test is the one an exchange would deliver.
    return deploy.AdmitResponse.from_wire(json.loads(lines[0])).receipt


def _trust_argv(keys, *extra):
    signer, host = keys
    return ["--key", str(signer), "--host-key", str(host), *extra]


# ---------------------------------------------------------------------------
# the flags exist at all: the issue's reproduce is `deploy-admit --help`
# ---------------------------------------------------------------------------


def test_the_parser_exposes_every_receiver_rule():
    """Before #3157 the runner's usage line stopped at `--runtime-version`, so
    none of these could be named on the command line at all."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args([
        "deploy-admit",
        "--max-age", "604800",
        "--not-before", "2026-01-01T00:00:00+00:00",
        "--capability-ceiling", "smtp",
        "--capability-ceiling", "fs",
        "--revoked-key", "deadbeef",
        "--recheck-source",
    ])
    assert args.max_age == 604800.0
    assert args.not_before == "2026-01-01T00:00:00+00:00"
    assert args.capability_ceiling == ["smtp", "fs"]
    assert args.revoked_key == ["deadbeef"]
    assert args.recheck_source is True


def test_no_receiver_rule_is_set_by_default():
    """The flags are opt-in: a runner that names none keeps the documented
    behaviour (single trust domain, no receiver policy supplied)."""
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(["deploy-admit"])
    assert args.max_age is None
    assert args.not_before is None
    assert args.capability_ceiling is None
    assert args.revoked_key is None
    assert args.recheck_source is False


# ---------------------------------------------------------------------------
# freshness: a past attested bundle must be refusable
# ---------------------------------------------------------------------------


def test_a_stale_bundle_is_refused_when_the_host_sets_a_freshness_window(
        monkeypatch, keys, stale_bundle):
    receipt = _run_cli(monkeypatch,
                       _trust_argv(keys, "--max-age", "60"),
                       _request(stale_bundle))
    assert receipt["verdict"] == deploy.REFUSE, receipt
    assert receipt["link"] == deploy.LINK_EVIDENCE, receipt
    assert "freshness TTL" in receipt["reason"], receipt

    # The very same bytes, only the flag differs. With no receiver policy the
    # host still admits what the signer signed: the fix is that the host CAN
    # bound it, not that an unconfigured host silently changes behaviour.
    admitted = _run_cli(monkeypatch, _trust_argv(keys), _request(stale_bundle))
    assert admitted["verdict"] == deploy.ACCEPT, admitted


def test_not_before_refuses_evidence_older_than_the_anchor(
        monkeypatch, keys, stale_bundle):
    anchor = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    receipt = _run_cli(monkeypatch,
                       _trust_argv(keys, "--not-before", anchor),
                       _request(stale_bundle))
    assert receipt["verdict"] == deploy.REFUSE, receipt
    assert receipt["link"] == deploy.LINK_EVIDENCE, receipt
    assert "freshness anchor" in receipt["reason"], receipt


def test_a_fresh_bundle_is_admitted_under_the_same_freshness_window(
        monkeypatch, keys, bundle):
    receipt = _run_cli(monkeypatch,
                       _trust_argv(keys, "--max-age", "3600"),
                       _request(bundle))
    assert receipt["verdict"] == deploy.ACCEPT, receipt


def test_a_malformed_not_before_is_rejected_before_serving(
        monkeypatch, keys, capsys):
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["deploy-admit", *_trust_argv(keys, "--not-before", "not-an-instant")])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    rc = deploy.deploy_admit_command(args)
    assert rc == 1
    assert "--not-before" in capsys.readouterr().err


def test_a_negative_max_age_is_rejected_before_serving(
        monkeypatch, keys, capsys):
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["deploy-admit", *_trust_argv(keys, "--max-age", "-5")])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    rc = deploy.deploy_admit_command(args)
    assert rc == 1
    assert "--max-age" in capsys.readouterr().err


@pytest.mark.parametrize("flag,first,second", [
    ("--max-age", "600", "1e9"),
    ("--not-before", "1700000000", "1"),
])
def test_a_repeated_single_value_rule_flag_is_refused_at_startup(
        keys, capsys, flag, first, second):
    """Last-wins would let a later, weaker value (appended after a host-side
    wrapper's own) replace the one the host pinned, so a repeat is a usage
    error that names the flag, never a silent override."""
    from revl.cli.parser import build_parser

    with pytest.raises(SystemExit) as stop:
        build_parser().parse_args(
            ["deploy-admit",
             *_trust_argv(keys, flag, first, flag, second)])
    assert stop.value.code != 0
    err = capsys.readouterr().err
    assert flag in err and "more than once" in err, err


# ---------------------------------------------------------------------------
# capability ceiling: what a pushed composition may reach
# ---------------------------------------------------------------------------


def test_capability_ceiling_refuses_a_bundle_that_reaches_outside_it(
        monkeypatch, keys, bundle):
    """The bundle emits `smtp`; a ceiling of `fs` excludes it. The same bundle
    with a ceiling that includes its surface admits, so the refusal is the
    ceiling and not a broken chain."""
    refused = _run_cli(monkeypatch,
                       _trust_argv(keys, "--capability-ceiling", "fs"),
                       _request(bundle))
    assert refused["verdict"] == deploy.REFUSE, refused
    assert refused["link"] == deploy.LINK_CAPABILITY, refused
    assert "outside" in refused["reason"], refused

    admitted = _run_cli(monkeypatch,
                        _trust_argv(keys, "--capability-ceiling", "smtp"),
                        _request(bundle))
    assert admitted["verdict"] == deploy.ACCEPT, admitted


# ---------------------------------------------------------------------------
# revocation: the signer key itself is named, not only deleted from disk
# ---------------------------------------------------------------------------


def test_revoked_key_refuses_the_signer(monkeypatch, keys, bundle):
    from revl import attest

    key_id = attest.key_id(SIGNER_KEY)
    receipt = _run_cli(monkeypatch,
                       _trust_argv(keys, "--revoked-key", key_id),
                       _request(bundle))
    assert receipt["verdict"] == deploy.REFUSE, receipt
    assert receipt["link"] == deploy.LINK_SIGNER, receipt
    assert "REVOKED" in receipt["reason"], receipt


def test_an_empty_revoked_key_id_is_rejected_before_serving(
        monkeypatch, keys, capsys):
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["deploy-admit", *_trust_argv(keys, "--revoked-key", "")])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    rc = deploy.deploy_admit_command(args)
    assert rc == 1
    assert "--revoked-key" in capsys.readouterr().err


def test_a_revoked_key_id_matching_no_loaded_key_is_rejected_before_serving(
        monkeypatch, keys, capsys):
    """A typo in the id leaves the signer trusted, so it is refused when the
    runner starts instead of admitting the bundle with no signal."""
    from revl import attest
    from revl.cli.parser import build_parser

    args = build_parser().parse_args(
        ["deploy-admit", *_trust_argv(keys, "--revoked-key", "nosuchkey")])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    rc = deploy.deploy_admit_command(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--revoked-key" in err and "nosuchkey" in err, err
    # The message offers the id form the chain check uses.
    assert attest.key_id(SIGNER_KEY) in err, err


# ---------------------------------------------------------------------------
# source re-check: the host measures with its own frontend
# ---------------------------------------------------------------------------


def test_recheck_source_refuses_a_bundle_whose_staged_source_does_not_compile(
        monkeypatch, keys, bundle, tmp_path):
    """`source/` is not part of the signed chain, so a signature over the IR and
    artifact admits while the staged source is broken. Only the receiver's own
    frontend run catches it, which is what `--recheck-source` turns on."""
    tampered = tmp_path / "tampered.revlbundle"
    shutil.copytree(bundle, tampered)
    source = tampered / deploy.SOURCE_ROOT / "mailer.rvl"
    source.write_text(source.read_text(encoding="utf-8")
                      + "\ncomponent { this is not revl\n", encoding="utf-8")

    # without the re-check the same bytes still admit: the signature does not
    # cover the source, so nothing else looks at it
    admitted = _run_cli(monkeypatch, _trust_argv(keys), _request(tampered))
    assert admitted["verdict"] == deploy.ACCEPT, admitted

    refused = _run_cli(monkeypatch,
                       _trust_argv(keys, "--recheck-source"),
                       _request(tampered))
    assert refused["verdict"] == deploy.REFUSE, refused
    assert refused["link"] == deploy.LINK_COMPOSITION, refused
    assert "re-ran its own frontend" in refused["reason"], refused


def test_recheck_source_admits_a_bundle_its_own_frontend_reproduces(
        monkeypatch, keys, bundle):
    receipt = _run_cli(monkeypatch,
                       _trust_argv(keys, "--recheck-source"),
                       _request(bundle))
    assert receipt["verdict"] == deploy.ACCEPT, receipt


# ---------------------------------------------------------------------------
# a `via = ssh` map configures the far runner through its `extra_args`
# ---------------------------------------------------------------------------


def test_ssh_extra_args_accept_the_receiver_rules():
    """The far runner's own flags reach it through the map's `extra_args`
    allowlist; a host operator could not set these on a remote runner otherwise.
    """
    verdict = deploy.admit_deploy_map({"processes": {"svc": {"deploy": {
        "via": "ssh", "host": "deploy@testhost", "known_hosts": "/k",
        "host_key": "/h", "remote_bundle": "/far/app.revlbundle",
        "extra_args": [
            "--key", "/p/key", "--host-key", "/p/key",
            "--max-age", "604800",
            "--not-before", "2026-01-01T00:00:00+00:00",
            "--capability-ceiling", "smtp",
            "--revoked-key", "deadbeef",
            "--recheck-source",
        ]}}}})
    assert verdict.ok, verdict.refusals
