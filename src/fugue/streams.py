"""Turning each agent's live output into common events.

Claude Code (`claude -p --output-format stream-json`), Codex
(`codex exec --json`) and the DeepSeek Harness (`dsh --profile headless --json`)
all print one JSON object per line as they work. The parsers here read those
lines and produce the same few kinds of event for every tool:

    {"type": "activity", "kind": "tool" | "command" | "thinking" | "error" | "info", "text": ...}
    {"type": "text_delta", "text": ...}   a piece of a message being written
    {"type": "message", "text": ...}      a complete message (the last one is the reply)
    {"type": "progress", "thinking_tokens": N}   how much it has thought so far (Claude only:
                                          at high effort it can think for minutes without
                                          writing anything or using a tool)

When the process ends, `outcome()` gives the final reply or the error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Outcome:
    ok: bool
    reply: str = ""
    session_id: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    tokens: dict | None = None


def _tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str, limit: int = 200) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return _short(line, limit)
    return ""


class ClaudeStream:
    """Parser for `claude -p --output-format stream-json --verbose --include-partial-messages`."""

    def __init__(self, cwd: Path):
        self.cwd = str(cwd)
        self.session_id: str | None = None
        self.result: dict | None = None
        self.other_lines: list[str] = []

    def _path(self, path: str) -> str:
        prefix = self.cwd.rstrip("/") + "/"
        return path[len(prefix):] if path.startswith(prefix) else path

    def describe_tool(self, name: str, args: dict) -> tuple[str, str]:
        if name == "Bash":
            return "command", "$ " + _first_line(args.get("command", ""))
        if name == "Read":
            return "tool", f"Read {self._path(args.get('file_path', ''))}"
        if name == "Grep":
            where = f" in {self._path(args['path'])}" if args.get("path") else ""
            return "tool", f"Search for {_short(args.get('pattern', ''), 80)!r}{where}"
        if name == "Glob":
            return "tool", f"Find files {args.get('pattern', '')}"
        if name == "WebSearch":
            return "tool", f"Web search: {_short(args.get('query', ''))}"
        if name == "WebFetch":
            return "tool", f"Fetch {args.get('url', '')}"
        if name in ("Task", "Agent"):
            return "tool", f"Subagent: {_short(args.get('description') or args.get('prompt', ''))}"
        if name == "TodoWrite":
            return "tool", "Update its to-do list"
        return "tool", f"{name} {_short(json.dumps(args, ensure_ascii=False), 120)}"

    def feed(self, line: str) -> list[dict]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                self.other_lines.append(line)
            return []
        if not isinstance(event, dict):
            return []
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id") or self.session_id
            return []
        if kind == "system" and event.get("subtype") == "thinking_tokens":
            return [{"type": "progress", "thinking_tokens": event.get("estimated_tokens") or 0}]
        if kind == "stream_event":
            inner = event.get("event") or {}
            delta = inner.get("delta") or {}
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                return [{"type": "text_delta", "text": delta.get("text", "")}]
            return []
        if kind == "assistant":
            out = []
            for block in (event.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "text" and block.get("text", "").strip():
                    out.append({"type": "message", "text": block["text"]})
                elif block_type == "tool_use":
                    activity_kind, text = self.describe_tool(block.get("name", "?"), block.get("input") or {})
                    out.append({"type": "activity", "kind": activity_kind, "text": text})
                elif block_type == "thinking" and block.get("thinking", "").strip():
                    out.append({"type": "activity", "kind": "thinking", "text": _first_line(block["thinking"])})
            return out
        if kind == "user":
            out = []
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    detail = block.get("content")
                    if isinstance(detail, list):
                        detail = " ".join(part.get("text", "") for part in detail if isinstance(part, dict))
                    out.append({"type": "activity", "kind": "error", "text": _first_line(detail or "tool failed")})
            return out
        if kind == "result":
            self.result = event
            self.session_id = event.get("session_id") or self.session_id
        return []

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        data = self.result
        if data is None:
            detail = _tail(stderr) or _tail("\n".join(self.other_lines))
            return Outcome(ok=False, error=detail or f"exited with code {returncode}")
        reply = data.get("result") or ""
        if data.get("is_error") or returncode != 0:
            return Outcome(ok=False, session_id=self.session_id,
                           error=_tail(reply or stderr) or f"exited with code {returncode}")
        if not reply.strip():
            return Outcome(ok=False, session_id=self.session_id, error="Claude returned an empty reply")
        return Outcome(ok=True, reply=reply, session_id=self.session_id, cost_usd=data.get("total_cost_usd"))


_SHELL_WRAPPER = re.compile(r"""^\S*/(?:ba|z)?sh\s+-l?c\s+(['"])(.*)\1$""", re.DOTALL)


def _unwrap_command(command) -> str:
    if isinstance(command, list):
        command = " ".join(command)
    match = _SHELL_WRAPPER.match(command.strip())
    return match.group(2) if match else command


class CodexStream:
    """Parser for `codex exec --json` (and `codex exec resume --json`)."""

    def __init__(self, reply_file: Path):
        self.reply_file = reply_file
        self.session_id: str | None = None
        self.last_message: str | None = None
        self.usage: dict | None = None
        self.errors: list[str] = []
        self.turn_failed = False
        self.started_items: set[str] = set()

    def feed(self, line: str) -> list[dict]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        kind = event.get("type")
        item = event.get("item") or {}
        item_type = item.get("type")
        if kind == "thread.started":
            self.session_id = event.get("thread_id")
        elif kind == "turn.completed":
            self.usage = event.get("usage")
        elif kind == "turn.failed":
            self.turn_failed = True
            self.errors.append((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self.errors.append(event.get("message") or "error")
            return [{"type": "activity", "kind": "error", "text": _first_line(event.get("message") or "error")}]
        elif kind == "item.started" and item_type == "command_execution":
            self.started_items.add(item.get("id"))
            return [{"type": "activity", "kind": "command", "text": "$ " + _first_line(_unwrap_command(item.get("command", "")))}]
        elif kind == "item.completed":
            return self._completed(item)
        return []

    def _completed(self, item: dict) -> list[dict]:
        item_type = item.get("type")
        if item_type == "agent_message":
            self.last_message = item.get("text") or ""
            return [{"type": "message", "text": self.last_message}] if self.last_message.strip() else []
        if item_type == "reasoning" and (item.get("text") or "").strip():
            return [{"type": "activity", "kind": "thinking", "text": _first_line(item["text"].replace("**", ""))}]
        if item_type == "command_execution":
            command = _first_line(_unwrap_command(item.get("command", "")))
            out = []
            if item.get("id") not in self.started_items:
                out.append({"type": "activity", "kind": "command", "text": f"$ {command}"})
            code = item.get("exit_code")
            if code not in (0, None):
                out.append({"type": "activity", "kind": "error", "text": f"exit code {code}: {command}"})
            return out
        if item_type == "file_change":
            paths = ", ".join(c.get("path", "?") for c in item.get("changes") or [])
            return [{"type": "activity", "kind": "tool", "text": f"Change files: {_short(paths)}"}]
        if item_type == "mcp_tool_call":
            return [{"type": "activity", "kind": "tool", "text": f"Tool {item.get('server', '?')}.{item.get('tool', '?')}"}]
        if item_type == "web_search":
            return [{"type": "activity", "kind": "tool", "text": f"Web search: {_short(item.get('query', ''))}"}]
        if item_type == "todo_list":
            return [{"type": "activity", "kind": "tool", "text": f"Update its to-do list ({len(item.get('items') or [])} items)"}]
        if item_type == "error":
            return [{"type": "activity", "kind": "error", "text": _first_line(item.get("message", "error"))}]
        return []

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        reply = self.reply_file.read_text() if self.reply_file.exists() else ""
        if not reply.strip() and self.last_message:
            reply = self.last_message
        if self.turn_failed or returncode != 0 or not reply.strip():
            error = "; ".join(self.errors) or _tail(stderr)
            if not error:
                error = f"exited with code {returncode}" if returncode else "Codex returned an empty reply"
            return Outcome(ok=False, session_id=self.session_id, error=error)
        return Outcome(ok=True, reply=reply, session_id=self.session_id, tokens=self.usage)


class DshStream:
    """Parser for `dsh --profile headless --json` (the DeepSeek Harness).

    It prints `session`, then `status` (turn and step boundaries, with each
    step's token usage), `thinking`, `text`, `tool_call` and `tool_result` as
    each step is committed (not token by token), and ends with `final`, the
    whole answer; or with `error` when the run fails outside a turn.
    """

    def __init__(self, cwd: Path):
        self.cwd = str(cwd)
        self.session_id: str | None = None
        self.final: str | None = None
        self.reason: str | None = None
        self.errors: list[str] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _path(self, path: str) -> str:
        prefix = self.cwd.rstrip("/") + "/"
        return path[len(prefix):] if path.startswith(prefix) else path

    def describe_tool(self, name: str, args: dict) -> tuple[str, str]:
        if isinstance(args.get("command"), str):
            return "command", "$ " + _first_line(args["command"])
        path = next((args[k] for k in ("path", "file_path", "filePath", "file") if isinstance(args.get(k), str)), None)
        query = args.get("pattern") or args.get("query")
        if path and query:
            return "tool", f"{name} {_short(str(query), 80)!r} in {self._path(path)}"
        if path:
            return "tool", f"{name} {self._path(path)}"
        if query:
            return "tool", f"{name} {_short(str(query))}"
        return "tool", f"{name} {_short(json.dumps(args, ensure_ascii=False), 120)}"

    def feed(self, line: str) -> list[dict]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        kind = event.get("type")
        if kind == "session":
            self.session_id = event.get("sessionId") or self.session_id
        elif kind == "status":
            if event.get("phase") == "step_end" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
                self.usage["input_tokens"] += int(usage.get("inputTokens") or 0)
                self.usage["output_tokens"] += int(usage.get("outputTokens") or 0)
            elif event.get("phase") == "turn_end":
                self.reason = event.get("reason")
        elif kind == "thinking" and (event.get("text") or "").strip():
            return [{"type": "activity", "kind": "thinking", "text": _first_line(event["text"])}]
        elif kind == "text" and (event.get("text") or "").strip():
            return [{"type": "message", "text": event["text"]}]
        elif kind == "tool_call":
            args = event.get("input") if isinstance(event.get("input"), dict) else {}
            activity_kind, text = self.describe_tool(str(event.get("tool") or "tool"), args)
            return [{"type": "activity", "kind": activity_kind, "text": text}]
        elif kind == "tool_result" and event.get("status") == "error":
            return [{"type": "activity", "kind": "error", "text": _first_line(event.get("result") or "tool failed")}]
        elif kind == "final":
            self.final = event.get("text") or ""
        elif kind == "error":
            message = event.get("message") or event.get("code") or "error"
            self.errors.append(str(message))
            return [{"type": "activity", "kind": "error", "text": _first_line(message)}]
        return []

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        reply = self.final or ""
        if returncode != 0 or not reply.strip():
            error = "; ".join(self.errors) or _tail(stderr)
            if not error:
                error = (f"exited with code {returncode}" + (f" ({self.reason})" if self.reason else "")
                         if returncode else "DeepSeek returned an empty reply")
            return Outcome(ok=False, session_id=self.session_id, error=error)
        return Outcome(ok=True, reply=reply, session_id=self.session_id, tokens=dict(self.usage))
