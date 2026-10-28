#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md (the GitHub release notes).

    python scripts/release_notes.py 0.2.0      # the "## 0.2.0 — …" section, heading excluded
    python scripts/release_notes.py --version  # the version in pyproject.toml

Standard library only, so the release workflow can run it with the runner's Python.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if m is None:
        raise SystemExit("no version in pyproject.toml")
    return m.group(1)


def section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(rf"^## {re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)", re.S | re.M)
    m = pattern.search(text)
    if m is None:
        raise SystemExit(f"CHANGELOG.md has no '## {version}' section")
    return m.group(1).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--version":
        print(pyproject_version())
    else:
        sys.stdout.write(section(argv[0].lstrip("v")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
