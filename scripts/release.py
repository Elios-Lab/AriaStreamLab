#!/usr/bin/env python3
"""Bump the version, date the changelog section, commit and tag — one command per change.

    python scripts/release.py patch            # 0.2.1 -> 0.2.2
    python scripts/release.py minor            # 0.2.1 -> 0.3.0
    python scripts/release.py 0.4.0            # an explicit version
    python scripts/release.py patch --no-tag   # bump and commit only

What it does, in order:

1. Refuses to run unless it can release cleanly: on the `main` branch, level with
   `origin/main` (nothing to pull, nothing unpushed), with no uncommitted changes other than
   CHANGELOG.md and pyproject.toml, to a version higher than the current one, whose tag
   exists neither here nor on `origin`.
2. Replaces `version = "…"` in pyproject.toml.
3. In CHANGELOG.md, turns the `## Unreleased` section into `## X.Y.Z — YYYY-MM-DD`
   (it must exist and must not be empty — write the notes first).
4. Commits "X.Y.Z" and creates the annotated tag vX.Y.Z. If the commit fails (a hook, no
   identity), both files are put back as they were.

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
BRANCH = "main"
REMOTE = "origin"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def git_ok(*args: str) -> subprocess.CompletedProcess[str]:
    """``git`` that may fail (the caller looks at the return code)."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def parse(version: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"not an X.Y.Z version: {version!r}")
    major, minor, patch = (int(x) for x in version.split("."))
    return major, minor, patch


def check_branch() -> None:
    """On BRANCH and level with REMOTE/BRANCH, which is what the workflow will build."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != BRANCH:
        raise SystemExit(
            f"releases are cut from {BRANCH!r}; this is {branch!r} — merge it first"
            if branch != "HEAD"
            else f"HEAD is detached; check out {BRANCH!r} first"
        )
    if git_ok("remote", "get-url", REMOTE).returncode != 0:
        print(f"note: no {REMOTE!r} remote; not checking that {BRANCH} is pushed")
        return
    fetched = git_ok("fetch", "--quiet", REMOTE, BRANCH)
    if fetched.returncode != 0:
        raise SystemExit(
            f"cannot fetch {REMOTE}/{BRANCH} to compare: {fetched.stderr.strip()}"
            "\n(offline? releasing needs the push anyway)"
        )
    behind, ahead = (
        int(n)
        for n in git("rev-list", "--left-right", "--count", f"{REMOTE}/{BRANCH}...HEAD").split()
    )
    if behind:
        raise SystemExit(f"{BRANCH} is {behind} commit(s) behind {REMOTE}/{BRANCH}: pull first")
    if ahead:
        raise SystemExit(
            f"{BRANCH} has {ahead} unpushed commit(s): push them and let CI pass first, so "
            "the release is a commit CI has already seen"
        )


def tag_exists(tag: str) -> str | None:
    """Where ``tag`` already exists (``"here"`` or the remote), ``None`` when nowhere."""
    if git("tag", "-l", tag).strip():
        return "here"
    if git_ok("remote", "get-url", REMOTE).returncode != 0:
        return None
    remote = git_ok("ls-remote", "--tags", REMOTE, f"refs/tags/{tag}")
    if remote.returncode != 0:
        raise SystemExit(f"cannot list the tags of {REMOTE}: {remote.stderr.strip()}")
    return REMOTE if remote.stdout.strip() else None


def current_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    if m is None:
        raise SystemExit("no version in pyproject.toml")
    return m.group(1)


def bump(version: str, part: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", part):
        if parse(part) <= parse(version):
            raise SystemExit(f"{part} is not higher than the current version {version}")
        return part
    major, minor, patch = parse(version)
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
    check_branch()

    old = current_version()
    new = bump(old, part)
    where = tag_exists(f"v{new}")
    if where is not None:
        raise SystemExit(f"tag v{new} already exists ({where})")

    changelog = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(r"^## Unreleased\s*\n(.*?)(?=^## |\Z)", changelog, re.S | re.M)
    if m is None or not m.group(1).strip():
        raise SystemExit(
            "CHANGELOG.md needs a non-empty '## Unreleased' section describing the change"
        )
    today = dt.date.today().isoformat()
    new_changelog = (
        changelog[: m.start()]
        + f"## {new} — {today}\n\n"
        + m.group(1).strip()
        + "\n\n"
        + changelog[m.end() :]
    )
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    new_pyproject = re.sub(
        r'^version\s*=\s*"[^"]+"', f'version = "{new}"', pyproject, count=1, flags=re.M
    )

    CHANGELOG.write_text(new_changelog, encoding="utf-8")
    PYPROJECT.write_text(new_pyproject, encoding="utf-8")
    try:
        git("add", "CHANGELOG.md", "pyproject.toml")
        git("commit", "-q", "-m", f"{new}")
    except subprocess.CalledProcessError as exc:  # leave no half-applied version bump behind
        CHANGELOG.write_text(changelog, encoding="utf-8")
        PYPROJECT.write_text(pyproject, encoding="utf-8")
        git_ok("reset", "-q", "--", "CHANGELOG.md", "pyproject.toml")
        raise SystemExit(
            f"the commit failed, nothing was changed: {(exc.stderr or exc.stdout).strip()}"
        ) from exc
    if tag:
        git("tag", "-a", f"v{new}", "-m", f"AriaStreamLab {new}")
    print(
        f"{old} -> {new}"
        + (f", tag v{new}" if tag else "")
        + f"\n  git push {REMOTE} {BRANCH}"
        + (f" v{new}" if tag else "")
        + "\n  pip install -e ."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
