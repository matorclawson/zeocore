"""The two signing identities and the designated requirement each must carry.

The audience picks the identity (ZEOCORE-SOW-20 §2, SOW-21 §2). Each is pinned
by its leaf certificate's SHA-1, so another certificate, even one from the same
team or a renewal, is refused rather than substituted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .manifest import Audience


@dataclass(frozen=True)
class Identity:
    audience: Audience
    common_name: str
    certificate_sha1: str  # upper-case hex, as `security find-identity` prints it
    team: str | None


INTERNAL: Final = Identity(
    audience="internal",
    common_name="ZEO Runtime Code Signing",
    certificate_sha1="093E02C58091461DDDC1320EC7F163B9854590B9",
    team=None,
)
DISTRIBUTED: Final = Identity(
    audience="distributed",
    common_name="Developer ID Application: Rodrigo Rivera (4CY8NU24K8)",
    certificate_sha1="18518F308DBD22D522C9E89E627278C81D5BC754",
    team="4CY8NU24K8",
)
IDENTITIES: Final = {INTERNAL.audience: INTERNAL, DISTRIBUTED.audience: DISTRIBUTED}


def for_audience(audience: Audience) -> Identity:
    return IDENTITIES[audience]


def audience_of_certificate(sha1: str) -> Audience | None:
    """Which audience a leaf certificate belongs to, if it is one of ours."""
    for identity in IDENTITIES.values():
        if identity.certificate_sha1 == sha1.upper():
            return identity.audience
    return None


def designated_requirement(identifier: str, identity: Identity) -> str:
    """The requirement as ``codesign -d -r-`` prints it, after ``designated =>``.

    Internal: the identifier and the self-signed certificate's pin. Distributed:
    Apple's Developer ID form, which names the identifier and the team; the
    leaf pin is checked on the extracted certificate as well.
    """
    if identity.team is None:
        pin = identity.certificate_sha1.lower()
        return f'identifier "{identifier}" and certificate root = H"{pin}"'
    return (
        f'identifier "{identifier}" and anchor apple generic and '
        "certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and "
        f'certificate leaf[subject.OU] = "{identity.team}"'
    )


_DESIGNATED: Final = re.compile(r"^(#\s*)?designated => (.+)$")


def parse_designated(output: str) -> tuple[str, bool] | None:
    """The designated requirement and whether it is explicit (not implied).

    ``codesign`` prints an implied requirement, such as an ad-hoc signature's
    bare cdhash, as a comment: ``# designated => cdhash H"..."``.
    """
    for line in output.splitlines():
        match = _DESIGNATED.match(line.strip())
        if match:
            return " ".join(match.group(2).split()), match.group(1) is None
    return None


def names_identity(requirement: str) -> bool:
    """Whether a requirement names both an identifier and a certificate.

    A bare cdhash names neither, so a binary carrying only that is not signed
    in the org's sense (SOW-19 §3).
    """
    return "identifier " in requirement and "certificate " in requirement
