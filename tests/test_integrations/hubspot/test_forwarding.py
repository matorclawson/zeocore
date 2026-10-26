"""The old import paths forward to zeo-integration-hubspot and keep every identity.

The Broker patches and subclasses through these paths (zeointegrations-SOW-02), so
the old module must BE the package's module, not a copy of its names.
"""

from __future__ import annotations

import importlib

import pytest

OLD = "zeo_core.integrations.hubspot"
NEW = "zeo_integrations.hubspot"


@pytest.mark.parametrize("name", ["client", "models", "transport", "workflow"])
def test_the_old_submodule_is_the_packages_module(name: str) -> None:
    assert importlib.import_module(f"{OLD}.{name}") is importlib.import_module(
        f"{NEW}.{name}"
    )


def test_the_private_names_the_broker_uses_are_the_same_objects() -> None:
    from zeo_core.integrations.hubspot import client as old
    from zeo_integrations.hubspot import client as new

    assert old.EMAILS is new.EMAILS
    assert old._id is new._id  # noqa: SLF001 -- the compatibility surface under test


@pytest.mark.parametrize(
    "module,names",
    [
        ("models", ["EmailAddress", "EmailContent", "EmailDraft", "Identifier"]),
        ("transport", ["HubSpotAPIError", "HubSpotTransport"]),
        ("client", ["HubSpotClient", "AssetType"]),
    ],
)
def test_public_classes_have_one_identity_through_both_paths(
    module: str, names: list[str]
) -> None:
    old = importlib.import_module(f"{OLD}.{module}")
    new = importlib.import_module(f"{NEW}.{module}")
    for name in names:
        assert getattr(old, name) is getattr(new, name), name


def test_the_package_root_names_are_the_packages_classes() -> None:
    from zeo_core.integrations import hubspot as root
    from zeo_integrations.hubspot import client, models, transport

    assert root.HubSpotClient is client.HubSpotClient
    assert root.EmailDraft is models.EmailDraft
    assert root.HubSpotAPIError is transport.HubSpotAPIError


def test_a_patch_through_the_old_path_is_the_patch_the_client_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zeo_core.integrations.hubspot import client as old
    from zeo_integrations.hubspot import client as new

    monkeypatch.setattr(old, "EMAILS", "/patched")
    assert new.EMAILS == "/patched"


def test_the_service_and_the_entry_point_stay_in_core() -> None:
    from zeo_core.integrations.hubspot import HubSpotIntegration, create_integration

    assert HubSpotIntegration.__module__ == f"{OLD}.service"
    assert create_integration().integration_id == "hubspot.marketing"
