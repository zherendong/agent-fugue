"""Where discussions live on disk, and reading and writing their files.

Every discussion is a folder under ~/.fugue/discussions/ (or $FUGUE_HOME/discussions/).
Only the process running a discussion writes its state and its event log.
Anyone (the command line, the web page) may add notes and requests to its
inbox or change its controls; the running process picks them up.

A discussion's members are known by letter ("A", "B"), drawn at random when
it starts. Each member is a tool (Claude Code or Codex) with its own model,
effort and extra arguments; two members may use the same tool and model.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

TOOLS = ("claude", "codex")
TOOL_NAMES = {"claude": "Claude", "codex": "Codex"}
LETTERS = "AB"

# How a discussion moves forward on its own. Anyone may change these at any time.
DEFAULT_CONTROL = {
    "max_rounds": 2,  # rounds of comments and updates per question, after the answers
    "pause_after_each_round": True,  # wait for the user after every round
    "paused": False,
    "stop_requested": False,  # stop the agents now, mid-round
}


class FugueError(Exception):
    """An error to show the user as a plain message, without a traceback."""


def assign_letters(count: int, seed: str | None = None) -> list[str]:
    """Letters for `count` members, in random order, so the first member isn't always "A".

    With a seed the order is the same every time (for converting old discussions).
    """
    rng = random.Random(seed) if seed is not None else random
    return rng.sample(LETTERS[:count], count)


def member_name(members: dict, letter: str, with_letter: bool = True) -> str:
    """A member's name for the user, e.g. "Claude (member A)", or "Codex · gpt-6.1-sol · high (member B)"
    when another member uses the same tool."""
    member = members[letter]
    name = TOOL_NAMES[member["tool"]]
    if sum(1 for m in members.values() if m["tool"] == member["tool"]) > 1:
        name += f" · {member.get('model') or 'default model'}"
        if member.get("effort"):
            name += f" · {member['effort']}"
    return f"{name} (member {letter})" if with_letter else name


def upgrade_state(state: dict) -> dict:
    """Convert a discussion from before members (one Claude, one Codex, keyed by tool).

    Claude and Codex get the letters they were already shown with, and keep
    their reply files, which are named after the tool. Nothing is written here;
    the new form is saved the next time the discussion is.
    """
    if "members" in state:
        return state
    if "labels" in state:
        letters = state["labels"]
    else:
        letters = dict(zip(TOOLS, assign_letters(len(TOOLS), seed=state["id"])))
    settings = state.get("settings", {})
    members = {}
    for tool in TOOLS:
        own = settings.get(tool, {})
        members[letters[tool]] = {"tool": tool, "model": own.get("model"), "effort": own.get("effort"),
                                  "args": own.get("args", []), "files": tool}
    new = {key: value for key, value in state.items() if key != "labels"}
    new["version"] = 3
    new["members"] = dict(sorted(members.items()))
    new["settings"] = {key: value for key, value in settings.items() if key not in TOOLS}
    new["legacy_names"] = {tool: letters[tool] for tool in TOOLS}
    new["sessions"] = {letters[tool]: sid for tool, sid in state.get("sessions", {}).items() if tool in letters}
    new["rounds"] = [{**rnd, "entries": {letters.get(name, name): entry for name, entry in rnd["entries"].items()}}
                     for rnd in state.get("rounds", [])]
    return new


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def fugue_home() -> Path:
    return Path(os.environ.get("FUGUE_HOME") or "~/.fugue").expanduser().absolute()


def discussions_dir() -> Path:
    path = fugue_home() / "discussions"
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
        self.state = upgrade_state(state)
        self.listeners: list[Callable[[dict], None]] = []
        self._events = None
        self._seq = 0

    # -- creating and loading

    @classmethod
    def create(cls, *, question: str, title: str, cwd: Path, members: list[dict], settings: dict,
               control: dict) -> "Discussion":
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
        letters = assign_letters(len(members))
        state = {
            "version": 3,
            "id": ident,
            "title": title,
            "question": question,
            "cwd": str(cwd),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "members": dict(sorted(zip(letters, members))),
            "settings": settings,
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
            raise FugueError(f"No discussion with id {ident!r}.")
        path = discussions_dir() / ident
        if not (path / "state.json").exists():
            raise FugueError(f"No discussion with id {ident!r}.")
        return cls.load(path)

    def reload(self) -> None:
        self.state = upgrade_state(json.loads((self.path / "state.json").read_text()))

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
    def members(self) -> dict[str, dict]:
        """Each member by letter: its tool, model, effort and extra arguments."""
        return self.state["members"]

    def other(self, letter: str) -> str:
        """The other member (a discussion has two)."""
        return next(other for other in self.members if other != letter)

    def name(self, letter: str, with_letter: bool = True) -> str:
        return member_name(self.members, letter, with_letter)

    @property
    def transcript_path(self) -> Path:
        return self.path / "transcript.md"

    @property
    def events_path(self) -> Path:
        return self.path / "events.jsonl"

    def _stem(self, letter: str) -> str:
        # Converted discussions keep their files, which are named after the tool.
        return self.members[letter].get("files") or letter

    def prompt_path(self, index: int, letter: str) -> Path:
        return self.path / "rounds" / f"{index:02d}-{self._stem(letter)}.prompt.md"

    def reply_path(self, index: int, letter: str) -> Path:
        return self.path / "rounds" / f"{index:02d}-{self._stem(letter)}.md"

    def log_stem(self, index: int, letter: str) -> Path:
        return self.path / "logs" / f"{index:02d}-{self._stem(letter)}"

    def read_reply(self, index: int, letter: str) -> str:
        return self.reply_path(index, letter).read_text()

    def move_to_trash(self) -> None:
        """Move this discussion's folder to the Trash, where it can be restored from Finder.

        Claude's and Codex's own sessions are left alone. $FUGUE_TRASH names a folder
        to move it to instead (the tests use it); without the macOS `trash`
        command, it goes to the fugue folder's own trash/.
        """
        if self.runner_pid() is not None:
            raise FugueError("This discussion is running. Stop it first, then delete it.")
        target = os.environ.get("FUGUE_TRASH")
        if not target and shutil.which("trash"):
            result = subprocess.run(["trash", str(self.path)], capture_output=True, text=True)
            if result.returncode != 0:
                raise FugueError(f"Couldn't move it to the Trash: {(result.stderr or result.stdout).strip()}")
            return
        folder = Path(target) if target else fugue_home() / "trash"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / self.id
        if destination.exists():
            destination = folder / f"{self.id}-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(self.path), str(destination))

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
            raise FugueError(f"Unknown setting: {', '.join(sorted(unknown))}")
        with self._locked_json("control.json", dict(DEFAULT_CONTROL)) as control:
            control.update(changes)
            return {**DEFAULT_CONTROL, **control}

    def _letters_for_notes(self, notes: dict) -> dict:
        """Notes keyed by member letter (converted discussions may have them keyed by tool)."""
        out: dict[str, list] = {}
        for key, value in notes.items():
            out.setdefault(self.state.get("legacy_names", {}).get(key, key), []).extend(value)
        return out

    @contextmanager
    def _inbox(self):
        with self._locked_json("inbox.json", {"notes": {}, "requests": []}) as inbox:
            inbox["notes"] = self._letters_for_notes(inbox.get("notes", {}))
            inbox.setdefault("requests", [])
            yield inbox

    def add_note(self, text: str, to: list[str]) -> None:
        with self._inbox() as inbox:
            for letter in to:
                inbox["notes"].setdefault(letter, []).append({"text": text, "shared": len(to) > 1})

    def remove_note(self, letter: str, index: int) -> None:
        with self._inbox() as inbox:
            notes = inbox["notes"].get(letter, [])
            if 0 <= index < len(notes):
                removed = notes.pop(index)
                # A note sent to every member is withdrawn from all of them.
                if removed["shared"]:
                    for other, theirs in inbox["notes"].items():
                        if other != letter:
                            inbox["notes"][other] = [
                                n for n in theirs if not (n["shared"] and n["text"] == removed["text"])]

    def take_notes(self, letter: str) -> list[dict]:
        with self._inbox() as inbox:
            return inbox["notes"].pop(letter, [])

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
        return {"notes": self._letters_for_notes(inbox.get("notes", {})), "requests": inbox.get("requests", [])}


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
        raise FugueError('No discussions yet. Start one with `fugue start "your question"`.')
    if ident is None:
        here = str((cwd or Path.cwd()).resolve())
        local = [d for d in discussions if d.state["cwd"] == here]
        return (local or discussions)[-1]
    matches = [d for d in discussions if d.id == ident] or [d for d in discussions if d.id.startswith(ident)]
    if not matches:
        raise FugueError(f"No discussion matches {ident!r}. See `fugue list`.")
    if len(matches) > 1:
        raise FugueError(f"{ident!r} matches several discussions: {', '.join(d.id for d in matches)}")
    return matches[0]
