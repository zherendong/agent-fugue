"""The defaults for new discussions, kept in the config file, and a status check of fugue's setup.

The settings page and `fugue start` read the same file, ~/.config/fugue/config.toml
(or $FUGUE_CONFIG). Saving from the page rewrites it; comments in it are not kept.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__
from .agents import codex_switched_on, find_binary, user_temp_dir
from .config import DEFAULT_TIMEOUT_MINUTES, config_path, load_config
from .store import AGENTS, DEFAULT_CONTROL, FugueError, discussions_dir


def read_defaults() -> dict:
    """What a new discussion starts with: the config file's values, or fugue's own defaults."""
    config = load_config()
    defaults = {
        "max_rounds": config.get("max_rounds", DEFAULT_CONTROL["max_rounds"]),
        "pause_after_each_round": config.get("pause_after_each_round", DEFAULT_CONTROL["pause_after_each_round"]),
        "web": config.get("web", True),
        "timeout_minutes": config.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES),
    }
    for agent in AGENTS:
        section = config.get(agent, {})
        defaults[agent] = {
            "model": section.get("model") or "",
            "effort": section.get("effort") or "",
            "args": shlex.join(section.get("args", [])),
        }
    return defaults


def save_defaults(new: dict) -> dict:
    """Check the values, write them to the config file (keeping keys fugue doesn't manage), and return them."""
    from .models import catalog, check_effort

    config = load_config()
    rounds = new.get("max_rounds")
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 0 <= rounds <= 20:
        raise FugueError("Rounds of comments and updates must be a whole number from 0 to 20.")
    for key in ("pause_after_each_round", "web"):
        if not isinstance(new.get(key), bool):
            raise FugueError(f"{key} must be true or false.")
    timeout = new.get("timeout_minutes")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
        raise FugueError("The time limit must be between 1 and 600 minutes.")
    config.update(max_rounds=rounds, pause_after_each_round=new["pause_after_each_round"], web=new["web"],
                  timeout_minutes=timeout)
    known = None
    for agent in AGENTS:
        given = new.get(agent) or {}
        section = dict(config.get(agent, {}))
        model, effort = str(given.get("model") or "").strip(), str(given.get("effort") or "").strip()
        try:
            args = shlex.split(str(given.get("args") or ""))
        except ValueError as exc:
            raise FugueError(f"The extra arguments for {agent} aren't valid shell syntax: {exc}") from None
        if effort:
            known = known or catalog()
            check_effort(agent, model or None, effort, known[agent])
        for key, value in (("model", model), ("effort", effort), ("args", args)):
            if value:
                section[key] = value
            else:
                section.pop(key, None)
        config[agent] = section
        if not section:
            config.pop(agent)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_toml(config), encoding="utf-8")
    return read_defaults()


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON's string escapes are valid in TOML
    if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise FugueError(f"fugue can't write the value {value!r} back to the config file; edit the file by hand.")


def dump_toml(data: dict) -> str:
    """Write a config with plain values and one level of tables, which is all fugue's config uses."""
    lines = [f"{key} = {_toml_value(value)}" for key, value in data.items() if not isinstance(value, dict)]
    for name, table in data.items():
        if isinstance(table, dict):
            if any(isinstance(value, dict) for value in table.values()):
                raise FugueError(f"The config file's [{name}] has nested tables, which fugue can't rewrite; "
                                "edit the file by hand.")
            lines += ["", f"[{name}]", *(f"{key} = {_toml_value(value)}" for key, value in table.items())]
    return "\n".join(lines).strip() + "\n"


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.setdefault("TMPDIR", user_temp_dir())
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def agent_status(agent: str) -> dict:
    """Is the agent's command found, which version, and is it logged in. Doesn't call any model."""
    try:
        path = find_binary(agent)
    except FugueError as exc:
        return {"found": False, "problem": str(exc)}
    found = {"found": True, "path": path}
    version = _run([path, "--version"])
    found["version"] = version.stdout.strip().splitlines()[0] if version and version.stdout.strip() else "unknown"
    if agent == "claude":
        login = _run([path, "auth", "status", "--json"])
        try:
            info = json.loads(login.stdout) if login else {}
        except json.JSONDecodeError:
            info = {}
        found["logged_in"] = bool(info.get("loggedIn"))
        found["login"] = f"logged in ({info.get('authMethod')})" if info.get("loggedIn") else "not logged in: run `claude` and /login"
    else:
        login = _run([path, "login", "status"])
        text = ((login.stdout or login.stderr).strip() if login else "") or "unknown"
        found["logged_in"] = bool(login and login.returncode == 0)
        found["login"] = text if found["logged_in"] else f"not logged in: run `codex login` ({text})"
    return found


def service_status(port: int) -> dict:
    """Whether the background service is installed and running (macOS only)."""
    if sys.platform != "darwin":
        return {"installed": False, "text": "only available on macOS"}
    from . import service

    if not service.plist_path().exists():
        return {"installed": False, "text": "not installed: `fugue service install` keeps this page running"}
    job = service.job() or {}
    installed_port = service.installed_port()
    if job.get("pid"):
        text = f"running (pid {job['pid']}) on port {installed_port}"
    else:
        text = "installed but not running"
    return {"installed": True, "running": bool(job.get("pid")), "port": installed_port, "text": text,
            "this_page": installed_port == port}


def storage_status() -> dict:
    root = discussions_dir()
    folders = [p for p in root.iterdir() if (p / "state.json").exists()]
    size = sum(f.stat().st_size for folder in folders for f in folder.rglob("*") if f.is_file())
    return {"path": str(root), "discussions": len(folders), "megabytes": round(size / 1_000_000, 1)}


def status(port: int) -> dict:
    return {
        "version": __version__,
        "port": port,
        "config_path": str(config_path()),
        "agents": {agent: agent_status(agent) for agent in AGENTS},
        "codex_switched_off": {"apps": True, **codex_switched_on()},
        "service": service_status(port),
        "storage": storage_status(),
    }
