"""The `fugue` command line."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, engine, transcript
from .agents import format_duration
from .config import DEFAULT_TIMEOUT_MINUTES, new_discussion
from .store import (MAX_MEMBERS, TOOL_NAMES, TOOLS, Discussion, FugueError, list_discussions, select_discussion,
                    step_number)


def err(message: str = "") -> None:
    print(message, file=sys.stderr)


def read_text(text: str | None, file: str | None, what: str) -> str:
    """Take text from the argument, a file, stdin (`-` or a pipe), or else an editor."""
    if file:
        content = Path(file).read_text()
    elif text and text != "-":
        content = text
    elif text == "-" or not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        content = edit_in_editor()
    content = content.strip()
    if not content:
        raise FugueError(f"The {what} is empty; nothing to do.")
    return content


def edit_in_editor() -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="fugue-", delete=False) as handle:
        path = handle.name
    try:
        subprocess.run([*shlex.split(editor), path], check=False)
        return Path(path).read_text()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Showing a discussion in the terminal while it runs


def print_entries(discussion: Discussion, rnd: dict, letters: list[str]) -> None:
    for letter in letters:
        entry = rnd["entries"][letter]
        heading = transcript.entry_heading(rnd["kind"], discussion, letter)
        print(f"\n━━━ Step {step_number(rnd['index'])} · {heading} ━━━\n")
        if entry["status"] == "done":
            print(discussion.read_reply(rnd["index"], letter).rstrip())
        else:
            print(f"(No reply: {entry.get('error') or 'not run yet'})")
    print(flush=True)


class TerminalView:
    """Prints progress for a discussion running in this process: one live status line, then the replies."""

    def __init__(self, discussion: Discussion, quiet: bool):
        self.discussion = discussion
        self.quiet = quiet
        self.live = sys.stderr.isatty()
        self.working: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.done = threading.Event()
        if self.live:
            threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while not self.done.wait(1.0):
            with self.lock:
                self._draw()

    def _draw(self) -> None:
        if not self.live or not self.working:
            return
        width = shutil.get_terminal_size((100, 20)).columns
        parts = [f"{self.discussion.name(m)} {format_duration(time.monotonic() - w['since'])} · {w['doing']}"
                 for m, w in self.working.items()]
        line = "  " + "  │  ".join(parts)
        sys.stderr.write("\r\x1b[2K" + (line if len(line) < width else line[: width - 2] + "…"))
        sys.stderr.flush()

    def _clear(self) -> None:
        if self.live:
            sys.stderr.write("\r\x1b[2K")

    def on_event(self, event: dict) -> None:
        with self.lock:
            self._handle(event)
            self._draw()

    def _handle(self, event: dict) -> None:
        kind, member = event["type"], event.get("member")
        name = self.discussion.name
        if kind == "round_started":
            self.discussion.reload()  # the runner keeps its own copy of the state
            label = transcript.round_heading(self.discussion.rounds[event["round"]], len(self.discussion.members) > 2)
            self._clear()
            err(f"{label}: asking {' and '.join(name(m) for m in event['members'])}")
        elif kind == "agent_started":
            self.working[member] = {"since": time.monotonic(), "doing": "starting"}
        elif kind == "activity" and member in self.working:
            self.working[member]["doing"] = event["text"]
        elif kind == "text_delta" and member in self.working:
            self.working[member]["doing"] = "writing"
        elif kind == "progress" and member in self.working:
            self.working[member]["doing"] = f"thinking (about {event['thinking_tokens']:,} tokens)"
        elif kind == "agent_finished":
            self.working.pop(member, None)
            self._clear()
            if event["ok"]:
                objections = event.get("objections")
                extra = "" if objections is None else f", {objections} objection{'' if objections == 1 else 's'}"
                err(f"  {name(member)} finished in {format_duration(event['duration_s'])}{extra}")
            else:
                err(f"  {name(member)} stopped after {format_duration(event['duration_s'])}: {event['error']}")
        elif kind == "round_finished" and not self.quiet:
            self.discussion.reload()
            rnd = self.discussion.rounds[event["round"]]
            self._clear()
            print_entries(self.discussion, rnd, [m for m, e in rnd["entries"].items() if e["status"] == "done"])
        elif kind == "notice":
            self._clear()
            err(event["text"])

    def close(self) -> None:
        self.done.set()
        with self.lock:
            self._clear()


def print_footer(discussion: Discussion) -> None:
    discussion.reload()
    status = engine.describe(discussion)
    err(f"Transcript: {discussion.transcript_path}")
    err(f"Status: {status['label']}")
    if status.get("detail"):
        err(f"  {status['detail']}")
    if status["phase"] == "running":
        err("It is running in another process. Watch it with `fugue web`.")
    elif status["phase"] == "finished":
        err('Ask a follow-up with `fugue ask "..."`, or run one more round with `fugue next`.')
    else:
        err(f"Next: {status['next']}. Run `fugue continue` for that, or add your view first with "
            '`fugue note "..."`.')


def run_here(discussion: Discussion, quiet: bool) -> int:
    """Run the discussion in this process until it needs the user."""
    view = TerminalView(discussion, quiet)
    runner = engine.Runner(discussion.id, listener=view.on_event)
    try:
        runner.run()
    finally:
        view.close()
    print_footer(discussion)
    return 0 if engine.describe(discussion)["phase"] in ("finished", "waiting", "paused", "running") else 1


# ---------------------------------------------------------------------------
# Commands


def parse_members(specs: list[str] | None, extra: list[str] | None) -> list[dict] | None:
    """The members from `--member TOOL[:MODEL[:EFFORT]]` (given two to four times) and their `--member-args`, in order."""
    if not specs:
        if extra:
            raise FugueError("--member-args goes with --member; to change the saved line-up, use the "
                             "Settings page or the config file.")
        return None
    if not 2 <= len(specs) <= MAX_MEMBERS:
        raise FugueError(f"Give --member two to {MAX_MEMBERS} times, once for each member, or leave it out to use "
                         "your saved line-up.")
    extra = extra or []
    if len(extra) > len(specs):
        raise FugueError("There are more --member-args than members.")
    members = []
    for index, spec in enumerate(specs):
        tool, _, rest = spec.partition(":")
        model, _, effort = rest.partition(":")
        members.append({"tool": tool.strip().lower(), "model": model, "effort": effort,
                        "args": extra[index] if index < len(extra) else ""})
    return members


def read_attachments(paths: list[str] | None) -> list[tuple[str, bytes]]:
    files = []
    for given in paths or []:
        path = Path(given).expanduser()
        if not path.is_file():
            raise FugueError(f"Not a file: {given}")
        files.append((path.name, path.read_bytes()))
    return files


def cmd_start(args: argparse.Namespace) -> int:
    question = read_text(args.question, args.file, "question")
    if args.no_folder and args.cwd:
        raise FugueError("--no-folder and --cwd don't go together.")
    discussion = new_discussion(
        question,
        cwd=None if args.no_folder else Path(args.cwd) if args.cwd else Path.cwd(),
        attachments=read_attachments(args.attach),
        title=args.title,
        timeout_minutes=args.timeout,
        members=parse_members(args.member, args.member_args),
        web=False if args.no_web else None,
        control={
            "max_rounds": args.rounds,
            "pause_after_each_round": False if args.no_pause else None,
        },
    )
    err(f"Started discussion {discussion.id}")
    for letter in discussion.members:
        err(f"  Member {letter}: {TOOL_NAMES[discussion.members[letter]['tool']]}, "
            f"{describe_choice(discussion.members[letter])}")
    return run_here(discussion, args.quiet)


def cmd_continue(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    discussion.update_control(paused=False)
    return run_here(discussion, args.quiet)


def cmd_next(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    last = discussion.rounds[-1] if discussion.rounds else None
    if last is None or not engine.is_complete(last):
        discussion.update_control(paused=False)  # continuing retries the unfinished round
    else:
        discussion.add_request({"type": "next"})
    return run_here(discussion, args.quiet)


def cmd_ask(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    question = read_text(args.question, args.file, "question")
    request = {"type": "ask", "question": question}
    files = read_attachments(args.attach)
    if files:
        request["attachments"] = discussion.attach(files)
    discussion.add_request(request)
    discussion.update_control(paused=False)
    return run_here(discussion, args.quiet)


def cmd_revote(args: argparse.Namespace) -> int:
    from .council import is_council

    discussion = select_discussion(args.discussion)
    if not is_council(discussion.members):
        raise FugueError("Voting again needs a council of three or four members.")
    discussion.add_request({"type": "revote"})
    discussion.update_control(paused=False)
    return run_here(discussion, args.quiet)


def cmd_pause(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    discussion.update_control(paused=True)
    if discussion.runner_pid():
        err("It will pause when the current step finishes. `fugue stop` stops the agents now.")
    else:
        err("Paused. `fugue continue` goes on.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    if not discussion.runner_pid():
        err("It isn't running.")
        return 0
    discussion.update_control(stop_requested=True)
    err("Stopping the agents. `fugue continue` retries the step later.")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    text = read_text(args.text, args.file, "note")
    target = args.to.upper() if args.to != "both" else "both"
    if target != "both" and target not in discussion.members:
        raise FugueError(f"--to must be both or a member's letter ({', '.join(discussion.members)}).")
    to = list(discussion.members) if target == "both" else [target]
    discussion.add_note(text, to)
    names = " and ".join(discussion.name(m) for m in to)
    err(f"Saved. It will be added to the next message fugue sends to {names}.")
    return 0


def _cell(entry: dict | None) -> str:
    if entry is None:
        return "-"
    if entry["status"] == "failed":
        return f"failed: {entry.get('error', '')}"[:60]
    if entry["status"] == "running":
        return "working"
    if entry["status"] != "done":
        return "not run yet"
    text = f"done in {format_duration(entry.get('duration_s', 0))}"
    objections = entry.get("objections")
    if objections is not None:
        text += f", {objections} objection{'' if objections == 1 else 's'}"
    elif entry.get("remaining") is not None:  # from earlier versions of fugue
        text += f", {entry['remaining']} disagreements left"
    return text


def cmd_status(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    state, control = discussion.state, discussion.read_control()
    status = engine.describe(discussion)
    print(f"Discussion {discussion.id}")
    print(f"Question:   {discussion.title}")
    print(f"Runs in:    {discussion.cwd}")
    print(f"Status:     {status['label']}")
    in_council = len(discussion.members) > 2
    pause = ("pausing after the vote and each revision" if in_council else "pausing after each step") \
        if control["pause_after_each_round"] else "without pausing"
    rounds = "review and revision" if in_council else "comments and updates"
    print(f"Plan:       up to {control['max_rounds']} rounds of {rounds} after the answers, {pause}")
    if status.get("next"):
        print(f"Next:       {status['next']}")
    everyone = "every member" if in_council else "both members"
    print(f"Web:        {f'{everyone} may use it' if state['settings'].get('web', True) else 'off'}")
    letters = list(discussion.members)
    for letter in letters:
        print(f"Member {letter}:   {TOOL_NAMES[discussion.members[letter]['tool']]}, "
              f"{describe_choice(discussion.members[letter])}")
    print(f"Transcript: {discussion.transcript_path}")
    print()
    rows = [("Step", "What", *(f"Member {letter}" for letter in letters))]
    for rnd in discussion.rounds:
        what = transcript.round_heading(rnd, in_council).split(": ", 1)[1]
        rows.append((str(step_number(rnd["index"])), what, *(_cell(rnd["entries"].get(letter)) for letter in letters)))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row[:-1], widths)) + "  " + row[-1])

    inbox = discussion.read_inbox()
    waiting = [discussion.name(letter) for letter in letters if inbox["notes"].get(letter)]
    if waiting:
        print(f"\nNotes waiting for the next step, for: {', '.join(waiting)}")
    for request in inbox["requests"]:
        print(f"Queued: {request['type']}")

    print("\nUsage so far:")
    for letter in letters:
        entries = [rnd["entries"][letter] for rnd in discussion.rounds if letter in rnd["entries"]]
        if discussion.members[letter]["tool"] == "claude":
            cost = sum(e.get("cost_usd") or 0 for e in entries)
            print(f"  {discussion.name(letter)}: reported ${cost:.2f}")
        else:
            tokens_in = sum((e.get("tokens") or {}).get("input_tokens", 0) for e in entries)
            tokens_out = sum((e.get("tokens") or {}).get("output_tokens", 0) for e in entries)
            print(f"  {discussion.name(letter)}: {tokens_in:,} input and {tokens_out:,} output tokens")

    sessions = state["sessions"]
    if sessions:
        print("\nTo carry on with a member yourself, with its full memory of this discussion:")
        for letter, session_id in sessions.items():
            print(f"  {discussion.name(letter)}: {transcript.resume_command(discussion, letter, session_id)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    if not discussion.rounds:
        raise FugueError("This discussion has no steps yet.")
    count = len(discussion.rounds)
    if args.step is not None and not 1 <= args.step <= count:
        raise FugueError(f"There is no step {args.step}; this discussion has {count} (1 to {count}).")
    rnd = discussion.rounds[args.step - 1 if args.step is not None else -1]
    wanted = args.member.upper() if args.member else None
    if wanted and wanted not in discussion.members:
        raise FugueError(f"--member must be one of the letters {', '.join(discussion.members)}.")
    letters = [m for m in discussion.members if m in rnd["entries"] and wanted in (None, m)]
    if args.prompt:
        for letter in letters:
            heading = f"Step {step_number(rnd['index'])} · exactly what fugue sent to {discussion.name(letter)}"
            print(f"\n━━━ {heading} ━━━\n")
            print(discussion.prompt_path(rnd["index"], letter).read_text().rstrip())
        print()
    else:
        print_entries(discussion, rnd, letters)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    discussions = list_discussions()
    if args.here:
        here = str(Path.cwd().resolve())
        discussions = [d for d in discussions if d.state["cwd"] == here]
    if not discussions:
        err("No discussions yet.")
        return 0
    home = str(Path.home())
    for discussion in reversed(discussions):
        folder = discussion.state["cwd"].replace(home, "~", 1)
        status = engine.describe(discussion)["label"]
        print(f"{discussion.id}  {status:<28}  {folder}  {discussion.title}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.id)
    if not args.yes:
        if not sys.stdin.isatty():
            raise FugueError("Pass --yes to delete without being asked.")
        answer = input(f"Move discussion {discussion.id} ({discussion.title}) to the Trash? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            err("Kept.")
            return 1
    discussion.move_to_trash()
    err(f"Moved {discussion.id} to the Trash. The members' own sessions are kept, so "
        "`claude --resume`, `codex resume` and `dsh --session-id` still find them.")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    transcript.write(discussion)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, str(discussion.transcript_path)], check=False)
    return 0


def describe_choice(member: dict) -> str:
    """How one member runs, e.g. "claude-opus-5-5, effort high"."""
    model = member.get("model") or "its default model"
    effort = f"effort {member['effort']}" if member.get("effort") else "its default effort"
    extra = f", with {shlex.join(member['args'])}" if member.get("args") else ""
    return f"{model}, {effort}{extra}"


def cmd_models(args: argparse.Namespace) -> int:
    from .models import catalog

    known = catalog(refresh=args.refresh)
    for tool in TOOLS:
        entry = known[tool]
        defaults = entry["defaults"]
        default = defaults.get("model") or "not set"
        if defaults.get("effort"):
            default += f" at {defaults['effort']}"
        checked = ""
        if entry.get("checked_at"):
            checked = time.strftime(", checked %Y-%m-%d %H:%M", time.localtime(entry["checked_at"]))
        print(f"{TOOL_NAMES[tool]}, tool name `{tool}` ({entry['source']}{checked})")
        print(f"  Default, used when you choose nothing: {default}")
        width = max(len(m["id"]) for m in entry["models"])
        for model in entry["models"]:
            extra = f"  (default {model['default_effort']})" if model.get("default_effort") else ""
            print(f"  {model['id']:<{width}}  {model['name']:<18}  {' '.join(model['efforts'])}{extra}")
        print()
    err("Use them with `fugue start --member TOOL:MODEL:EFFORT --member TOOL:MODEL:EFFORT`, "
        "for example --member claude:claude-opus-5-5:high --member codex::medium, or pick them on the page.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .server import serve

    return serve(port=args.port, open_browser=not args.no_open, default_cwd=Path.cwd())


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    if args.action == "install":
        return service.install(args.port)
    return {"uninstall": service.uninstall, "status": service.status, "restart": service.restart}[args.action]()


# ---------------------------------------------------------------------------
# Parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fugue",
        description="Ask two to four coding agents (Claude Code, Codex and the DeepSeek Harness, in any mix) "
                    "the same question, then let them check each other's answers.",
    )
    parser.add_argument("--version", action="version", version=f"fugue {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    which = argparse.ArgumentParser(add_help=False)
    which.add_argument("-d", "--discussion", metavar="ID",
                       help="discussion id, or a unique start of it (default: the latest one in this folder)")
    quiet = argparse.ArgumentParser(add_help=False)
    quiet.add_argument("-q", "--quiet", action="store_true", help="show progress only, not the replies")

    p = sub.add_parser("web", help="open the web page that shows all discussions live")
    p.add_argument("--port", type=int, default=int(os.environ.get("FUGUE_PORT", 8765)),
                   help="port on 127.0.0.1 (default: 8765)")
    p.add_argument("--no-open", action="store_true", help="don't open the browser")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("service", help="keep the web page running in the background (macOS)",
                       description="Run `fugue web` in the background as a launchd agent: it starts when you "
                                   "log in and starts again if it stops, so the page answers without a "
                                   "terminal window open. It uses the PATH of the shell you install it from.")
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    s = actions.add_parser("install", help="install it and start it now (again, to change its settings)")
    s.add_argument("--port", type=int, default=int(os.environ.get("FUGUE_PORT", 8765)),
                   help="port on 127.0.0.1 (default: 8765)")
    actions.add_parser("uninstall", help="stop it and remove it")
    actions.add_parser("status", help="show whether it is running and the page answers")
    actions.add_parser("restart", help="restart it, for example after changing fugue's code")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("start", parents=[quiet], help="ask the members a new question",
                       description="Ask the members a new question. Each answers on its own. Two members then "
                                   "alternate comments (each comments on the other's answer) and updates (each "
                                   "rewrites its answer in light of the comments); three or four vote for the "
                                   "best answer to start from, its author merges in the others' points, and the "
                                   "others review it while the author revises. It goes on until no one objects "
                                   "or the round limit is reached. It pauses after every step (a council after "
                                   "its vote and each revision) until you run `fugue continue`, unless you pass "
                                   "--no-pause.")
    p.add_argument("question", nargs="?",
                   help="the question; use - to read it from stdin, or leave it out to write it in $EDITOR")
    p.add_argument("-f", "--file", help="read the question from this file")
    p.add_argument("-n", "--rounds", type=int,
                   help="most rounds after the answers: of comments and updates, or in a council of reviews "
                        "and revisions (default: 2)")
    p.add_argument("--no-pause", action="store_true", help="run every step without waiting for you")
    p.add_argument("--no-web", action="store_true",
                   help="don't let any member use the web (on by default for every member)")
    p.add_argument("--title", help="short name for the discussion (default: the question's first line)")
    p.add_argument("--cwd", help="folder the members work in (default: the current folder)")
    p.add_argument("--no-folder", action="store_true",
                   help="no project folder: the members work only from the question and its attachments")
    p.add_argument("--attach", action="append", metavar="FILE",
                   help="attach a file to the question (PDF, Word, images, text; repeat for more)")
    p.add_argument("--member", action="append", metavar="TOOL[:MODEL[:EFFORT]]",
                   help="a member: claude, codex or dsh (the DeepSeek Harness), optionally with a model and effort "
                        "(see `fugue models`). Give it two to four times, e.g. --member claude:claude-opus-5-5:high "
                        "--member codex:gpt-6.1-sol --member dsh:deepseek-flash; leave out a part for the tool's "
                        "own default. Two members comment on each other's answers; three or four vote for a "
                        "starting point, which its author merges and the others review. Without --member, your "
                        "saved line-up is used (Settings on the page).")
    p.add_argument("--member-args", action="append", metavar="ARGS",
                   help='extra arguments for the matching --member (`claude -p`, `codex exec` or '
                        '`dsh --profile headless`), in the same order, e.g. --member-args="--add-dir ../shared"')
    p.add_argument("--timeout", type=float, metavar="MINUTES",
                   help=f"give up on an agent after this long (default: {DEFAULT_TIMEOUT_MINUTES})")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("continue", parents=[which, quiet], help="run the next step, or retry after a failure")
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser("next", parents=[which, quiet],
                       help="run one more round, even after they agreed or reached the limit")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("ask", parents=[which, quiet], help="ask the members a follow-up question",
                       description="Ask a follow-up question in the same discussion. Each member answers on "
                                   "its own first; then the discussion goes on as before: comments and updates "
                                   "with two members, or a vote, a merge and reviews in a council.")
    p.add_argument("question", nargs="?", help="the question; - for stdin, or leave it out to use $EDITOR")
    p.add_argument("-f", "--file", help="read the question from this file")
    p.add_argument("--attach", action="append", metavar="FILE", help="attach a file to the follow-up (repeat for more)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("note", parents=[which], help="add your own view to the next step",
                       description="Save a note from you. fugue adds it to the next message it sends.")
    p.add_argument("text", nargs="?", help="the note; use - for stdin, or leave it out to use $EDITOR")
    p.add_argument("-f", "--file", help="read the note from this file")
    p.add_argument("--to", default="both", metavar="both|LETTER",
                   help="who gets the note: both (every member), or one member's letter, A to D (default: both)")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("revote", parents=[which, quiet],
                       help="a council of three or four: everyone answers again, then votes again",
                       description="Every member writes its own best answer again, taking in the discussion so "
                                   "far, and the council votes again on where to start.")
    p.set_defaults(func=cmd_revote)

    p = sub.add_parser("pause", parents=[which], help="pause when the current step finishes")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("stop", parents=[which], help="stop the members now, mid-step")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", parents=[which], help="show the steps so far and how to resume each member")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", parents=[which], help="print the replies from one step")
    p.add_argument("step", nargs="?", type=int, help="step number, from 1 (default: the last one)")
    p.add_argument("--member", metavar="LETTER", help="only this member's reply (its letter, A to D)")
    p.add_argument("--prompt", action="store_true", help="show exactly what fugue sent instead of the reply")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("models", help="list the models and effort levels you can choose for each tool",
                       description="Codex's list comes from `codex debug models` (fugue keeps a copy for a "
                                   "day); Claude's and DeepSeek's are built into fugue, since Claude Code and the "
                                   "DeepSeek Harness have no such command.")
    p.add_argument("--refresh", action="store_true", help="ask Codex again instead of using fugue's copy")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("list", help="list all discussions, newest first")
    p.add_argument("--here", action="store_true", help="only the ones that run in this folder")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete", help="move a discussion to the Trash",
                       description="Move a discussion's folder to the Trash, where Finder can restore it "
                                   "(without the `trash` command, to fugue's own trash folder). The members' "
                                   "own sessions in Claude Code, Codex or the DeepSeek Harness are kept.")
    p.add_argument("id", help="the discussion's id, or a unique start of it (see `fugue list`)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("open", parents=[which], help="open the transcript in your default Markdown app")
    p.set_defaults(func=cmd_open)
    return parser


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Being killed or losing the terminal stops the agents cleanly, like Ctrl-C,
    # instead of leaving them running on their own.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _interrupt)
        signal.signal(signal.SIGHUP, _interrupt)
    try:
        return args.func(args)
    except FugueError as exc:
        err(f"fugue: {exc}")
        return 1
    except KeyboardInterrupt:
        err("\nStopped. `fugue continue` retries the agents that didn't finish.")
        return 130
