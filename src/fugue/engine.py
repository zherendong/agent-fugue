"""Planning rounds, running them, and moving a discussion forward on its own."""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable

from . import attachments, council, transcript
from .agents import DSH_ENV, AgentCall, claude_command, codex_command, dsh_command, dsh_overlay, find_binary
from .prompts import build_prompt, join_letters
from .store import TOOL_NAMES, Discussion, FugueError, now_iso, step_number
from .streams import ClaudeStream, CodexStream, DshStream, Outcome

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
    sender = discussion.other(agent)
    seen: set[int] = set()
    for rnd in discussion.rounds[:before]:
        entry = rnd["entries"].get(agent)
        if entry:
            seen.update(entry["forwarded"])
    return [
        rnd["index"] for rnd in discussion.rounds[:before]
        if rnd["entries"].get(sender, {}).get("status") == "done" and rnd["index"] not in seen
    ]


def closing_readers(rnd: dict) -> list[str]:
    """After a comment round with no objections: the members whose answers got suggestions.

    Each writes one closing version taking in the suggestions it accepts. A
    comment from a version of fugue that didn't ask for the count has none.
    """
    if rnd["kind"] != "comment" or not agreed(rnd):
        return []
    letters = list(rnd["entries"])
    return [other for letter in letters for other in letters
            if other != letter and (rnd["entries"][letter].get("suggestions") or 0) > 0]


def agreement(rounds: list[dict]) -> dict | None:
    """The round in which the members agreed, if the latest question ended that way (closing versions included)."""
    if not rounds:
        return None
    last = rounds[-1]
    if last.get("closing") and len(rounds) > 1 and agreed(rounds[-2]):
        return rounds[-2]
    return last if agreed(last) else None


def agreed(rnd: dict) -> bool:
    """True when, in a comment round (or a council's review), no member objects to the answer it read."""
    if rnd["kind"] == "review":
        return council.agreed(rnd)
    entries = rnd["entries"]
    return (
        rnd["kind"] == "comment"
        and len(entries) > 1
        and all(entry["status"] == "done" and entry.get("objections") == 0 for entry in entries.values())
    )


def create_round(discussion: Discussion, kind: str, participants: list[str], question: str | None = None,
                 files: list[dict] | None = None, closing: bool = False) -> dict:
    """Add a round, writing the exact message each participant will receive.

    `files` are the attachments that come with the question of an answer round.
    `closing` marks the update after an agreement, which takes in the last suggestions.
    """
    if discussion.rounds and not is_complete(discussion.rounds[-1]):
        raise FugueError("The last round hasn't finished. Continue the discussion to retry it first.")
    index = len(discussion.rounds)
    followup = kind == "answer" and index > 0
    rnd: dict = {"index": index, "kind": kind, "created_at": now_iso(), "entries": {}}
    if kind == "answer":
        rnd["question"] = question
        if files:
            rnd["attachments"] = files
    if closing:
        rnd["closing"] = True

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
        sender = discussion.other(agent)
        messages = [(i, discussion.rounds[i]["kind"], discussion.read_reply(i, sender)) for i in forwarded]
        notes = discussion.take_notes(agent)
        asked = question
        if kind == "answer" and question is not None:
            asked = attachments.with_attachments(question, files, tool=discussion.members[agent]["tool"])
        prompt = build_prompt(kind, me=agent, other=sender,
                              question=asked, followup=followup, messages=messages,
                              notes=[(n["text"], n["shared"]) for n in notes], closing=closing)
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


SUGGESTION_PATTERNS = [
    re.compile(r"suggestions?\s*[:：]\s*\**\s*(\d+|none|zero)", re.IGNORECASE),
    re.compile(r"建议\s*[:：]\s*\**\s*(\d+|无)"),
]


def parse_suggestions(reply: str) -> int | None:
    """Read the 'Suggestions: N' line that follows the objections."""
    for line in reversed([line for line in reply.splitlines() if line.strip()][-5:]):
        for pattern in SUGGESTION_PATTERNS:
            match = pattern.search(line)
            if match:
                value = match.group(1).lower()
                return int(value) if value.isdigit() else 0
    return None


def parse_objections(reply: str) -> int | None:
    """Read the 'Objections: N' line members end their comments with."""
    for line in reversed([line for line in reply.splitlines() if line.strip()][-5:]):
        for pattern in COUNT_PATTERNS:
            match = pattern.search(line)
            if match:
                value = match.group(1).lower()
                return int(value) if value.isdigit() else 0
    return None


def _make_call(discussion: Discussion, rnd: dict, letter: str) -> AgentCall:
    """The command for one member in one round, built for that member's tool."""
    settings = discussion.state["settings"]
    member = discussion.members[letter]
    session_id = discussion.state["sessions"].get(letter)
    stem = discussion.log_stem(rnd["index"], letter)
    web = settings.get("web", True)
    env = None
    if member["tool"] == "claude":
        cmd = claude_command(find_binary("claude"), session_id=session_id, model=member.get("model"),
                             extra_args=member.get("args", []),
                             name=f"fugue: {discussion.title} (member {letter})",
                             cwd=discussion.cwd, effort=member.get("effort"), web=web)
        stream = ClaudeStream(discussion.cwd)
    elif member["tool"] == "dsh":
        overlay = stem.with_suffix(".dsh-patch.yml")
        overlay.write_text(dsh_overlay(model=member.get("model"), effort=member.get("effort"), web=web))
        cmd = dsh_command(find_binary("dsh"), session_id=session_id, overlay_file=overlay,
                          extra_args=member.get("args", []))
        stream = DshStream(discussion.cwd)
        env = DSH_ENV
    else:
        reply_file = stem.with_suffix(".last-message.md")
        reply_file.unlink(missing_ok=True)
        cmd = codex_command(find_binary("codex"), session_id=session_id, model=member.get("model"),
                            extra_args=member.get("args", []), reply_file=reply_file,
                            effort=member.get("effort"), web=web,
                            images=attachments.images(rnd.get("attachments")) if rnd["kind"] == "answer" else [])
        stream = CodexStream(reply_file)
    return AgentCall(letter, cmd, cwd=discussion.cwd, prompt_file=discussion.prompt_path(rnd["index"], letter),
                     log_stem=stem, timeout_s=settings["timeout_minutes"] * 60, stream=stream, env=env)


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
    if rnd["kind"] in ("comment", "review"):
        entry["objections"] = parse_objections(outcome.reply)
        entry["suggestions"] = parse_suggestions(outcome.reply)
    council.record(discussion, rnd, call.agent, outcome.reply)
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
    discussion.emit({"type": "round_started", "round": index, "kind": rnd["kind"], "members": todo})
    for call in calls:
        call.start()
        discussion.emit({"type": "agent_started", "round": index, "member": call.agent})

    # Pieces of text arrive many times a second; they are written out in batches.
    text_buffers = {call.agent: "" for call in calls}
    last_flush = time.monotonic()
    # Thinking progress arrives about once a second; pass it on every few seconds.
    last_progress: dict[str, float] = {}

    def progress(agent: str, event: dict) -> None:
        now = time.monotonic()
        if now - last_progress.get(agent, 0.0) >= 2.0:
            last_progress[agent] = now
            discussion.emit({**event, "round": index, "member": agent})

    def flush(agent: str) -> None:
        if text_buffers[agent]:
            discussion.emit({"type": "text_delta", "round": index, "member": agent, "text": text_buffers[agent]})
            text_buffers[agent] = ""

    def finished(call: AgentCall) -> None:
        flush(call.agent)
        _record(discussion, rnd, call)
        discussion.save()
        entry = rnd["entries"][call.agent]
        discussion.emit({"type": "agent_finished", "round": index, "member": call.agent,
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
                    discussion.emit({**event, "round": index, "member": call.agent})
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
    if last.get("closing"):
        return None
    if agreed(last):
        return {"action": "next", "closing": True} if closing_readers(last) else None
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
    if council.is_council(state.get("members", {})):
        kind = council.automatic_kind(rounds, control)
        return {"action": "next", "kind": kind} if kind else None
    return automatic_step(rounds, control)


def plan_for(discussion: Discussion) -> dict | None:
    return plan(discussion.state, discussion.read_control(), discussion.read_inbox()["requests"])


STEP_LABELS = {
    "answer": "each answers the question on their own",
    "ask": "each answers your follow-up on their own",
    "comment": "each comments on the other's answer",
    "update": "each updates their answer",
    "closing": "the final versions, taking in the last suggestions",
}


def step_label(rounds: list[dict], step: dict | None, in_council: bool = False) -> str | None:
    """Say in plain words what a planned step will do."""
    if step is None:
        return None
    if step["action"] == "retry":
        return f"retry step {step_number(rounds[-1]['index'])} for the members that didn't finish"
    if in_council and step["action"] == "revote":
        return council.step_label(rounds, "reopen")
    if in_council and step["action"] == "next":
        return council.step_label(rounds, step.get("kind") or council.NATURAL_NEXT[rounds[-1]["kind"]])
    if step["action"] == "next" and step.get("closing"):
        return STEP_LABELS["closing"]
    if step["action"] == "next":
        return STEP_LABELS[NEXT_KIND[rounds[-1]["kind"]]]
    return STEP_LABELS.get(step["action"], step["action"])


def short_name(discussion: Discussion, letter: str) -> str:
    """A member's name in a one-line status: its tool, or its letter when both members use the same tool."""
    tool = discussion.members[letter]["tool"]
    if sum(1 for member in discussion.members.values() if member["tool"] == tool) > 1:
        return f"member {letter}"
    return TOOL_NAMES[tool]


def describe(discussion: Discussion) -> dict:
    """Where a discussion stands, and what its next step would be, for showing to the user."""
    state = discussion.state
    rounds = state["rounds"]
    last = rounds[-1] if rounds else None
    control = discussion.read_control()
    requests = discussion.read_inbox()["requests"]
    in_council = council.is_council(discussion.members)
    # What "continue" would do, paused or not.
    next_step = step_label(rounds, plan(state, {**control, "paused": False}, requests), in_council)
    if discussion.runner_pid() is not None:
        working = [short_name(discussion, a) for a, e in (last or {"entries": {}})["entries"].items()
                   if e["status"] == "running"]
        label = f"Step {step_number(last['index'])}: {join_letters(working)} working" if working else "Running"
        return {"phase": "running", "label": label, "needs_you": False, "next": next_step}
    if last and not is_complete(last):
        failed = {a: e["error"] for a, e in last["entries"].items()
                  if e["status"] == "failed" and e.get("error") not in (None, "stopped", "interrupted")}
        label = f"Stopped in step {step_number(last['index'])}"
        if failed:
            label += f": {join_letters([short_name(discussion, a) for a in failed])} failed"
        detail = state.get("problem") or "; ".join(f"{discussion.name(a)}: {error}" for a, error in failed.items())
        return {"phase": "stopped", "label": label, "detail": detail, "needs_you": True, "next": next_step}
    if plan(state, control, requests) is not None:
        return {"phase": "ready", "label": f"Ready: next, {next_step}", "needs_you": True, "next": next_step}
    if control["paused"] and next_step:
        label = f"Paused after step {step_number(last['index'])}" if last else "Paused"
        return {"phase": "paused", "label": label, "detail": state.get("problem", ""), "needs_you": True,
                "next": next_step}
    agreed_in = agreement(rounds)
    if agreed_in:
        return {"phase": "finished", "label": f"Agreed in step {step_number(agreed_in['index'])}", "needs_you": False,
                "next": None}
    comments = [rnd for rnd in current_question(rounds)
                if rnd["kind"] == "comment" or (in_council and rnd["kind"] == "review")]
    detail = ""
    if comments:
        # Who raised how many objections in the last comments (or reviews). With two members the
        # last update came after them and says what each author did with them, so they aren't
        # called open; a council's last revision is reviewed once more, so its reviews are.
        last = comments[-1]
        raised = []
        for a, e in last["entries"].items():
            count = e.get("objections")
            if count == 0:
                continue
            what = "?" if count is None else f"{count} objection{'' if count == 1 else 's'}"
            raised.append(f"{discussion.name(a)} raised {what}" if in_council
                          else f"{discussion.name(a)} raised {what} on {discussion.name(discussion.other(a))}'s answer")
        if raised:
            where = "reviews" if in_council else "comments"
            then = ("; the answer wasn't revised after them" if in_council
                    else "; each author's last update says what it did with them")
            detail = f"The last {where} (step {step_number(last['index'])}): {', '.join(raised)}{then}."
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
                if discussion.state.pop("problem", None) is not None:
                    discussion.save()
                try:
                    rnd = self._execute(discussion, step)
                except FugueError as exc:
                    # Pause, so the runner doesn't try the same step again and again, and
                    # keep the reason where the page and `fugue status` show it.
                    discussion.state["problem"] = str(exc)
                    discussion.save()
                    discussion.update_control(paused=True)
                    discussion.emit({"type": "notice", "level": "error", "text": str(exc)})
                    break
                finally:
                    transcript.write(discussion)
                if not is_complete(rnd):
                    discussion.update_control(paused=True, stop_requested=False)
                    break
                if agreed(rnd):
                    discussion.emit({"type": "notice", "level": "info",
                                     "text": "No reviewer objects: the council agrees." if rnd["kind"] == "review"
                                     else "No objections on either side: the members agree."})
                # A council pauses only after its vote and after each revision. An agreement
                # with suggestions goes straight on to the closing versions, which end it.
                closing_next = bool(closing_readers(rnd) or council.closing_reviewed(rnd))
                if discussion.read_control()["pause_after_each_round"] and not closing_next and (
                        not council.is_council(discussion.members) or rnd["kind"] in council.PAUSE_AFTER):
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
        if council.is_council(discussion.members):
            return self._execute_council(discussion, step)
        if action == "retry":
            rnd = discussion.rounds[-1]
        elif action == "answer":
            rnd = create_round(discussion, "answer", list(discussion.members), question=step["question"],
                               files=discussion.state.get("attachments"))
        elif action == "ask":
            rnd = create_round(discussion, "answer", list(discussion.members), question=step["request"]["question"],
                               files=step["request"].get("attachments"))
        elif action == "next" and step.get("closing"):
            rnd = create_round(discussion, "update", closing_readers(discussion.rounds[-1]), closing=True)
        elif action == "next":
            rnd = create_round(discussion, next_kind(discussion), list(discussion.members))
        else:
            raise FugueError(f"fugue no longer handles the request {action!r}; it was dropped.")
        run_round(discussion, rnd, should_stop=self._should_stop(discussion))
        return rnd

    def _execute_council(self, discussion: Discussion, step: dict) -> dict:
        action = step["action"]
        if action == "retry":
            rnd = discussion.rounds[-1]
        elif action in ("answer", "ask"):
            question = step["question"] if action == "answer" else step["request"]["question"]
            files = discussion.state.get("attachments") if action == "answer" else step["request"].get("attachments")
            rnd = council.create_round(discussion, "answer", question=question, files=files)
        elif action == "revote":
            rnd = council.create_round(discussion, "answer", reopen=True)
        elif action == "next":
            kind = step.get("kind") or council.NATURAL_NEXT[discussion.rounds[-1]["kind"]]
            if kind == "reopen":
                rnd = council.create_round(discussion, "answer", reopen=True)
            elif kind == "closing":
                rnd = council.create_round(discussion, "revise", closing=True)
            else:
                rnd = council.create_round(discussion, kind)
        else:
            raise FugueError(f"fugue no longer handles the request {action!r}; it was dropped.")
        run_round(discussion, rnd, should_stop=self._should_stop(discussion))
        notice = council.finish_round(discussion, rnd)
        if notice:
            discussion.emit({"type": "notice", "level": "info", "text": notice})
        return rnd
