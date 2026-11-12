"""The release-signing keys the updater pins (docs/updates.md).

A release manifest is signed by the release workflow's private key and checked
by the updater against one of the public keys in this module. Pinning is the
whole trust decision: a manifest names a ``key_id``, the updater selects the
matching entry here, and the embedded public key in the manifest is only
compared against the pinned one, never used to verify itself.

Each entry is a public key, its fingerprint and the window in which it carries
authority:

    {"keyId": <16 lowercase hex>,
     "publicKey": <128 lowercase hex X||Y P-256>,
     "notBefore": <ISO 8601 UTC>,
     "notAfter": <ISO 8601 UTC>}

The tuple is empty before a maintainer provisions the release key. That is the
correct pre-provisioning state, not a placeholder: with nothing pinned, a
networked manifest cannot be verified and is refused rather than trusted.
"""

from __future__ import annotations

# Provision the release key out of band, then paste its printed entry here:
#
#     python3 tools/release_signing.py keygen --private-key-out /secure/path
#
# Keep the private identity outside the repository (the tool refuses to write
# one inside it unless --force is given). An empty tuple refuses every signed
# manifest, so this file is the one deliberate step between "unsigned releases
# are refused" and "signed releases are accepted".
RELEASE_KEYS: tuple[dict, ...] = ()
