"""Forwards `workflow` to the zeo-integration-hubspot package.

The workflow validator keeps its identity.
This module IS the package's module, so a patch through either import path is
the same patch. Credential discovery, the service and the `hubspot.marketing`
entry point stay in zeocore (zeointegrations-SOW-02).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from zeo_integrations.hubspot import workflow as _forwarded

if TYPE_CHECKING:  # what a type checker sees at the old path
    from zeo_integrations.hubspot.workflow import *  # noqa: F403

sys.modules[__name__] = _forwarded
