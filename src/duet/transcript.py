"""Rendering a discussion as one Markdown file the user can read top to bottom."""

from __future__ import annotations

import re
import shlex

from .agents import format_duration
from .prompts import NAMES
from .store import AGENTS, Discussion, other_agent

ROUND_LABELS = {
    "answer": "independent answers",
    "comment": "each comments on the other's answer",
    "update": "each updates their answer",
    # Kinds from discussions made by earlier versions of duet.
    "review": "each reviews the other's answer",
    "discuss": "discussion",
    "final": "final answer",
}

_HEADING = re.compile(r"^(#{1,6})(\s)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def demote_headings(text: str, levels: int = 3) -> str:
    """Push the agent's own headings below the transcript's, leaving code blocks alone."""
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            match = _HEADING.match(line)
            if match:
                depth = min(len(match.group(1)) + levels, 6)
                line = "#" * depth + line[len(match.group(1)):]
        out.append(line)
    return "\n".join(out)


def member_name(agent: str, labels: dict[str, str] | None) -> str:
    """The agent's real name for the user, with its letter in the council, e.g. "Claude (member A)"."""
    return f"{NAMES[agent]} (member {labels[agent]})" if labels else NAMES[agent]


def entry_heading(kind: str, agent: str, labels: dict[str, str] | None = None) -> str:
    name, other = member_name(agent, labels), NAMES[other_agent(agent)]
    return {
        "answer": name,
        "comment": f"{name}, commenting on {other}'s answer",
        "update": f"{name}'s updated answer",
        "review": f"{name}, reviewing {other}'s answer",
        "discuss": f"{name}, replying to {other}",
        "final": f"{name}'s final answer",
    }[kind]


def round_heading(rnd: dict) -> str:
    if rnd["kind"] == "answer" and rnd["index"] > 0:
        return f"Round {rnd['index']}: follow-up question, independent answers"
    return f"Round {rnd['index']}: {ROUND_LABELS[rnd['kind']]}"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def render(discussion: Discussion) -> str:
    state = discussion.state
    sessions = state["sessions"]
    lines = [f"# {discussion.title}", ""]
    lines.append(f"Asked in `{discussion.cwd}` at {state['created_at']}. Discussion id: `{discussion.id}`.")
    lines.append("")
    labels = discussion.labels
    lines.append("The members only know each other by letter: "
                 + ", ".join(f"{NAMES[a]} is member {labels[a]}" for a in AGENTS) + ".")
    lines.append("")
    if sessions:
        lines.append("To carry on with either agent yourself, with its full memory of this discussion:")
        lines.append("")
        if "claude" in sessions:
            lines.append(f"- Claude: `cd {shlex.quote(str(discussion.cwd))} && claude --resume {sessions['claude']}`")
        if "codex" in sessions:
            lines.append(f"- Codex: `cd {shlex.quote(str(discussion.cwd))} && codex resume {sessions['codex']}`")
        lines.append("")
    lines += ["## Question", "", state["question"].strip(), ""]

    for rnd in discussion.rounds:
        lines += ["---", "", f"## {round_heading(rnd)}", ""]
        if rnd["kind"] == "answer" and rnd["index"] > 0:
            lines += [_quote(rnd["question"]), ""]
        for agent in AGENTS:
            entry = rnd["entries"].get(agent)
            if entry is None:
                continue
            lines += [f"### {entry_heading(rnd['kind'], agent, labels)}", ""]
            for note in entry.get("notes", []):
                target = "both agents" if note["shared"] else NAMES[agent]
                lines += [f"Your note to {target}:", "", _quote(note["text"]), ""]
            if entry["status"] == "done":
                lines += [demote_headings(discussion.read_reply(rnd["index"], agent).strip()), ""]
            elif entry["status"] == "running":
                lines += ["*Working...*", ""]
            elif entry["status"] == "failed":
                lines += [f"*Failed after {format_duration(entry.get('duration_s', 0))}: {entry.get('error')}*", ""]
            else:
                lines += ["*Not run yet.*", ""]

    pending = discussion.read_inbox()["notes"]
    if any(pending.values()):
        lines += ["---", "", "## Notes waiting for the next round", ""]
        shown_shared = set()
        for agent in AGENTS:
            for note in pending.get(agent, []):
                if note["shared"]:
                    if note["text"] in shown_shared:
                        continue
                    shown_shared.add(note["text"])
                target = "both agents" if note["shared"] else NAMES[agent]
                lines += [f"To {target}:", "", _quote(note["text"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def write(discussion: Discussion) -> None:
    discussion.transcript_path.write_text(render(discussion))
