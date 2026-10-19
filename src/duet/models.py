"""The models each agent can run, and the effort levels each model accepts.

Codex reports its own models (`codex debug models`), so its list stays current
by itself; duet keeps a copy for a day. Claude Code has no such command, so
duet carries a short list of the current Claude models. Their effort levels
match Anthropic's documentation.

"Default" always means duet passes nothing, so each tool uses what its own
settings say (~/.claude/settings.json, ~/.codex/config.toml).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import tomllib
from pathlib import Path

from .agents import codex_home, find_binary
from .store import DuetError, duet_home

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_MODELS = [
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1", "efforts": CLAUDE_EFFORTS},
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "efforts": CLAUDE_EFFORTS},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "efforts": CLAUDE_EFFORTS},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "efforts": CLAUDE_EFFORTS},
]
CLAUDE_ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                  "haiku": "claude-haiku-5-5"}

# Codex's list on 2026-10-19, used only when Codex can't be asked.
CODEX_FALLBACK = [
    {"id": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
     "default_effort": "low"},
    {"id": "gpt-6-astra", "name": "GPT-6-Astra", "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
     "default_effort": "medium"},
    {"id": "gpt-6-sol", "name": "GPT-6-Sol", "efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
     "default_effort": "medium"},
    {"id": "gpt-6-luna", "name": "GPT-6-Luna", "efforts": ["low", "medium", "high", "xhigh", "max"],
     "default_effort": "medium"},
]
CACHE_SECONDS = 24 * 3600


def _cache_path() -> Path:
    return duet_home() / "codex-models.json"


def _normalize_codex(raw: dict) -> list[dict]:
    """Keep the models Codex shows in its own picker, in its order."""
    models = []
    for item in raw.get("models") or []:
        if item.get("visibility") not in (None, "list") or not item.get("slug"):
            continue
        levels = []
        for level in item.get("supported_reasoning_levels") or []:
            name = level.get("effort") if isinstance(level, dict) else level
            if name:
                levels.append(name)
        models.append({
            "id": item["slug"],
            "name": item.get("display_name") or item["slug"],
            "efforts": levels,
            "default_effort": item.get("default_reasoning_level"),
            "description": item.get("description") or "",
        })
    return models


def codex_models(refresh: bool = False) -> dict:
    """Codex's models: from duet's copy if it is less than a day old, else from Codex itself."""
    cache = _cache_path()
    if not refresh and cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - saved["checked_at"] < CACHE_SECONDS and saved["models"]:
                return saved
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    try:
        result = subprocess.run([find_binary("codex"), "debug", "models"], capture_output=True, text=True,
                                encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
        models = _normalize_codex(json.loads(result.stdout)) if result.returncode == 0 else []
        source = "codex debug models"
    except (DuetError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        models = []
    if not models:
        try:  # the copy Codex keeps for itself
            models = _normalize_codex(json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8")))
            source = "Codex's own cache (~/.codex/models_cache.json)"
        except (OSError, json.JSONDecodeError):
            models = []
    if not models:
        return {"models": CODEX_FALLBACK, "source": "duet's built-in list (Codex could not be asked)",
                "checked_at": None}
    saved = {"models": models, "source": source, "checked_at": time.time()}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
    return saved


def tool_defaults() -> dict:
    """What each tool runs when duet passes no model or effort: the user's own settings."""
    claude = {"model": None, "effort": None}
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
        claude["model"] = settings.get("model")
        claude["effort"] = settings.get("effortLevel")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    codex = {"model": None, "effort": None}
    try:
        with open(codex_home() / "config.toml", "rb") as handle:
            config = tomllib.load(handle)
        codex["model"] = config.get("model")
        codex["effort"] = config.get("model_reasoning_effort")
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return {"claude": claude, "codex": codex}


def catalog(refresh: bool = False) -> dict:
    """Everything the pickers need, for both agents."""
    codex = codex_models(refresh)
    defaults = tool_defaults()
    return {
        "claude": {"models": CLAUDE_MODELS, "source": "duet's built-in list of current Claude models",
                   "checked_at": None, "defaults": defaults["claude"]},
        "codex": {**codex, "defaults": defaults["codex"]},
    }


def find_model(agent: str, model: str | None, models: list[dict]) -> dict | None:
    """The catalog entry for a model id or alias, ignoring suffixes like `[1m]`."""
    if not model:
        return None
    base = re.sub(r"\[[^\]]*\]$", "", model.strip())
    if agent == "claude":
        base = CLAUDE_ALIASES.get(base, base)
    return next((m for m in models if m["id"] == base), None)


def check_effort(agent: str, model: str | None, effort: str | None, entry: dict) -> None:
    """Refuse an effort level the model doesn't accept (`entry` is that agent's part of `catalog()`)."""
    if not effort:
        return
    models = entry["models"]
    known = find_model(agent, model or entry["defaults"].get("model"), models)
    allowed = known["efforts"] if known else sorted({level for m in models for level in m["efforts"]})
    if effort not in allowed:
        who = known["name"] if known else ("Claude" if agent == "claude" else "Codex")
        raise DuetError(f"{who} doesn't take effort {effort!r}; it takes {', '.join(allowed)}.")
