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
from .prompts import NAMES
from .store import AGENTS, Discussion, FugueError, list_discussions, select_discussion


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


def print_entries(discussion: Discussion, rnd: dict, agents: list[str]) -> None:
    for agent in agents:
        entry = rnd["entries"][agent]
        heading = transcript.entry_heading(rnd["kind"], agent, discussion.labels)
        print(f"\n━━━ Round {rnd['index']} · {heading} ━━━\n")
        if entry["status"] == "done":
            print(discussion.read_reply(rnd["index"], agent).rstrip())
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
        parts = [f"{NAMES[a]} {format_duration(time.monotonic() - w['since'])} · {w['doing']}"
                 for a, w in self.working.items()]
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
        kind, agent = event["type"], event.get("agent")
        if kind == "round_started":
            self.discussion.reload()  # the runner keeps its own copy of the state
            label = transcript.round_heading(self.discussion.rounds[event["round"]])
            self._clear()
            err(f"{label}: asking {' and '.join(NAMES[a] for a in event['agents'])}")
        elif kind == "agent_started":
            self.working[agent] = {"since": time.monotonic(), "doing": "starting"}
        elif kind == "activity" and agent in self.working:
            self.working[agent]["doing"] = event["text"]
        elif kind == "text_delta" and agent in self.working:
            self.working[agent]["doing"] = "writing"
        elif kind == "progress" and agent in self.working:
            self.working[agent]["doing"] = f"thinking (about {event['thinking_tokens']:,} tokens)"
        elif kind == "agent_finished":
            self.working.pop(agent, None)
            self._clear()
            if event["ok"]:
                objections = event.get("objections")
                extra = "" if objections is None else f", {objections} objection{'' if objections == 1 else 's'}"
                err(f"  {NAMES[agent]} finished in {format_duration(event['duration_s'])}{extra}")
            else:
                err(f"  {NAMES[agent]} stopped after {format_duration(event['duration_s'])}: {event['error']}")
        elif kind == "round_finished" and not self.quiet:
            self.discussion.reload()
            rnd = self.discussion.rounds[event["round"]]
            self._clear()
            print_entries(self.discussion, rnd, [a for a, e in rnd["entries"].items() if e["status"] == "done"])
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


def cmd_start(args: argparse.Namespace) -> int:
    question = read_text(args.question, args.file, "question")
    discussion = new_discussion(
        question,
        cwd=Path(args.cwd) if args.cwd else Path.cwd(),
        title=args.title,
        timeout_minutes=args.timeout,
        models={"claude": args.claude_model, "codex": args.codex_model},
        efforts={"claude": args.claude_effort, "codex": args.codex_effort},
        web=False if args.no_web else None,
        extra_args={"claude": args.claude_args, "codex": args.codex_args},
        control={
            "max_rounds": args.rounds,
            "pause_after_each_round": False if args.no_pause else None,
        },
    )
    err(f"Started discussion {discussion.id}")
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
    discussion.add_request({"type": "ask", "question": question})
    discussion.update_control(paused=False)
    return run_here(discussion, args.quiet)


def cmd_pause(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    discussion.update_control(paused=True)
    if discussion.runner_pid():
        err("It will pause when the current round finishes. `fugue stop` stops the agents now.")
    else:
        err("Paused. `fugue continue` goes on.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    if not discussion.runner_pid():
        err("It isn't running.")
        return 0
    discussion.update_control(stop_requested=True)
    err("Stopping the agents. `fugue continue` retries the round later.")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    text = read_text(args.text, args.file, "note")
    to = list(AGENTS) if args.to == "both" else [args.to]
    discussion.add_note(text, to)
    names = " and ".join(NAMES[a] for a in to)
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
    pause = "pausing after each round" if control["pause_after_each_round"] else "without pausing"
    print(f"Plan:       up to {control['max_rounds']} rounds of comments and updates after the answers, {pause}")
    if status.get("next"):
        print(f"Next:       {status['next']}")
    print(f"Web:        {'both members may use it' if state['settings'].get('web', True) else 'off for both members'}")
    labels = discussion.labels
    print(f"Claude:     member {labels['claude']}, {describe_choice('claude', state['settings'])}")
    print(f"Codex:      member {labels['codex']}, {describe_choice('codex', state['settings'])}")
    print(f"Transcript: {discussion.transcript_path}")
    print()
    rows = [("Round", "What", "Claude", "Codex")]
    for rnd in discussion.rounds:
        what = transcript.round_heading(rnd).split(": ", 1)[1]
        rows.append((str(rnd["index"]), what, _cell(rnd["entries"].get("claude")),
                     _cell(rnd["entries"].get("codex"))))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths)) + "  " + row[3])

    inbox = discussion.read_inbox()
    waiting = [NAMES[a] for a in AGENTS if inbox["notes"].get(a)]
    if waiting:
        print(f"\nNotes waiting for the next round, for: {', '.join(waiting)}")
    for request in inbox["requests"]:
        print(f"Queued: {request['type']}")

    entries = [e for rnd in discussion.rounds for e in rnd["entries"].values()]
    cost = sum(e.get("cost_usd") or 0 for e in entries)
    tokens_in = sum((e.get("tokens") or {}).get("input_tokens", 0) for e in entries)
    tokens_out = sum((e.get("tokens") or {}).get("output_tokens", 0) for e in entries)
    print(f"\nUsage so far: Claude reported ${cost:.2f}; Codex used {tokens_in:,} input and "
          f"{tokens_out:,} output tokens.")

    sessions = state["sessions"]
    if sessions:
        print("\nTo carry on with either agent yourself, with its full memory of this discussion:")
        if "claude" in sessions:
            print(f"  cd {shlex.quote(str(discussion.cwd))} && claude --resume {sessions['claude']}")
        if "codex" in sessions:
            print(f"  cd {shlex.quote(str(discussion.cwd))} && codex resume {sessions['codex']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    if not discussion.rounds:
        raise FugueError("This discussion has no rounds yet.")
    try:
        rnd = discussion.rounds[args.round if args.round is not None else -1]
    except IndexError:
        raise FugueError(f"There is no round {args.round}; this discussion has "
                        f"{len(discussion.rounds)} (0 to {len(discussion.rounds) - 1}).") from None
    agents = [a for a in AGENTS if a in rnd["entries"] and (args.agent in (None, a))]
    if args.prompt:
        for agent in agents:
            print(f"\n━━━ Round {rnd['index']} · exactly what fugue sent to {NAMES[agent]} ━━━\n")
            print(discussion.prompt_path(rnd["index"], agent).read_text().rstrip())
        print()
    else:
        print_entries(discussion, rnd, agents)
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
    err(f"Moved {discussion.id} to the Trash. Claude's and Codex's own sessions are kept, so "
        "`claude --resume` and `codex resume` still find them.")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    discussion = select_discussion(args.discussion)
    transcript.write(discussion)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, str(discussion.transcript_path)], check=False)
    return 0


def describe_choice(agent: str, settings: dict) -> str:
    """How one agent runs in a discussion, e.g. "claude-opus-5-5 at high"."""
    chosen = settings.get(agent, {})
    model = chosen.get("model") or "its default model"
    effort = f"effort {chosen['effort']}" if chosen.get("effort") else "its default effort"
    return f"{model}, {effort}"


def cmd_models(args: argparse.Namespace) -> int:
    from .models import catalog

    known = catalog(refresh=args.refresh)
    for agent in AGENTS:
        entry = known[agent]
        defaults = entry["defaults"]
        default = defaults.get("model") or "not set"
        if defaults.get("effort"):
            default += f" at {defaults['effort']}"
        checked = ""
        if entry.get("checked_at"):
            checked = time.strftime(", checked %Y-%m-%d %H:%M", time.localtime(entry["checked_at"]))
        print(f"{NAMES[agent]} ({entry['source']}{checked})")
        print(f"  Default, used when you choose nothing: {default}")
        width = max(len(m["id"]) for m in entry["models"])
        for model in entry["models"]:
            extra = f"  (default {model['default_effort']})" if model.get("default_effort") else ""
            print(f"  {model['id']:<{width}}  {model['name']:<18}  {' '.join(model['efforts'])}{extra}")
        print()
    err("Use them with `fugue start --claude-model ID --claude-effort LEVEL` (and --codex-...), "
        "or pick them on the page.")
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
        description="Ask Claude Code and Codex the same question, then let them review each other.",
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

    p = sub.add_parser("start", parents=[quiet], help="ask both agents a new question",
                       description="Ask both agents a new question. Each answers on its own; then rounds of "
                                   "comments (each comments on the other's answer) and updates (each rewrites "
                                   "its answer in light of the comments) alternate, until neither has "
                                   "objections or the round limit is reached. It pauses after every round "
                                   "until you run `fugue continue`, unless you pass --no-pause.")
    p.add_argument("question", nargs="?",
                   help="the question; use - to read it from stdin, or leave it out to write it in $EDITOR")
    p.add_argument("-f", "--file", help="read the question from this file")
    p.add_argument("-n", "--rounds", type=int,
                   help="most rounds of comments and updates after the answers (default: 2 of each)")
    p.add_argument("--no-pause", action="store_true", help="run all the rounds without waiting for you")
    p.add_argument("--no-web", action="store_true",
                   help="don't let either member use the web (on by default for both)")
    p.add_argument("--title", help="short name for the discussion (default: the question's first line)")
    p.add_argument("--cwd", help="folder the agents work in (default: the current folder)")
    p.add_argument("--claude-model", metavar="ID", help="model for Claude, e.g. claude-opus-5-5 (see `fugue models`)")
    p.add_argument("--codex-model", metavar="ID", help="model for Codex, e.g. gpt-6.1-sol (see `fugue models`)")
    p.add_argument("--claude-effort", metavar="LEVEL", help="effort for Claude, e.g. high (see `fugue models`)")
    p.add_argument("--codex-effort", metavar="LEVEL", help="reasoning effort for Codex, e.g. medium")
    p.add_argument("--claude-args", metavar="ARGS",
                   help='extra arguments for `claude -p`, e.g. --claude-args="--add-dir ../shared"')
    p.add_argument("--codex-args", metavar="ARGS",
                   help="extra arguments for `codex exec`, e.g. --codex-args='-c web_search=\"disabled\"'")
    p.add_argument("--timeout", type=float, metavar="MINUTES",
                   help=f"give up on an agent after this long (default: {DEFAULT_TIMEOUT_MINUTES})")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("continue", parents=[which, quiet], help="run the next round, or retry after a failure")
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser("next", parents=[which, quiet],
                       help="run one more round, even after they agreed or reached the limit")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("ask", parents=[which, quiet], help="ask both agents a follow-up question",
                       description="Ask a follow-up question in the same discussion. Each agent answers on "
                                   "its own first, then comment and update rounds follow as before.")
    p.add_argument("question", nargs="?", help="the question; - for stdin, or leave it out to use $EDITOR")
    p.add_argument("-f", "--file", help="read the question from this file")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("note", parents=[which], help="add your own view to the next round",
                       description="Save a note from you. fugue adds it to the next message it sends.")
    p.add_argument("text", nargs="?", help="the note; use - for stdin, or leave it out to use $EDITOR")
    p.add_argument("-f", "--file", help="read the note from this file")
    p.add_argument("--to", choices=["both", "claude", "codex"], default="both",
                   help="who gets the note (default: both)")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("pause", parents=[which], help="pause when the current round finishes")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("stop", parents=[which], help="stop the agents now, mid-round")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", parents=[which], help="show the rounds so far and how to resume each agent")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", parents=[which], help="print the replies from one round")
    p.add_argument("round", nargs="?", type=int, help="round number (default: the last one)")
    p.add_argument("--agent", choices=["claude", "codex"], help="only this agent's reply")
    p.add_argument("--prompt", action="store_true", help="show exactly what fugue sent instead of the reply")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("models", help="list the models and effort levels you can choose for each agent",
                       description="Codex's list comes from `codex debug models` (fugue keeps a copy for a "
                                   "day); Claude's is built into fugue, since Claude Code has no such command.")
    p.add_argument("--refresh", action="store_true", help="ask Codex again instead of using fugue's copy")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("list", help="list all discussions, newest first")
    p.add_argument("--here", action="store_true", help="only the ones that run in this folder")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete", help="move a discussion to the Trash",
                       description="Move a discussion's folder to the Trash, where Finder can restore it. "
                                   "Claude's and Codex's own sessions are kept.")
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
