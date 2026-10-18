"""zeocore-SOW-36 slice 1: two distributions registering one integration name
is an error that names each of them, never "the last one wins".

These tests install real (fake) distributions: a ``*.dist-info`` directory with
an ``entry_points.txt`` on ``sys.path``, found by ``importlib.metadata`` exactly
as an installed package would be. Only the factory module is written to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zeo_core.integrations.core import load_integrations
from zeo_core.integrations.core.registry import IntegrationRegistry
from zeo_core.integrations.loader import (
    list_available_entry_points,
    load_enabled_entry_points,
)

GROUP = "zeo_core.integrations"


def _install(
    root: Path, dist: str, version: str, name: str, module: str, integration_id: str
) -> None:
    """A distribution ``dist`` registering ``name`` -> ``module:factory``."""
    (root / f"{module}.py").write_text(
        "class _Integration:\n"
        f"    integration_id = {integration_id!r}\n"
        f"    name = {name!r}\n"
        "    version = '1.0.0'\n"
        "    def initialize(self):\n"
        "        from zeo_core.integrations.core.results import IntegrationResult\n"
        "        return IntegrationResult.success_result(message='ok')\n"
        "    def is_available(self):\n"
        "        return True\n"
        "def factory():\n"
        "    return _Integration()\n"
    )
    info = root / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    )
    (info / "entry_points.txt").write_text(f"[{GROUP}]\n{name} = {module}:factory\n")


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    for module in [m for m in sys.modules if m.startswith("fakeint_")]:
        monkeypatch.delitem(sys.modules, module)
    return tmp_path


def test_one_registration_loads_from_an_installed_distribution(site: Path) -> None:
    _install(site, "zeo-plugin-fakeone", "1.2.3", "fakeone", "fakeint_a", "fakeone")
    registry = IntegrationRegistry()
    report = load_enabled_entry_points(registry, ["fakeone"])
    assert report.success and report.loaded == ["fakeone"]
    row = next(
        r for r in list_available_entry_points() if r["integration_id"] == "fakeone"
    )
    assert row["ambiguous"] == "false"
    assert row["distribution"] == "zeo-plugin-fakeone 1.2.3"


def test_two_distributions_registering_one_name_is_an_error_naming_both(
    site: Path,
) -> None:
    _install(site, "zeo-plugin-dupa", "1.0.0", "dupname", "fakeint_a", "dupname")
    other = site / "second"
    other.mkdir()
    sys.path.insert(0, str(other))
    try:
        _install(other, "zeo-plugin-dupb", "2.0.0", "dupname", "fakeint_b", "dupname")
        registry = IntegrationRegistry()
        report = load_enabled_entry_points(registry, ["dupname"])
        assert not report.success
        assert report.loaded == []
        (message,) = report.errors
        assert "zeo-plugin-dupa 1.0.0" in message
        assert "zeo-plugin-dupb 2.0.0" in message
        assert "dupname" in message
        rows = [
            r for r in list_available_entry_points() if r["integration_id"] == "dupname"
        ]
        assert len(rows) == 2 and {r["ambiguous"] for r in rows} == {"true"}
    finally:
        sys.path.remove(str(other))


def test_the_guard_covers_boot_and_non_strict_mode(site: Path) -> None:
    _install(site, "zeo-plugin-dupa", "1.0.0", "dupname", "fakeint_a", "dupname")
    other = site / "second"
    other.mkdir()
    sys.path.insert(0, str(other))
    try:
        _install(other, "zeo-plugin-dupb", "2.0.0", "dupname", "fakeint_b", "dupname")
        _install(other, "zeo-plugin-fine", "1.0.0", "finename", "fakeint_c", "finename")
        registry = IntegrationRegistry()
        report = load_integrations(
            ["dupname", "finename"], registry=registry, strict=False
        )
        assert not report.success
        assert report.skipped == ["dupname"]
        assert report.loaded == ["finename"]
        assert len(report.errors) == 1
        strict = load_integrations(
            ["dupname", "finename"], registry=IntegrationRegistry()
        )
        assert not strict.success and strict.loaded == []
    finally:
        sys.path.remove(str(other))


def test_an_integration_id_that_differs_from_the_name_is_reported_not_refused(
    site: Path,
) -> None:
    _install(site, "zeo-plugin-gm", "1.0.0", "fake.mail", "fakeint_d", "fakemail")
    report = load_enabled_entry_points(IntegrationRegistry(), ["fake.mail"])
    assert report.success
    assert report.id_mismatches == ["fake.mail -> fakemail"]


def test_the_registry_works_with_no_integration_installed(site: Path) -> None:
    registry = IntegrationRegistry()
    report = load_enabled_entry_points(registry, [], strict=True)
    assert report.success and report.loaded == []
    missing = load_enabled_entry_points(registry, ["nothing-installed"], strict=False)
    assert missing.skipped == ["nothing-installed"]
