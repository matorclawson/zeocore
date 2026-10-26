"""Forwards `transport` to the zeo-integration-hubspot package (zeointegrations-SOW-02).

The transport and `HubSpotAPIError`. This module IS the package's module (same object, so the classes keep their
identity and a patch on either import path is the same patch). Credential discovery,
the service and the `hubspot.marketing` entry point stay in zeocore.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from zeo_integrations.hubspot import transport as _forwarded

if TYPE_CHECKING:  # what a type checker sees at the old path
    from zeo_integrations.hubspot.transport import *  # noqa: F403

sys.modules[__name__] = _forwarded
