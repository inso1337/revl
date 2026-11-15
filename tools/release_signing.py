#!/usr/bin/env python3
"""Generate a release signing key and sign a release manifest.

The updater accepts a networked release only when its manifest names a key
pinned in ``src/revl/update/release_keys.py``. This tool is the maintainer side
of that exchange, and it is deliberately small: generate a key, print its
public half for pinning, sign a JSON manifest. It never sends a private scalar
anywhere, and it never prints one.

    tools/release_signing.py keygen \\
        --private-key-out ~/.config/revl/release-key.json
    tools/release_signing.py sign release.json \\
        --key-file ~/.config/revl/release-key.json

The private identity file is the whole value of the protected-environment
secret ``REVL_RELEASE_SIGNING_KEY``. A workflow materialises that secret
to a file outside the checkout and passes its path to ``sign``; the private key
never travels in an argument or a log line.

``keygen`` refuses to write inside the repository unless ``--force`` is given:
a signing key committed by accident is a key that every clone can use, and the
pin then proves nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.peer_identity import (  # noqa: E402
    generate_identity,
    load_private_identity,
    sign_record,
    write_private_identity,
)
from revl.update.signing import RELEASE_DOMAIN  # noqa: E402

#: The default name a release key's identity records. It is metadata, not the
#: key, and the updater pins the public half by fingerprint rather than by it.
DEFAULT_PEER_ID = "revl-release"

#: What tells the maintainer where the private file belongs. Kept in one place
#: so the printed instructions and the documentation cannot drift.
SECRET_NAME = "REVL_RELEASE_SIGNING_KEY"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_instant(value: str, *, field: str) -> datetime:
    """An ISO 8601 instant, with ``Z`` accepted as UTC."""
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{field} is not an ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_instant(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _five_years_after(when: datetime) -> datetime:
    """The same day five years on. A 29 February lands on the 28th."""
    try:
        return when.replace(year=when.year + 5)
    except ValueError:
        return when.replace(year=when.year + 5, day=28)


def _inside_repository(target: Path) -> bool:
    try:
        return target.resolve().is_relative_to(ROOT.resolve())
    except OSError:
        # A path that cannot be resolved cannot be shown to be outside the
        # repository, so treat it as inside and let --force override.
        return True


def keygen(private_key_out: str | Path, *, peer_id: str = DEFAULT_PEER_ID,
           not_before: str | None = None,
           not_after: str | None = None,
           force: bool = False) -> dict:
    """Generate a key, write it at mode 0600 and print its public entry.

    Returns the entry that belongs in :data:`release_keys.RELEASE_KEYS`. The
    private scalar is written only by ``write_private_identity`` and is never
    returned or printed. An existing file or link at the target is refused,
    ``--force`` included, and the file is created with ``O_EXCL``.
    """
    target = Path(private_key_out).expanduser()
    if _inside_repository(target) and not force:
        raise ValueError(
            f"refusing to write a private signing key inside the repository "
            f"({ROOT}); choose a path outside it, or pass --force if you "
            f"really mean to keep a test key here")
    if os.path.lexists(target):
        raise ValueError(
            f"refusing to overwrite {target}: a release key that exists is "
            f"never replaced in place. A lost or retired key is a rotation: "
            f"generate the new key at a new path and pin it beside the old one")
    started = _utc_now()
    not_before_at = (_parse_instant(not_before, field="--not-before")
                     if not_before is not None else started)
    not_after_at = (_parse_instant(not_after, field="--not-after")
                    if not_after is not None else _five_years_after(started))
    if not_before_at > not_after_at:
        raise ValueError(
            f"--not-before {_format_instant(not_before_at)} is later than "
            f"--not-after {_format_instant(not_after_at)}")
    identity = generate_identity(peer_id)
    write_private_identity(target, identity, exclusive=True)
    entry = {
        "keyId": identity.key_id,
        "publicKey": identity.public_key.hex(),
        "notBefore": _format_instant(not_before_at),
        "notAfter": _format_instant(not_after_at),
    }
    print("RELEASE_KEYS entry, ready to paste into "
          "src/revl/update/release_keys.py:")
    print(json.dumps(entry))
    print()
    print(f"private identity: {target} (mode 0600)")
    print("Store the whole private identity file as the protected-environment")
    print(f"secret {SECRET_NAME}. The workflow writes that secret to a")
    print("temporary file outside the checkout and signs with it.")
    print("Never commit it; never paste it into an issue or a log.")
    print(f"key id: {identity.key_id}")
    return entry


def sign_manifest(manifest: str | Path, key_file: str | Path, *,
                  out: str | Path | None = None) -> dict:
    """Sign ``manifest`` and write sorted, indented JSON.

    ``out`` defaults to the manifest itself, which is what a release dry run
    wants; the document stays the same JSON object plus the signature members
    ``sign_record`` adds. Refuses a manifest that is not a JSON object.
    """
    source = Path(manifest)
    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{source} must hold a JSON object, not "
                         f"{type(document).__name__}")
    identity = load_private_identity(key_file)
    signed = sign_record(RELEASE_DOMAIN, document, identity)
    destination = Path(out) if out is not None else source
    destination.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"wrote {destination} (sorted JSON, signature covers every other "
          f"member)")
    print(f"key id: {identity.key_id}")
    return signed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="generate a release signing key or sign a release "
                    "manifest (docs/updates.md)")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser(
        "keygen", help="generate a P-256 release key and print its pin")
    generate.add_argument("--private-key-out", required=True, metavar="PATH",
                          help="where to write the private identity (0600)")
    generate.add_argument("--peer-id", default=DEFAULT_PEER_ID, metavar="NAME",
                          help=f"identity name (default {DEFAULT_PEER_ID})")
    generate.add_argument("--not-before", default=None, metavar="ISO",
                          help="first instant the key carries authority "
                               "(default: now UTC)")
    generate.add_argument("--not-after", default=None, metavar="ISO",
                          help="last instant the key carries authority "
                               "(default: now + 5 years)")
    generate.add_argument("--force", action="store_true",
                          help="allow a private key inside the repository")

    sign = commands.add_parser("sign", help="sign a release manifest")
    sign.add_argument("manifest", metavar="MANIFEST",
                      help="the JSON release manifest to sign")
    sign.add_argument("--key-file", required=True, metavar="PATH",
                      help=f"private identity file (the {SECRET_NAME} value)")
    sign.add_argument("--out", default=None, metavar="PATH",
                      help="where to write the signed manifest (default: "
                           "overwrite MANIFEST)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "keygen":
            keygen(args.private_key_out, peer_id=args.peer_id,
                   not_before=args.not_before, not_after=args.not_after,
                   force=args.force)
        else:
            sign_manifest(args.manifest, args.key_file, out=args.out)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
