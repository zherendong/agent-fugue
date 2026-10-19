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
from fugue.transcript import demote_headings

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
    return re.sub(r"<(answer|comments|message) [^>]*>[\s\S]*?</\1>", "", prompt)


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
    assert engine.describe(discussion)["label"] == "Agreed in round 1"
    # One more round can still be asked for; without pausing, its update is then commented on again.
    assert fugue("next", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update", "comment"]
    assert engine.describe(latest())["label"] == "Agreed in round 3"


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
    assert f'<answer from="member {other}" round="0">' in to_claude
    assert f"You are member {me} of a council of two; the other member is member {other}." in to_claude
    assert "Objections: <number>" in to_claude and "turned down" not in to_claude
    # Round 2: each gets the comments on its own answer and rewrites it in full.
    update = to("claude", made[4:6])["prompt"]
    assert "Codex reply number 2." in update and "Codex reply number 1." not in update
    assert f'<comments from="member {other}" round="1">' in update
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
    comments = prompt.index('<comments from="member B" round="1">')
    answer = prompt.index("1. Your full updated answer")
    responses = prompt.index("2. After the answer, under a heading of its own, your response to member B's objections")
    assert comments < answer < responses
    assert "You are member A of a council of two" in prompt


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
    assert status["next"].startswith("retry round 0")
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
    assert "1 objection" in out and "Paused after round 1" in out
    assert "Next:       each updates their answer" in out
    assert "Member A:   " in out and "Member B:   " in out
    assert fugue("show", "0", "--member", letter_of(latest(), "codex")) == 0
    out = capsys.readouterr().out
    assert "Codex reply number 1." in out and "Claude reply" not in out
    assert fugue("show", "1", "--prompt") == 0
    assert "exactly what fugue sent" in capsys.readouterr().out
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


def test_members_must_be_given_twice_with_known_tools(env, capsys):
    assert fugue("start", "Q?", "-q", "--member", "codex") == 1
    assert "Give --member twice" in capsys.readouterr().err
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
    with pytest.raises(Exception, match="two members"):
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
