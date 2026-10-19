"""Starting the Claude Code and Codex command-line tools headless and reading their output live."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from functools import cache
from pathlib import Path

from .store import DuetError
from .streams import ClaudeStream, CodexStream, Outcome

# Both agents run read-only: they can read files and run commands, but not
# change files. Two agents editing the same checkout in parallel would trip
# over each other.
CODEX_READ_ONLY = ["-c", 'sandbox_mode="read-only"']
CREDENTIAL_DIRS = ["~/.ssh", "~/.aws"]


def _excluded_commands(cwd: Path) -> list[str]:
    """Commands the user's Claude Code settings run outside the sandbox (`sandbox.excludedCommands`)."""
    found = []
    for path in (Path.home() / ".claude" / "settings.json", cwd / ".claude" / "settings.json",
                 cwd / ".claude" / "settings.local.json"):
        try:
            found += json.loads(path.read_text(encoding="utf-8")).get("sandbox", {}).get("excludedCommands") or []
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return found


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def codex_isolation_args() -> list[str]:
    """Switch off, for duet's runs only, the parts of the user's Codex setup that act outside its sandbox.

    Plugins (computer use, Chrome, ...), MCP servers and the apps connector run
    outside Codex's read-only sandbox, on the machine or in the user's accounts.
    Each one that is on gets a config override; the config file is not changed.
    """
    args = ["--disable", "apps"]
    try:
        with open(codex_home() / "config.toml", "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return args
    for section in ("plugins", "mcp_servers"):
        for name, entry in (config.get(section) or {}).items():
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            if "." in name:  # Codex's -c can't address a key with a dot in it
                raise DuetError(f"duet can't switch off the Codex {section[:-1].replace('_', ' ')} {name!r} for its "
                                "runs, because its name contains a dot. Rename or disable it in ~/.codex/config.toml.")
            args += ["-c", f"{section}.{name}.enabled=false"]
    return args


def claude_read_only_args(cwd: Path, web: bool = True) -> list[str]:
    """Run Claude's shell commands in Claude Code's own sandbox, with writes blocked.

    Nobody can answer a permission prompt in a headless run, so Claude runs in
    "don't ask" mode, where anything that would ask is refused. Bash is allowed
    outright because every command runs in the macOS sandbox, which refuses
    writes everywhere except the temp folder, refuses network access, and can't
    be left: no retries outside it, and the run fails if the sandbox can't start.
    The Read tool may read anywhere except credential folders, as Codex can in
    its read-only sandbox. With `web`, Claude may also search the web and fetch
    pages, except from this machine (localhost); without it, both tools are off.
    The settings apply to duet's runs only.
    """
    sandbox = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "filesystem": {"denyWrite": [str(cwd), str(Path.home())], "denyRead": CREDENTIAL_DIRS},
    }
    denied = ["Edit", "Write", "NotebookEdit", *(f"Read({d}/**)" for d in CREDENTIAL_DIRS)]
    allowed = ["Read(//**)"]
    if web:
        allowed += ["WebSearch", "WebFetch"]
        denied += ["WebFetch(domain:localhost)", "WebFetch(domain:127.0.0.1)"]
    else:
        denied += ["WebSearch", "WebFetch"]
    # Commands listed in sandbox.excludedCommands run outside the sandbox, so
    # allowing Bash outright would let them write. Then only the commands the
    # sandbox approves by itself run.
    if not _excluded_commands(cwd):
        allowed.insert(0, "Bash")
    return ["--permission-mode", "dontAsk", "--disallowedTools", ",".join(denied),
            "--allowedTools", ",".join(allowed), "--settings", json.dumps({"sandbox": sandbox})]


@cache
def user_temp_dir() -> str:
    """The per-user temp folder, for runs started without $TMPDIR (e.g. by launchd)."""
    if sys.platform == "darwin":
        try:
            found = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
            if found:
                return found
        except (OSError, subprocess.TimeoutExpired):
            pass
    return tempfile.gettempdir()


def find_binary(agent: str) -> str:
    env_name = f"DUET_{agent.upper()}_BIN"
    name = os.environ.get(env_name, agent)
    path = shutil.which(name)
    if not path:
        raise DuetError(f"Cannot find the `{name}` command. Install it, or set {env_name} to its path.")
    return os.path.abspath(path)  # the agents run in another folder


def claude_command(binary: str, *, session_id: str | None, model: str | None, extra_args: list[str],
                   name: str, cwd: Path, effort: str | None = None, web: bool = True) -> list[str]:
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           *claude_read_only_args(cwd, web)]
    cmd += ["--resume", session_id] if session_id else ["--name", name]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd + list(extra_args)


def codex_command(binary: str, *, session_id: str | None, model: str | None, extra_args: list[str],
                  reply_file: Path, effort: str | None = None, web: bool = True) -> list[str]:
    cmd = [binary, "exec"]
    if session_id:
        cmd.append("resume")
    cmd += ["--json", "--skip-git-repo-check", *CODEX_READ_ONLY, *codex_isolation_args(), "-o", str(reply_file)]
    if not web:
        cmd += ["-c", 'web_search="disabled"']
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd += list(extra_args)
    if session_id:
        cmd.append(session_id)
    return cmd + ["-"]  # read the prompt from stdin


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


class AgentCall:
    """One agent process for one round.

    Its output goes to a log file, which `poll()` reads as it grows, so events
    can be shown while the agent is still working.
    """

    def __init__(self, agent: str, cmd: list[str], *, cwd: Path, prompt_file: Path, log_stem: Path,
                 timeout_s: float, stream: ClaudeStream | CodexStream):
        self.agent = agent
        self.cmd = cmd
        self.cwd = cwd
        self.prompt_file = prompt_file
        self.stdout_path = log_stem.with_suffix(".stdout")
        self.stderr_path = log_stem.with_suffix(".stderr")
        self.timeout_s = timeout_s
        self.stream = stream
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.duration_s = 0.0
        self.outcome: Outcome | None = None
        self._files: list = []
        self._reader = None
        self._partial = ""

    def start(self) -> None:
        self.started = time.monotonic()
        env = dict(os.environ)
        # Claude Code refuses to start when it thinks it is nested inside
        # another Claude Code session; duet's calls are separate headless runs.
        env.pop("CLAUDECODE", None)
        # The temp folder is the one place Claude's sandbox lets commands write.
        env.setdefault("TMPDIR", user_temp_dir())
        stdin = open(self.prompt_file, "rb")
        stdout = open(self.stdout_path, "wb")
        stderr = open(self.stderr_path, "wb")
        self._files = [stdin, stdout, stderr]
        self._reader = open(self.stdout_path, "r", encoding="utf-8", errors="replace")
        try:
            # A separate process group lets duet stop the agent together with
            # anything it started, and keeps Ctrl-C from reaching it directly.
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                                         env=env, start_new_session=True)
        except OSError as exc:
            self.finish(Outcome(ok=False, error=f"could not start {self.cmd[0]}: {exc}"))

    @property
    def elapsed(self) -> float:
        return self.duration_s if self.outcome else time.monotonic() - self.started

    def _read_events(self, final: bool = False) -> list[dict]:
        if self._reader is None:
            return []
        chunk = self._reader.read()
        if not chunk and not (final and self._partial):
            return []
        text = self._partial + chunk
        lines = text.split("\n")
        self._partial = "" if final else lines.pop()
        events = []
        for line in lines:
            if line.strip():
                events.extend(self.stream.feed(line))
        return events

    def poll(self) -> list[dict]:
        """Return the new events since the last call. Sets `outcome` once the agent has stopped."""
        if self.outcome is not None:
            return []
        events = self._read_events()
        returncode = self.proc.poll()
        if returncode is None:
            if self.elapsed > self.timeout_s:
                self.stop()
                self.finish(Outcome(ok=False, error=f"timed out after {format_duration(self.timeout_s)}"))
            return events
        events += self._read_events(final=True)
        stderr = self.stderr_path.read_text(errors="replace") if self.stderr_path.exists() else ""
        self.finish(self.stream.outcome(returncode, stderr))
        return events

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        except ProcessLookupError:
            pass

    def finish(self, outcome: Outcome) -> None:
        for handle in [*self._files, self._reader]:
            if handle is not None:
                handle.close()
        self._files, self._reader = [], None
        self.duration_s = time.monotonic() - self.started
        self.outcome = outcome
