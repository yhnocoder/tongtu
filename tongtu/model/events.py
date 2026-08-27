from __future__ import annotations

import json
from collections.abc import Callable

CLAUDE_TOOL_ARGUMENTS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


def summarize_stream_json(event: dict) -> str | None:
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return _tool_action(block.get("name"), block.get("input"))
    return None


def summarize_codex_json(event: dict) -> str | None:
    msg = event.get("msg")
    if isinstance(msg, dict):
        return _codex_protocol_action(msg)
    item = event.get("item")
    if isinstance(item, dict):
        return _codex_item_action(item)
    return None


SUMMARIZERS: dict[str, Callable[[dict], str | None]] = {
    "stream-json": summarize_stream_json,
    "codex-json": summarize_codex_json,
}


def summarizer(events: str | None) -> Callable[[bytes], str | None] | None:
    if events is None:
        return None
    parse = SUMMARIZERS[events]

    def summarize(line: bytes) -> str | None:
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return parse(data)

    return summarize


def _tool_action(name: object, arguments: object) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    key = CLAUDE_TOOL_ARGUMENTS.get(name)
    value = arguments.get(key) if isinstance(arguments, dict) and key is not None else None
    if isinstance(value, str) and value.strip():
        return f"{name}: {_one_line(value)}"
    return name


def _codex_protocol_action(msg: dict) -> str | None:
    kind = msg.get("type")
    if kind == "exec_command_begin":
        return _command_action(msg.get("command"))
    if kind == "patch_apply_begin":
        changes = msg.get("changes")
        if isinstance(changes, dict) and changes:
            return f"patch: {_one_line(' '.join(sorted(changes)))}"
        return "patch"
    return None


def _codex_item_action(item: dict) -> str | None:
    kind = item.get("type") or item.get("item_type")
    if kind == "command_execution":
        return _command_action(item.get("command"))
    if kind == "file_change":
        changes = item.get("changes")
        paths = [entry.get("path") for entry in changes if isinstance(entry, dict)] if isinstance(changes, list) else []
        names = [path for path in paths if isinstance(path, str) and path]
        if names:
            return f"patch: {_one_line(' '.join(names))}"
        return "patch"
    return None


def _command_action(command: object) -> str | None:
    if isinstance(command, str) and command.strip():
        return f"exec: {_one_line(command)}"
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return f"exec: {_one_line(' '.join(command))}"
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())
