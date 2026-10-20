"""Tests of fugue's engine and command line against fake `claude` and `codex` commands (see tests/fakes)."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from fugue import cli, engine
from fugue.engine import parse_objections, plan
from fugue.store import DEFAULT_CONTROL, Discussion, list_discussions
from fugue.transcript import demote_headings, resume_command

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "project"
    work.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("FUGUE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUGUE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("FUGUE_CLAUDE_BIN", str(FAKES / "claude"))
    monkeypatch.setenv("FUGUE_CODEX_BIN", str(FAKES / "codex"))
    monkeypatch.setenv("FUGUE_DSH_BIN", str(FAKES / "dsh"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "user" / ".dsh"))
    monkeypatch.setenv("FAKE_LOG_DIR", str(logs))
    # A home folder of its own, so the user's real Claude and Codex settings don't leak in.
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-test-big"\nmodel_reasoning_effort = "high"\n\n'
        '[plugins."chrome@openai-bundled"]\nenabled = true\n\n'
        '[plugins."old@openai-bundled"]\nenabled = false\n\n'
        '[mcp_servers.node_repl]\ncommand = "/bin/node_repl"\n\n'
        '[mcp_servers.computer-use]\ncommand = "/bin/cua"\nenabled = false\n')
    (home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("FUGUE_TRASH", str(tmp_path / "trash"))  # never the real Trash
    return {"work": work, "logs": logs, "trash": tmp_path / "trash", "config": tmp_path / "no-config.toml"}


def calls(env) -> list[dict]:
    path = env["logs"] / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def latest() -> Discussion:
    return list_discussions()[-1]


def kinds(discussion: Discussion) -> list[str]:
    return [r["kind"] for r in discussion.rounds]


def events(discussion: Discussion) -> list[dict]:
    return [json.loads(line) for line in discussion.events_path.read_text().splitlines()]


def to(agent: str, batch: list[dict]) -> dict:
    """The call to this tool in a batch of calls (the fakes log their tool as "agent")."""
    return next(c for c in batch if c["agent"] == agent)


def letter_of(discussion: Discussion, tool: str) -> str:
    """The letter of the member that uses this tool, in a discussion with one Claude and one Codex."""
    return next(letter for letter, member in discussion.members.items() if member["tool"] == tool)


def fugue(*args: str) -> int:
    return cli.main(list(args))


def without_forwarded(prompt: str) -> str:
    """fugue's own words in a prompt, without the other member's forwarded replies."""
    return re.sub(r"<(answer|comments|message|ballot) [^>]*>[\s\S]*?</\1>", "", prompt)


def test_it_pauses_after_every_round_by_default(env):
    assert fugue("start", "Should we cache embeddings?", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer"]
    status = engine.describe(discussion)
    assert status["phase"] == "paused" and status["next"] == "each comments on the other's answer"
    for call in calls(env):
        assert call["prompt"].strip() == "Should we cache embeddings?"
        assert Path(call["cwd"]).resolve() == env["work"].resolve()
    assert fugue("continue", "-q") == 0
    assert kinds(latest()) == ["answer", "comment"]
    assert engine.describe(latest())["next"] == "each updates their answer"
    assert fugue("continue", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update"]
    # Nothing is written into the project folder.
    assert list(env["work"].iterdir()) == []


def test_a_round_that_cannot_start_pauses_and_says_why(env):
    folder = env["work"] / "moved"
    folder.mkdir()
    assert fugue("start", "Q?", "--cwd", str(folder), "-q") == 0
    folder.rmdir()
    fugue("continue", "-q")
    status = engine.describe(latest())
    assert status["phase"] in ("paused", "stopped") and "no longer exists" in status["detail"]
    # The reason stays until a round starts again.
    folder.mkdir()
    assert fugue("continue", "-q") == 0
    assert kinds(latest()) == ["answer", "comment"]
    assert not engine.describe(latest()).get("detail")


def test_without_pausing_it_runs_to_the_round_limit(env):
    assert fugue("start", "Q?", "--no-pause", "-q") == 0
    discussion = latest()
    # Every comment round has objections, so it runs the default 2 rounds of comments and updates.
    assert kinds(discussion) == ["answer", "comment", "update", "comment", "update"]
    assert len(calls(env)) == 10
    status = engine.describe(discussion)
    assert status["phase"] == "finished" and status["label"] == "Finished at the round limit"
    for letter in discussion.members:
        assert f"{discussion.name(letter)} 1" in status["detail"]
    assert "runner" not in discussion.state
    text = discussion.transcript_path.read_text()
    assert "Claude reply number 1." in text and "Codex reply number 5." in text


def test_it_stops_once_neither_member_has_objections(env, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    assert fugue("start", "Q?", "--no-pause", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "comment"]
    assert engine.describe(discussion)["label"] == "Agreed in step 2"
    # One more round can still be asked for; without pausing, its update is then commented on again.
    assert fugue("next", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update", "comment"]
    assert engine.describe(latest())["label"] == "Agreed in step 4"


def test_read_only_flags_and_sessions(env):
    fugue("start", "Q?", "-n", "1", "--no-pause", "-q")
    made = calls(env)
    claude_first, codex_first = to("claude", made[:2]), to("codex", made[:2])
    assert "--resume" not in claude_first["args"]
    assert "stream-json" in claude_first["args"]
    args = claude_first["args"]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--disallowedTools") + 1].startswith("Edit,Write,NotebookEdit,Read(~/.ssh/**)")
    # The web is on by default: search and page fetches, but not from this machine.
    assert args[args.index("--allowedTools") + 1] == "Bash,Read(//**),WebSearch,WebFetch"
    assert args[args.index("--disallowedTools") + 1].endswith("WebFetch(domain:localhost),WebFetch(domain:127.0.0.1)")
    sandbox = json.loads(args[args.index("--settings") + 1])["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"] and not sandbox["allowUnsandboxedCommands"]
    assert str(env["work"].resolve()) in sandbox["filesystem"]["denyWrite"]
    assert "~/.ssh" in sandbox["filesystem"]["denyRead"]
    assert codex_first["args"][:2] != ["exec", "resume"]
    assert 'sandbox_mode="read-only"' in codex_first["args"]
    assert not any("web_search" in a for a in codex_first["args"])
    # What in the user's Codex setup acts outside its sandbox is switched off for the run.
    joined = " ".join(codex_first["args"])
    assert "--disable apps" in joined
    assert "plugins.chrome@openai-bundled.enabled=false" in codex_first["args"]
    assert "mcp_servers.node_repl.enabled=false" in codex_first["args"]
    assert "old@openai-bundled" not in joined and "computer-use" not in joined  # already off
    # Nothing chosen: no model or effort is passed, so each tool uses its own settings.
    assert "--effort" not in claude_first["args"] and "--model" not in claude_first["args"]
    assert not any("model_reasoning_effort" in a for a in codex_first["args"]) and "-m" not in codex_first["args"]
    discussion = latest()
    sessions = discussion.state["sessions"]
    claude_second, codex_second = to("claude", made[2:]), to("codex", made[2:])
    claude_letter, codex_letter = letter_of(discussion, "claude"), letter_of(discussion, "codex")
    assert claude_second["args"][claude_second["args"].index("--resume") + 1] == sessions[claude_letter]
    assert codex_second["args"][:2] == ["exec", "resume"] and codex_second["session"] == sessions[codex_letter]
    name = claude_first["args"][claude_first["args"].index("--name") + 1]
    assert name == f"fugue: Q? (member {claude_letter})"
    assert 'sandbox_mode="read-only"' in codex_second["args"]


def test_web_can_be_turned_off_for_both(env):
    fugue("start", "Q?", "-n", "0", "--no-web", "-q")
    made = calls(env)
    claude, codex = to("claude", made)["args"], to("codex", made)["args"]
    assert claude[claude.index("--allowedTools") + 1] == "Bash,Read(//**)"
    assert claude[claude.index("--disallowedTools") + 1].endswith("WebSearch,WebFetch")
    assert 'web_search="disabled"' in codex
    assert latest().state["settings"]["web"] is False


def test_each_round_forwards_the_right_message(env):
    fugue("start", "Q?", "--no-pause", "-q")
    made = calls(env)
    # Round 1: each comments on the other's answer, and only on that.
    comment = made[2:4]
    to_claude, to_codex = to("claude", comment)["prompt"], to("codex", comment)["prompt"]
    assert "Codex reply number 1." in to_claude and "Claude reply number" not in to_claude
    assert "Claude reply number 1." in to_codex and "Codex reply number" not in to_codex
    discussion = latest()
    me, other = letter_of(discussion, "claude"), letter_of(discussion, "codex")
    assert f'<answer from="member {other}" step="1">' in to_claude
    assert f"You are member {me} of a council of two; the other member is member {other}." in to_claude
    assert "Objections: <number>" in to_claude and "turned down" not in to_claude
    # Round 2: each gets the comments on its own answer and rewrites it in full.
    update = to("claude", made[4:6])["prompt"]
    assert "Codex reply number 2." in update and "Codex reply number 1." not in update
    assert f'<comments from="member {other}" step="2">' in update
    assert f"Here are member {other}'s comments on your answer" in update
    assert "full updated answer" in update and "Objections: <number>" not in update
    # Round 3: each comments on the other's updated answer, which may turn down earlier comments.
    later = to("claude", made[6:8])["prompt"]
    assert "Codex reply number 3." in later and "Codex reply number 2." not in later
    assert "after reading your comments" in later and "turned down" in later
    assert discussion.rounds[1]["entries"][me]["objections"] == 1
    assert discussion.rounds[1]["entries"][other]["objections"] == 1  # parsed through the **bold**


def test_update_prompt_asks_for_the_answer_before_the_responses():
    from fugue.prompts import build_prompt

    prompt = build_prompt("update", me="A", other="B", messages=[(1, "comment", "Comments from B.")])
    comments = prompt.index('<comments from="member B" step="2">')
    answer = prompt.index("1. Your full updated answer")
    responses = prompt.index('2. A line that says exactly "## Responses"')
    assert comments < answer < responses
    assert "You are member A of a council of two" in prompt
    # The answer stands on its own: the discussion about it goes after the heading.
    assert "give both views" not in prompt and "Leave the discussion out of it" in prompt
    assert 'Write about yourself in the first person ("I")' in prompt and "Call yourself" not in prompt


def test_comments_stay_on_the_question_and_count_their_suggestions():
    from fugue.prompts import build_prompt

    prompt = build_prompt("comment", me="A", other="B", messages=[(0, "answer", "B's answer.")])
    assert "What important point is missing for what the user asked" in prompt
    assert "a suggestion at most" in prompt and "not how you went about it" in prompt
    assert prompt.rstrip().endswith("write 0 if you have none.")
    assert "Objections: <number>\nSuggestions: <number>" in prompt
    assert engine.parse_suggestions("Points.\n\nObjections: 0\nSuggestions: 3") == 3
    assert engine.parse_suggestions("Points.\n\nObjections: 0") is None


def test_suggestions_without_objections_get_one_closing_version(env, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    monkeypatch.setenv("FAKE_SUGGESTIONS", "2")
    # Pausing after every step doesn't hold up the closing versions: they end the discussion.
    assert fugue("start", "Q?", "-q") == 0
    assert fugue("continue", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "comment", "update"]
    closing = discussion.rounds[2]
    assert closing.get("closing") and sorted(closing["entries"]) == ["A", "B"]
    prompt = discussion.prompt_path(2, "A").read_text()
    assert "Member B has no objections to your answer" in prompt and "This is your final version" in prompt
    status = engine.describe(discussion)
    assert status["phase"] == "finished" and status["label"] == "Agreed in step 2"


def test_only_members_whose_answers_got_suggestions_write_a_closing_version():
    rounds = [{"index": 0, "kind": "answer", "entries": {}},
              {"index": 1, "kind": "comment", "entries": {
                  "A": {"status": "done", "objections": 0, "suggestions": 2},
                  "B": {"status": "done", "objections": 0, "suggestions": 0}}}]
    # A's suggestions are about B's answer, so only B writes a closing version.
    assert engine.closing_readers(rounds[1]) == ["B"]
    assert engine.automatic_step(rounds, dict(DEFAULT_CONTROL)) == {"action": "next", "closing": True}
    rounds[1]["entries"]["A"]["suggestions"] = None  # a comment from before the count was asked for
    assert engine.automatic_step(rounds, dict(DEFAULT_CONTROL)) is None


def test_each_member_keeps_one_letter_for_the_whole_discussion(env, monkeypatch):
    fugue("start", "Q?", "-n", "1", "--no-pause", "-q")
    discussion = latest()
    assert sorted(discussion.members) == ["A", "B"]
    for call in calls(env)[2:]:
        me = letter_of(discussion, call["agent"])
        other = discussion.other(me)
        assert f"You are member {me} " in call["prompt"] and f'from="member {other}"' in call["prompt"]
    # The letters are drawn at random for each new discussion, so the first member isn't always "A".
    import fugue.store as store
    assert {store.assign_letters(2)[0] for _ in range(30)} == {"A", "B"}
    assert store.assign_letters(2, seed="x") == store.assign_letters(2, seed="x")


def test_members_never_see_each_others_names(env):
    fugue("start", "Q?", "--no-pause", "-q")
    fugue("note", "Mind the latency.", "--to", "both")
    fugue("ask", "And what about cost?", "-q")
    for call in calls(env):
        own_words = without_forwarded(call["prompt"])
        assert "Claude" not in own_words and "Codex" not in own_words, own_words


def test_notes_reach_only_their_recipient(env, capsys):
    fugue("start", "Q?", "-q")
    assert fugue("note", "Hi.", "--to", "codex") == 1  # by letter, not by tool
    assert "--to must be both or a member's letter" in capsys.readouterr().err
    assert fugue("note", "Please consider latency on mobile.", "--to", letter_of(latest(), "codex").lower()) == 0
    assert fugue("continue", "-q") == 0
    comment = calls(env)[2:]
    assert "latency on mobile" in to("codex", comment)["prompt"]
    assert "sent only to you" in to("codex", comment)["prompt"]
    assert "latency on mobile" not in to("claude", comment)["prompt"]
    fugue("continue", "-q")
    assert all("latency on mobile" not in c["prompt"] for c in calls(env)[4:])
    assert "latency on mobile" in latest().transcript_path.read_text()


def test_failed_agent_stops_the_discussion_and_is_retried_alone(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    assert fugue("start", "Q?", "-q") == 1
    discussion = latest()
    claude_letter, codex_letter = letter_of(discussion, "claude"), letter_of(discussion, "codex")
    assert discussion.rounds[0]["entries"][claude_letter]["status"] == "done"
    assert discussion.rounds[0]["entries"][codex_letter]["status"] == "failed"
    status = engine.describe(discussion)
    assert status["phase"] == "stopped" and "simulated Codex failure" in status["detail"]
    assert status["next"].startswith("retry step 1")
    assert codex_letter not in discussion.state["sessions"]

    monkeypatch.delenv("FAKE_CODEX_FAIL")
    before = len(calls(env))
    assert fugue("continue", "-q") == 0
    retried = calls(env)[before:]
    assert [c["agent"] for c in retried] == ["codex"]
    assert retried[0]["prompt"].strip() == "Q?"
    assert "--resume" not in retried[0]["args"] and retried[0]["args"][:2] != ["exec", "resume"]
    assert kinds(latest()) == ["answer"] and engine.describe(latest())["phase"] == "paused"


def test_follow_up_question_is_answered_separately_then_commented_on(env):
    fugue("start", "Q?", "-n", "1", "--no-pause", "-q")
    assert kinds(latest()) == ["answer", "comment", "update"]
    assert fugue("ask", "And what about cost?", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update", "answer", "comment", "update"]
    prompt = to("claude", calls(env)[6:8])["prompt"]
    assert "follow-up question" in prompt and "And what about cost?" in prompt
    assert "Codex reply number 3." in prompt  # Codex's updated answer, which Claude hadn't seen
    assert "Codex reply number 4." in to("claude", calls(env)[8:10])["prompt"]


def test_live_events(env):
    fugue("start", "Q?", "-n", "1", "--no-pause", "-q")
    discussion = latest()
    claude, codex = letter_of(discussion, "claude"), letter_of(discussion, "codex")
    log = events(discussion)
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1))
    texts = {(e["member"], e["text"]) for e in log if e["type"] == "activity"}
    assert (claude, "Read src/cache.py") in texts
    assert (claude, "File does not exist.") in texts
    assert (codex, "$ rg -n cache src") in texts
    assert (codex, "exit code 1: rg -n cache src") in texts
    assert (codex, "Checking the cache layer") in texts
    deltas = "".join(e["text"] for e in log if e["type"] == "text_delta" and e["member"] == claude and e["round"] == 0)
    assert deltas.startswith("Claude reply number 1.")
    assert any(e["type"] == "progress" and e["member"] == claude and e["thinking_tokens"] == 1500 for e in log)
    finished = [e for e in log if e["type"] == "agent_finished" and e["round"] == 1]
    assert {e["member"]: e["objections"] for e in finished} == {"A": 1, "B": 1}
    assert [e["type"] for e in log][:2] == ["runner", "round_started"]
    assert sorted(log[1]["members"]) == ["A", "B"]


def test_stop_mid_round(env, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "20")
    from fugue.config import new_discussion

    discussion = new_discussion("Q?", cwd=env["work"])
    runner = engine.Runner(discussion.id)
    thread = threading.Thread(target=runner.run)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and len(calls(env)) < 2:
        time.sleep(0.1)
    # A second runner sees the first one and leaves.
    assert engine.Runner(discussion.id).run() is False
    discussion.reload()
    assert engine.describe(discussion)["phase"] == "running"
    started = time.time()
    discussion.update_control(stop_requested=True)
    thread.join(15)
    assert not thread.is_alive() and time.time() - started < 10
    discussion.reload()
    entries = discussion.rounds[0]["entries"]
    assert {e["status"] for e in entries.values()} == {"failed"}
    assert {e["error"] for e in entries.values()} == {"stopped"}
    control = discussion.read_control()
    assert control["paused"] and not control["stop_requested"]
    assert engine.describe(discussion)["phase"] == "stopped"


def test_plan():
    control = dict(DEFAULT_CONTROL, max_rounds=2, pause_after_each_round=False)

    def rnd(kind, objections=1, status="done"):
        return {"kind": kind, "index": 0,
                "entries": {a: {"status": status, "objections": objections} for a in ("A", "B")}}

    def step(*rounds, **changes):
        planned = plan({"rounds": list(rounds), "question": "Q"}, dict(control, **changes), [])
        return planned and planned["action"]

    assert step() == "answer"
    assert step(rnd("answer")) == "next"                       # comments come next
    assert step(rnd("answer"), rnd("comment")) == "next"       # then updates
    assert step(rnd("answer"), rnd("comment", objections=0)) is None  # agreed
    assert step(rnd("answer"), rnd("comment"), rnd("update"), rnd("comment")) == "next"
    assert step(rnd("answer"), rnd("comment"), rnd("update"), rnd("comment"), rnd("update")) is None  # the limit
    assert step(rnd("answer"), rnd("comment"), rnd("update"), max_rounds=1) is None
    assert step(rnd("answer"), max_rounds=0) is None
    assert step(rnd("answer"), rnd("review"), rnd("discuss"), rnd("final")) is None  # from earlier versions
    assert step(rnd("answer"), paused=True) is None
    request = {"id": "1", "type": "next"}
    assert plan({"rounds": [rnd("answer")]}, dict(control, paused=True), [request])["request"] == request
    assert step(rnd("answer", status="failed")) == "retry"
    assert plan({"rounds": [rnd("answer", status="failed")]}, dict(control, paused=True), [request]) is None


def test_status_show_and_list(env, capsys):
    fugue("start", "Q?", "-q")
    fugue("continue", "-q")
    capsys.readouterr()
    assert fugue("status") == 0
    out = capsys.readouterr().out
    assert "claude --resume" in out and "codex resume" in out
    assert "1 objection" in out and "Paused after step 2" in out
    assert "Next:       each updates their answer" in out
    assert "Member A:   " in out and "Member B:   " in out
    assert fugue("show", "1", "--member", letter_of(latest(), "codex")) == 0
    out = capsys.readouterr().out
    assert "Step 1 · " in out and "Codex reply number 1." in out and "Claude reply" not in out
    assert fugue("show", "2", "--prompt") == 0
    assert "Step 2 · exactly what fugue sent" in capsys.readouterr().out
    assert fugue("show", "3") == 1  # there are two steps, numbered from 1
    assert "There is no step 3" in capsys.readouterr().err
    assert fugue("list") == 0
    assert latest().id in capsys.readouterr().out


def test_bash_is_not_allowed_outright_when_commands_skip_the_sandbox(env):
    settings = Path.home() / ".claude" / "settings.json"
    settings.write_text('{"model": "opus", "sandbox": {"excludedCommands": ["docker"]}}')
    fugue("start", "Q?", "-n", "0", "-q")
    args = to("claude", calls(env))["args"]
    assert args[args.index("--allowedTools") + 1] == "Read(//**),WebSearch,WebFetch"  # no Bash
    assert json.loads(args[args.index("--settings") + 1])["sandbox"]["enabled"]


def test_models_lists_both_agents(env, capsys):
    assert fugue("models") == 0
    out = capsys.readouterr().out
    assert "gpt-test-big" in out and "low medium high ultra  (default low)" in out
    assert "gpt-hidden" not in out
    assert "claude-opus-5-5" in out and "low medium high xhigh max" in out
    assert "Default, used when you choose nothing: gpt-test-big at high" in out
    assert "Default, used when you choose nothing: opus" in out


def test_chosen_models_and_efforts_reach_the_agents(env):
    assert fugue("start", "Q?", "-n", "0", "-q",
                "--member", "claude:claude-opus-5-5:xhigh", "--member", "codex:gpt-test-big:ultra",
                "--member-args", "--add-dir '../a b'") == 0
    made = calls(env)
    claude, codex = to("claude", made), to("codex", made)
    assert claude["args"][claude["args"].index("--model") + 1] == "claude-opus-5-5"
    assert claude["args"][claude["args"].index("--effort") + 1] == "xhigh"
    assert claude["args"][-2:] == ["--add-dir", "../a b"]  # --member-args goes with the first --member
    assert codex["args"][codex["args"].index("-m") + 1] == "gpt-test-big"
    assert 'model_reasoning_effort="ultra"' in codex["args"] and "--add-dir" not in codex["args"]
    discussion = latest()
    assert discussion.members[letter_of(discussion, "claude")]["effort"] == "xhigh"
    assert discussion.members[letter_of(discussion, "codex")]["effort"] == "ultra"


def test_two_members_can_use_the_same_tool(env):
    assert fugue("start", "Q?", "-n", "1", "--no-pause", "-q",
                "--member", "codex:gpt-test-big:high", "--member", "codex:gpt-test-small:low") == 0
    discussion = latest()
    made = calls(env)
    assert [c["agent"] for c in made] == ["codex"] * 6
    by_session = {letter: session for letter, session in discussion.state["sessions"].items()}
    assert len(set(by_session.values())) == 2
    big = next(letter for letter, m in discussion.members.items() if m["model"] == "gpt-test-big")
    small = discussion.other(big)
    first = {c["session"]: c for c in made[:2]}
    assert first[by_session[big]]["args"][first[by_session[big]]["args"].index("-m") + 1] == "gpt-test-big"
    assert 'model_reasoning_effort="low"' in first[by_session[small]]["args"]
    # Each has its own reply files, and each comments on the other's answer, not its own.
    replies = {letter: discussion.read_reply(0, letter) for letter in discussion.members}
    assert replies[big] != replies[small]
    for call in made[2:4]:
        me = big if call["session"] == by_session[big] else small
        assert replies[discussion.other(me)] in call["prompt"] and replies[me] not in call["prompt"]
        assert f"You are member {me} " in call["prompt"]
    # The user tells them apart by model and effort.
    assert discussion.name(big) == f"Codex · gpt-test-big · high (member {big})"
    text = discussion.transcript_path.read_text()
    assert f"Codex · gpt-test-small · low (member {small}), commenting on Codex · gpt-test-big · high's answer" in text
    # Two of the same tool, model and effort are told apart by their letters.
    assert fugue("start", "Q?", "-n", "1", "--no-pause", "-q", "--member", "claude", "--member", "claude") == 0
    twins = next(d for d in list_discussions() if d.id != discussion.id)
    assert twins.name("A") == "Claude · default model (member A)"
    text = twins.transcript_path.read_text()
    assert "Claude · default model (member A), commenting on Claude · default model (member B)'s answer" in text


def test_a_deepseek_member_runs_read_only_and_resumes_its_session(env):
    assert fugue("start", "Q?", "-n", "1", "--no-pause", "-q",
                "--member", "dsh:deepseek-v4-pro:max", "--member", "claude", "--no-web") == 0
    discussion = latest()
    made = calls(env)
    first, later = [c for c in made if c["agent"] == "dsh"][0], [c for c in made if c["agent"] == "dsh"][1:]
    assert first["args"][:2] == ["--profile", "headless"] and first["args"][-2:] == ["--json", "-"]
    assert first["permission"] == "read-only" and first["telemetry"] == "DISABLED"
    overlay = Path(first["args"][first["args"].index("--patch") + 1]).read_text()
    assert "model: \"deepseek-v4-pro\"" in overlay and "reasoningEffort: \"max\"" in overlay
    assert "policy: never" in overlay and "id: web-search-deepseek" in overlay  # the web is off
    assert "- id: credentials\n  disabled: false\n" in overlay  # the key saved in dsh is used
    letter = letter_of(discussion, "dsh")
    session = discussion.state["sessions"][letter]
    assert session.startswith("session-") and all(c["session"] == session for c in later)
    assert all(c["args"][c["args"].index("--session-id") + 1] == session for c in later)
    assert discussion.read_reply(0, letter).startswith("DeepSeek reply number 1.")
    assert discussion.rounds[1]["entries"][letter]["objections"] == 1
    assert discussion.rounds[0]["entries"][letter]["tokens"] == {"input_tokens": 500, "output_tokens": 60}
    texts = [e["text"] for e in events(discussion) if e["type"] == "activity" and e["member"] == letter]
    assert "grep 'cache' in src" in texts and "no matches" in texts and "Looking for the cache code first." in texts
    assert discussion.name(letter) == f"DeepSeek (member {letter})"
    # Its resume command continues the session with the settings of its last run.
    last_overlay = discussion.log_stem(discussion.rounds[-1]["index"], letter).with_suffix(".dsh-patch.yml")
    assert last_overlay.exists() and last_overlay != Path(first["args"][first["args"].index("--patch") + 1])
    assert resume_command(discussion, letter, session) == (
        f'cd {discussion.cwd} && dsh --profile headless --patch {last_overlay} --session-id {session} "<your message>"')


def test_a_failed_deepseek_run_says_why(env, monkeypatch):
    monkeypatch.setenv("FAKE_DSH_FAIL", "1")
    assert fugue("start", "Q?", "-q", "--member", "dsh", "--member", "codex") == 1
    discussion = latest()
    entry = discussion.rounds[0]["entries"][letter_of(discussion, "dsh")]
    assert entry["status"] == "failed" and "simulated DeepSeek failure" in entry["error"]


def test_dsh_status_finds_a_key_without_reading_it(env, monkeypatch):
    from fugue.settings import agent_status

    home = Path(os.environ["DSH_HOME"])
    report = agent_status("dsh")
    assert report["logged_in"] is False
    assert report["login"] == f"no DeepSeek API key: save one on dsh's Models page, or put DEEPSEEK_API_KEY in {home / '.env'}"
    home.mkdir(parents=True, exist_ok=True)
    # dsh ignores the older flat layout and keys that aren't under `refs:`.
    (home / ".credentials.yaml").write_text("DEEPSEEK_API_KEY: sk-test\n")
    assert agent_status("dsh")["logged_in"] is False
    (home / ".credentials.yaml").write_text("version: 1\nrecords:\n  DEEPSEEK_API_KEY: sk-test\n")
    assert agent_status("dsh")["logged_in"] is False
    (home / ".env").write_text("export DEEPSEEK_API_KEY=sk-test\n")
    assert agent_status("dsh")["login"] == "DeepSeek API key in dsh's .env"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert agent_status("dsh")["login"] == "DeepSeek API key from the environment"
    # A key saved in dsh wins over the environment, as in dsh.
    (home / ".credentials.yaml").write_text("version: 1\n# saved by dsh\nrefs:\n  # the API key\n  DEEPSEEK_API_KEY: sk-test\n")
    report = agent_status("dsh")
    assert report["logged_in"] and report["login"] == "DeepSeek API key saved in dsh" and "sk-test" not in str(report)
    assert report["version"] == "0.2.0-rc.2"


def test_members_must_be_given_two_to_four_times_with_known_tools(env, capsys):
    assert fugue("start", "Q?", "-q", "--member", "codex") == 1
    assert "Give --member two to 4 times" in capsys.readouterr().err
    assert fugue("start", "Q?", "-q", *["--member", "codex"] * 5) == 1
    assert "Give --member two to 4 times" in capsys.readouterr().err
    assert fugue("start", "Q?", "-q", "--member", "gemini", "--member", "codex") == 1
    assert "must be one of: claude, codex" in capsys.readouterr().err
    assert list_discussions() == []


def test_an_effort_the_model_does_not_take_is_refused(env, capsys):
    assert fugue("start", "Q?", "-q", "--member", "codex:gpt-test-small:ultra", "--member", "claude") == 1
    assert "GPT-Test-Small doesn't take effort 'ultra'; it takes low, medium." in capsys.readouterr().err
    # Without a model, the effort is checked against the tool's default model (opus here).
    assert fugue("start", "Q?", "-q", "--member", "claude::ultra", "--member", "codex") == 1
    assert "Claude Opus 5.5 doesn't take effort 'ultra'" in capsys.readouterr().err
    assert list_discussions() == [] and calls(env) == []


def downgrade(discussion: Discussion, with_letters: bool = True) -> None:
    """Rewrite a discussion the way fugue saved it before members: keyed by tool, files named after the tool."""
    state = json.loads((discussion.path / "state.json").read_text())
    letters = {member["tool"]: letter for letter, member in state["members"].items()}
    tool_of = {letter: tool for tool, letter in letters.items()}
    old = {key: value for key, value in state.items() if key not in ("members", "legacy_names")}
    old["version"] = 2
    old["settings"] = {**state["settings"], **{
        tool: {"model": m["model"], "effort": m["effort"], "args": m["args"]}
        for m in state["members"].values() for tool in [m["tool"]]}}
    old["sessions"] = {tool_of[letter]: sid for letter, sid in state["sessions"].items()}
    old["rounds"] = [{**rnd, "entries": {tool_of[letter]: e for letter, e in rnd["entries"].items()}}
                     for rnd in state["rounds"]]
    if with_letters:
        old["labels"] = letters
    (discussion.path / "state.json").write_text(json.dumps(old))
    for folder in ("rounds", "logs"):
        for path in (discussion.path / folder).iterdir():
            stem, _, rest = path.name.partition(".")
            index, _, letter = stem.partition("-")
            path.rename(path.with_name(f"{index}-{tool_of[letter]}.{rest}"))
    lines = (discussion.events_path).read_text().splitlines()
    events = []
    for line in lines:
        event = json.loads(line)
        if "member" in event:
            event["agent"] = tool_of[event.pop("member")]
        if "members" in event:
            event["agents"] = [tool_of[letter] for letter in event.pop("members")]
        events.append(json.dumps(event))
    discussion.events_path.write_text("\n".join(events) + "\n")


def test_a_discussion_from_before_members_is_converted_and_carries_on(env):
    fugue("start", "Q?", "-q")  # paused after the answers
    discussion = latest()
    claude, codex = letter_of(discussion, "claude"), letter_of(discussion, "codex")
    sessions = dict(discussion.state["sessions"])
    fugue("note", "Mind the latency.", "--to", codex)
    inbox = json.loads((discussion.path / "inbox.json").read_text())
    inbox["notes"] = {"codex": inbox["notes"][codex], "claude": inbox["notes"].get(claude, [])}
    (discussion.path / "inbox.json").write_text(json.dumps(inbox))
    downgrade(discussion)

    old = latest()
    assert old.members[claude]["tool"] == "claude" and old.members[codex]["tool"] == "codex"
    assert old.state["sessions"] == sessions and old.state["legacy_names"] == {"claude": claude, "codex": codex}
    assert old.read_reply(0, claude).startswith("Claude reply number 1.")
    assert old.read_inbox()["notes"][codex][0]["text"] == "Mind the latency."
    assert fugue("continue", "-q") == 0
    later = latest()
    assert later.state["version"] == 3 and "labels" not in later.state
    comment = calls(env)[2:4]
    assert to("claude", comment)["args"][to("claude", comment)["args"].index("--resume") + 1] == sessions[claude]
    assert "Codex reply number 1." in to("claude", comment)["prompt"]
    assert "Mind the latency." in to("codex", comment)["prompt"]
    # The old reply files are still the ones read, and the new round's go next to them.
    assert later.reply_path(1, codex).name == "01-codex.md" and later.read_reply(1, codex).startswith("Codex reply")
    assert "Claude reply number 1." in later.transcript_path.read_text()

    # One from before letters existed gets letters from its id, the same every time it's read.
    fugue("start", "Q2?", "-n", "0", "-q")
    pick = lambda: next(d for d in list_discussions() if d.id != later.id)
    downgrade(pick(), with_letters=False)
    first, again = pick(), pick()
    assert first.members == again.members and sorted(first.members) == ["A", "B"]


def test_delete_moves_a_discussion_to_the_trash(env, capsys):
    fugue("start", "Q?", "-n", "0", "-q")
    discussion = latest()
    # Not while it runs.
    discussion.state["runner"] = {"pid": os.getpid(), "started_at": "now"}
    discussion.save()
    assert fugue("delete", discussion.id, "--yes") == 1
    assert "running" in capsys.readouterr().err
    discussion.state.pop("runner")
    discussion.save()
    assert fugue("delete", discussion.id[:8], "--yes") == 0
    assert list_discussions() == []
    assert (env["trash"] / discussion.id / "state.json").exists()


def test_settings_are_saved_to_the_config_file_and_used(env):
    import tomllib

    from fugue.settings import read_defaults, save_defaults

    defaults = read_defaults()
    assert defaults["max_rounds"] == 2 and defaults["web"] is True
    assert [m["tool"] for m in defaults["members"]] == ["claude", "codex"] and defaults["members"][0]["model"] == ""
    saved = save_defaults({
        "max_rounds": 4, "pause_after_each_round": False, "web": False, "timeout_minutes": 45,
        "members": [{"tool": "codex", "model": "gpt-test-big", "effort": "high", "args": "--add-dir '../a b'"},
                    {"tool": "codex", "model": "", "effort": "", "args": ""}],
    })
    assert saved["max_rounds"] == 4 and saved["members"][0]["args"] == "--add-dir '../a b'"
    config = tomllib.loads(env["config"].read_text())
    assert config["members"] == [
        {"tool": "codex", "model": "gpt-test-big", "effort": "high", "args": ["--add-dir", "../a b"]},
        {"tool": "codex"}]
    assert config["web"] is False
    fugue("start", "Q?", "-q")
    discussion = latest()
    assert discussion.read_control()["max_rounds"] == 4 and not discussion.read_control()["pause_after_each_round"]
    assert sorted(m["model"] or "" for m in discussion.members.values()) == ["", "gpt-test-big"]
    assert discussion.state["settings"]["web"] is False
    with pytest.raises(Exception, match="doesn't take effort"):
        save_defaults({**saved, "members": [{"tool": "codex", "model": "gpt-test-small", "effort": "ultra"},
                                            {"tool": "claude"}]})
    with pytest.raises(Exception, match="two to 4 members"):
        save_defaults({**saved, "members": [{"tool": "codex"}]})


def test_a_config_file_from_before_members_still_gives_the_line_up(env):
    from fugue.settings import read_defaults, save_defaults

    env["config"].write_text('max_rounds = 3\n\n[claude]\nmodel = "claude-sonnet-5-5"\n\n[codex]\neffort = "low"\n')
    defaults = read_defaults()
    assert defaults["members"] == [
        {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "", "args": ""},
        {"tool": "codex", "model": "", "effort": "low", "args": ""}]
    save_defaults(defaults)
    text = env["config"].read_text()
    assert "[[members]]" in text and "[claude]" not in text and "max_rounds = 3" in text


def test_status_reports_both_agents_without_calling_a_model(env):
    from fugue.settings import status

    report = status(8765)
    assert report["agents"]["claude"]["version"] == "9.9.9 (Fake Claude)"
    assert report["agents"]["claude"]["logged_in"] and report["agents"]["codex"]["logged_in"]
    assert report["codex_switched_off"]["plugins"] == ["chrome@openai-bundled"]
    assert report["codex_switched_off"]["mcp_servers"] == ["node_repl"]
    assert report["storage"]["discussions"] == 0
    assert calls(env) == []  # no agent was asked anything


def test_parse_objections():
    assert parse_objections("text\n\nObjections: 3") == 3
    assert parse_objections("text\n**Objections: 0**\n") == 0
    assert parse_objections("text\nobjections: none") == 0
    assert parse_objections("分析\n\n异议：2") == 2
    assert parse_objections("older\n\nRemaining disagreements: 4") == 4
    assert parse_objections("no marker here") is None


def test_demote_headings_skips_code_blocks():
    text = "# Title\n\n```\n# a shell comment\n```\n## Sub"
    assert demote_headings(text) == "#### Title\n\n```\n# a shell comment\n```\n##### Sub"


# ---------------------------------------------------------------------------
# A council of three or four members: vote, merge, review, revise


COUNCIL = ("--member", "claude", "--member", "codex", "--member", "dsh")


def round_letters(rnd: dict) -> list[str]:
    return sorted(rnd["entries"])


def test_a_council_votes_merges_reviews_and_revises(env, monkeypatch):
    monkeypatch.setenv("FAKE_PREFER", "B,C,A")
    assert fugue("start", "Q?", "-n", "1", "--no-pause", "-q", *COUNCIL) == 0
    discussion = latest()
    assert sorted(discussion.members) == ["A", "B", "C"]
    assert kinds(discussion) == ["answer", "vote", "merge", "review", "revise", "review"]
    answer, vote, merge, review, revise, last = discussion.rounds
    # Everyone votes on the others' answers, never their own, each in its own order.
    for voter, entry in vote["entries"].items():
        assert sorted(entry["candidates"]) == sorted(set("ABC") - {voter})
        prompt = discussion.prompt_path(vote["index"], voter).read_text()
        assert f'<answer from="member {voter}"' not in prompt
        assert all(f'<answer from="member {other}" step="1">' in prompt for other in entry["candidates"])
        assert "Ranking: <their letters" in prompt
    # B is ranked first by A and C, so B's answer is the starting point.
    assert vote["vote"]["points"] == {"A": 0, "B": 2, "C": 1}
    assert vote["vote"]["chosen"] == "B" and vote["vote"]["chosen_by"] == "by vote" and vote["vote"]["valid"] == 3
    # B merges, reading the other ballots; A and C review; B revises from their reviews.
    assert round_letters(merge) == ["B"] and round_letters(review) == ["A", "C"] and round_letters(revise) == ["B"]
    merge_prompt = discussion.prompt_path(merge["index"], "B").read_text()
    assert '<ballot from="member A" step="2">' in merge_prompt and '<ballot from="member C" step="2">' in merge_prompt
    assert "points from other members' answers that you did not take" in merge_prompt
    review_prompt = discussion.prompt_path(review["index"], "A").read_text()
    assert '<answer from="member B" step="3">' in review_prompt and "Starting point: <keep or change>" in review_prompt
    assert "You are member A of a council of 3: members A, B and C." in review_prompt
    assert review["entries"]["A"]["objections"] == 1 and review["entries"]["A"]["starting_point"] == "keep"
    revise_prompt = discussion.prompt_path(revise["index"], "B").read_text()
    assert '<comments from="member A" step="4">' in revise_prompt and '<comments from="member C" step="4">' in revise_prompt
    later_review = discussion.prompt_path(last["index"], "C").read_text()
    assert '<answer from="member B" step="5">' in later_review and "turned down one of your earlier objections" in later_review
    # The members only ever see letters.
    for call in calls(env):
        own_words = without_forwarded(call["prompt"])
        assert not any(name in own_words for name in ("Claude", "Codex", "DeepSeek")), own_words
    # B's session goes on from its answer to its merge and revision.
    b_tool = discussion.members["B"]["tool"]
    b_sessions = {c["session"] for c in calls(env) if c["agent"] == b_tool}
    assert len(b_sessions) == 1
    status = engine.describe(discussion)
    assert status["label"] == "Finished at the round limit" and "Objections still open after step 6" in status["detail"]
    text = discussion.transcript_path.read_text()
    assert "Step 2: each votes for the best answer to start from" in text
    assert "Starting point: member B's answer, by vote (3 of 3 ballots counted)." in text


def test_a_council_pauses_after_the_vote_and_after_each_revision(env, monkeypatch):
    from fugue import council

    monkeypatch.setenv("FAKE_PREFER", "B,C,A")
    assert fugue("start", "Q?", "-q", *COUNCIL) == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "vote"]
    status = engine.describe(discussion)
    assert status["phase"] == "paused" and status["next"] == "member B merges the others' points into its answer"
    # You can start from another answer than the vote chose, before the merge.
    council.choose_editor(discussion, "C")
    assert discussion.rounds[1]["vote"]["chosen_by"] == "by you"
    assert engine.describe(latest())["next"] == "member C merges the others' points into its answer"
    assert fugue("continue", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "vote", "merge", "review", "revise"]
    assert round_letters(discussion.rounds[2]) == ["C"] and round_letters(discussion.rounds[3]) == ["A", "B"]
    with pytest.raises(Exception, match="only right after a vote"):
        council.choose_editor(discussion, "A")


def test_a_council_stops_when_no_reviewer_objects(env, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    assert fugue("start", "Q?", "--no-pause", "-q", *COUNCIL) == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "vote", "merge", "review"]
    assert engine.describe(discussion)["label"] == "Agreed in step 4"


def test_a_council_with_suggestions_but_no_objections_closes_with_a_final_version(env, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    monkeypatch.setenv("FAKE_SUGGESTIONS", "1")
    assert fugue("start", "Q?", "--no-pause", "-q", *COUNCIL) == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "vote", "merge", "review", "revise"]
    assert discussion.rounds[4].get("closing")
    editor = list(discussion.rounds[4]["entries"])[0]
    assert "This is the council's final version" in discussion.prompt_path(4, editor).read_text()
    assert engine.describe(discussion)["label"] == "Agreed in step 4"


def test_a_council_votes_again_once_when_reviewers_want_another_start(env, monkeypatch):
    monkeypatch.setenv("FAKE_STARTING_POINT", "change")
    assert fugue("start", "Q?", "-n", "1", "--no-pause", "-q", *COUNCIL) == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "vote", "merge", "review", "answer", "vote", "merge", "review", "revise",
                                 "review"]
    again = discussion.rounds[4]
    assert again.get("reopen") and round_letters(again) == ["A", "B", "C"]
    prompt = discussion.prompt_path(4, "A").read_text()
    assert "the council will vote again" in prompt and "Write your own best answer" in prompt
    # The second vote is on the new answers.
    vote = discussion.rounds[5]
    assert all(f'step="5"' in discussion.prompt_path(5, voter).read_text() for voter in vote["entries"])


def test_a_council_with_unreadable_ballots_draws_the_starting_point(env, monkeypatch):
    monkeypatch.setenv("FAKE_NO_RANKING", "1")
    assert fugue("start", "Q?", "-q", *COUNCIL) == 0
    vote = latest().rounds[1]
    assert vote["vote"]["valid"] == 0 and vote["vote"]["chosen_by"].startswith("drawn at random")
    assert all(entry["ballot_problem"] == "no Ranking line at the end" for entry in vote["entries"].values())


def test_rankings_and_ties():
    from fugue.council import parse_ranking, parse_starting_point, tally

    assert parse_ranking("text\n\nRanking: C, B", ["B", "C"]) == (["C", "B"], None)
    assert parse_ranking("text\n**Ranking:** member B > member C", ["B", "C"]) == (["B", "C"], None)
    assert parse_ranking("Ranking: B", ["B", "C"]) == (None, "ranked B instead of B and C")
    assert parse_starting_point("x\nObjections: 2\nStarting point: **Change**") == "change"

    class Fake:
        id = "x"
    ballots = {"A": ["B", "C"], "B": ["A", "C"], "C": ["A", "B"], "D": ["B", "A"]}
    rnd = {"index": 1, "entries": {v: {"ranking": r} for v, r in ballots.items()}}
    result = tally(Fake(), rnd)
    assert result["points"] == {"A": 2, "B": 2, "C": 0, "D": 0}
    assert result["chosen"] in ("A", "B") and result["chosen_by"] == "drawn at random between A and B, who tied"


# ---------------------------------------------------------------------------
# Attached files


def test_a_question_with_attachments_and_no_folder(env, capsys):
    from tests.test_attachments import PNG, tiny_pdf

    files = env["work"].parent / "files"
    files.mkdir()
    (files / "resume.pdf").write_bytes(tiny_pdf("Ten years of Rust"))
    (files / "shot.png").write_bytes(PNG)
    assert fugue("start", "Review this resume.", "-n", "0", "-q", "--no-folder",
                "--attach", str(files / "resume.pdf"), "--attach", str(files / "shot.png")) == 0
    discussion = latest()
    folder = discussion.attachments_dir
    assert discussion.state["no_folder"] and Path(discussion.state["cwd"]) == folder
    assert sorted(p.name for p in folder.iterdir()) == ["resume.pdf", "resume.pdf.page-1.png", "resume.pdf.txt", "shot.png"]
    made = calls(env)
    assert all(Path(c["cwd"]).resolve() == folder.resolve() for c in made)
    claude, codex = to("claude", made), to("codex", made)
    for call in (claude, codex):
        assert call["prompt"].startswith("Review this resume.\n\nThe user attached these files")
        assert f"{folder / 'resume.pdf.txt'}" in call["prompt"]
    assert "Open it with your image or file reading tool." in claude["prompt"]
    assert "Open the PDF itself with your file reader" in claude["prompt"]
    assert "It is also attached to this message." in codex["prompt"]
    assert "Its pages are attached to this message as images" in codex["prompt"]
    assert f"--image={folder / 'shot.png'}" in codex["args"] and codex["args"][-1] == "-"
    assert f"--image={folder / 'resume.pdf.page-1.png'}" in codex["args"]
    text = discussion.transcript_path.read_text()
    assert f"- `{folder / 'resume.pdf'}` (and its text: resume.pdf.txt, 1 page image)" in text
    # The folders offered for new questions leave this one out.
    assert discussion.state["cwd"] not in [d.state["cwd"] for d in list_discussions() if not d.state.get("no_folder")]


def test_a_follow_up_with_an_attachment(env):
    from tests.test_attachments import PNG

    image = env["work"].parent / "chart.png"
    image.write_bytes(PNG)
    fugue("start", "Q?", "-n", "0", "-q")
    assert fugue("ask", "And this chart?", "--attach", str(image), "-q") == 0
    discussion = latest()
    followup = discussion.rounds[-1]
    assert followup["kind"] == "answer" and followup["attachments"][0]["name"] == "chart.png"
    codex_calls = [c for c in calls(env) if c["agent"] == "codex"]
    assert not any(a.startswith("--image=") for a in codex_calls[0]["args"])  # the first question had none
    assert f"--image={discussion.attachments_dir / 'chart.png'}" in codex_calls[-1]["args"]
    assert codex_calls[-1]["args"][:2] == ["exec", "resume"]
    assert "chart.png" in to("claude", calls(env)[-2:])["prompt"]
