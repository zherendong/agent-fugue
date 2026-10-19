"""Tests of duet's engine and command line against fake `claude` and `codex` commands (see tests/fakes)."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from duet import cli, engine
from duet.engine import parse_objections, plan
from duet.store import DEFAULT_CONTROL, Discussion, list_discussions
from duet.transcript import demote_headings

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def env(tmp_path, monkeypatch):
    work = tmp_path / "project"
    work.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("DUET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DUET_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("DUET_CLAUDE_BIN", str(FAKES / "claude"))
    monkeypatch.setenv("DUET_CODEX_BIN", str(FAKES / "codex"))
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
    return {"work": work, "logs": logs}


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
    return next(c for c in batch if c["agent"] == agent)


def duet(*args: str) -> int:
    return cli.main(list(args))


def without_forwarded(prompt: str) -> str:
    """duet's own words in a prompt, without the other member's forwarded replies."""
    return re.sub(r"<(answer|comments|message) [^>]*>[\s\S]*?</\1>", "", prompt)


def test_it_pauses_after_every_round_by_default(env):
    assert duet("start", "Should we cache embeddings?", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer"]
    status = engine.describe(discussion)
    assert status["phase"] == "paused" and status["next"] == "each comments on the other's answer"
    for call in calls(env):
        assert call["prompt"].strip() == "Should we cache embeddings?"
        assert Path(call["cwd"]).resolve() == env["work"].resolve()
    assert duet("continue", "-q") == 0
    assert kinds(latest()) == ["answer", "comment"]
    assert engine.describe(latest())["next"] == "each updates their answer"
    assert duet("continue", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update"]
    # Nothing is written into the project folder.
    assert list(env["work"].iterdir()) == []


def test_without_pausing_it_runs_to_the_round_limit(env):
    assert duet("start", "Q?", "--no-pause", "-q") == 0
    discussion = latest()
    # Every comment round has objections, so it runs the default 2 rounds of comments and updates.
    assert kinds(discussion) == ["answer", "comment", "update", "comment", "update"]
    assert len(calls(env)) == 10
    status = engine.describe(discussion)
    assert status["phase"] == "finished" and status["label"] == "Finished at the round limit"
    assert "Claude 1, Codex 1" in status["detail"]
    assert "runner" not in discussion.state
    text = discussion.transcript_path.read_text()
    assert "Claude reply number 1." in text and "Codex reply number 5." in text


def test_it_stops_once_neither_member_has_objections(env, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    assert duet("start", "Q?", "--no-pause", "-q") == 0
    discussion = latest()
    assert kinds(discussion) == ["answer", "comment"]
    assert engine.describe(discussion)["label"] == "Agreed in round 1"
    # One more round can still be asked for; without pausing, its update is then commented on again.
    assert duet("next", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update", "comment"]
    assert engine.describe(latest())["label"] == "Agreed in round 3"


def test_read_only_flags_and_sessions(env):
    duet("start", "Q?", "-n", "1", "--no-pause", "-q")
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
    sessions = latest().state["sessions"]
    claude_second, codex_second = to("claude", made[2:]), to("codex", made[2:])
    assert claude_second["args"][claude_second["args"].index("--resume") + 1] == sessions["claude"]
    assert codex_second["args"][:2] == ["exec", "resume"] and codex_second["session"] == sessions["codex"]
    assert 'sandbox_mode="read-only"' in codex_second["args"]


def test_web_can_be_turned_off_for_both(env):
    duet("start", "Q?", "-n", "0", "--no-web", "-q")
    made = calls(env)
    claude, codex = to("claude", made)["args"], to("codex", made)["args"]
    assert claude[claude.index("--allowedTools") + 1] == "Bash,Read(//**)"
    assert claude[claude.index("--disallowedTools") + 1].endswith("WebSearch,WebFetch")
    assert 'web_search="disabled"' in codex
    assert latest().state["settings"]["web"] is False


def test_each_round_forwards_the_right_message(env):
    duet("start", "Q?", "--no-pause", "-q")
    made = calls(env)
    # Round 1: each comments on the other's answer, and only on that.
    comment = made[2:4]
    to_claude, to_codex = to("claude", comment)["prompt"], to("codex", comment)["prompt"]
    assert "Codex reply number 1." in to_claude and "Claude reply number" not in to_claude
    assert "Claude reply number 1." in to_codex and "Codex reply number" not in to_codex
    letters = latest().state["labels"]
    me, other = letters["claude"], letters["codex"]
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
    discussion = latest()
    assert discussion.rounds[1]["entries"]["claude"]["objections"] == 1
    assert discussion.rounds[1]["entries"]["codex"]["objections"] == 1  # parsed through the **bold**


def test_each_member_keeps_one_letter_for_the_whole_discussion(env, monkeypatch):
    duet("start", "Q?", "-n", "1", "--no-pause", "-q")
    letters = latest().state["labels"]
    assert sorted(letters.values()) == ["A", "B"]
    for call in calls(env)[2:]:
        me, other = letters[call["agent"]], letters["codex" if call["agent"] == "claude" else "claude"]
        assert f"You are member {me} " in call["prompt"] and f'from="member {other}"' in call["prompt"]
    # The letters are drawn at random for each new discussion, so neither tool is always "A".
    import duet.store as store
    seen = set()
    for _ in range(20):
        seen.add(store.assign_labels()["claude"])
    assert seen == {"A", "B"}
    # A discussion made before letters existed gets the same letters wherever it's read.
    discussion = latest()
    del discussion.state["labels"]
    assert discussion.labels == store.assign_labels(seed=discussion.id)


def test_members_never_see_each_others_names(env):
    duet("start", "Q?", "--no-pause", "-q")
    duet("note", "Mind the latency.", "--to", "both")
    duet("ask", "And what about cost?", "-q")
    for call in calls(env):
        own_words = without_forwarded(call["prompt"])
        assert "Claude" not in own_words and "Codex" not in own_words, own_words


def test_notes_reach_only_their_recipient(env):
    duet("start", "Q?", "-q")
    assert duet("note", "Please consider latency on mobile.", "--to", "codex") == 0
    assert duet("continue", "-q") == 0
    comment = calls(env)[2:]
    assert "latency on mobile" in to("codex", comment)["prompt"]
    assert "sent only to you" in to("codex", comment)["prompt"]
    assert "latency on mobile" not in to("claude", comment)["prompt"]
    duet("continue", "-q")
    assert all("latency on mobile" not in c["prompt"] for c in calls(env)[4:])
    assert "latency on mobile" in latest().transcript_path.read_text()


def test_failed_agent_stops_the_discussion_and_is_retried_alone(env, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    assert duet("start", "Q?", "-q") == 1
    discussion = latest()
    assert discussion.rounds[0]["entries"]["claude"]["status"] == "done"
    assert discussion.rounds[0]["entries"]["codex"]["status"] == "failed"
    status = engine.describe(discussion)
    assert status["phase"] == "stopped" and "simulated Codex failure" in status["detail"]
    assert status["next"].startswith("retry round 0")
    assert "codex" not in discussion.state["sessions"]

    monkeypatch.delenv("FAKE_CODEX_FAIL")
    before = len(calls(env))
    assert duet("continue", "-q") == 0
    retried = calls(env)[before:]
    assert [c["agent"] for c in retried] == ["codex"]
    assert retried[0]["prompt"].strip() == "Q?"
    assert "--resume" not in retried[0]["args"] and retried[0]["args"][:2] != ["exec", "resume"]
    assert kinds(latest()) == ["answer"] and engine.describe(latest())["phase"] == "paused"


def test_follow_up_question_is_answered_separately_then_commented_on(env):
    duet("start", "Q?", "-n", "1", "--no-pause", "-q")
    assert kinds(latest()) == ["answer", "comment", "update"]
    assert duet("ask", "And what about cost?", "-q") == 0
    assert kinds(latest()) == ["answer", "comment", "update", "answer", "comment", "update"]
    prompt = to("claude", calls(env)[6:8])["prompt"]
    assert "follow-up question" in prompt and "And what about cost?" in prompt
    assert "Codex reply number 3." in prompt  # Codex's updated answer, which Claude hadn't seen
    assert "Codex reply number 4." in to("claude", calls(env)[8:10])["prompt"]


def test_live_events(env):
    duet("start", "Q?", "-n", "1", "--no-pause", "-q")
    log = events(latest())
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1))
    texts = {(e["agent"], e["text"]) for e in log if e["type"] == "activity"}
    assert ("claude", "Read src/cache.py") in texts
    assert ("claude", "File does not exist.") in texts
    assert ("codex", "$ rg -n cache src") in texts
    assert ("codex", "exit code 1: rg -n cache src") in texts
    assert ("codex", "Checking the cache layer") in texts
    deltas = "".join(e["text"] for e in log if e["type"] == "text_delta" and e["agent"] == "claude" and e["round"] == 0)
    assert deltas.startswith("Claude reply number 1.")
    assert any(e["type"] == "progress" and e["agent"] == "claude" and e["thinking_tokens"] == 1500 for e in log)
    finished = [e for e in log if e["type"] == "agent_finished" and e["round"] == 1]
    assert {e["agent"]: e["objections"] for e in finished} == {"claude": 1, "codex": 1}
    assert [e["type"] for e in log][:2] == ["runner", "round_started"]


def test_stop_mid_round(env, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "20")
    from duet.config import new_discussion

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
                "entries": {a: {"status": status, "objections": objections} for a in ("claude", "codex")}}

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
    duet("start", "Q?", "-q")
    duet("continue", "-q")
    capsys.readouterr()
    assert duet("status") == 0
    out = capsys.readouterr().out
    assert "claude --resume" in out and "codex resume" in out
    assert "1 objection" in out and "Paused after round 1" in out
    assert "Next:       each updates their answer" in out
    assert duet("show", "0", "--agent", "codex") == 0
    out = capsys.readouterr().out
    assert "Codex reply number 1." in out and "Claude reply" not in out
    assert duet("show", "1", "--prompt") == 0
    assert "exactly what duet sent" in capsys.readouterr().out
    assert duet("list") == 0
    assert latest().id in capsys.readouterr().out


def test_bash_is_not_allowed_outright_when_commands_skip_the_sandbox(env):
    settings = Path.home() / ".claude" / "settings.json"
    settings.write_text('{"model": "opus", "sandbox": {"excludedCommands": ["docker"]}}')
    duet("start", "Q?", "-n", "0", "-q")
    args = to("claude", calls(env))["args"]
    assert args[args.index("--allowedTools") + 1] == "Read(//**),WebSearch,WebFetch"  # no Bash
    assert json.loads(args[args.index("--settings") + 1])["sandbox"]["enabled"]


def test_models_lists_both_agents(env, capsys):
    assert duet("models") == 0
    out = capsys.readouterr().out
    assert "gpt-test-big" in out and "low medium high ultra  (default low)" in out
    assert "gpt-hidden" not in out
    assert "claude-opus-5-5" in out and "low medium high xhigh max" in out
    assert "Default, used when you choose nothing: gpt-test-big at high" in out
    assert "Default, used when you choose nothing: opus" in out


def test_chosen_models_and_efforts_reach_the_agents(env):
    assert duet("start", "Q?", "-n", "0", "-q",
                "--claude-model", "claude-opus-5-5", "--claude-effort", "xhigh",
                "--codex-model", "gpt-test-big", "--codex-effort", "ultra") == 0
    made = calls(env)
    claude, codex = to("claude", made), to("codex", made)
    assert claude["args"][claude["args"].index("--model") + 1] == "claude-opus-5-5"
    assert claude["args"][claude["args"].index("--effort") + 1] == "xhigh"
    assert codex["args"][codex["args"].index("-m") + 1] == "gpt-test-big"
    assert 'model_reasoning_effort="ultra"' in codex["args"]
    settings = latest().state["settings"]
    assert (settings["claude"]["effort"], settings["codex"]["effort"]) == ("xhigh", "ultra")


def test_an_effort_the_model_does_not_take_is_refused(env, capsys):
    assert duet("start", "Q?", "-q", "--codex-model", "gpt-test-small", "--codex-effort", "ultra") == 1
    assert "GPT-Test-Small doesn't take effort 'ultra'; it takes low, medium." in capsys.readouterr().err
    # Without a model, the effort is checked against the tool's default model (opus here).
    assert duet("start", "Q?", "-q", "--claude-effort", "ultra") == 1
    assert "Claude Opus 5.5 doesn't take effort 'ultra'" in capsys.readouterr().err
    assert list_discussions() == [] and calls(env) == []


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
