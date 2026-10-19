"""The local web server behind `duet web`.

It serves the page, a small JSON API, and a live stream of each discussion's
events (Server-Sent Events, read from the discussion's event log, so
discussions run from the command line show up live too). Discussions started
or continued from the page run in this process.

It listens on 127.0.0.1 only. Every API call must carry a random token that
is only handed to the page this server serves, and the Host and Origin
headers must name this server, so other websites open in the browser can't
use it to start agents on this machine.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, engine, transcript
from .agents import find_binary
from .store import AGENTS, Discussion, DuetError, list_discussions

STATIC_FILES = {"app.js": "text/javascript", "app.css": "text/css"}
CONTROL_KEYS = {"paused": bool, "pause_after_each_round": bool, "max_rounds": int}
ROUTE = re.compile(r"^/api/discussions/([A-Za-z0-9][A-Za-z0-9._-]*)(?:/([a-z]+))?(?:/([A-Za-z0-9]+))?$")


class BadRequest(Exception):
    pass


def summary(discussion: Discussion) -> dict:
    return {
        "id": discussion.id,
        "title": discussion.title,
        "cwd": discussion.state["cwd"],
        "created_at": discussion.state["created_at"],
        "updated_at": discussion.state["updated_at"],
        "rounds": len(discussion.rounds),
        "status": engine.describe(discussion),
    }


def detail(discussion: Discussion) -> dict:
    rounds = []
    for rnd in discussion.rounds:
        entries = {}
        for agent, entry in rnd["entries"].items():
            item = {key: entry.get(key) for key in
                    ("status", "error", "duration_s", "objections", "remaining", "started_at", "finished_at",
                     "cost_usd", "tokens")}
            item["notes"] = entry.get("notes", [])
            item["forwarded"] = entry.get("forwarded", [])
            item["heading"] = transcript.entry_heading(rnd["kind"], agent, discussion.labels)
            if entry["status"] == "done":
                item["reply"] = discussion.read_reply(rnd["index"], agent)
            entries[agent] = item
        rounds.append({"index": rnd["index"], "kind": rnd["kind"], "question": rnd.get("question"),
                       "label": transcript.round_heading(rnd), "entries": entries})
    return {
        **summary(discussion),
        "question": discussion.state["question"],
        "control": discussion.read_control(),
        "inbox": discussion.read_inbox(),
        "sessions": discussion.state["sessions"],
        "labels": discussion.labels,
        "settings": discussion.state["settings"],
        "transcript_path": str(discussion.transcript_path),
        "rounds": rounds,
    }


class App:
    """What the request handlers share: the token, and the runners started from the page."""

    def __init__(self, port: int, default_cwd: Path):
        self.port = port
        self.default_cwd = default_cwd
        self.token = secrets.token_urlsafe(32)
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def kick(self, discussion_id: str) -> None:
        """Make sure the discussion moves forward: start a runner unless one is already running it."""
        runner = engine.Runner(discussion_id, stop_event=self.stop_event)
        thread = threading.Thread(target=self._run, args=(runner,), name=f"runner {discussion_id}", daemon=True)
        thread.start()
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]

    @staticmethod
    def _run(runner: engine.Runner) -> None:
        try:
            runner.run()
        except Exception:
            traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = f"duet/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:  # keep the terminal quiet
        pass

    # -- plumbing

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _allowed(self, query: dict, *, needs_token: bool) -> bool:
        if self.headers.get("Host") not in self.app.hosts:
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in self.app.hosts}:
            self._error(HTTPStatus.FORBIDDEN, "Requests from other sites are not allowed.")
            return False
        if needs_token:
            token = self.headers.get("X-Duet-Token") or (query.get("token") or [""])[0]
            if not secrets.compare_digest(token, self.app.token):
                self._error(HTTPStatus.UNAUTHORIZED, "Missing or wrong token. Reload the page.")
                return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise BadRequest("Request too large.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise BadRequest("The request body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise BadRequest("The request body must be a JSON object.")
        return data

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        is_api = url.path.startswith("/api/")
        if not self._allowed(query, needs_token=is_api):
            return
        try:
            if method == "GET" and not is_api:
                self._static(url.path)
            elif is_api:
                self._api(method, url.path, query)
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except (DuetError, BadRequest) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # -- the page

    def _static(self, path: str) -> None:
        web = resources.files("duet") / "web"
        if path in ("/", "/index.html"):
            page = (web / "index.html").read_text(encoding="utf-8").replace("__DUET_TOKEN__", self.app.token)
            self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
        elif path.startswith("/static/") and path[8:] in STATIC_FILES:
            name = path[8:]
            self._send(HTTPStatus.OK, (web / name).read_bytes(), f"{STATIC_FILES[name]}; charset=utf-8")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")

    # -- the API

    def _api(self, method: str, path: str, query: dict) -> None:
        if path == "/api/info" and method == "GET":
            return self._json({"version": __version__, "home": str(Path.home()),
                               "agents": {a: _has_binary(a) for a in AGENTS}})
        if path == "/api/models" and method == "GET":
            from .models import catalog

            return self._json(catalog(refresh=(query.get("refresh") or [""])[0] == "1"))
        if path == "/api/folders" and method == "GET":
            recent = []
            for discussion in reversed(list_discussions()):
                if discussion.state["cwd"] not in recent:
                    recent.append(discussion.state["cwd"])
            return self._json({"default": str(self.app.default_cwd), "recent": recent[:15]})
        if path == "/api/discussions":
            if method == "GET":
                return self._json([summary(d) for d in reversed(list_discussions())])
            if method == "POST":
                return self._create(self._body())
        match = ROUTE.match(path)
        if not match:
            return self._error(HTTPStatus.NOT_FOUND, "Not found.")
        ident, action, item = match.groups()
        discussion = Discussion.by_id(ident)
        if action is None and method == "GET":
            return self._json(detail(discussion))
        if action == "events" and method == "GET":
            return self._events(discussion, query)
        if action == "prompt" and method == "GET":
            index, agent = int((query.get("round") or ["-1"])[0]), (query.get("agent") or [""])[0]
            if agent not in AGENTS or not discussion.prompt_path(index, agent).exists():
                return self._error(HTTPStatus.NOT_FOUND, "No such message.")
            return self._json({"text": discussion.prompt_path(index, agent).read_text()})
        if action == "notes" and method == "POST":
            body = self._body()
            text, to = str(body.get("text") or "").strip(), body.get("to", "both")
            if not text:
                raise BadRequest("The note is empty.")
            if to not in ("both", *AGENTS):
                raise BadRequest("`to` must be both, claude or codex.")
            discussion.add_note(text, list(AGENTS) if to == "both" else [to])
            return self._json({"ok": True})
        if action == "notes" and method == "DELETE":
            body = self._body()
            if body.get("agent") not in AGENTS:
                raise BadRequest("Unknown agent.")
            discussion.remove_note(body["agent"], int(body.get("index", -1)))
            return self._json({"ok": True})
        if action == "control" and method == "POST":
            return self._control(discussion, self._body())
        if action == "requests" and method == "POST":
            return self._request(discussion, self._body())
        if action == "requests" and method == "DELETE" and item:
            discussion.pop_request(item)
            return self._json({"ok": True})
        if action == "stop" and method == "POST":
            if discussion.runner_pid() is not None:
                discussion.update_control(stop_requested=True)
            return self._json({"ok": True})
        return self._error(HTTPStatus.NOT_FOUND, "Not found.")

    def _create(self, body: dict) -> None:
        from .config import new_discussion

        def text(key):
            value = body.get(key)
            return str(value).strip() or None if value is not None else None

        control = {}
        for key in ("max_rounds", "pause_after_each_round"):
            if body.get(key) is not None:
                control[key] = _checked(key, body[key])
        timeout = body.get("timeout_minutes")
        web = body.get("web")
        if web is not None and not isinstance(web, bool):
            raise BadRequest("web must be true or false.")
        discussion = new_discussion(
            str(body.get("question") or ""),
            cwd=Path(text("cwd") or str(self.app.default_cwd)),
            title=text("title"),
            timeout_minutes=float(timeout) if timeout else None,
            models={"claude": text("claude_model"), "codex": text("codex_model")},
            efforts={"claude": text("claude_effort"), "codex": text("codex_effort")},
            web=web,
            extra_args={"claude": text("claude_args"), "codex": text("codex_args")},
            control=control,
        )
        self.app.kick(discussion.id)
        self._json({"id": discussion.id}, HTTPStatus.CREATED)

    def _control(self, discussion: Discussion, body: dict) -> None:
        changes = {key: _checked(key, value) for key, value in body.items()}
        control = discussion.update_control(**changes)
        self.app.kick(discussion.id)
        self._json(control)

    def _request(self, discussion: Discussion, body: dict) -> None:
        kind = body.get("type")
        if kind == "next":
            request = {"type": "next"}
        elif kind == "ask":
            question = str(body.get("question") or "").strip()
            if not question:
                raise BadRequest("The question is empty.")
            request = {"type": "ask", "question": question}
        else:
            raise BadRequest("`type` must be next or ask.")
        request = discussion.add_request(request)
        if kind == "ask":
            discussion.update_control(paused=False)
        self.app.kick(discussion.id)
        self._json(request, HTTPStatus.CREATED)

    def _events(self, discussion: Discussion, query: dict) -> None:
        """Stream the discussion's event log, starting after the event the page last saw."""
        try:
            after = int(self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        path = discussion.events_path
        handle, partial, last_write = None, "", time.monotonic()
        try:
            while not self.app.stop_event.is_set():
                if handle is None and path.exists():
                    handle = open(path, encoding="utf-8")
                chunk = handle.read() if handle else ""
                out = []
                if chunk:
                    lines = (partial + chunk).split("\n")
                    partial = lines.pop()
                    for line in lines:
                        match = re.match(r'\{"seq": (\d+)', line)
                        if match and int(match.group(1)) > after:
                            out.append(f"id: {match.group(1)}\ndata: {line}\n\n")
                if out:
                    self.wfile.write("".join(out).encode())
                    self.wfile.flush()
                    last_write = time.monotonic()
                elif time.monotonic() - last_write > 15:
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if handle:
                handle.close()


def _has_binary(agent: str) -> bool:
    try:
        find_binary(agent)
        return True
    except DuetError:
        return False


def _checked(key: str, value):
    expected = CONTROL_KEYS.get(key)
    if expected is None:
        raise BadRequest(f"Unknown setting: {key}")
    if expected is bool and not isinstance(value, bool):
        raise BadRequest(f"{key} must be true or false.")
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
            raise BadRequest(f"{key} must be a whole number from 0 to 20.")
    return value


def make_server(port: int, default_cwd: Path) -> tuple[ThreadingHTTPServer, App]:
    app = App(port, default_cwd)
    handler = type("DuetHandler", (Handler,), {"app": app})
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    if port == 0:  # tests ask for any free port
        app.port = httpd.server_address[1]
        app.hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
    return httpd, app


def serve(port: int, open_browser: bool, default_cwd: Path) -> int:
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd, app = make_server(port, default_cwd)
    except OSError:
        print(f"Port {port} is already in use; duet web may already be running at {url}", file=sys.stderr)
        if open_browser:
            webbrowser.open(url)
        return 1
    print(f"duet is running at {url}\nDiscussions started from the page run in this process; "
          "Ctrl-C stops them (continue them later from the page).", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...", file=sys.stderr)
    finally:
        app.stop_event.set()
        httpd.server_close()
        for thread in app.threads:
            thread.join(timeout=20)
    return 0
