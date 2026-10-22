"""The Setup page's model: environment, packages, the command runner, the doctor checks."""

from __future__ import annotations

import sys
import time

import pytest

from ariastreamlab.dashboard import setup as model


def test_environment_and_packages() -> None:
    env = model.environment()
    assert env["executable"] == sys.executable and env["ariastreamlab"]
    assert isinstance(env["sdk_compatible"], bool)
    if env["sdk_compatible"]:
        assert env["sdk_python_note"] == ""
    else:
        assert "wheels" in env["sdk_python_note"]
    pk = {p.name: p for p in model.packages()}
    assert pk["numpy"].installed and pk["numpy"].required
    assert pk["nicegui"].installed
    assert pk["projectaria_client_sdk"].required is False
    assert "projectaria-tools" in pk["projectaria_tools"].pip
    argv = model.pip_install_argv(["projectaria-tools~=2.3"], upgrade=True)
    assert argv[:4] == [sys.executable, "-m", "pip", "install"] and "--upgrade" in argv


def test_actions_build_their_commands(monkeypatch) -> None:
    keys = [a.key for a in model.ACTIONS]
    assert "install_sdk" in keys and "auth_pair" in keys and "hotspot_start" in keys
    by_key = {a.key: a for a in model.ACTIONS}
    assert by_key["install_sdk"].argv()[-2:] == [
        "projectaria-client-sdk~=2.5",
        "projectaria-tools~=2.3",
    ]
    assert by_key["install_certs"].confirm and by_key["hotspot_start"].confirm
    monkeypatch.setattr(model.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="aria_gen2"):
        by_key["device_list"].argv()
    monkeypatch.setattr(model.shutil, "which", lambda name: "/usr/local/bin/aria_gen2")
    assert by_key["hotspot_start"].argv() == [
        "/usr/local/bin/aria_gen2",
        "device",
        "hotspot",
        "start",
        "--wifi-band",
        "BAND_5GHZ",
        "--channel-bandwidth",
        "BW_40MHZ",
    ]


def test_command_run_streams_output_and_reports_exit() -> None:
    run = model.CommandRun(
        [
            sys.executable,
            "-c",
            "import sys,time; print('one'); time.sleep(0.2); print('two', file=sys.stderr); sys.exit(3)",
        ]
    ).start(timeout_s=10)
    deadline = time.monotonic() + 5
    while run.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not run.running and run.returncode == 3 and not run.ok
    assert run.lines == ["one", "two"] and run.text() == "one\ntwo"
    slow = model.CommandRun(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"]
    ).start(timeout_s=0.5)
    deadline = time.monotonic() + 5
    while slow.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not slow.running and slow.error is not None and "timed out" in slow.error
    cancelled = model.CommandRun([sys.executable, "-c", "import time; time.sleep(30)"]).start()
    time.sleep(0.3)
    cancelled.cancel()
    deadline = time.monotonic() + 5
    while cancelled.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not cancelled.running and cancelled.returncode not in (None, 0)
    bad = model.CommandRun(["/nonexistent/command"]).start()
    deadline = time.monotonic() + 5
    while bad.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert bad.error is not None and "FileNotFoundError" in bad.error


def test_doctor_checks_are_available_to_the_page() -> None:
    result = model.checks()
    names = [c.name for c in result]
    assert "python" in names and "ariastreamlab" in names and "nicegui (dashboard)" in names
    assert all(c.status in ("ok", "warn", "fail", "skip") for c in result)
