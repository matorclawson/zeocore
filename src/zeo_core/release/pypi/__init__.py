"""The PyPI target adapter for ``pypi:zeocore`` (ZEOCORE-SOW-26).

See ``stages``. Only ``apply`` has an effect: it creates the release tag.
"""

from .reads import GitHubCli, PyPIHttp, TargetUnreadableError
from .stages import (
    OPERATOR_REVIEWER,
    ManifestError,
    Outcome,
    PyPIManifest,
    admit,
    apply,
    load,
    plan,
    reconcile,
    verify,
)

__all__ = [
    "OPERATOR_REVIEWER",
    "GitHubCli",
    "ManifestError",
    "Outcome",
    "PyPIHttp",
    "PyPIManifest",
    "TargetUnreadableError",
    "admit",
    "apply",
    "load",
    "plan",
    "reconcile",
    "verify",
]
