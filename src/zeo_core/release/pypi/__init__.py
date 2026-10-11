"""The PyPI target adapter (ZEOCORE-SOW-26), for each reviewed target.

See ``stages``. ``apply`` creates the release tag as the ``zeo-release`` App;
``approve`` approves its waiting deployment as matorclawson, once. Nothing
else has an effect.
"""

from .app import ReleaseApp
from .reads import GitHubCli, PyPIHttp, TargetUnreadableError
from .stages import (
    ManifestError,
    Outcome,
    PyPIManifest,
    TagCreator,
    admit,
    apply,
    approve,
    load,
    plan,
    reconcile,
    verify,
)
from .targets import APPROVER, RELEASE_APP_SLUG, TARGETS, Target

__all__ = [
    "APPROVER",
    "RELEASE_APP_SLUG",
    "TARGETS",
    "GitHubCli",
    "ManifestError",
    "Outcome",
    "PyPIHttp",
    "PyPIManifest",
    "ReleaseApp",
    "TagCreator",
    "Target",
    "TargetUnreadableError",
    "admit",
    "apply",
    "approve",
    "load",
    "plan",
    "reconcile",
    "verify",
]
