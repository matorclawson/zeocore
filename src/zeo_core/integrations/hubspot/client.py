"""Forwards `client` to the zeo-integration-hubspot package (zeointegrations-SOW-02).

The client; `EMAILS` and the private `_id` stay importable from here. This module IS the package's module (same object, so the classes keep their
identity and a patch on either import path is the same patch). Credential discovery,
the service and the `hubspot.marketing` entry point stay in zeocore.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from zeo_integrations.hubspot import client as _forwarded

if TYPE_CHECKING:  # what a type checker sees at the old path
    from zeo_integrations.hubspot.client import *  # noqa: F403

sys.modules[__name__] = _forwarded
