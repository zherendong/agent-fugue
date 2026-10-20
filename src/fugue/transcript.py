"""Rendering a discussion as one Markdown file the user can read top to bottom."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .agents import format_duration
from .store import Discussion, step_number

ROUND_LABELS = {
    "answer": "independent answers",
    # A council of three or four.
    "vote": "each votes for the best answer to start from",
    "merge": "the chosen member merges the others' points",
    "revise": "the chosen member revises the answer",
    "comment": "each comments on the other's answer",
    "update": "each updates their answer",
    # Kinds from discussions made by earlier versions of fugue.
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


COUNCIL_REVIEW_LABEL = "the others review the answer"


def entry_heading(kind: str, discussion: Discussion, letter: str) -> str:
    if len(discussion.members) > 2:
        name = discussion.name(letter)
        return {
            "answer": name,
            "vote": f"{name}'s vote",
            "merge": f"{name} merges the others' points into its answer",
            "review": f"{name}'s review",
            "revise": f"{name}'s revised answer",
        }[kind]
    other_letter = discussion.other(letter)
    # Two members can have the same name (same tool, model and effort); then the letter tells them apart.
    same = discussion.name(letter, with_letter=False) == discussion.name(other_letter, with_letter=False)
    name, other = discussion.name(letter), discussion.name(other_letter, with_letter=same)
    return {
        "answer": name,
        "comment": f"{name}, commenting on {other}'s answer",
        "update": f"{name}'s updated answer",
        "review": f"{name}, reviewing {other}'s answer",
        "discuss": f"{name}, replying to {other}",
        "final": f"{name}'s final answer",
    }[kind]


def resume_command(discussion: Discussion, letter: str, session_id: str) -> str:
    """The command that opens a member's session in its own tool, with its memory of the discussion."""
    folder = shlex.quote(str(discussion.cwd))
    if discussion.members[letter]["tool"] == "claude":
        return f"cd {folder} && claude --resume {session_id}"
    return f"cd {folder} && codex resume {session_id}"


def round_heading(rnd: dict, in_council: bool = False) -> str:
    """A round's heading, as a step: "Step 2: each comments on the other's answer"."""
    number = step_number(rnd["index"])
    if rnd.get("closing"):
        return f"Step {number}: the final version, taking in the last suggestions"
    if rnd["kind"] == "answer" and rnd.get("reopen"):
        return f"Step {number}: everyone answers again, for a new vote"
    if rnd["kind"] == "answer" and rnd["index"] > 0:
        return f"Step {number}: follow-up question, independent answers"
    if rnd["kind"] == "review" and in_council:
        return f"Step {number}: {COUNCIL_REVIEW_LABEL}"
    return f"Step {number}: {ROUND_LABELS[rnd['kind']]}"


def vote_summary(rnd: dict) -> str:
    """One line on how a council's vote came out."""
    vote = rnd.get("vote")
    if not vote:
        return ""
    points = ", ".join(f"member {letter} {count}" for letter, count in
                       sorted(vote["points"].items(), key=lambda item: -item[1]))
    return (f"Points: {points}. Starting point: member {vote['chosen']}'s answer, {vote['chosen_by']} "
            f"({vote['valid']} of {len(rnd['entries'])} ballots counted).")


def attachment_lines(records: list[dict] | None) -> list[str]:
    if not records:
        return []
    lines = ["Attached:", ""]
    for record in records:
        extra = " (and its text: " + Path(record["text"]).name + ")" if record.get("text") else ""
        lines.append(f"- `{record['path']}`{extra}")
    return lines + [""]


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def render(discussion: Discussion) -> str:
    state = discussion.state
    sessions = state["sessions"]
    lines = [f"# {discussion.title}", ""]
    lines.append(f"Asked in `{discussion.cwd}` at {state['created_at']}. Discussion id: `{discussion.id}`.")
    lines.append("")
    lines.append("The members only know each other by letter: "
                 + ", ".join(f"member {letter} is {discussion.name(letter, with_letter=False)}"
                             for letter in discussion.members) + ".")
    lines.append("")
    if sessions:
        lines.append("To carry on with a member yourself, with its full memory of this discussion:")
        lines.append("")
        for letter, session_id in sessions.items():
            lines.append(f"- {discussion.name(letter)}: `{resume_command(discussion, letter, session_id)}`")
        lines.append("")
    lines += ["## Question", "", state["question"].strip(), ""]
    lines += attachment_lines(state.get("attachments"))

    in_council = len(discussion.members) > 2
    for rnd in discussion.rounds:
        lines += ["---", "", f"## {round_heading(rnd, in_council)}", ""]
        if rnd["kind"] == "answer" and rnd["index"] > 0 and not rnd.get("reopen"):
            lines += [_quote(rnd["question"]), ""]
            lines += attachment_lines(rnd.get("attachments"))
        if rnd.get("vote"):
            lines += [vote_summary(rnd), ""]
        for letter in discussion.members:
            entry = rnd["entries"].get(letter)
            if entry is None:
                continue
            lines += [f"### {entry_heading(rnd['kind'], discussion, letter)}", ""]
            for note in entry.get("notes", []):
                target = "both members" if note["shared"] else f"member {letter}"
                lines += [f"Your note to {target}:", "", _quote(note["text"]), ""]
            if entry["status"] == "done":
                lines += [demote_headings(discussion.read_reply(rnd["index"], letter).strip()), ""]
            elif entry["status"] == "running":
                lines += ["*Working...*", ""]
            elif entry["status"] == "failed":
                lines += [f"*Failed after {format_duration(entry.get('duration_s', 0))}: {entry.get('error')}*", ""]
            else:
                lines += ["*Not run yet.*", ""]

    pending = discussion.read_inbox()["notes"]
    if any(pending.values()):
        lines += ["---", "", "## Notes waiting for the next step", ""]
        shown_shared = set()
        for letter in discussion.members:
            for note in pending.get(letter, []):
                if note["shared"]:
                    if note["text"] in shown_shared:
                        continue
                    shown_shared.add(note["text"])
                target = "both members" if note["shared"] else f"member {letter}"
                lines += [f"To {target}:", "", _quote(note["text"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def write(discussion: Discussion) -> None:
    discussion.transcript_path.write_text(render(discussion))
