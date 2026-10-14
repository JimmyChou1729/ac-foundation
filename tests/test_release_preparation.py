from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREPARE = (ROOT / "scripts/release-ac-foundation.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def release_tree(root: Path, dependency: str) -> None:
    (root / "VERSION").write_text("2.2.0\n")
    for name in ("ac-document", "ac-jobs", "ac-llm", "ac-proposer-reviewer"):
        package = root / "packages" / name
        module = package / "src" / name.replace("-", "_")
        module.mkdir(parents=True)
        deps = [] if name == "ac-jobs" else [dependency]
        (package / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "2.2.0"\ndependencies = {json.dumps(deps)}\n'
        )
        (module / "__init__.py").write_text('__version__ = "2.2.0"\n')


@pytest.mark.parametrize("version,expected", [("2.2.1", "ac-jobs>=2.2.0,<3"), ("2.3.0", "ac-jobs>=2.2.0,<3"), ("3.0.0", "ac-jobs>=3,<4")])
def test_release_preserves_minor_floor_and_advances_major(tmp_path: Path, version: str, expected: str) -> None:
    release_tree(tmp_path, "ac-jobs>=2.2.0,<3")
    result = subprocess.run([sys.executable, "-c", PREPARE, str(tmp_path), version], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    project = tomllib.loads((tmp_path / "packages/ac-llm/pyproject.toml").read_text())["project"]
    assert project["version"] == version
    assert project["dependencies"] == [expected]
    assert (tmp_path / "VERSION").read_text().strip() == version


@pytest.mark.parametrize("dependency", ["ac-jobs>=1,<3", "ac-jobs>=2.3.0,<3", "ac-jobs>=2.2.0,<4", "ac-jobs==2.2.0"])
def test_release_rejects_incompatible_floor_without_writes(tmp_path: Path, dependency: str) -> None:
    release_tree(tmp_path, dependency)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = subprocess.run([sys.executable, "-c", PREPARE, str(tmp_path), "2.3.0"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "compatible major-bounded floor" in result.stderr
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
