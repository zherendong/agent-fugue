"""Where discussions live on disk, and reading and writing their files.

Every discussion is a folder under ~/.duet/discussions/ (or $DUET_HOME/discussions/).
Only the process running a discussion writes its state and its event log.
Anyone (the command line, the web page) may add notes and requests to its
inbox or change its controls; the running process picks them up.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

AGENTS = ("claude", "codex")

# How a discussion moves forward on its own. Anyone may change these at any time.
DEFAULT_CONTROL = {
    "max_rounds": 2,  # rounds of comments and updates per question, after the answers
    "pause_after_each_round": True,  # wait for the user after every round
    "paused": False,
    "stop_requested": False,  # stop the agents now, mid-round
}


class DuetError(Exception):
    """An error to show the user as a plain message, without a traceback."""


def other_agent(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def assign_labels(seed: str | None = None) -> dict[str, str]:
    """Give each member a letter for the whole discussion, at random, so no tool is always "A".

    With a seed the letters are the same every time, which keeps them stable for
    discussions made before letters existed until their state is next saved.
    """
    rng = random.Random(seed) if seed is not None else random
    return dict(zip(AGENTS, rng.sample("AB", len(AGENTS))))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def duet_home() -> Path:
    return Path(os.environ.get("DUET_HOME") or "~/.duet").expanduser().absolute()


def discussions_dir() -> Path:
    path = duet_home() / "discussions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:6])[:40].strip("-")


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Discussion:
    """One question (plus follow-ups) and the rounds of replies about it, in its own folder."""

    def __init__(self, path: Path, state: dict):
        self.path = path
        self.state = state
        self.listeners: list[Callable[[dict], None]] = []
        self._events = None
        self._seq = 0

    # -- creating and loading

    @classmethod
    def create(cls, *, question: str, title: str, cwd: Path, settings: dict, control: dict) -> "Discussion":
        root = discussions_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = _slug(title)
        base = f"{stamp}-{slug}" if slug else stamp
        ident, n = base, 2
        while (root / ident).exists():
            ident, n = f"{base}-{n}", n + 1
        path = root / ident
        (path / "rounds").mkdir(parents=True)
        (path / "logs").mkdir()
        state = {
            "version": 2,
            "id": ident,
            "title": title,
            "question": question,
            "cwd": str(cwd),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "settings": settings,
            "labels": assign_labels(),
            "sessions": {},
            "rounds": [],
        }
        discussion = cls(path, state)
        discussion.save()
        _write_json(path / "control.json", {**DEFAULT_CONTROL, **control})
        return discussion

    @classmethod
    def load(cls, path: Path) -> "Discussion":
        return cls(path, json.loads((path / "state.json").read_text()))

    @classmethod
    def by_id(cls, ident: str) -> "Discussion":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ident):
            raise DuetError(f"No discussion with id {ident!r}.")
        path = discussions_dir() / ident
        if not (path / "state.json").exists():
            raise DuetError(f"No discussion with id {ident!r}.")
        return cls.load(path)

    def reload(self) -> None:
        self.state = json.loads((self.path / "state.json").read_text())

    def save(self) -> None:
        self.state["updated_at"] = now_iso()
        _write_json(self.path / "state.json", self.state)

    # -- simple accessors

    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def title(self) -> str:
        return self.state["title"]

    @property
    def cwd(self) -> Path:
        return Path(self.state["cwd"])

    @property
    def rounds(self) -> list[dict]:
        return self.state["rounds"]

    @property
    def labels(self) -> dict[str, str]:
        """Each member's letter. Discussions made before letters existed get them on first use."""
        if "labels" not in self.state:
            self.state["labels"] = assign_labels(seed=self.id)
        return self.state["labels"]

    @property
    def transcript_path(self) -> Path:
        return self.path / "transcript.md"

    @property
    def events_path(self) -> Path:
        return self.path / "events.jsonl"

    def prompt_path(self, index: int, agent: str) -> Path:
        return self.path / "rounds" / f"{index:02d}-{agent}.prompt.md"

    def reply_path(self, index: int, agent: str) -> Path:
        return self.path / "rounds" / f"{index:02d}-{agent}.md"

    def read_reply(self, index: int, agent: str) -> str:
        return self.reply_path(index, agent).read_text()

    def runner_pid(self) -> int | None:
        """The process running this discussion right now, if any."""
        runner = self.state.get("runner")
        if runner and pid_alive(runner["pid"]):
            return runner["pid"]
        return None

    # -- the event log: what happened, in order, for anyone watching live

    def emit(self, event: dict) -> dict:
        """Append an event to the log. Only the process running the discussion calls this."""
        if self._events is None:
            existing = self.events_path.read_text().splitlines() if self.events_path.exists() else []
            self._seq = json.loads(existing[-1])["seq"] if existing else 0
            self._events = open(self.events_path, "a")
        self._seq += 1
        event = {"seq": self._seq, "t": round(time.time(), 3), **event}
        self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.flush()
        for listener in self.listeners:
            listener(event)
        return event

    def close_events(self) -> None:
        if self._events is not None:
            self._events.close()
            self._events = None

    # -- running lock: at most one process runs a discussion at a time

    @contextmanager
    def running_lock(self, wait_s: float = 2.0):
        """Yield True while holding the run lock, or False if another runner holds it."""
        handle = open(self.path / ".run.lock", "w")
        deadline = time.monotonic() + wait_s
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(0.1)
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

    # -- small shared files, each changed under its own short lock

    @contextmanager
    def _locked_json(self, name: str, default):
        path = self.path / name
        with open(self.path / f".{name}.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = json.loads(path.read_text()) if path.exists() else default
            yield data
            _write_json(path, data)

    def read_control(self) -> dict:
        path = self.path / "control.json"
        stored = json.loads(path.read_text()) if path.exists() else {}
        return {**DEFAULT_CONTROL, **stored}

    def update_control(self, **changes) -> dict:
        unknown = set(changes) - set(DEFAULT_CONTROL)
        if unknown:
            raise DuetError(f"Unknown setting: {', '.join(sorted(unknown))}")
        with self._locked_json("control.json", dict(DEFAULT_CONTROL)) as control:
            control.update(changes)
            return {**DEFAULT_CONTROL, **control}

    def _inbox(self):
        return self._locked_json("inbox.json", {"notes": {}, "requests": []})

    def add_note(self, text: str, to: list[str]) -> None:
        with self._inbox() as inbox:
            for agent in to:
                inbox["notes"].setdefault(agent, []).append({"text": text, "shared": len(to) > 1})

    def remove_note(self, agent: str, index: int) -> None:
        with self._inbox() as inbox:
            notes = inbox["notes"].get(agent, [])
            if 0 <= index < len(notes):
                removed = notes.pop(index)
                # A note sent to both agents is withdrawn from both.
                if removed["shared"]:
                    other = inbox["notes"].get(other_agent(agent), [])
                    inbox["notes"][other_agent(agent)] = [
                        n for n in other if not (n["shared"] and n["text"] == removed["text"])]

    def take_notes(self, agent: str) -> list[dict]:
        with self._inbox() as inbox:
            return inbox["notes"].pop(agent, [])

    def add_request(self, request: dict) -> dict:
        request = {"id": f"{time.time_ns():x}", **request}
        with self._inbox() as inbox:
            inbox["requests"].append(request)
        return request

    def pop_request(self, request_id: str) -> None:
        with self._inbox() as inbox:
            inbox["requests"] = [r for r in inbox["requests"] if r["id"] != request_id]

    def read_inbox(self) -> dict:
        path = self.path / "inbox.json"
        inbox = json.loads(path.read_text()) if path.exists() else {}
        return {"notes": inbox.get("notes", {}), "requests": inbox.get("requests", [])}


def list_discussions() -> list[Discussion]:
    found = []
    for path in discussions_dir().glob("*/state.json"):
        try:
            found.append(Discussion.load(path.parent))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(found, key=lambda d: d.state["updated_at"])


def select_discussion(ident: str | None, cwd: Path | None = None) -> Discussion:
    """Pick a discussion by id (a unique start of it is enough).

    Without an id: the most recently used discussion in `cwd`, or else the most
    recently used one anywhere.
    """
    discussions = list_discussions()
    if not discussions:
        raise DuetError('No discussions yet. Start one with `duet start "your question"`.')
    if ident is None:
        here = str((cwd or Path.cwd()).resolve())
        local = [d for d in discussions if d.state["cwd"] == here]
        return (local or discussions)[-1]
    matches = [d for d in discussions if d.id == ident] or [d for d in discussions if d.id.startswith(ident)]
    if not matches:
        raise DuetError(f"No discussion matches {ident!r}. See `duet list`.")
    if len(matches) > 1:
        raise DuetError(f"{ident!r} matches several discussions: {', '.join(d.id for d in matches)}")
    return matches[0]
