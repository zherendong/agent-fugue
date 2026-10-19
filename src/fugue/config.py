"""Defaults from ~/.config/fugue/config.toml, and creating discussions from them."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path

from .agents import find_binary
from .store import AGENTS, DEFAULT_CONTROL, Discussion, FugueError

DEFAULT_TIMEOUT_MINUTES = 30
CONTROL_KEYS = ("max_rounds", "pause_after_each_round")


def config_path() -> Path:
    return Path(os.environ.get("FUGUE_CONFIG") or "~/.config/fugue/config.toml").expanduser()


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise FugueError(f"Could not read {path}: {exc}") from None


def make_title(question: str) -> str:
    first = next((line for line in question.splitlines() if line.strip()), "Untitled")
    first = first.strip().lstrip("#").strip()
    return first if len(first) <= 80 else first[:77] + "..."


def new_discussion(question: str, *, cwd: Path, title: str | None = None, timeout_minutes: float | None = None,
                   models: dict | None = None, efforts: dict | None = None, extra_args: dict | None = None,
                   web: bool | None = None, control: dict | None = None) -> Discussion:
    """Create a discussion from the config file's defaults with the given values on top.

    `models` and `efforts` that are None fall back to the config file, then to the
    tool's own default (fugue passes nothing).
    `extra_args` values are strings in shell syntax, added after the config file's args.
    `control` values that are None fall back to the config file, then to fugue's defaults.
    """
    question = question.strip()
    if not question:
        raise FugueError("The question is empty.")
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise FugueError(f"Not a folder: {cwd}")
    for agent in AGENTS:
        find_binary(agent)
    config = load_config()
    models, efforts, extra_args, control = models or {}, efforts or {}, extra_args or {}, control or {}
    settings: dict = {
        "timeout_minutes": timeout_minutes or config.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES),
        # Whether the members may use the web: both or neither.
        "web": web if web is not None else bool(config.get("web", True)),
    }
    for agent in AGENTS:
        section = config.get(agent, {})
        settings[agent] = {
            "model": models.get(agent) or section.get("model"),
            "effort": efforts.get(agent) or section.get("effort"),
            "args": [*section.get("args", []), *shlex.split(extra_args.get(agent) or "")],
        }
    if any(settings[agent]["effort"] for agent in AGENTS):
        from .models import catalog, check_effort

        known = catalog()
        for agent in AGENTS:
            check_effort(agent, settings[agent]["model"], settings[agent]["effort"], known[agent])
    chosen = {}
    for key in CONTROL_KEYS:
        value = control.get(key)
        chosen[key] = value if value is not None else config.get(key, DEFAULT_CONTROL[key])
    return Discussion.create(question=question, title=title or make_title(question), cwd=cwd,
                             settings=settings, control=chosen)
