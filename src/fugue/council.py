"""Discussions of three or four members: vote for a starting point, merge, review, revise.

Two members comment on each other's answers and update their own, side by
side. With more members, that would make everyone read everyone and rewrite
every round, so the council works from one shared answer instead:

1. answer   every member answers the question on its own;
2. vote     every member ranks the other members' answers (not its own), each
            voter seeing them in its own random order; the answer with the most
            points (a Borda count) is the starting point, and its author the
            member who writes the shared answer from then on;
3. merge    that member merges the other members' best points into its answer
            and lists the points it didn't take, with reasons;
4. review   every other member reviews the result: objections (problems that
            would change its conclusions), including their own points left out,
            and whether another starting point would have been clearly better;
5. revise   the writing member revises the answer in light of the reviews;

and reviews and revisions alternate until no reviewer objects or the round
limit is reached. When no reviewer objects but some made suggestions, the
writing member writes one closing version that takes in the ones it accepts. When the reviews stall (objections not going down for two
rounds) or most reviewers say the starting point was wrong, every member
answers again and the council votes again, once per question unless the user
asks for more.
"""

from __future__ import annotations

import random
import re

from .attachments import with_attachments
from .prompts import build_council_prompt, join_letters
from .store import Discussion, FugueError, now_iso

# What follows each kind of round when nothing stops it.
NATURAL_NEXT = {"answer": "vote", "vote": "merge", "merge": "review", "revise": "review", "review": "revise"}
# Where "pause after each round" stops a council: after the vote (to see it and
# pick another starting point if you like) and after each revision.
PAUSE_AFTER = {"vote", "revise"}

STEP_LABELS = {
    "vote": "each votes for the best answer to start from",
    "merge": "member {editor} merges the others' points into its answer",
    "review": "the others review member {editor}'s answer",
    "revise": "member {editor} revises the answer",
    "reopen": "each answers again, for a new vote",
    "closing": "member {editor} writes the final version, taking in the last suggestions",
}


def is_council(members: dict | list) -> bool:
    return len(members) >= 3


def is_complete(rnd: dict) -> bool:
    return all(entry["status"] == "done" for entry in rnd["entries"].values())


def question_rounds(rounds: list[dict]) -> list[dict]:
    """The rounds of the latest question: from its first answers on (answering again doesn't start a new one)."""
    starts = [i for i, rnd in enumerate(rounds) if rnd["kind"] == "answer" and not rnd.get("reopen")]
    return rounds[starts[-1]:] if starts else []


def latest(rounds: list[dict], kind: str) -> dict | None:
    return next((rnd for rnd in reversed(question_rounds(rounds)) if rnd["kind"] == kind), None)


def editor_of(rounds: list[dict]) -> str | None:
    """The member who writes the shared answer: chosen by the latest vote."""
    vote = latest(rounds, "vote")
    return (vote.get("vote") or {}).get("chosen") if vote else None


def total_objections(rnd: dict) -> int | None:
    counts = [entry.get("objections") for entry in rnd["entries"].values()]
    return None if any(count is None for count in counts) else sum(counts)


def agreed(rnd: dict) -> bool:
    return (rnd["kind"] == "review" and len(rnd["entries"]) > 0
            and all(e["status"] == "done" and e.get("objections") == 0 for e in rnd["entries"].values()))


def closing_reviewed(rnd: dict) -> bool:
    """A review with no objections in which some reviewer still made suggestions."""
    return agreed(rnd) and any((entry.get("suggestions") or 0) > 0 for entry in rnd["entries"].values())


def wants_new_vote(rounds: list[dict]) -> str | None:
    """Why the council should answer again and vote again, if it should."""
    current = question_rounds(rounds)
    since = max((i for i, rnd in enumerate(current) if rnd["kind"] == "vote"), default=-1)
    reviews = [rnd for rnd in current[since + 1:] if rnd["kind"] == "review" and is_complete(rnd)]
    if not reviews:
        return None
    last = reviews[-1]
    change = sum(1 for e in last["entries"].values() if e.get("starting_point") == "change")
    if change * 2 > len(last["entries"]):
        return "most reviewers said another starting point would have been clearly better"
    totals = [total_objections(rnd) for rnd in reviews[-3:]]
    if len(totals) == 3 and None not in totals and totals[2] > 0 and totals[2] >= totals[1] >= totals[0]:
        return "the objections haven't gone down over the last two reviews"
    return None


def automatic_kind(rounds: list[dict], control: dict) -> str | None:
    """The next round when nobody asked for one, or None when the council is done."""
    last = rounds[-1]
    if last.get("closing"):
        return None
    if last["kind"] != "review":
        return NATURAL_NEXT[last["kind"]]
    if agreed(last):
        return "closing" if closing_reviewed(last) else None
    current = question_rounds(rounds)
    if sum(1 for rnd in current if rnd["kind"] == "revise") >= control["max_rounds"]:
        return None
    if wants_new_vote(rounds) and not any(rnd.get("reopen") for rnd in current):
        return "reopen"
    return "revise"


def step_label(rounds: list[dict], kind: str) -> str:
    editor = editor_of(rounds) or "?"
    return STEP_LABELS[kind].format(editor=editor)


# -- reading what members wrote

RANKING = re.compile(r"ranking\s*[:：]\s*\**\s*(.+)", re.IGNORECASE)
STARTING_POINT = re.compile(r"starting\s+point\s*[:：]\s*\**\s*(keep|change)", re.IGNORECASE)


def _last_lines(reply: str, count: int = 8) -> list[str]:
    return [line for line in reply.splitlines() if line.strip()][-count:]


def parse_ranking(reply: str, candidates: list[str]) -> tuple[list[str] | None, str | None]:
    """The 'Ranking: B, D, C' line a ballot ends with, checked against the answers it was given."""
    for line in reversed(_last_lines(reply)):
        match = RANKING.search(line)
        if not match:
            continue
        found = list(dict.fromkeys(re.findall(r"\b([A-Z])\b", match.group(1).upper())))
        if sorted(found) == sorted(candidates):
            return found, None
        return None, f"ranked {', '.join(found) or 'nothing'} instead of {join_letters(sorted(candidates))}"
    return None, "no Ranking line at the end"


def parse_starting_point(reply: str) -> str | None:
    for line in reversed(_last_lines(reply)):
        match = STARTING_POINT.search(line)
        if match:
            return match.group(1).lower()
    return None


def tally(discussion: Discussion, rnd: dict) -> dict:
    """Count a vote: each ballot gives an answer one point for every answer ranked below it."""
    letters = sorted(rnd["entries"])
    points = dict.fromkeys(letters, 0)
    first = dict.fromkeys(letters, 0)
    valid = 0
    for entry in rnd["entries"].values():
        ranking = entry.get("ranking")
        if not ranking:
            continue
        valid += 1
        for place, letter in enumerate(ranking):
            points[letter] += len(ranking) - 1 - place
        first[ranking[0]] += 1
    rng = random.Random(f"{discussion.id}:{rnd['index']}")
    best = max((points[letter], first[letter]) for letter in letters)
    top = [letter for letter in letters if (points[letter], first[letter]) == best]
    if valid == 0:
        winner, how = rng.choice(letters), "drawn at random, since no ballot could be read"
    elif len(top) > 1:
        winner, how = rng.choice(top), f"drawn at random between {join_letters(top)}, who tied"
    else:
        winner, how = top[0], "by vote"
    return {"points": points, "first": first, "valid": valid, "winner": winner, "chosen": winner, "chosen_by": how}


def record(discussion: Discussion, rnd: dict, letter: str, reply: str) -> None:
    """Read what the round asked for out of one member's reply."""
    entry = rnd["entries"][letter]
    if rnd["kind"] == "vote":
        ranking, problem = parse_ranking(reply, entry.get("candidates", []))
        entry["ranking"] = ranking
        entry["ballot_problem"] = problem
    elif rnd["kind"] == "review":
        entry["starting_point"] = parse_starting_point(reply)


def finish_round(discussion: Discussion, rnd: dict) -> str | None:
    """Work that follows a finished round: counting a vote. Returns a notice for the user, if any."""
    if rnd["kind"] != "vote" or not is_complete(rnd):
        return None
    rnd["vote"] = tally(discussion, rnd)
    discussion.save()
    vote = rnd["vote"]
    return (f"Member {vote['chosen']}'s answer is the starting point ({vote['points'][vote['chosen']]} points, "
            f"{vote['chosen_by']}).")


def choose_editor(discussion: Discussion, letter: str) -> None:
    """Start from another member's answer than the vote chose: only right after the vote."""
    if letter not in discussion.members:
        raise FugueError(f"No member {letter!r}.")
    last = discussion.rounds[-1] if discussion.rounds else None
    if not last or last["kind"] != "vote" or not is_complete(last) or "vote" not in last:
        raise FugueError("You can pick the starting point only right after a vote, before the merge starts.")
    last["vote"]["chosen"] = letter
    last["vote"]["chosen_by"] = "by you" if letter != last["vote"]["winner"] else "by vote"
    discussion.save()


# -- writing the messages for a round


def create_round(discussion: Discussion, kind: str, *, question: str | None = None, reopen: bool = False,
                 files: list[dict] | None = None, closing: bool = False) -> dict:
    """Add a council round, writing the exact message each participant will receive."""
    rounds = discussion.rounds
    if rounds and not is_complete(rounds[-1]):
        raise FugueError("The last round hasn't finished. Continue the discussion to retry it first.")
    index = len(rounds)
    letters = list(discussion.members)
    editor = editor_of(rounds)
    rnd: dict = {"index": index, "kind": kind, "created_at": now_iso(), "entries": {}}
    if kind == "answer":
        rnd["question"] = question
        if reopen:
            rnd["reopen"] = True
        if files:
            rnd["attachments"] = files
    elif kind != "vote" and editor is None:
        raise FugueError(f"A {kind} round needs a vote first.")
    if closing:
        rnd["closing"] = True

    planned: dict[str, dict] = {}
    previous = rounds[-1] if rounds else None
    if kind == "answer":
        for letter in letters:
            planned[letter] = {"messages": []}
    elif kind == "vote":
        if previous is None or previous["kind"] != "answer":
            raise FugueError("A vote follows the members' answers.")
        for letter in letters:
            others = [other for other in previous["entries"] if other != letter]
            random.Random(f"{discussion.id}:{index}:{letter}").shuffle(others)
            planned[letter] = {"candidates": others, "messages": [
                (previous["index"], "answer", other, discussion.read_reply(previous["index"], other)) for other in others]}
    elif kind == "merge":
        planned[editor] = {"messages": [
            (previous["index"], "vote", voter, discussion.read_reply(previous["index"], voter))
            for voter in previous["entries"] if voter != editor]}
    elif kind == "review":
        if previous["kind"] not in ("merge", "revise"):
            raise FugueError("A review follows the shared answer.")
        shared = (previous["index"], previous["kind"], editor, discussion.read_reply(previous["index"], editor))
        for letter in letters:
            if letter != editor:
                planned[letter] = {"messages": [shared], "later": previous["kind"] == "revise"}
    elif kind == "revise":
        if previous["kind"] != "review":
            raise FugueError("A revision follows the reviews.")
        planned[editor] = {"messages": [
            (previous["index"], "review", reviewer, discussion.read_reply(previous["index"], reviewer))
            for reviewer in previous["entries"]]}
    else:
        raise FugueError(f"Unknown council round {kind!r}.")

    for letter, plan in planned.items():
        notes = discussion.take_notes(letter)
        asked = question
        if kind == "answer" and question is not None:
            asked = with_attachments(question, files, images_attached=discussion.members[letter]["tool"] == "codex")
        prompt = build_council_prompt(
            kind, me=letter, letters=letters, editor=editor, question=asked,
            followup=kind == "answer" and index > 0 and not reopen, reopen=reopen, later=plan.get("later", False),
            closing=closing,
            messages=plan["messages"], notes=[(n["text"], n["shared"]) for n in notes])
        discussion.prompt_path(index, letter).write_text(prompt)
        entry = {"status": "pending", "notes": notes,
                 "forwarded": [[i, sender] for i, _, sender, _ in plan["messages"]]}
        if "candidates" in plan:
            entry["candidates"] = plan["candidates"]
        rnd["entries"][letter] = entry
    rounds.append(rnd)
    discussion.save()
    return rnd
