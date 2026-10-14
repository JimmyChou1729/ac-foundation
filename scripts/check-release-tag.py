#!/usr/bin/env python3
"""Check a release tag against repository metadata without importing packages."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path


PACKAGES = ("ac-document", "ac-jobs", "ac-llm", "ac-proposer-reviewer")
TAG_PATTERN = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def check_tag(root: Path, tag: str) -> None:
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError("release tag must be vMAJOR.MINOR.PATCH (no leading zeros)")
    version = tag[1:]
    if (root / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError(f"VERSION does not match {tag}")
    observed = {path.parent.name for path in (root / "packages").glob("*/pyproject.toml")}
    if observed != set(PACKAGES):
        raise ValueError(f"package set mismatch: {sorted(observed)}")
    for package in PACKAGES:
        project_path = root / "packages" / package / "pyproject.toml"
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))["project"]
        if project["name"] != package or project["version"] != version:
            raise ValueError(f"{project_path.relative_to(root)} does not match {tag}")
        init_path = project_path.parent / "src" / package.replace("-", "_") / "__init__.py"
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
        versions = [
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        ]
        if versions != [version]:
            raise ValueError(f"{init_path.relative_to(root)} __version__ does not match {tag}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        check_tag(args.root, args.tag)
    except (OSError, ValueError, KeyError, SyntaxError, TypeError) as error:
        print(f"Release tag validation failed: {error}", file=sys.stderr)
        return 1
    print(f"Release metadata matches {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
