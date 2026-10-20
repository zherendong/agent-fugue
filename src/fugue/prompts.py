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

# How a forwarded reply is wrapped, by the kind of round it came from. The
# kinds "discuss" and "final" are from discussions made by earlier versions of
# fugue; "review" is both an old kind and a council round.
ANSWER_KINDS = ("answer", "update", "final", "merge", "revise")
COMMENT_KINDS = ("comment", "review")
BALLOT_KINDS = ("vote",)

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

# The updated answer comes first and the responses to the comments after it,
# so whoever reads the reply (the user, or the page's "Where it ended") meets
# the answer straight away.
UPDATE_TASK = """\
Update your answer in light of member {other}'s comments. Where you can check one of their claims yourself, check it rather than trusting it. Don't give up a point you still believe is right, and don't keep one you now think is wrong. Take the suggestions that improve your answer.

Write your reply in two parts, in this order:

1. Your full updated answer to the question. It must stand on its own for a reader who has seen nothing else: no references to earlier rounds, to "my previous answer" or to the comments. Where a point is still disputed between you and member {other}, say so in the answer and give both views.
2. After the answer, under a heading of its own, your response to member {other}'s objections: for each one, say whether you accept it, accept part of it, or turn it down, with your reasons in a sentence or two. You don't need to explain skipping a suggestion.

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
    """Wrap one forwarded reply so the receiver can tell whose it is, what it is and where it ends.

    It carries the step number the user sees (from 1), so a member citing a step means the same one.
    """
    tag = ("answer" if kind in ANSWER_KINDS else "comments" if kind in COMMENT_KINDS
           else "ballot" if kind in BALLOT_KINDS else "message")
    return f'<{tag} from="member {sender_label}" step="{round_index + 1}">\n{text.strip()}\n</{tag}>'


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


# ---------------------------------------------------------------------------
# The council of three or four
#
# With more than two members, every member still answers on its own first.
# Then each votes for the best answer to start from (not its own); the member
# whose answer wins merges the other members' best points into it; the others
# review the result; it revises; and reviews and revisions alternate until no
# reviewer objects or the round limit is reached. If the reviews stall, or
# most reviewers say the starting point was wrong, everyone answers again and
# the council votes again. Placeholders: {me} the receiving member, {members}
# all members' letters, {editor} the member whose answer was chosen.

COUNCIL_WHO = "You are member {me} of a council of {count}: members {members}."

COUNCIL_LETTERS = (
    "Call yourself member {me} and the others by their letters (member A, member B, ...), in every reply. You may "
    'write the word "member" in the language you are writing in, but keep the letters, and never just say "the '
    'other member": your replies are passed on, and the phrase would mean someone else to the reader. Don\'t name '
    "the tool or model any of you runs on."
)

COUNCIL_FOLLOWUP_INTRO = (
    COUNCIL_WHO + " The user has a follow-up question for the council. Answer it on your own first: the other "
    "members are answering it separately, and afterwards the council will vote on the best answer to start from."
)

REOPEN_INTRO = (
    COUNCIL_WHO + " The council's reviews of member {editor}'s answer have stalled, or most reviewers said it was "
    "the wrong place to start, so the council will vote again. Write your own best answer to the question now, "
    "taking into account everything you have seen in this discussion. It doesn't need to follow member {editor}'s "
    "answer. It must stand on its own for a reader who has seen nothing else."
)

VOTE_INTRO = (
    COUNCIL_WHO + " Each of you answered the question above on your own. Here are the other members' answers, in "
    "an order drawn at random for you:"
)

VOTE_TASK = """\
Decide which of these answers is the best starting point for the council's shared answer. The member whose answer wins will merge the other members' best points into it, and the rest of you will then review the result. Your own answer is not in this list.

For each answer, say briefly what it gets right, and list the problems that would change its conclusions or recommendations, if there are any. Where you can check a claim yourself, check it rather than trusting it. Judge correctness first, then how fully it answers the question, then clarity, and don't let an answer's place in the list sway you.

Write in the same language as the question. End with this line, kept in English and exactly in this form, ranking every answer above from best to worst:

Ranking: <their letters, best first, separated by commas>"""

MERGE_INTRO = (
    COUNCIL_WHO + " The council voted your answer the best starting point. Here are the other members' ballots, "
    "with what each of them found right and wrong in the answers, yours included:"
)

MERGE_TASK = """\
Write the council's shared answer, starting from your own.

1. Merge in the points from the other members' answers that make it more correct or more complete: you read their answers when you voted. Fix the problems the ballots found in your answer where you agree they are problems. Where you can check a claim yourself, check it rather than trusting it.
2. It must stand on its own for a reader who has seen nothing else: no references to members, votes or earlier answers. Where members disagree on something that matters, say so and give the views.
3. After the answer, under a heading of its own, list the points from other members' answers that you did not take, each with the member's letter and your reason in a sentence or two.

Write in the same language as the question."""

REVIEW_INTRO = (
    COUNCIL_WHO + " The council voted member {editor}'s answer the best starting point, and member {editor} merged "
    "the other members' points into it. Here is the result, ending with the points they did not take:"
)
REVIEW_INTRO_LATER = (
    COUNCIL_WHO + " Here is member {editor}'s revised answer. They wrote it after reading the reviews, yours "
    "included, and it ends with their responses to the objections:"
)

REVIEW_TASK = """\
Review this answer as it stands now. Member {editor} will read the reviews and revise it.

1. What is wrong, unsupported or risky, and why. Where you can check a claim yourself, check it rather than trusting it.
2. Points of yours that were left out or misrepresented and still matter: say which, and why the reason given for leaving them out doesn't hold.
3. What important point is missing.{rejections}

Judge only what the answer says now: don't repeat a point it already handles, and don't invent objections.

Keep two kinds of point apart. An objection is a problem that would change the answer's conclusions or recommendations if it were fixed. Anything smaller (more precision, extra detail, better wording) goes in a short list of suggestions at the end, marked as suggestions."""

REVIEW_REJECTIONS = """
4. Where member {editor} turned down one of your earlier objections, say whether their reasons convince you. If they do, drop the point; if not, say what evidence would settle it."""

REVIEW_FOOTER = """\
Write in the same language as the question. End with these two lines, kept in English and exactly in this form even if the rest is in another language:

Objections: <number>
Starting point: <keep or change>

where <number> counts your objections only, not your suggestions (0 if you would accept the answer's conclusions and recommendations as they stand), and "change" means you think another member's answer would have been a clearly better place to start, so the council should answer again and vote again. Write "keep" otherwise."""

REVISE_INTRO = COUNCIL_WHO + " Here are the other members' reviews of the council's answer you wrote:"

REVISE_TASK = """\
Revise the council's answer in light of the reviews. Where you can check a reviewer's claim yourself, check it rather than trusting it. Don't give up a point you still believe is right, and don't keep one you now think is wrong. Take the suggestions that improve the answer.

Write your reply in two parts, in this order:

1. The full revised answer. It must stand on its own for a reader who has seen nothing else: no references to members, reviews or earlier versions. Where members still disagree on something that matters, say so and give the views.
2. After the answer, under a heading of its own, your response to each reviewer's objections: for each one, say whether you accept it, accept part of it, or turn it down, with your reasons in a sentence or two. Then, under its own heading, the points from other members you still have not taken, with your reasons.

Write in the same language as the question."""


def join_letters(letters: list[str]) -> str:
    return letters[0] if len(letters) == 1 else ", ".join(letters[:-1]) + " and " + letters[-1]


def build_council_prompt(
    kind: str,
    *,
    me: str,
    letters: list[str],
    editor: str | None = None,
    question: str | None = None,
    followup: bool = False,
    reopen: bool = False,
    later: bool = False,
    messages: list[tuple[int, str, str, str]] = (),
    notes: list[tuple[str, bool]] = (),
) -> str:
    """Build the message for one member of a council of three or four in one round.

    `messages` holds (round index, round kind, sender letter, reply text) of
    the replies forwarded to this member, in the order to show them. `later`
    is true for reviews after a revision, which may have turned objections down.
    """
    def fill(template: str, **extra) -> str:
        return template.format(me=me, editor=editor, count=len(letters), members=join_letters(letters), **extra)

    blocks = [format_message(sender, i, k, text) for i, k, sender, text in messages]
    parts: list[str] = []

    if kind == "answer" and reopen:
        parts.append(fill(REOPEN_INTRO))
        parts.append(fill(COUNCIL_LETTERS))
    elif kind == "answer" and followup:
        parts.append(fill(COUNCIL_FOLLOWUP_INTRO))
        parts.append(f"<question>\n{(question or '').strip()}\n</question>")
        parts.append(fill(COUNCIL_LETTERS))
    elif kind == "answer":
        # The very first round is the user's question, unchanged.
        parts.append(question or "")
    elif kind == "vote":
        parts.append(fill(VOTE_INTRO))
        parts.extend(blocks)
        parts.append(fill(VOTE_TASK))
        parts.append(fill(COUNCIL_LETTERS))
    elif kind == "merge":
        parts.append(fill(MERGE_INTRO))
        parts.extend(blocks)
        parts.append(fill(MERGE_TASK))
        parts.append(fill(COUNCIL_LETTERS))
    elif kind == "review":
        parts.append(fill(REVIEW_INTRO_LATER if later else REVIEW_INTRO))
        parts.extend(blocks)
        parts.append(fill(REVIEW_TASK, rejections=fill(REVIEW_REJECTIONS) if later else ""))
        parts.append(fill(COUNCIL_LETTERS))
    elif kind == "revise":
        parts.append(fill(REVISE_INTRO))
        parts.extend(blocks)
        parts.append(fill(REVISE_TASK))
        parts.append(fill(COUNCIL_LETTERS))
    else:
        raise ValueError(f"unknown council round kind: {kind}")

    for text, shared in notes:
        header = ("A note from the user (the person who asked the question). Every member received the same note:"
                  if shared else NOTE_PRIVATE)
        parts.append(f"{header}\n\n<user_note>\n{text.strip()}\n</user_note>\n\n{NOTE_TAIL}")

    if kind == "review":
        parts.append(fill(REVIEW_FOOTER))

    return "\n\n".join(parts).strip() + "\n"
