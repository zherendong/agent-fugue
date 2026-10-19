"""The messages fugue sends to the council's members.

Everything the members are told about each other is in this file, so the
wording can be tuned in one place.

The council works in rounds that every member takes part in, in parallel:
first each answers the question on its own, then comment rounds (each member
comments on the other members' current answers) alternate with update rounds
(each member reads the comments on its own answer and rewrites it). Each
round depends only on the round before it, so no message has to keep track of
replies that are still being written.

Members never learn which agent or model the others are. Each has a letter
for the whole discussion ("member A", "member B"), assigned at random, so
that nobody defers to, or discounts, a name, and so that a reply forwarded
from one member to another still says unambiguously who is meant: "the other
member" would mean someone different to the reader than to the writer.

Long forwarded content goes first and the instructions come after it,
because models follow instructions better when they come after the material
they refer to. Placeholders: {me} is the receiving member's letter, {other}
the other member's.
"""

from __future__ import annotations

NAMES = {"claude": "Claude", "codex": "Codex"}  # for the user's eyes only, never sent to a member

# How a forwarded reply is wrapped, by the kind of round it came from. The
# kinds "review", "discuss" and "final" are from discussions made by earlier
# versions of fugue.
ANSWER_KINDS = ("answer", "update", "final")
COMMENT_KINDS = ("comment", "review")

WHO = "You are member {me} of a council of two; the other member is member {other}."

LETTERS = (
    "Call yourself member {me} and the other member member {other}, in every reply. You may write the "
    'word "member" in the language you are writing in, but keep the letters, and never just say "the '
    "other member\": your replies are passed on, and the phrase would mean someone else to the reader. "
    "Don't name the tool or model either of you runs on."
)

COMMENT_INTRO = WHO + " You both answered the question above on your own. Here is member {other}'s current answer:"
COMMENT_INTRO_LATER = (
    WHO + " Here is member {other}'s current answer. They wrote it after reading your comments on their "
    "previous answer:"
)

COMMENT_TASK = """\
Comment on member {other}'s answer as it stands now. Member {other} will read your comments and update their answer; you will update yours in the next round, so don't rewrite your own answer here.

1. What they got right that you missed or gave too little weight. Say so plainly.
2. What is wrong, unsupported or risky, and why. Where you can check one of their claims yourself (in the code, the docs, or by running a command), check it rather than trusting it.
3. What important point is missing.{rejections}

Judge only what their answer says now: don't repeat a point it already handles, and don't invent objections. If their answer shows a mistake in yours, note it in one line.

Keep two kinds of point apart. An objection is a problem that would change their answer's conclusions or recommendations if it were fixed: an error, an unsupported claim, or an omission that matters for the user's decision. Anything smaller (more precision, extra detail, better wording, a nice-to-have) goes in a short list of suggestions at the end, marked as suggestions; member {other} may take them or leave them."""

COMMENT_REJECTIONS = """
4. Where member {other} turned down one of your earlier comments, say whether their reasons convince you. If they do, drop the point; if not, say what evidence would settle it."""

COMMENT_FOOTER = """\
Write in the same language as the question. End with this line, kept in English and exactly in this form even if the rest is in another language:

Objections: <number>

where <number> counts your objections only, not your suggestions: the problems in member {other}'s current answer that would change its conclusions or recommendations if fixed. Write 0 if you would accept its conclusions and recommendations as they stand."""

UPDATE_INTRO = WHO + " Here are member {other}'s comments on your answer:"

UPDATE_TASK = """\
Update your answer.

First, go through member {other}'s objections: for each one, say whether you accept it, accept part of it, or turn it down, with your reasons in a sentence or two. Where you can check one of their claims yourself, check it rather than trusting it. Don't give up a point you still believe is right, and don't keep one you now think is wrong. Take the suggestions that improve your answer; you don't need to explain skipping one.

Then write your full updated answer to the question. It must stand on its own for a reader who has seen nothing else: no references to earlier rounds or to "my previous answer". Where a point is still disputed between you and member {other}, say so in the answer and give both views.

Write in the same language as the question."""

FOLLOWUP_INTRO = (
    WHO + " The user has a follow-up question for the council. Answer it on your own first: member "
    "{other} is answering it separately, and afterwards you will comment on each other's answers."
)
FOLLOWUP_CONTEXT = (
    "For context, here is what member {other} said last in the discussion so far, which you haven't "
    "seen yet. You don't need to reply to it directly."
)

NOTE_SHARED = "A note from the user (the person who asked the question). Member {other} received the same note:"
NOTE_PRIVATE = "A note from the user (the person who asked the question), sent only to you:"
NOTE_TAIL = "Take it into account in this reply."


def format_message(sender_label: str, round_index: int, kind: str, text: str) -> str:
    """Wrap one forwarded reply so the receiver can tell whose it is, what it is and where it ends."""
    tag = "answer" if kind in ANSWER_KINDS else "comments" if kind in COMMENT_KINDS else "message"
    return f'<{tag} from="member {sender_label}" round="{round_index}">\n{text.strip()}\n</{tag}>'


def build_prompt(
    kind: str,
    *,
    me: str,
    other: str,
    question: str | None = None,
    followup: bool = False,
    messages: list[tuple[int, str, str]] = (),
    notes: list[tuple[str, bool]] = (),
) -> str:
    """Build the message for one member in one round.

    `me` and `other` are the members' letters. `messages` holds (round index,
    round kind, reply text) of the other member's replies this member hasn't
    seen yet. `notes` holds (text, shared) pairs from the user, where `shared`
    means the other member got the same note.
    """
    def fill(template: str, **extra) -> str:
        return template.format(me=me, other=other, **extra)

    blocks = [format_message(other, i, k, text) for i, k, text in messages]
    parts: list[str] = []

    if kind == "answer" and not followup:
        # The very first round is the user's question, unchanged, so each member
        # answers exactly as it would if the user had asked it directly.
        parts.append(question or "")
    elif kind == "answer":
        parts.append(fill(FOLLOWUP_INTRO))
        if blocks:
            parts.append(fill(FOLLOWUP_CONTEXT))
            parts.extend(blocks)
        parts.append(f"<question>\n{(question or '').strip()}\n</question>")
        parts.append(fill(LETTERS))
    elif kind == "comment":
        # After an update round, the other member's answer was written with your
        # earlier comments in hand, and may turn some of them down.
        later = any(k == "update" for _, k, _ in messages)
        parts.append(fill(COMMENT_INTRO_LATER if later else COMMENT_INTRO))
        parts.extend(blocks)
        parts.append(fill(COMMENT_TASK, rejections=fill(COMMENT_REJECTIONS) if later else ""))
        parts.append(fill(LETTERS))
    elif kind == "update":
        parts.append(fill(UPDATE_INTRO))
        parts.extend(blocks)
        parts.append(fill(UPDATE_TASK))
        parts.append(fill(LETTERS))
    else:
        raise ValueError(f"unknown round kind: {kind}")

    for text, shared in notes:
        header = fill(NOTE_SHARED) if shared else NOTE_PRIVATE
        parts.append(f"{header}\n\n<user_note>\n{text.strip()}\n</user_note>\n\n{NOTE_TAIL}")

    if kind == "comment":
        parts.append(fill(COMMENT_FOOTER))

    return "\n\n".join(parts).strip() + "\n"
