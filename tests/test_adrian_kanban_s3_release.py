"""Release identity and dark-install preflight tests derived from S3 section 6."""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def release_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_name = "s3_adrian_kanban_release"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    modules = {
        name: importlib.import_module(f"{package_name}.release.{name}")
        for name in ("models", "manifest", "preflight")
    }
    modules["versioning"] = importlib.import_module(f"{package_name}.versioning")
    yield modules
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            sys.modules.pop(module_name, None)


def _release_inputs(tmp_path: Path, modules):
    source = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    package_root = (tmp_path / "adrian-kanban-0.2.0").resolve()
    shutil.copytree(
        source,
        package_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    desktop_build = (tmp_path / "Hermes-0.2.0.exe").resolve()
    desktop_build.write_bytes(b"closed-desktop-build")
    database_path = (tmp_path / "kanban.sqlite3").resolve()
    database_path.write_bytes(b"disposable-database")
    profile_names = (
        "builder-tester",
        "default",
        "independent-reviewer",
        "test-authority-reviewer",
    )
    component_names = (
        "backend",
        "desktop",
        "dispatcher",
        "gateway",
        "private-adapter",
    )
    manifest = modules["manifest"].build_release_manifest(
        package_root=package_root,
        desktop_build_path=desktop_build,
        database_path=database_path,
        source_commit="a" * 40,
        profile_names=profile_names,
        component_names=component_names,
        created_at=1_700_000_000,
    )
    return manifest, package_root, desktop_build, database_path


def _matching_preflight(manifest, package_root: Path, database_path: Path):
    versions = json.loads(manifest.versions_json)
    return {
        "profile_package_paths": {
            name: package_root for name in manifest.profile_names
        },
        "component_versions": {
            name: versions for name in manifest.component_names
        },
        "component_database_paths": {
            name: database_path for name in manifest.component_names
        },
        "mutation_authority": "native",
    }


def test_release_manifest_is_canonical_deterministic_and_round_trips(
    tmp_path, release_modules
):
    manifest_module = release_modules["manifest"]
    manifest, package_root, desktop_build, database_path = _release_inputs(
        tmp_path, release_modules
    )
    same = manifest_module.build_release_manifest(
        package_root=package_root,
        desktop_build_path=desktop_build,
        database_path=database_path,
        source_commit="a" * 40,
        profile_names=manifest.profile_names,
        component_names=manifest.component_names,
        created_at=1_700_000_000,
    )

    assert same.payload == manifest.payload
    assert same.digest == manifest.digest
    assert manifest.release_id.startswith("adrian-kanban-0.2.0-")
    manifest_path = (tmp_path / "release-manifest.json").resolve()
    manifest_module.write_release_manifest(manifest, manifest_path)
    assert manifest_path.read_text(encoding="utf-8") == manifest.payload
    assert manifest_module.load_release_manifest(manifest_path) == manifest


def test_dark_preflight_accepts_one_package_database_and_version_set(
    tmp_path, release_modules
):
    manifest, package_root, _, database_path = _release_inputs(
        tmp_path, release_modules
    )
    result = release_modules["preflight"].verify_dark_install(
        manifest=manifest,
        **_matching_preflight(manifest, package_root, database_path),
    )

    assert result.all_passed
    assert result.release_id == manifest.release_id
    assert {finding.code for finding in result.findings} >= {
        "package_digest",
        "desktop_digest",
        "profile_release_identity",
        "component_version_identity",
        "component_database_identity",
        "dark_authority",
    }


@pytest.mark.parametrize(
    ("mutation", "failed_code"),
    [
        ("authority", "dark_authority"),
        ("version", "component_version_identity"),
        ("database", "component_database_identity"),
        ("profile", "profile_release_identity"),
    ],
)
def test_dark_preflight_fails_closed_on_identity_mismatch(
    tmp_path, release_modules, mutation, failed_code
):
    manifest, package_root, _, database_path = _release_inputs(
        tmp_path, release_modules
    )
    values = _matching_preflight(manifest, package_root, database_path)
    if mutation == "authority":
        values["mutation_authority"] = "adrian-kanban"
    elif mutation == "version":
        values["component_versions"]["gateway"] = {
            **values["component_versions"]["gateway"],
            "protocol_version": "wrong",
        }
    elif mutation == "database":
        other = (tmp_path / "other.sqlite3").resolve()
        other.write_bytes(b"other")
        values["component_database_paths"]["dispatcher"] = other
    else:
        other_package = (tmp_path / "other-package").resolve()
        shutil.copytree(package_root, other_package)
        values["profile_package_paths"]["default"] = other_package

    result = release_modules["preflight"].verify_dark_install(
        manifest=manifest, **values
    )
    assert not result.all_passed
    assert any(
        finding.code == failed_code and not finding.passed
        for finding in result.findings
    )


def test_dark_preflight_detects_package_and_desktop_tampering(
    tmp_path, release_modules
):
    manifest, package_root, desktop_build, database_path = _release_inputs(
        tmp_path, release_modules
    )
    (package_root / "versioning.py").write_text("tampered\n", encoding="utf-8")
    desktop_build.write_bytes(b"tampered-desktop")

    result = release_modules["preflight"].verify_dark_install(
        manifest=manifest,
        **_matching_preflight(manifest, package_root, database_path),
    )
    assert not result.all_passed
    assert any(
        finding.code == "package_digest" and not finding.passed
        for finding in result.findings
    )
    assert any(
        finding.code == "desktop_digest" and not finding.passed
        for finding in result.findings
    )


def test_dark_install_plan_never_activates_live_pointer(tmp_path, release_modules):
    manifest, _, _, _ = _release_inputs(tmp_path, release_modules)
    release_root = (tmp_path / "releases").resolve()
    profile_roots = {
        name: (tmp_path / "profiles" / name).resolve()
        for name in manifest.profile_names
    }

    plan = release_modules["preflight"].plan_dark_install(
        manifest=manifest,
        release_root=release_root,
        profile_roots=profile_roots,
    )

    assert plan.activation_permitted is False
    assert plan.mutation_authority == "native"
    assert plan.package_path.parent == release_root
    assert set(plan.profile_candidate_links) == set(manifest.profile_names)
    assert all("dark" in str(path) for path in plan.profile_candidate_links.values())


def test_release_records_reject_malformed_identity_and_noncanonical_findings(
    tmp_path, release_modules
):
    models = release_modules["models"]
    manifest, _, _, _ = _release_inputs(tmp_path, release_modules)

    assert manifest.digest == models.digest_json(manifest.canonical_dict())
    with pytest.raises(models.ReleaseRejected):
        replace(
            manifest,
            versions_json=models.canonical_json({"protocol_version": "2"}),
        )
    with pytest.raises(models.ReleaseRejected):
        replace(manifest, package_root=(tmp_path / "missing-package").resolve())
    with pytest.raises(models.ReleaseRejected):
        models.ReleaseFinding(
            code="noncanonical",
            passed=False,
            expected_json='{"expected": true}',
            observed_json=models.canonical_json({"observed": False}),
            remediation="regenerate the finding with canonical_json",
        )


def test_manifest_loader_rejects_non_list_name_shapes(tmp_path, release_modules):
    models = release_modules["models"]
    manifest, _, _, _ = _release_inputs(tmp_path, release_modules)
    payload = manifest.canonical_dict()
    payload["profile_names"] = "default"

    with pytest.raises(models.ReleaseRejected):
        models.manifest_from_payload(models.canonical_json(payload))


def test_manifest_writer_creates_missing_parent(tmp_path, release_modules):
    manifest, _, _, _ = _release_inputs(tmp_path, release_modules)
    target = (tmp_path / "new" / "nested" / "release.json").resolve()

    release_modules["manifest"].write_release_manifest(manifest, target)

    assert target.read_text(encoding="utf-8") == manifest.payload


def test_dark_plan_rejects_unsorted_candidate_links(tmp_path, release_modules):
    models = release_modules["models"]
    manifest, _, _, _ = _release_inputs(tmp_path, release_modules)
    links = {
        "test-authority-reviewer": (tmp_path / "three").resolve(),
        "default": (tmp_path / "one").resolve(),
        "independent-reviewer": (tmp_path / "two").resolve(),
        "builder-tester": (tmp_path / "four").resolve(),
    }

    with pytest.raises(models.ReleaseRejected):
        models.DarkInstallPlan(
            release_id=manifest.release_id,
            package_path=(tmp_path / "future-package").resolve(),
            profile_candidate_links=links,
            activation_permitted=False,
            mutation_authority="native",
        )
