"""Supported, separately versioned Python filesystem host lifecycle APIs.

Bind before Session.load or any filesystem guard use. This configures only the
Python stdlib filesystem runtime, not arbitrary host code or sandbox grants.
Committed sidecar cleanup is trusted-host-only and requires exclusive metadata
write ownership and durable actual commit acknowledgment. See docs/witnessed-fs.md
for the lifetime, cleanup concurrency contract, and recovery limits.
"""

from importlib import import_module
from pathlib import Path
import sys

from ._paths import backends_root

_backend = backends_root() / "python"
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_runtime = import_module("revl_fs_workspace")
if Path(_runtime.__file__).resolve() != (_backend / "revl_fs_workspace.py").resolve():
    raise ImportError("filesystem runtime belongs to a different Revl installation")

PINNED_ROOT_API_VERSION = _runtime.PINNED_ROOT_API_VERSION
bind_workspace_root = _runtime.bind_workspace_root
COMMITTED_SIDECAR_API_VERSION = _runtime.COMMITTED_SIDECAR_API_VERSION
finalize_committed_sidecar = _runtime.finalize_committed_sidecar
FsOpError = _runtime.FsOpError
ConfinementError = _runtime.ConfinementError

__all__ = [
    "PINNED_ROOT_API_VERSION", "bind_workspace_root",
    "COMMITTED_SIDECAR_API_VERSION", "finalize_committed_sidecar",
    "FsOpError", "ConfinementError",
]
