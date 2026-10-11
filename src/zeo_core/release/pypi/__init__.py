"""The PyPI target adapter for ``pypi:zeocore`` (ZEOCORE-SOW-26).

See ``stages``. The stages built here have no effect; ``apply``, the one
effect, follows the design's review.
"""

from .reads import GitHubCli, PyPIHttp, TargetUnreadableError
from .stages import (
    OPERATOR_REVIEWER,
    ManifestError,
    Outcome,
    PyPIManifest,
    admit,
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
    "load",
    "plan",
    "reconcile",
    "verify",
]
