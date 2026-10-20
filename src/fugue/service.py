"""Keeping `fugue web` running in the background on macOS, as a launchd user agent.

The agent starts when the user logs in and is started again if it stops, so the
page answers without a terminal window left open. It gets the PATH of the shell
that installed it, so it finds `claude`, `codex` and the tools they run.

macOS guards some folders in the home folder (Desktop, Documents, Downloads)
with its privacy protection. A program started by launchd needs the user's
permission before it can read them, even though the same command works in a
terminal (there the terminal app's permission covers it). So installing waits
for the page to answer, which is when macOS asks.
"""

from __future__ import annotations

import http.client
import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .agents import find_binary
from .store import FugueError, fugue_home

LABEL = "ai.fugue.web"
PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads")
START_TIMEOUT_S = 60


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return fugue_home() / "web.log"


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def fugue_command() -> list[str]:
    """How to start this same fugue: the `fugue` launcher it was run from, or else this Python."""
    launcher = Path(sys.argv[0])
    if launcher.name == "fugue" and launcher.is_file():
        return [str(launcher.absolute())]
    return [sys.executable, "-m", "fugue"]


def environment() -> dict[str, str]:
    """What the service takes from the shell that installs it: PATH, the language, and fugue's own settings."""
    folders = [folder for folder in os.environ.get("PATH", "").split(os.pathsep) if folder]
    # launchd starts programs with almost no environment; the agents need HOME and a UTF-8 language.
    env = {"PATH": os.pathsep.join(dict.fromkeys(folders)), "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
           "PYTHONUNBUFFERED": "1"}
    for name, value in os.environ.items():
        if name in ("LANG", "LC_ALL") or name.startswith("FUGUE_"):
            env[name] = value
    return env


def build_plist(port: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [*fugue_command(), "web", "--no-open", "--port", str(port)],
        "EnvironmentVariables": environment(),
        "WorkingDirectory": str(Path.home()),  # the default folder for new discussions
        "RunAtLoad": True,
        "KeepAlive": True,
        # Without this, launchd throttles the CPU and disk use of the agents it runs.
        "ProcessType": "Interactive",
        # Room to stop the running agents cleanly before launchd kills fugue.
        "ExitTimeOut": 30,
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
    }


def installed_port() -> int | None:
    try:
        with open(plist_path(), "rb") as handle:
            arguments = plistlib.load(handle)["ProgramArguments"]
        return int(arguments[arguments.index("--port") + 1])
    except (OSError, plistlib.InvalidFileException, KeyError, ValueError, IndexError):
        return None


def job() -> dict[str, str] | None:
    """launchd's view of the service (state, pid, last exit code), or None if it isn't loaded."""
    result = launchctl("print", target())
    if result.returncode != 0:
        return None
    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t"):  # top-level fields only
            key, sep, value = line.strip().partition(" = ")
            if sep:
                info.setdefault(key, value)
    return info


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def page_answers(port: int) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()  # closing with the page unread resets the connection, which fugue logs as an error
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


def wait_until(condition: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def python_binary() -> Path:
    """The program macOS sees running the service, and so the one its privacy settings apply to."""
    return Path(os.path.realpath(sys.executable))


def protected_folder(path: Path) -> str | None:
    for name in PROTECTED_FOLDERS:
        if path.resolve().is_relative_to(Path.home() / name):
            return name
    return None


def require_macos() -> None:
    if sys.platform != "darwin":
        raise FugueError("The background service uses launchd, so it only works on macOS.")


def say(message: str = "") -> None:
    print(message, file=sys.stderr)


def access_help() -> str:
    return (f"If macOS asks whether {python_binary().name} may access a folder, click Allow. If it doesn't ask, "
            "open System Settings → Privacy & Security → Full Disk Access, click +, press Cmd-Shift-G, "
            f"enter {python_binary()} and switch it on; the service tries again every 10 seconds.")


# ---------------------------------------------------------------------------
# Commands


def install(port: int) -> int:
    require_macos()
    if job() is not None:  # replace it, so the new settings apply
        launchctl("bootout", target())
        wait_until(lambda: job() is None and not port_in_use(port), 35)
    if port_in_use(port):
        raise FugueError(f"Something is already using port {port}, probably `fugue web` running in a terminal. "
                        "Stop it (Ctrl-C) and run this again, or choose another port with --port.")
    from .config import default_members

    for tool in {member["tool"] for member in default_members()}:
        try:
            find_binary(tool)
        except FugueError as exc:
            say(f"Warning: {exc} The service uses the PATH of this shell.")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(port), handle)
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise FugueError(f"launchctl could not start it: {(result.stderr or result.stdout).strip()}")

    url = f"http://127.0.0.1:{port}/"
    say(f"Installed {path}")
    say(f"Waiting for {url} to answer...")
    folder = protected_folder(Path(__file__))
    if folder:
        say(f"fugue's code is in your {folder} folder, so macOS may ask whether {python_binary().name} "
            "may access it. Click Allow.")
    if wait_until(lambda: page_answers(port), START_TIMEOUT_S):
        say(f"fugue is running in the background at {url} and starts again when you log in.")
        say(f"Log: {log_path()}")
        return 0
    say(f"It is installed, but the page doesn't answer yet. {access_help()}")
    say(f"Log: {log_path()}. Check again with `fugue service status`.")
    return 1


def uninstall() -> int:
    require_macos()
    path = plist_path()
    loaded = job() is not None
    if not loaded and not path.exists():
        say("The background service isn't installed.")
        return 0
    if loaded:
        result = launchctl("bootout", target())
        if result.returncode != 0 and job() is not None:
            raise FugueError(f"launchctl could not stop it: {(result.stderr or result.stdout).strip()}")
    path.unlink(missing_ok=True)
    say("Removed the background service. Discussions that were running in it have stopped; "
        "continue them later from the page or with `fugue continue`.")
    return 0


def restart() -> int:
    """Stop fugue cleanly; launchd starts it again because of KeepAlive."""
    require_macos()
    info = job()
    if info is None:
        raise FugueError("The background service isn't running. `fugue service install` starts it.")
    port = installed_port()
    if info.get("state") == "running":
        launchctl("kill", str(int(signal.SIGTERM)), target())
        if port is not None:
            wait_until(lambda: not page_answers(port), 35)
    else:
        launchctl("kickstart", target())
    if port is None or wait_until(lambda: page_answers(port), START_TIMEOUT_S):
        say("Restarted.")
        return 0
    say(f"Restarted, but the page doesn't answer yet. Log: {log_path()}")
    return 1


def status() -> int:
    require_macos()
    path = plist_path()
    info = job()
    if info is None and not path.exists():
        say("The background service isn't installed. `fugue service install` sets it up.")
        return 1
    port = installed_port()
    url = f"http://127.0.0.1:{port}/" if port else "the page (port unknown)"
    print(f"Service:  {path if path.exists() else 'loaded, but its file is gone'}")
    if info is None:
        print("Process:  not loaded; it starts at the next login, or now with `fugue service install`")
    elif info.get("state") == "running":
        print(f"Process:  running (pid {info.get('pid', '?')})")
    else:
        print(f"Process:  not running (last exit code {info.get('last exit code', 'unknown')}); "
              "launchd tries again every 10 seconds")
    answers = port is not None and page_answers(port)
    print(f"Page:     {url} {'answers' if answers else 'does not answer'}")
    print(f"Log:      {log_path()}")
    if not answers and info is not None:
        tail = log_path().read_text(errors="replace").splitlines()[-10:] if log_path().exists() else []
        if tail:
            print("\nEnd of the log:\n  " + "\n  ".join(tail))
        print(f"\n{access_help()}")
    return 0 if answers else 1
