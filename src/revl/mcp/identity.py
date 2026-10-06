"""The identity of the compiler answering an MCP session (issue #2007).

A client can pin a revision, drive a server, and have no way to tell that the
server is a *different* compiler: `revl_state` and the `initialize` result
carried nothing that identified the process that answered. That assumption
failed in practice — a session drove a server at the pin while reading source
from main, and spent real effort reconciling behaviour that differed by 4,000+
commits. A tool that answers from the wrong tree is indistinguishable from one
that answers correctly unless it says which tree.

Two fields, and they answer different questions:

* `revision` — `git rev-parse HEAD` of the checkout the package was imported
  from, read by `cert.tree_commit` (the reader the certificate path already
  uses), or a build id `revl-<version>` when the package is not a checkout at
  all (an installed wheel, an exported tarball). The issue names this field
  `revision`; it is spelled exactly that on the wire.
* `source_digest` — sha256 over the compiler's own `*.py`, by path. The
  revision says *which commit*, the digest says *which bytes*: a checkout at
  the pinned commit whose working tree has been edited reports the pinned
  revision and a digest the pin does not name.

Both are computed once per package root and cached: the code that answered a
call is the code imported at start-up, so the identity describes the running
process, not the disk as it changes underneath it.

`assert_identity` is the client half. It turns a pin mismatch into a hard
error naming both values, because a warning is exactly what the wrong-tree
session would have scrolled past.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..cert import sha256_bytes, tree_commit
from ..errors import RevlError

#: Bumped when the digest recipe changes, so two different recipes can never
#: produce the same digest for the same tree.
DIGEST_RECIPE = "revl-mcp-source-1"

#: `{resolved root: identity}`, computed on first use. The process's identity
#: is fixed at import, so a cache cannot go stale within a run.
_CACHE: dict[Path, dict] = {}

#: Where a *modern* result carries its `serverInfo`: `server/discover` puts it
#: nowhere else, and every HTTP result carries it there too. Spelled out here
#: rather than imported from `http_transport`, so that a stdio server does not
#: pull the HTTP transport (`http.server`, `ssl`, `select`) in to read one
#: string; the test suite pins the two spellings equal instead.
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"


class IdentityMismatch(RevlError):
    """The server is not the compiler the caller pinned (issue #2007)."""


def package_root() -> Path:
    """The directory the `revl` package was imported from."""
    return Path(__file__).resolve().parent.parent


def build_id() -> str:
    """What to report when there is no commit to report: the package's own
    version. A revision a client can still compare, never a blank."""
    try:
        from importlib.metadata import version  # noqa: PLC0415 - optional

        return f"revl-{version('revl')}"
    except Exception:  # noqa: BLE001 — a package with no metadata still answers
        return "revl-unknown"


def revision(root: Path | None = None) -> str:
    """The commit the answering compiler was read at, or a build id when the
    package is not a checkout."""
    return tree_commit(_root(root)) or build_id()


def source_digest(root: Path | None = None) -> str:
    """The sha256 of the compiler's own source, hex.

    Every `*.py` under `root`, in path order, by path AND by content: a file
    renamed, added or edited is a different digest, and so is a file that is
    not there. `__pycache__` is not source and is skipped, so a compiled
    checkout and a clean one of the same revision agree.
    """
    root = _root(root)
    files = sorted(path for path in root.rglob("*.py")
                   if "__pycache__" not in path.parts)
    digest = hashlib.sha256(f"{DIGEST_RECIPE}\0{len(files)}\0".encode("utf-8"))
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_bytes(path.read_bytes()).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def identity(root: Path | None = None) -> dict:
    """`{"revision", "source_digest"}` for the package at `root` (default: the
    running one), computed once per root."""
    root = _root(root)
    cached = _CACHE.get(root)
    if cached is None:
        cached = {"revision": revision(root), "source_digest": source_digest(root)}
        _CACHE[root] = cached
    return dict(cached)


def reported_identity(reported) -> dict:
    """The identity block out of whatever payload a client has in hand.

    Every shape that carries it: an `initialize` result (its `serverInfo`), a
    `serverInfo` block itself, a `revl_state` payload (the fields at the top
    level), and a `server/discover` result — which carries `serverInfo` under
    `_meta` and nowhere else, so a client that pinched `serverInfo` off the top
    level alone read nothing and reported the pinned server as a stranger. A
    whole JSON-RPC response is unwrapped to its `result` first, since that is
    what a client off the HTTP transport holds.
    """
    found = _server_info_in(reported)
    if found is None and isinstance(reported, dict) and "jsonrpc" in reported:
        found = _server_info_in(reported.get("result"))
    if found is not None:
        return found
    return reported if isinstance(reported, dict) else {}


def _server_info_in(payload) -> dict | None:
    """The `serverInfo` block of one result: at the top level, or in `_meta`."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("serverInfo"), dict):
        return payload["serverInfo"]
    meta = payload.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get(META_SERVER_INFO), dict):
        return meta[META_SERVER_INFO]
    return None


def assert_identity(reported, *, revision: str,
                    source_digest: str | None = None) -> dict:
    """The server's identity, or `IdentityMismatch` naming both values.

    `reported` is any payload carrying the identity (`initialize` result, a
    `server/discover` result, `serverInfo`, `revl_state`). A field the caller
    did not pin — a `source_digest` left as None — is not compared, so pinning
    the revision alone is enough to catch a server from another tree.
    """
    got = reported_identity(reported)
    got_revision = got.get("revision")
    got_digest = got.get("source_digest")
    wrong = []
    if got_revision != revision:
        wrong.append(
            f"revision: pinned {revision!r}, server reports {got_revision!r}")
    if source_digest is not None and got_digest != source_digest:
        wrong.append(
            f"source_digest: pinned {source_digest!r}, "
            f"server reports {got_digest!r}")
    if wrong:
        raise IdentityMismatch(
            "<mcp>", 0,
            "the server is not the compiler you pinned — " + "; ".join(wrong),
            hint=("Every tool this server answers comes from ITS tree, so its "
                  "answers are not answers about the revision you pinned. "
                  "Point the client at the pinned checkout, or re-pin to "
                  f"{got_revision!r}."),
            code="MCP-IDENTITY")
    return got


def _root(root: Path | None) -> Path:
    return Path(root).resolve() if root is not None else package_root()
