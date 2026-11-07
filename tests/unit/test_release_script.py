"""scripts/release.py against throwaway repositories (a bare ``origin`` and a clone)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release.py"
CHANGELOG = "# Changelog\n\n## Unreleased\n\n- a fix\n\n## 0.1.0 — 2026-01-01\n\n- first\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A clone of a bare origin, on main, level with it; and the script pointed at it."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))  # no signing, hooks or aliases
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in {"NAME": "Test", "EMAIL": "test@example.invalid"}.items():
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", v)
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", v)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    (work / "CHANGELOG.md").write_text(CHANGELOG)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "start")
    _git(work, "push", "-q", "origin", "main")
    spec = importlib.util.spec_from_file_location("release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", work)
    monkeypatch.setattr(mod, "PYPROJECT", work / "pyproject.toml")
    monkeypatch.setattr(mod, "CHANGELOG", work / "CHANGELOG.md")
    return mod, work, origin


def test_a_release_bumps_dates_commits_and_tags(repo: Any) -> None:
    mod, work, _ = repo
    assert mod.main(["patch"]) == 0
    assert 'version = "0.1.1"' in (work / "pyproject.toml").read_text()
    assert "## 0.1.1 — " in (work / "CHANGELOG.md").read_text()
    assert _git(work, "log", "-1", "--format=%s").strip() == "0.1.1"
    assert _git(work, "tag", "-l").split() == ["v0.1.1"]


def test_only_from_main(repo: Any) -> None:
    mod, work, _ = repo
    _git(work, "checkout", "-q", "-b", "feature")
    with pytest.raises(SystemExit, match="cut from 'main'"):
        mod.main(["patch"])  # before: tagged the feature branch, printed `push origin main`


def test_not_with_unpushed_commits(repo: Any) -> None:
    mod, work, _ = repo
    (work / "a.txt").write_text("x")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "local only")
    with pytest.raises(SystemExit, match="1 unpushed commit"):
        mod.main(["patch"])


def test_not_when_behind_origin(repo: Any, tmp_path: Path) -> None:
    mod, _, origin = repo
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "b.txt").write_text("y")
    _git(other, "add", "b.txt")
    _git(other, "commit", "-q", "-m", "someone else")
    _git(other, "push", "-q", "origin", "main")
    with pytest.raises(SystemExit, match="behind"):
        mod.main(["patch"])


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9"])
def test_the_version_only_goes_up(repo: Any, version: str) -> None:
    mod, _, _ = repo
    with pytest.raises(SystemExit, match="not higher"):
        mod.main([version])


def test_a_tag_that_exists_only_on_origin_is_refused(repo: Any, tmp_path: Path) -> None:
    mod, _, origin = repo
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "tag", "v0.1.1")
    _git(other, "push", "-q", "origin", "v0.1.1")
    with pytest.raises(SystemExit, match=r"v0\.1\.1 already exists \(origin\)"):
        mod.main(["patch"])  # before: only local tags were checked


def test_a_failed_commit_leaves_nothing_half_done(repo: Any) -> None:
    mod, work, _ = repo
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(SystemExit, match="hook says no"):
        mod.main(["patch"])
    assert 'version = "0.1.0"' in (work / "pyproject.toml").read_text()
    assert (work / "CHANGELOG.md").read_text() == CHANGELOG
    assert _git(work, "status", "--porcelain") == ""  # nothing staged or modified
    assert _git(work, "tag", "-l") == ""


def test_without_a_remote_it_still_releases(repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    mod, work, _ = repo
    _git(work, "remote", "remove", "origin")
    assert mod.main(["minor"]) == 0
    assert "no 'origin' remote" in capsys.readouterr().out
    assert _git(work, "tag", "-l").split() == ["v0.2.0"]
