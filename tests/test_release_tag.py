from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-release-tag.py"
SPEC = importlib.util.spec_from_file_location("foundation_release_tag", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


@pytest.fixture
def release_tree(tmp_path: Path) -> Path:
    (tmp_path / "VERSION").write_text("3.2.1\n", encoding="utf-8")
    for package in CHECKER.PACKAGES:
        root = tmp_path / "packages" / package
        module = root / "src" / package.replace("-", "_")
        module.mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "3.2.1"\n', encoding="utf-8"
        )
        (module / "__init__.py").write_text(
            'raise RuntimeError("must not import package code")\n__version__ = "3.2.1"\n',
            encoding="utf-8",
        )
    return tmp_path


def test_matching_metadata_does_not_import_packages(release_tree: Path) -> None:
    CHECKER.check_tag(release_tree, "v3.2.1")


@pytest.mark.parametrize("tag", ["3.2.1", "v3.2", "v03.2.1", "v3.2.1-rc.1", "v3.2.1\n", "dev"])
def test_rejects_malformed_tags(release_tree: Path, tag: str) -> None:
    with pytest.raises(ValueError, match="release tag must"):
        CHECKER.check_tag(release_tree, tag)


def test_rejects_tag_version_mismatch(release_tree: Path) -> None:
    with pytest.raises(ValueError, match="VERSION does not match"):
        CHECKER.check_tag(release_tree, "v3.2.2")


@pytest.mark.parametrize("package", CHECKER.PACKAGES)
@pytest.mark.parametrize("target", ["project", "module"])
def test_rejects_each_package_version_drift(release_tree: Path, package: str, target: str) -> None:
    root = release_tree / "packages" / package
    path = root / "pyproject.toml" if target == "project" else root / "src" / package.replace("-", "_") / "__init__.py"
    path.write_text(path.read_text(encoding="utf-8").replace("3.2.1", "3.2.0"), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        CHECKER.check_tag(release_tree, "v3.2.1")


def test_rejects_missing_package(release_tree: Path) -> None:
    (release_tree / "packages/ac-jobs/pyproject.toml").unlink()
    with pytest.raises(ValueError, match="package set mismatch"):
        CHECKER.check_tag(release_tree, "v3.2.1")


def test_cli_reports_error_without_traceback(release_tree: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(release_tree), "--tag", "v9.0.0"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "VERSION does not match" in result.stderr
    assert "Traceback" not in result.stderr
