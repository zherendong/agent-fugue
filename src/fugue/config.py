"""Defaults from ~/.config/fugue/config.toml, and creating discussions from them."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path

from .agents import find_binary
from .store import DEFAULT_CONTROL, LEGACY_TOOLS, MAX_MEMBERS, TOOLS, Discussion, FugueError

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


def normalize_member(given: dict) -> dict:
    """A member as fugue stores it: tool, model, effort and extra arguments (a list)."""
    tool = str(given.get("tool") or "").strip()
    if tool not in TOOLS:
        raise FugueError(f"A member's tool must be one of: {', '.join(TOOLS)} (got {tool!r}).")
    args = given.get("args") or []
    if isinstance(args, str):
        try:
            args = shlex.split(args)
        except ValueError as exc:
            raise FugueError(f"The extra arguments for a {tool} member aren't valid shell syntax: {exc}") from None
    return {
        "tool": tool,
        "model": str(given.get("model") or "").strip() or None,
        "effort": str(given.get("effort") or "").strip() or None,
        "args": [str(arg) for arg in args],
    }


def default_members(config: dict | None = None) -> list[dict]:
    """The usual line-up from the config file: [[members]], or else one Claude and one Codex.

    Config files from before members had a [claude] and a [codex] table instead.
    """
    config = load_config() if config is None else config
    if isinstance(config.get("members"), list) and config["members"]:
        return [normalize_member(member) for member in config["members"]]
    return [normalize_member({"tool": tool, **config.get(tool, {})}) for tool in LEGACY_TOOLS]


def check_members(members: list[dict]) -> None:
    if not 2 <= len(members) <= MAX_MEMBERS:
        raise FugueError(f"A discussion has two to {MAX_MEMBERS} members.")
    for tool in {member["tool"] for member in members}:
        find_binary(tool)
    if any(member["effort"] for member in members):
        from .models import catalog, check_effort

        known = catalog()
        for member in members:
            check_effort(member["tool"], member["model"], member["effort"], known[member["tool"]])


def new_discussion(question: str, *, cwd: Path, title: str | None = None, timeout_minutes: float | None = None,
                   members: list[dict] | None = None, web: bool | None = None,
                   control: dict | None = None) -> Discussion:
    """Create a discussion from the config file's defaults with the given values on top.

    `members` lists each member's tool, model, effort and extra arguments, in the
    order the user gave them; their letters are drawn at random. Without it, the
    config file's line-up is used. `control` values that are None fall back to
    the config file, then to fugue's defaults.
    """
    question = question.strip()
    if not question:
        raise FugueError("The question is empty.")
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise FugueError(f"Not a folder: {cwd}")
    config = load_config()
    chosen_members = [normalize_member(m) for m in members] if members else default_members(config)
    check_members(chosen_members)
    control = control or {}
    settings: dict = {
        "timeout_minutes": timeout_minutes or config.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES),
        # Whether the members may use the web: all or none.
        "web": web if web is not None else bool(config.get("web", True)),
    }
    chosen = {}
    for key in CONTROL_KEYS:
        value = control.get(key)
        chosen[key] = value if value is not None else config.get(key, DEFAULT_CONTROL[key])
    return Discussion.create(question=question, title=title or make_title(question), cwd=cwd,
                             members=chosen_members, settings=settings, control=chosen)
