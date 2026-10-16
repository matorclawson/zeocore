"""
The reviewed tracker, footer and hidden-text data for newsletter cleaning.

It is package data (``data/newsletter_noise.json``), versioned with the code and
read only from the installed package, never from the caller's directory. zeonewsroom
extends it by small data-only pull requests, each with the message that showed the
pattern and no sender content copied.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

from zeo_core.integrations.pandoc.refusals import refusal


@dataclass(frozen=True, slots=True)
class Noise:
    """The validated data file."""

    pixel_hosts: tuple[str, ...]
    opaque_hosts: tuple[str, ...]
    redirectors: tuple[tuple[str, str, str], ...]
    tracking_params: frozenset[str]
    per_recipient_params: frozenset[str]
    footer_patterns: tuple[re.Pattern[str], ...]
    hidden_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    form_tags: tuple[str, ...]
    version: str
    sha256: str


@cache
def load() -> Noise:
    """The package's data, validated; a bad file is a typed refusal."""
    try:
        raw = (
            resources.files("zeo_core.integrations.pandoc")
            .joinpath("data", "newsletter_noise.json")
            .read_bytes()
        )
        data = json.loads(raw)
        if data["schema_version"] != 1:
            raise ValueError("schema_version")
        return Noise(
            pixel_hosts=tuple(str(h).lower() for h in data["pixel_hosts"]),
            opaque_hosts=tuple(str(h).lower() for h in data["opaque_redirect_hosts"]),
            redirectors=tuple(
                (str(r["host"]).lower(), str(r["path"]), str(r["param"]))
                for r in data["redirectors"]
            ),
            tracking_params=frozenset(str(p).lower() for p in data["tracking_params"]),
            per_recipient_params=frozenset(
                str(p).lower() for p in data["per_recipient_params"]
            ),
            footer_patterns=tuple(
                re.compile(str(p), re.IGNORECASE) for p in data["footer_patterns"]
            ),
            hidden_patterns=tuple(
                (str(h["kind"]), re.compile(str(h["pattern"]), re.IGNORECASE))
                for h in data["hidden_style_patterns"]
            ),
            form_tags=tuple(str(t).lower() for t in data["form_tags"]),
            version=str(data["version"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        raise refusal("data_invalid") from error
