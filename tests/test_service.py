"""Tests of `fugue service` with launchctl and the page replaced by fakes, so nothing is installed."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys

import pytest

from fugue import cli, service


class FakeLaunchd:
    """Records launchctl calls; the service counts as loaded between bootstrap and bootout."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False
        self.serving = False  # whether the page answers

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        out, code = "", 0
        if args[0] == "bootstrap":
            self.loaded = self.serving = True
        elif args[0] == "bootout":
            self.loaded = self.serving = False
        elif args[0] == "print":
            if self.loaded:
                out = f"{args[1]} = {{\n\tstate = running\n\tpid = 4242\n\tlast exit code = (never exited)\n}}\n"
            else:
                code = 113
        return subprocess.CompletedProcess(["launchctl", *args], code, out, "")


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FUGUE_HOME", str(tmp_path / "fugue"))
    monkeypatch.setenv("PATH", os.pathsep.join(["/opt/homebrew/bin", "/usr/bin", "/opt/homebrew/bin", "/bin"]))
    monkeypatch.setenv("FUGUE_CLAUDE_BIN", "/bin/echo")
    monkeypatch.setenv("FUGUE_CODEX_BIN", "/bin/echo")
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchd()
    monkeypatch.setattr(service, "launchctl", fake)
    monkeypatch.setattr(service, "page_answers", lambda port: fake.serving)
    monkeypatch.setattr(service, "port_in_use", lambda port: fake.serving)
    monkeypatch.setattr(service, "protected_folder", lambda path: None)
    return fake


def installed_plist() -> dict:
    with open(service.plist_path(), "rb") as handle:
        return plistlib.load(handle)


def test_install_writes_a_launch_agent_and_starts_it(launchd, tmp_path):
    assert cli.main(["service", "install", "--port", "8777"]) == 0
    plist = installed_plist()
    assert service.plist_path() == tmp_path / "user/Library/LaunchAgents/ai.fugue.web.plist"
    assert plist["Label"] == "ai.fugue.web"
    assert plist["ProgramArguments"][-4:] == ["web", "--no-open", "--port", "8777"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["EnvironmentVariables"]["PATH"] == os.pathsep.join(["/opt/homebrew/bin", "/usr/bin", "/bin"])
    assert plist["EnvironmentVariables"]["FUGUE_HOME"] == str(tmp_path / "fugue")
    assert plist["EnvironmentVariables"]["HOME"] == str(tmp_path / "user")
    assert plist["EnvironmentVariables"]["LANG"].endswith("UTF-8")
    assert plist["StandardErrorPath"] == str(tmp_path / "fugue/web.log")
    assert plist["WorkingDirectory"] == str(tmp_path / "user")
    assert ("bootstrap", f"gui/{os.getuid()}", str(service.plist_path())) in launchd.calls
    assert service.installed_port() == 8777


def test_install_again_replaces_the_running_one(launchd):
    assert cli.main(["service", "install"]) == 0
    assert cli.main(["service", "install", "--port", "8778"]) == 0
    assert [call[0] for call in launchd.calls if call[0] in ("bootstrap", "bootout")] == \
        ["bootstrap", "bootout", "bootstrap"]
    assert service.installed_port() == 8778


def test_install_refuses_a_port_in_use(launchd, monkeypatch, capsys):
    monkeypatch.setattr(service, "port_in_use", lambda port: True)
    assert cli.main(["service", "install"]) == 1
    assert "already using port 8765" in capsys.readouterr().err
    assert not service.plist_path().exists()
    assert not launchd.loaded


def test_install_explains_what_to_do_when_the_page_does_not_answer(launchd, monkeypatch, capsys):
    monkeypatch.setattr(service, "START_TIMEOUT_S", 0)
    monkeypatch.setattr(service, "page_answers", lambda port: False)
    monkeypatch.setattr(service, "port_in_use", lambda port: False)
    assert cli.main(["service", "install"]) == 1
    err = capsys.readouterr().err
    assert "Full Disk Access" in err and str(service.python_binary()) in err
    assert service.plist_path().exists()  # installed; it keeps trying once access is granted


def test_status_and_uninstall(launchd, capsys):
    assert cli.main(["service", "status"]) == 1
    assert "isn't installed" in capsys.readouterr().err

    cli.main(["service", "install"])
    capsys.readouterr()
    assert cli.main(["service", "status"]) == 0
    out = capsys.readouterr().out
    assert "running (pid 4242)" in out and "http://127.0.0.1:8765/ answers" in out

    assert cli.main(["service", "uninstall"]) == 0
    assert ("bootout", f"gui/{os.getuid()}/ai.fugue.web") in launchd.calls
    assert not service.plist_path().exists()
    assert cli.main(["service", "uninstall"]) == 0
    assert "isn't installed" in capsys.readouterr().err


def test_restart_stops_it_cleanly_and_waits_for_the_page(launchd, monkeypatch):
    cli.main(["service", "install"])
    answers = iter([False, True])  # down once stopped, then up again
    monkeypatch.setattr(service, "page_answers", lambda port: next(answers))
    assert cli.main(["service", "restart"]) == 0
    assert ("kill", "15", f"gui/{os.getuid()}/ai.fugue.web") in launchd.calls  # SIGTERM, not a hard kill


def test_only_on_macos(launchd, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cli.main(["service", "install"]) == 1
    assert "only works on macOS" in capsys.readouterr().err
    assert launchd.calls == []
