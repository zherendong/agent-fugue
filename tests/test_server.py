"""Tests of the web server's API against the fake agents."""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from fugue.server import make_server

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture
def server(tmp_path, monkeypatch):
    work = tmp_path / "project"
    work.mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("FUGUE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUGUE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("FUGUE_CLAUDE_BIN", str(FAKES / "claude"))
    monkeypatch.setenv("FUGUE_CODEX_BIN", str(FAKES / "codex"))
    monkeypatch.setenv("FAKE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("FUGUE_TRASH", str(tmp_path / "trash"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    httpd, app = make_server(0, work)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    yield {"app": app, "port": app.port, "work": work}
    app.stop_event.set()
    httpd.shutdown()
    httpd.server_close()
    for runner in app.threads:
        runner.join(10)


def call(server, method, path, body=None, *, token=True, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    sent = {"Content-Type": "application/json"}
    if token:
        sent["X-Fugue-Token"] = server["app"].token
    sent.update(headers or {})
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=sent)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        return response.status, json.loads(raw)
    except json.JSONDecodeError:
        return response.status, raw.decode()


def letter_of(data, tool):
    """The letter of the member that uses this tool, in a discussion with one Claude and one Codex."""
    return next(letter for letter, member in data["members"].items() if member["tool"] == tool)


def wait_for_phase(server, ident, phases, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = call(server, "GET", f"/api/discussions/{ident}")
        if data["status"]["phase"] in phases:
            return data
        time.sleep(0.2)
    raise AssertionError(f"still {data['status']} after {timeout}s")


def test_page_carries_the_token_and_api_needs_it(server):
    status, page = call(server, "GET", "/", token=False)
    assert status == 200 and server["app"].token in page
    assert call(server, "GET", "/static/app.js", token=False)[0] == 200
    assert call(server, "GET", "/static/../store.py", token=False)[0] == 404
    assert call(server, "GET", "/api/discussions", token=False)[0] == 401
    assert call(server, "GET", "/api/discussions", headers={"X-Fugue-Token": "wrong"})[0] == 401
    assert call(server, "GET", "/api/discussions")[0] == 200


def test_other_hosts_and_origins_are_refused(server):
    assert call(server, "GET", "/", token=False, headers={"Host": "evil.example:80"})[0] == 403
    status, _ = call(server, "POST", "/api/discussions", {"question": "Q?"},
                     headers={"Origin": "https://evil.example"})
    assert status == 403


def test_create_and_run_a_discussion(server, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    status, created = call(server, "POST", "/api/discussions", {
        "question": "Should we cache?", "cwd": str(server["work"]), "max_rounds": 2,
        "pause_after_each_round": False})
    assert status == 201
    ident = created["id"]
    data = wait_for_phase(server, ident, {"finished"})
    assert [r["kind"] for r in data["rounds"]] == ["answer", "comment"]
    assert data["status"]["label"] == "Agreed in round 1"
    claude, codex = letter_of(data, "claude"), letter_of(data, "codex")
    assert data["members"][claude]["name"] == f"Claude (member {claude})"
    assert data["members"][claude]["short_name"] == "Claude"
    assert data["rounds"][0]["entries"][claude]["reply"].startswith("Claude reply number 1.")
    assert data["rounds"][1]["entries"][codex]["objections"] == 0
    assert data["rounds"][1]["entries"][codex]["heading"].startswith(f"Codex (member {codex}), commenting on Claude")
    assert data["control"]["max_rounds"] == 2
    assert set(data["sessions"]) == {"A", "B"}
    assert data["resume"][codex] == f"cd {server['work'].resolve()} && codex resume {data['sessions'][codex]}"
    assert data["legacy_names"] == {}
    _, listing = call(server, "GET", "/api/discussions")
    assert listing[0]["id"] == ident and listing[0]["status"]["phase"] == "finished"
    _, prompt = call(server, "GET", f"/api/discussions/{ident}/prompt?round=1&member={claude}")
    assert "Codex reply number 1." in prompt["text"]
    assert call(server, "GET", f"/api/discussions/{ident}/prompt?round=1&member=claude")[0] == 404
    # Everything Claude had seen when it answered round 1: the question, its own answer, then round 1's message.
    _, context = call(server, "GET", f"/api/discussions/{ident}/context?round=1&member={claude}")
    assert [(i["round"], i["role"]) for i in context["items"]] == [(0, "sent"), (0, "reply"), (1, "sent")]
    assert context["items"][0]["text"].strip() == "Should we cache?"
    assert context["items"][1]["text"].startswith("Claude reply number 1.")
    assert context["items"][2]["text"] == prompt["text"]
    assert call(server, "GET", f"/api/discussions/{ident}/context?round=7&member={claude}")[0] == 404
    _, folders = call(server, "GET", "/api/folders")
    assert folders["recent"][0] == str(server["work"].resolve())


def test_it_pauses_after_every_round_and_names_the_next_step(server):
    _, created = call(server, "POST", "/api/discussions", {"question": "Q?", "cwd": str(server["work"])})
    ident = created["id"]
    data = wait_for_phase(server, ident, {"paused"})
    assert [r["kind"] for r in data["rounds"]] == ["answer"]
    assert data["status"]["next"] == "each comments on the other's answer"
    assert call(server, "POST", f"/api/discussions/{ident}/control", {"paused": False})[0] == 200
    time.sleep(0.5)
    data = wait_for_phase(server, ident, {"paused"})
    assert [r["kind"] for r in data["rounds"]] == ["answer", "comment"]
    assert data["status"]["next"] == "each updates their answer"


def test_event_stream(server, monkeypatch):
    monkeypatch.setenv("FAKE_OBJECTIONS", "0")
    _, created = call(server, "POST", "/api/discussions",
                      {"question": "Q?", "cwd": str(server["work"]), "pause_after_each_round": False})
    wait_for_phase(server, created["id"], {"finished"})
    connection = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    token = server["app"].token
    connection.request("GET", f"/api/discussions/{created['id']}/events?after=3&token={token}")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/event-stream")
    events = []
    while len(events) < 10:
        line = response.fp.readline().decode()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    connection.close()
    assert events[0]["seq"] == 4
    assert any(e["type"] == "activity" and e["text"] == "Read src/cache.py" for e in events)


def test_notes_requests_and_controls(server):
    status, created = call(server, "POST", "/api/discussions", {"question": "Q?", "cwd": str(server["work"])})
    ident = created["id"]
    data = wait_for_phase(server, ident, {"paused"})
    assert [r["kind"] for r in data["rounds"]] == ["answer"]

    claude, codex = letter_of(data, "claude"), letter_of(data, "codex")
    assert call(server, "POST", f"/api/discussions/{ident}/notes", {"text": "Hi", "to": "codex"})[0] == 400
    assert call(server, "POST", f"/api/discussions/{ident}/notes", {"text": "Mind the latency.", "to": codex})[0] == 200
    assert call(server, "POST", f"/api/discussions/{ident}/notes", {"text": "Withdraw me", "to": "both"})[0] == 200
    _, data = call(server, "GET", f"/api/discussions/{ident}")
    assert len(data["inbox"]["notes"][codex]) == 2 and len(data["inbox"]["notes"][claude]) == 1
    assert call(server, "DELETE", f"/api/discussions/{ident}/notes", {"member": claude, "index": 0})[0] == 200
    _, data = call(server, "GET", f"/api/discussions/{ident}")
    assert data["inbox"]["notes"][claude] == [] and len(data["inbox"]["notes"][codex]) == 1

    assert call(server, "POST", f"/api/discussions/{ident}/control", {"max_rounds": "lots"})[0] == 400
    assert call(server, "POST", f"/api/discussions/{ident}/control", {"color": "red"})[0] == 400
    assert call(server, "POST", f"/api/discussions/{ident}/control", {"auto_final": False})[0] == 400
    status, control = call(server, "POST", f"/api/discussions/{ident}/control",
                           {"pause_after_each_round": False, "max_rounds": 1, "paused": False})
    assert status == 200 and control["max_rounds"] == 1
    time.sleep(0.5)
    data = wait_for_phase(server, ident, {"finished"})
    assert [r["kind"] for r in data["rounds"]] == ["answer", "comment", "update"]
    comments = data["rounds"][1]["entries"]
    assert comments[codex]["notes"][0]["text"] == "Mind the latency."
    assert comments[claude]["notes"] == []

    status, queued = call(server, "POST", f"/api/discussions/{ident}/requests", {"type": "ask", "question": "And cost?"})
    assert status == 201
    time.sleep(0.5)
    data = wait_for_phase(server, ident, {"finished"})
    assert [r["kind"] for r in data["rounds"]] == ["answer", "comment", "update", "answer", "comment", "update"]
    assert data["rounds"][3]["question"] == "And cost?"

    assert call(server, "POST", f"/api/discussions/{ident}/requests", {"type": "dance"})[0] == 400
    assert call(server, "POST", f"/api/discussions/{ident}/requests", {"type": "final"})[0] == 400
    assert call(server, "POST", f"/api/discussions/{ident}/requests", {"type": "next"})[0] == 201
    time.sleep(0.5)
    data = wait_for_phase(server, ident, {"finished"})
    # One more round of comments; without pausing, its update follows.
    assert [r["kind"] for r in data["rounds"]][-2:] == ["comment", "update"] and len(data["rounds"]) == 8


def test_models_and_efforts(server):
    status, models = call(server, "GET", "/api/models")
    assert status == 200
    assert [m["id"] for m in models["codex"]["models"]] == ["gpt-test-big", "gpt-test-small"]
    assert models["codex"]["source"] == "codex debug models"
    assert "claude-sonnet-5-5" in [m["id"] for m in models["claude"]["models"]]
    assert models["codex"]["defaults"] == {"model": None, "effort": None}
    status, created = call(server, "POST", "/api/discussions", {
        "question": "Q?", "cwd": str(server["work"]), "max_rounds": 0,
        "members": [{"tool": "codex", "model": "gpt-test-small", "effort": "medium", "args": ""},
                    {"tool": "codex", "model": "gpt-test-big", "effort": "", "args": "--add-dir ../x"}]})
    assert status == 201
    data = wait_for_phase(server, created["id"], {"finished"})
    members = sorted(data["members"].values(), key=lambda m: m["model"])
    assert [m["model"] for m in members] == ["gpt-test-big", "gpt-test-small"]
    assert members[0]["args"] == ["--add-dir", "../x"] and members[1]["effort"] == "medium"
    assert {m["short_name"] for m in members} == {"Codex · gpt-test-big", "Codex · gpt-test-small · medium"}
    status, error = call(server, "POST", "/api/discussions", {
        "question": "Q?", "cwd": str(server["work"]),
        "members": [{"tool": "codex", "model": "gpt-test-small", "effort": "high"}, {"tool": "claude"}]})
    assert status == 400 and "doesn't take effort 'high'" in error["error"]
    assert call(server, "POST", "/api/discussions", {"question": "Q?", "members": "two codexes"})[0] == 400
    assert call(server, "POST", "/api/discussions", {"question": "Q?", "members": [{"tool": "codex"}]})[0] == 400


def test_delete_settings_and_status(server):
    _, created = call(server, "POST", "/api/discussions", {"question": "Q?", "cwd": str(server["work"])})
    wait_for_phase(server, created["id"], {"paused"})
    assert call(server, "DELETE", f"/api/discussions/{created['id']}")[0] == 200
    assert call(server, "GET", "/api/discussions")[1] == []
    status, settings = call(server, "GET", "/api/settings")
    assert status == 200 and settings["defaults"]["pause_after_each_round"] is True
    assert [m["tool"] for m in settings["defaults"]["members"]] == ["claude", "codex"]
    new = {**settings["defaults"], "max_rounds": 5, "members": [{"tool": "claude"}, {"tool": "claude", "effort": "low"}]}
    status, saved = call(server, "POST", "/api/settings", new)
    assert status == 200 and saved["defaults"]["max_rounds"] == 5
    assert [m["effort"] for m in saved["defaults"]["members"]] == ["", "low"]
    assert call(server, "POST", "/api/settings", {**new, "max_rounds": "lots"})[0] == 400
    status, report = call(server, "GET", "/api/status")
    assert status == 200 and report["agents"]["codex"]["found"] and report["port"] == server["port"]


def test_bad_input(server):
    assert call(server, "POST", "/api/discussions", {"question": " "})[0] == 400
    assert call(server, "POST", "/api/discussions", {"question": "Q?", "cwd": "/no/such/folder"})[0] == 400
    assert call(server, "GET", "/api/discussions/nope")[0] == 400
    assert call(server, "GET", "/api/discussions/..%2F..")[0] == 404
