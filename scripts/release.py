#!/usr/bin/env python3
"""Bump the version, date the changelog section, commit and tag — one command per change.

    python scripts/release.py patch            # 0.2.1 -> 0.2.2
    python scripts/release.py minor            # 0.2.1 -> 0.3.0
    python scripts/release.py 0.4.0            # an explicit version
    python scripts/release.py patch --no-tag   # bump and commit only

What it does, in order:

1. Refuses to run with uncommitted changes other than CHANGELOG.md and pyproject.toml.
2. Replaces `version = "…"` in pyproject.toml.
3. In CHANGELOG.md, turns the `## Unreleased` section into `## X.Y.Z — YYYY-MM-DD`
   (it must exist and must not be empty — write the notes first).
4. Commits "X.Y.Z" and creates the annotated tag vX.Y.Z.

Then `git push origin main vX.Y.Z` publishes it: the release workflow runs the CI suite on
the tag and, when it passes, builds the wheel and creates the GitHub release from that
changelog section. After the bump, reinstall the
package in your environment (`pip install -e .`) so that `ariastreamlab --version` — and
every session manifest — reports the new number: an editable install records the version
at install time.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def current_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    if m is None:
        raise SystemExit("no version in pyproject.toml")
    return m.group(1)


def bump(version: str, part: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", part):
        return part
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unknown part {part!r}: major, minor, patch or X.Y.Z")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    part = argv[0]
    tag = "--no-tag" not in argv

    dirty = [
        line[3:]
        for line in git("status", "--porcelain").splitlines()
        if line[3:] not in ("CHANGELOG.md", "pyproject.toml") and not line.startswith("??")
    ]
    if dirty:
        raise SystemExit("commit or stash these first: " + ", ".join(dirty))

    old = current_version()
    new = bump(old, part)
    if git("tag", "-l", f"v{new}").strip():
        raise SystemExit(f"tag v{new} already exists")

    changelog = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(r"^## Unreleased\s*\n(.*?)(?=^## |\Z)", changelog, re.S | re.M)
    if m is None or not m.group(1).strip():
        raise SystemExit(
            "CHANGELOG.md needs a non-empty '## Unreleased' section describing the change"
        )
    today = dt.date.today().isoformat()
    changelog = (
        changelog[: m.start()]
        + f"## {new} — {today}\n\n"
        + m.group(1).strip()
        + "\n\n"
        + changelog[m.end() :]
    )
    CHANGELOG.write_text(changelog, encoding="utf-8")

    text = PYPROJECT.read_text(encoding="utf-8")
    text = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{new}"', text, count=1, flags=re.M)
    PYPROJECT.write_text(text, encoding="utf-8")

    git("add", "CHANGELOG.md", "pyproject.toml")
    git("commit", "-q", "-m", f"{new}")
    if tag:
        git("tag", "-a", f"v{new}", "-m", f"AriaStreamLab {new}")
    print(
        f"{old} -> {new}"
        + (f", tag v{new}" if tag else "")
        + f"\n  git push origin main v{new}\n  pip install -e ."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
