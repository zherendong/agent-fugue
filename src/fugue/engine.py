"""Planning rounds, running them, and moving a discussion forward on its own."""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable

from . import transcript
from .agents import AgentCall, claude_command, codex_command, find_binary
from .prompts import NAMES, build_prompt
from .store import AGENTS, Discussion, FugueError, now_iso, other_agent
from .streams import ClaudeStream, CodexStream, Outcome

# -- what each round contains


def is_complete(rnd: dict) -> bool:
    return all(entry["status"] == "done" for entry in rnd["entries"].values())


# What follows each kind of round: after the answers, comments and updates
# alternate. The kinds "review", "discuss" and "final" come from discussions
# made by earlier versions of fugue.
NEXT_KIND = {"answer": "comment", "comment": "update", "update": "comment",
             "review": "update", "discuss": "update", "final": "comment"}


def next_kind(discussion: Discussion) -> str:
    return NEXT_KIND[discussion.rounds[-1]["kind"]]


def current_question(rounds: list[dict]) -> list[dict]:
    """The rounds after the answers to the most recent question."""
    starts = [i for i, rnd in enumerate(rounds) if rnd["kind"] == "answer"]
    return rounds[starts[-1] + 1:] if starts else []


def unseen(discussion: Discussion, agent: str, before: int) -> list[int]:
    """Rounds (before `before`) whose reply from the other agent hasn't been forwarded to `agent` yet."""
    sender = other_agent(agent)
    seen: set[int] = set()
    for rnd in discussion.rounds[:before]:
        entry = rnd["entries"].get(agent)
        if entry:
            seen.update(entry["forwarded"])
    return [
        rnd["index"] for rnd in discussion.rounds[:before]
        if rnd["entries"].get(sender, {}).get("status") == "done" and rnd["index"] not in seen
    ]


def agreed(rnd: dict) -> bool:
    """True when, in a comment round, no member objects to any other member's answer."""
    entries = rnd["entries"]
    return (
        rnd["kind"] == "comment"
        and set(entries) == set(AGENTS)
        and all(entry["status"] == "done" and entry.get("objections") == 0 for entry in entries.values())
    )


def create_round(discussion: Discussion, kind: str, participants: list[str], question: str | None = None) -> dict:
    """Add a round, writing the exact message each participant will receive."""
    if discussion.rounds and not is_complete(discussion.rounds[-1]):
        raise FugueError("The last round hasn't finished. Continue the discussion to retry it first.")
    index = len(discussion.rounds)
    followup = kind == "answer" and index > 0
    rnd: dict = {"index": index, "kind": kind, "created_at": now_iso(), "entries": {}}
    if kind == "answer":
        rnd["question"] = question

    planned = {}
    for agent in participants:
        # The first round is isolated: nothing from the other member is forwarded.
        forwarded = [] if (kind == "answer" and not followup) else unseen(discussion, agent, index)
        if kind in ("comment", "update") and not forwarded:
            continue  # nothing new from the other member to work with
        planned[agent] = forwarded
    if not planned:
        raise FugueError("Neither member has anything new from the other to work with.")

    for agent, forwarded in planned.items():
        sender = other_agent(agent)
        messages = [(i, discussion.rounds[i]["kind"], discussion.read_reply(i, sender)) for i in forwarded]
        notes = discussion.take_notes(agent)
        prompt = build_prompt(kind, me=discussion.labels[agent], other=discussion.labels[sender],
                              question=question, followup=followup, messages=messages,
                              notes=[(n["text"], n["shared"]) for n in notes])
        discussion.prompt_path(index, agent).write_text(prompt)
        rnd["entries"][agent] = {"status": "pending", "forwarded": forwarded, "notes": notes}
    discussion.rounds.append(rnd)
    discussion.save()
    return rnd


# -- running one round


COUNT_PATTERNS = [
    re.compile(r"objections?\s*[:：]\s*\**\s*(\d+|none|zero)", re.IGNORECASE),
    re.compile(r"异议\s*[:：]\s*\**\s*(\d+|无)"),
    # The line asked for by earlier versions of fugue.
    re.compile(r"remaining\s+disagreements?\s*[:：]\s*\**\s*(\d+|none|zero)", re.IGNORECASE),
]


def parse_objections(reply: str) -> int | None:
    """Read the 'Objections: N' line members end their comments with."""
    for line in reversed([line for line in reply.splitlines() if line.strip()][-5:]):
        for pattern in COUNT_PATTERNS:
            match = pattern.search(line)
            if match:
                value = match.group(1).lower()
                return int(value) if value.isdigit() else 0
    return None


def _make_call(discussion: Discussion, rnd: dict, agent: str) -> AgentCall:
    settings = discussion.state["settings"]
    agent_settings = settings[agent]
    session_id = discussion.state["sessions"].get(agent)
    stem = discussion.path / "logs" / f"{rnd['index']:02d}-{agent}"
    if agent == "claude":
        cmd = claude_command(find_binary("claude"), session_id=session_id, model=agent_settings.get("model"),
                             extra_args=agent_settings.get("args", []), name=f"fugue: {discussion.title}",
                             cwd=discussion.cwd, effort=agent_settings.get("effort"),
                             web=settings.get("web", True))
        stream = ClaudeStream(discussion.cwd)
    else:
        reply_file = stem.with_suffix(".last-message.md")
        reply_file.unlink(missing_ok=True)
        cmd = codex_command(find_binary("codex"), session_id=session_id, model=agent_settings.get("model"),
                            extra_args=agent_settings.get("args", []), reply_file=reply_file,
                            effort=agent_settings.get("effort"), web=settings.get("web", True))
        stream = CodexStream(reply_file)
    return AgentCall(agent, cmd, cwd=discussion.cwd, prompt_file=discussion.prompt_path(rnd["index"], agent),
                     log_stem=stem, timeout_s=settings["timeout_minutes"] * 60, stream=stream)


def _record(discussion: Discussion, rnd: dict, call: AgentCall) -> None:
    entry = rnd["entries"][call.agent]
    outcome = call.outcome or Outcome(ok=False, error="interrupted")
    entry["duration_s"] = round(call.duration_s, 1)
    entry["finished_at"] = now_iso()
    if not outcome.ok:
        entry["status"] = "failed"
        entry["error"] = outcome.error
        return
    discussion.reply_path(rnd["index"], call.agent).write_text(outcome.reply.rstrip() + "\n")
    entry["status"] = "done"
    entry.pop("error", None)
    if rnd["kind"] == "comment":
        entry["objections"] = parse_objections(outcome.reply)
    if outcome.cost_usd is not None:
        entry["cost_usd"] = outcome.cost_usd
    if outcome.tokens is not None:
        entry["tokens"] = outcome.tokens
    if outcome.session_id:
        discussion.state["sessions"][call.agent] = outcome.session_id


def run_round(discussion: Discussion, rnd: dict, *, should_stop: Callable[[], bool] = lambda: False) -> None:
    """Run every participant of `rnd` that hasn't succeeded yet, in parallel, emitting live events."""
    todo = [agent for agent, entry in rnd["entries"].items() if entry["status"] != "done"]
    if not todo:
        return
    if not discussion.cwd.is_dir():
        raise FugueError(f"The folder this discussion runs in no longer exists: {discussion.cwd}")
    index = rnd["index"]
    calls = [_make_call(discussion, rnd, agent) for agent in todo]
    for call in calls:
        entry = rnd["entries"][call.agent]
        entry["status"] = "running"
        entry["started_at"] = now_iso()
        entry.pop("error", None)
    discussion.save()
    discussion.emit({"type": "round_started", "round": index, "kind": rnd["kind"], "agents": todo})
    for call in calls:
        call.start()
        discussion.emit({"type": "agent_started", "round": index, "agent": call.agent})

    # Pieces of text arrive many times a second; they are written out in batches.
    text_buffers = {call.agent: "" for call in calls}
    last_flush = time.monotonic()
    # Thinking progress arrives about once a second; pass it on every few seconds.
    last_progress: dict[str, float] = {}

    def progress(agent: str, event: dict) -> None:
        now = time.monotonic()
        if now - last_progress.get(agent, 0.0) >= 2.0:
            last_progress[agent] = now
            discussion.emit({**event, "round": index, "agent": agent})

    def flush(agent: str) -> None:
        if text_buffers[agent]:
            discussion.emit({"type": "text_delta", "round": index, "agent": agent, "text": text_buffers[agent]})
            text_buffers[agent] = ""

    def finished(call: AgentCall) -> None:
        flush(call.agent)
        _record(discussion, rnd, call)
        discussion.save()
        entry = rnd["entries"][call.agent]
        discussion.emit({"type": "agent_finished", "round": index, "agent": call.agent,
                         "ok": entry["status"] == "done", "error": entry.get("error"),
                         "duration_s": entry["duration_s"], "objections": entry.get("objections")})

    def stop_all(reason: str) -> None:
        for call in waiting:
            call.stop()
            call.finish(Outcome(ok=False, error=reason))
            finished(call)
        waiting.clear()

    waiting = list(calls)
    for call in list(waiting):  # a call that couldn't even start has already finished
        if call.outcome is not None:
            waiting.remove(call)
            finished(call)
    try:
        while waiting:
            for call in list(waiting):
                for event in call.poll():
                    if event["type"] == "text_delta":
                        text_buffers[call.agent] += event["text"]
                        continue
                    if event["type"] == "progress":
                        progress(call.agent, event)
                        continue
                    flush(call.agent)
                    discussion.emit({**event, "round": index, "agent": call.agent})
                if call.outcome is not None:
                    waiting.remove(call)
                    finished(call)
            if time.monotonic() - last_flush > 0.3:
                for agent in text_buffers:
                    flush(agent)
                last_flush = time.monotonic()
            if waiting and should_stop():
                stop_all("stopped")
            if waiting:
                time.sleep(0.2)
    except BaseException:
        stop_all("interrupted")
        raise
    finally:
        discussion.emit({"type": "round_finished", "round": index, "complete": is_complete(rnd)})


# -- moving a discussion forward on its own


def automatic_step(rounds: list[dict], control: dict) -> dict | None:
    """The next round when nobody asked for one: comments and updates until agreement or the round limit."""
    last = rounds[-1]
    if agreed(last):
        return None
    current = current_question(rounds)
    if any(rnd["kind"] == "final" for rnd in current):
        return None  # discussions made by earlier versions of fugue ended with final answers
    updates = sum(1 for rnd in current if rnd["kind"] == "update")
    if last["kind"] != "comment" and updates >= control["max_rounds"]:
        return None
    return {"action": "next"}


def plan(state: dict, control: dict, requests: list[dict]) -> dict | None:
    """Decide the next step of a discussion, or None if it should wait for the user."""
    rounds = state["rounds"]
    if not rounds:
        return {"action": "answer", "question": state["question"]}
    if not is_complete(rounds[-1]):
        # A failed or stopped round is retried when the user continues.
        return None if control["paused"] else {"action": "retry"}
    if requests:
        return {"action": requests[0]["type"], "request": requests[0]}
    if control["paused"]:
        return None
    return automatic_step(rounds, control)


def plan_for(discussion: Discussion) -> dict | None:
    return plan(discussion.state, discussion.read_control(), discussion.read_inbox()["requests"])


STEP_LABELS = {
    "answer": "each answers the question on their own",
    "ask": "each answers your follow-up on their own",
    "comment": "each comments on the other's answer",
    "update": "each updates their answer",
}


def step_label(rounds: list[dict], step: dict | None) -> str | None:
    """Say in plain words what a planned step will do."""
    if step is None:
        return None
    if step["action"] == "retry":
        return f"retry round {rounds[-1]['index']} for the agents that didn't finish"
    if step["action"] == "next":
        return STEP_LABELS[NEXT_KIND[rounds[-1]["kind"]]]
    return STEP_LABELS.get(step["action"], step["action"])


def describe(discussion: Discussion) -> dict:
    """Where a discussion stands, and what its next step would be, for showing to the user."""
    state = discussion.state
    rounds = state["rounds"]
    last = rounds[-1] if rounds else None
    control = discussion.read_control()
    requests = discussion.read_inbox()["requests"]
    # What "continue" would do, paused or not.
    next_step = step_label(rounds, plan(state, {**control, "paused": False}, requests))
    if discussion.runner_pid() is not None:
        working = [NAMES[a] for a, e in (last or {"entries": {}})["entries"].items() if e["status"] == "running"]
        label = f"Round {last['index']}: {' and '.join(working)} working" if working else "Running"
        return {"phase": "running", "label": label, "needs_you": False, "next": next_step}
    if last and not is_complete(last):
        failed = {a: e["error"] for a, e in last["entries"].items()
                  if e["status"] == "failed" and e.get("error") not in (None, "stopped", "interrupted")}
        label = f"Stopped in round {last['index']}"
        if failed:
            label += f": {' and '.join(NAMES[a] for a in failed)} failed"
        detail = "; ".join(f"{NAMES[a]}: {error}" for a, error in failed.items())
        return {"phase": "stopped", "label": label, "detail": detail, "needs_you": True, "next": next_step}
    if plan(state, control, requests) is not None:
        return {"phase": "ready", "label": f"Ready: next, {next_step}", "needs_you": True, "next": next_step}
    if control["paused"] and next_step:
        label = f"Paused after round {last['index']}" if last else "Paused"
        return {"phase": "paused", "label": label, "needs_you": True, "next": next_step}
    if last and agreed(last):
        return {"phase": "finished", "label": f"Agreed in round {last['index']}", "needs_you": False, "next": None}
    comments = [rnd for rnd in current_question(rounds) if rnd["kind"] == "comment"]
    detail = ""
    if comments:
        counts = ", ".join(f"{NAMES[a]} {e.get('objections') if e.get('objections') is not None else '?'}"
                           for a, e in comments[-1]["entries"].items())
        detail = f"Objections still open after round {comments[-1]['index']}: {counts}."
    label = "Finished at the round limit" if comments else "Finished"
    return {"phase": "finished", "label": label, "detail": detail, "needs_you": False, "next": None}


class Runner:
    """Runs a discussion until it needs the user, then returns.

    Any number of runners may be started for the same discussion (from the
    command line or the web server); only one gets the run lock, and the
    others return at once. The one running picks up new requests and
    control changes between rounds.
    """

    def __init__(self, discussion_id: str, *, listener: Callable[[dict], None] | None = None,
                 stop_event: threading.Event | None = None):
        self.discussion_id = discussion_id
        self.listener = listener
        self.stop_event = stop_event or threading.Event()

    def run(self) -> bool:
        """Return True if this runner did any work, False if another one was already running."""
        did_work = False
        while not self.stop_event.is_set():
            discussion = Discussion.by_id(self.discussion_id)
            if self.listener:
                discussion.listeners.append(self.listener)
            with discussion.running_lock() as acquired:
                if not acquired:
                    return did_work
                discussion.reload()
                if plan_for(discussion) is None:
                    return did_work
                did_work = True
                try:
                    self._loop(discussion)
                finally:
                    discussion.close_events()
            # Someone may have sent a request while this runner was finishing and
            # given up waiting for the lock, so look once more after letting go.
            discussion.reload()
            if plan_for(discussion) is None:
                break
        return did_work

    def _should_stop(self, discussion: Discussion) -> Callable[[], bool]:
        last_check = [0.0]

        def check() -> bool:
            if self.stop_event.is_set():
                return True
            if time.monotonic() - last_check[0] < 1.0:
                return False
            last_check[0] = time.monotonic()
            return discussion.read_control()["stop_requested"]
        return check

    def _loop(self, discussion: Discussion) -> None:
        discussion.state["runner"] = {"pid": os.getpid(), "started_at": now_iso()}
        discussion.save()
        discussion.emit({"type": "runner", "state": "started"})
        try:
            while not self.stop_event.is_set():
                control = discussion.read_control()
                if control["stop_requested"]:
                    control = discussion.update_control(stop_requested=False)
                step = plan(discussion.state, control, discussion.read_inbox()["requests"])
                if step is None:
                    break
                if "request" in step:
                    discussion.pop_request(step["request"]["id"])
                try:
                    rnd = self._execute(discussion, step)
                except FugueError as exc:
                    discussion.emit({"type": "notice", "level": "error", "text": str(exc)})
                    break
                finally:
                    transcript.write(discussion)
                if not is_complete(rnd):
                    discussion.update_control(paused=True, stop_requested=False)
                    break
                if agreed(rnd):
                    discussion.emit({"type": "notice", "level": "info",
                                     "text": "No objections on either side: the members agree."})
                if discussion.read_control()["pause_after_each_round"]:
                    discussion.update_control(paused=True)
        except KeyboardInterrupt:
            discussion.update_control(paused=True, stop_requested=False)
            raise
        finally:
            if self.stop_event.is_set() and discussion.rounds and not is_complete(discussion.rounds[-1]):
                discussion.update_control(paused=True, stop_requested=False)
            discussion.state.pop("runner", None)
            discussion.save()
            transcript.write(discussion)
            discussion.emit({"type": "runner", "state": "stopped"})

    def _execute(self, discussion: Discussion, step: dict) -> dict:
        action = step["action"]
        if action == "retry":
            rnd = discussion.rounds[-1]
        elif action == "answer":
            rnd = create_round(discussion, "answer", list(AGENTS), question=step["question"])
        elif action == "ask":
            rnd = create_round(discussion, "answer", list(AGENTS), question=step["request"]["question"])
        elif action == "next":
            rnd = create_round(discussion, next_kind(discussion), list(AGENTS))
        else:
            raise FugueError(f"fugue no longer handles the request {action!r}; it was dropped.")
        run_round(discussion, rnd, should_stop=self._should_stop(discussion))
        return rnd
